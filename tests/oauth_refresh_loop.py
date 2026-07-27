#!/usr/bin/env python3
"""OAuth refresher contract: provider payloads, rotation safety, and atomic credential updates."""

import base64
import importlib.util
import json
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "oauth_refresh_loop.py"
requests_stub = types.ModuleType("requests")
requests_stub.JSONDecodeError = json.JSONDecodeError
requests_stub.RequestException = OSError
requests_stub.post = None
sys.modules["requests"] = requests_stub
spec = importlib.util.spec_from_file_location("oauth_refresh_loop", SCRIPT)
assert spec and spec.loader
refresher = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = refresher
spec.loader.exec_module(refresher)


class Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status
        self.ok = status == 200
        self.text = json.dumps(body)

    def json(self):
        return self.body


def jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print(f"[OK ] {label}")


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
    provider = refresher.Provider("claude", claude_file, "https://example.test/claude", "claude-client", claude_mirror)
    with patch.object(
        refresher.requests,
        "post",
        return_value=Response({"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}),
    ) as post:
        check("Claude expired credential refreshes", refresher.refresh_if_due(provider, 5400) == "refreshed")
    request = post.call_args.kwargs["json"]
    check("Claude refresh request uses the stored token", request["refresh_token"] == "old-refresh")
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
        patch.object(refresher.time, "time", return_value=time.time() + 700),
        patch.object(refresher.requests, "post") as post,
    ):
        check(
            "short-lived credentials do not repeatedly rotate after cooldown",
            refresher.refresh_if_due(provider, 5400) == "current",
        )
        post.assert_not_called()

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
    provider = refresher.Provider("codex", codex_file, "https://example.test/codex", "codex-client", codex_mirror)
    with patch.object(refresher.requests, "post") as post:
        check("Codex current credential is not rotated early", refresher.refresh_if_due(provider, 5400) == "current")
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
    with patch.object(
        refresher.requests,
        "post",
        return_value=Response(
            {"access_token": "codex-new-access", "refresh_token": "codex-new-refresh", "id_token": "new-id"}
        ),
    ) as post:
        check("Codex near-expiry credential refreshes", refresher.refresh_if_due(provider, 5400) == "refreshed")
    request = post.call_args.kwargs["json"]
    check("Codex refresh request uses the stored token", request["refresh_token"] == "codex-old-refresh")
    updated = json.loads(codex_file.read_text())
    check(
        "Codex access, identity, and rotated refresh tokens persist",
        updated["tokens"]
        == {"access_token": "codex-new-access", "refresh_token": "codex-new-refresh", "id_token": "new-id"},
    )
    mirrored = json.loads(codex_mirror.read_text())["tokens"]
    check(
        "Codex worker mirror uses only the parser placeholder",
        mirrored["refresh_token"] == refresher.CODEX_REFRESH_PLACEHOLDER,
    )

    # A server may rotate the refresh token and then return an incomplete response. Preserve the
    # new token before reporting the malformed exchange so a retry does not replay the old one.
    with patch.object(
        refresher.requests,
        "post",
        return_value=Response({"refresh_token": "codex-rescue-refresh"}),
    ):
        try:
            refresher.refresh_if_due(provider, 5400, force=True)
        except ValueError:
            pass
        else:
            raise AssertionError("incomplete response should fail")
    updated = json.loads(codex_file.read_text())
    check("rotated token survives an incomplete response", updated["tokens"]["refresh_token"] == "codex-rescue-refresh")

    old_access = jwt(int(time.time()) + 3600)
    codex_file.write_text(json.dumps({"tokens": {"access_token": old_access, "refresh_token": "before-regression"}}))
    with patch.object(
        refresher.requests,
        "post",
        return_value=Response({"access_token": jwt(int(time.time()) + 60), "refresh_token": "after-regression"}),
    ):
        try:
            refresher.refresh_if_due(provider, 5400, force=True)
        except ValueError:
            pass
        else:
            raise AssertionError("regressing response should fail")
    updated = json.loads(codex_file.read_text())
    check("regressing access token is rejected", updated["tokens"]["access_token"] == old_access)
    check("rotated token survives a regressing response", updated["tokens"]["refresh_token"] == "after-regression")

    leftovers = list(root.rglob(".refresh.*"))
    check("atomic writes leave no temporary files", not leftovers)
