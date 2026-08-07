#!/usr/bin/env python3
"""OAuth rotation contract: provider payloads, rotation safety, and atomic credential updates.

The rotation core lives in `tauceti_worker.oauth`, shared by the standalone refresher daemon and the
pacer's own auto-refresh, so this exercises the module rather than the script.
"""

import base64
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tauceti_worker import oauth  # noqa: E402


def jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print(f"[OK ] {label}")


def responds(body, status=200):
    """Patch the one HTTP seam. The core returns (status, parsed-body-or-None) and never the raw text."""
    return patch.object(oauth, "_post_json", return_value=(status, body))


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    claude_file = root / ".claude" / ".credentials.json"
    claude_file.parent.mkdir()
    claude_mirror = root / "worker-claude" / ".credentials.json"
    claude_file.write_text(
        json.dumps(
            {
                "accountLabel": "worker",
                "claudeAiOauth": {
                    "accessToken": "old-access",
                    "refreshToken": "old-refresh",
                    "expiresAt": 1,
                    "scopes": ["user:profile"],
                },
            }
        )
    )
    provider = oauth.Provider("claude", claude_file, "https://example.test/claude", "claude-client", claude_mirror)
    check("a stored refresh token makes the credential renewable", oauth.renewable(provider))
    with responds({"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}) as post:
        check("Claude expired credential refreshes", oauth.refresh_if_due(provider, 5400) == "refreshed")
    request = post.call_args.args[1]
    check("Claude refresh request uses the stored token", request["refresh_token"] == "old-refresh")
    check("Claude refresh request carries the client scope", request["scope"] == oauth.CLAUDE_SCOPE)
    updated = json.loads(claude_file.read_text())
    check(
        "Claude access and rotated refresh tokens persist",
        updated["claudeAiOauth"]["accessToken"] == "new-access"
        and updated["claudeAiOauth"]["refreshToken"] == "new-refresh",
    )
    check("Claude credential permissions are private", (claude_file.stat().st_mode & 0o777) == 0o600)
    mirrored = json.loads(claude_mirror.read_text())["claudeAiOauth"]
    check("Claude worker mirror excludes the refresh token", "refreshToken" not in mirrored)
    # The configured 90-minute skew exceeds this token's one-hour lifetime. Once the
    # refresher knows the issuance time, clamp the skew to half the observed lifetime
    # so it does not rotate every ten-minute cooldown interval.
    with (
        patch.object(oauth.time, "time", return_value=time.time() + 700),
        patch.object(oauth, "_post_json") as post,
    ):
        check(
            "short-lived credentials do not repeatedly rotate after cooldown",
            oauth.refresh_if_due(provider, 5400) == "current",
        )
        post.assert_not_called()

    # A mirror is a credential with the refresh token deliberately removed. Nothing may try to spend it.
    mirror_provider = oauth.Provider("claude", claude_mirror, "https://example.test/claude", "claude-client")
    check("a stripped worker mirror is not renewable", not oauth.renewable(mirror_provider))
    check(
        "a missing credential file is not renewable",
        not oauth.renewable(oauth.Provider("claude", root / "nope", "", "")),
    )

    codex_file = root / ".codex" / "auth.json"
    codex_file.parent.mkdir()
    codex_mirror = root / "worker-codex" / "auth.json"
    codex_file.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": jwt(int(time.time()) + 7200),
                    "refresh_token": "codex-old-refresh",
                    "id_token": "old-id",
                },
            }
        )
    )
    provider = oauth.Provider("codex", codex_file, "https://example.test/codex", "codex-client", codex_mirror)
    with patch.object(oauth, "_post_json") as post:
        check("Codex current credential is not rotated early", oauth.refresh_if_due(provider, 5400) == "current")
        post.assert_not_called()

    codex_file.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": jwt(int(time.time()) + 60),
                    "refresh_token": "codex-old-refresh",
                    "id_token": "old-id",
                }
            }
        )
    )
    with responds(
        {"access_token": "codex-new-access", "refresh_token": "codex-new-refresh", "id_token": "new-id"}
    ) as post:
        check("Codex near-expiry credential refreshes", oauth.refresh_if_due(provider, 5400) == "refreshed")
    request = post.call_args.args[1]
    check("Codex refresh request uses the stored token", request["refresh_token"] == "codex-old-refresh")
    check("Codex refresh request sends no Claude scope", "scope" not in request)
    updated = json.loads(codex_file.read_text())
    check(
        "Codex access, identity, and rotated refresh tokens persist",
        updated["tokens"]
        == {"access_token": "codex-new-access", "refresh_token": "codex-new-refresh", "id_token": "new-id"},
    )
    mirrored = json.loads(codex_mirror.read_text())["tokens"]
    check(
        "Codex worker mirror uses only the parser placeholder",
        mirrored["refresh_token"] == oauth.CODEX_REFRESH_PLACEHOLDER,
    )
    check(
        "the parser placeholder is never treated as spendable",
        not oauth.renewable(oauth.Provider("codex", codex_mirror, "https://example.test/codex", "codex-client")),
    )

    # A server may rotate the refresh token and then return an incomplete response. Preserve the
    # new token before reporting the malformed exchange so a retry does not replay the old one.
    with responds({"refresh_token": "codex-rescue-refresh"}):
        try:
            oauth.refresh_if_due(provider, 5400, force=True)
        except ValueError:
            pass
        else:
            raise AssertionError("incomplete response should fail")
    updated = json.loads(codex_file.read_text())
    check("rotated token survives an incomplete response", updated["tokens"]["refresh_token"] == "codex-rescue-refresh")

    old_access = jwt(int(time.time()) + 3600)
    codex_file.write_text(json.dumps({"tokens": {"access_token": old_access, "refresh_token": "before-regression"}}))
    with responds({"access_token": jwt(int(time.time()) + 60), "refresh_token": "after-regression"}):
        try:
            oauth.refresh_if_due(provider, 5400, force=True)
        except ValueError:
            pass
        else:
            raise AssertionError("regressing response should fail")
    updated = json.loads(codex_file.read_text())
    check("regressing access token is rejected", updated["tokens"]["access_token"] == old_access)
    check("rotated token survives a regressing response", updated["tokens"]["refresh_token"] == "after-regression")

    # A non-200 is named by its status and never by its body: the token endpoint is allowed to echo
    # request details, and a refresh token must not reach a log through an exception message.
    with responds(None, status=400):
        try:
            oauth.refresh_if_due(provider, 5400, force=True)
        except RuntimeError as error:
            check("a rejected exchange names the status only", str(error) == "OAuth endpoint returned HTTP 400")
        else:
            raise AssertionError("a non-200 exchange should fail")

    # An operator-facing caller polls far faster than the refresher daemon does. The attempt bound is
    # stamped BEFORE the exchange, so a credential that cannot be rotated is retried at a bounded rate
    # instead of once per poll.
    throttled = root / ".throttled" / "auth.json"
    throttled.parent.mkdir()
    throttled.write_text(json.dumps({"tokens": {"access_token": jwt(int(time.time()) + 60), "refresh_token": "rt"}}))
    provider = oauth.Provider("codex", throttled, "https://example.test/codex", "codex-client")
    with responds(None, status=400):
        try:
            oauth.refresh_if_due(provider, 5400, attempt_interval_seconds=600)
        except RuntimeError:
            pass
        else:
            raise AssertionError("a non-200 exchange should fail")
    with patch.object(oauth, "_post_json") as post:
        check(
            "a failed rotation is not retried until its attempt interval elapses",
            oauth.refresh_if_due(provider, 5400, attempt_interval_seconds=600) == "cooldown",
        )
        post.assert_not_called()
    with patch.object(oauth, "_post_json") as post:
        check(
            "the attempt bound also holds a forced rotation back",
            oauth.refresh_if_due(provider, 5400, force=True, attempt_interval_seconds=600) == "cooldown",
        )
        post.assert_not_called()
    with (
        patch.object(oauth.time, "time", return_value=time.time() + 601),
        responds({"access_token": jwt(int(time.time()) + 7200), "refresh_token": "rt2"}),
    ):
        check(
            "a rotation is attempted again once the interval elapses",
            oauth.refresh_if_due(provider, 5400, attempt_interval_seconds=600) == "refreshed",
        )

    leftovers = list(root.rglob(".refresh.*"))
    check("atomic writes leave no temporary files", not leftovers)
