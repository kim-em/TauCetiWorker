#!/usr/bin/env python3
"""Single-writer OAuth refresher for an unattended TauCeti Docker worker."""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import signal
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_SCOPE = "user:profile user:inference user:sessions:claude_code user:mcp_servers"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_REFRESH_PLACEHOLDER = "rt.0.tauceti-worker-placeholder-never-a-real-refresh-token"


@dataclass(frozen=True)
class Provider:
    name: str
    credentials: Path
    token_url: str
    client_id: str
    mirror: Path | None = None


def _provider(name: str) -> Provider:
    home = Path(os.environ.get("HOME", "/root"))
    if name == "claude":
        config = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude"))
        provider = Provider(
            name,
            config / ".credentials.json",
            os.environ.get("TAUCETI_CLAUDE_TOKEN_URL", CLAUDE_TOKEN_URL),
            CLAUDE_CLIENT_ID,
        )
    else:
        provider = Provider(
            name,
            home / ".codex" / "auth.json",
            os.environ.get("TAUCETI_CODEX_TOKEN_URL", CODEX_TOKEN_URL),
            CODEX_CLIENT_ID,
        )
    mirror = os.environ.get("TAUCETI_REFRESH_MIRROR")
    return Provider(
        provider.name, provider.credentials, provider.token_url, provider.client_id, Path(mirror) if mirror else None
    )


def _jwt_exp(token: str) -> int | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return int(exp) if exp is not None else None
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _expires_at(provider: Provider, data: dict[str, Any]) -> float | None:
    if provider.name == "claude":
        value = (data.get("claudeAiOauth") or {}).get("expiresAt")
        if isinstance(value, (int, float)):
            return value / 1000 if value >= 100_000_000_000 else float(value)
        return None
    token = (data.get("tokens") or {}).get("access_token")
    return _jwt_exp(token) if isinstance(token, str) else None


def _refresh_request(provider: Provider, data: dict[str, Any]) -> tuple[dict[str, str], str]:
    if provider.name == "claude":
        block = data.get("claudeAiOauth") or {}
        refresh_token = block.get("refreshToken")
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": provider.client_id,
            "scope": CLAUDE_SCOPE,
        }
    else:
        refresh_token = (data.get("tokens") or {}).get("refresh_token")
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": provider.client_id,
        }
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ValueError(f"no renewable {provider.name} credential; log in again")
    return payload, refresh_token


def _merge_response(
    provider: Provider,
    credentials: dict[str, Any],
    response: dict[str, Any],
    old_refresh_token: str,
) -> dict[str, Any]:
    """Merge every usable rotated token before validating the rest of the response.

    Refresh tokens are single-use. If the server returned a new one, preserving it is more
    important than rejecting an incomplete response and leaving the already-consumed token on disk.
    """
    refresh_token = response.get("refresh_token") or old_refresh_token
    access_token = response.get("access_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ValueError("refresh response contained no refresh token")

    prior_expiry = _expires_at(provider, credentials)
    if provider.name == "claude":
        expires_in = response.get("expires_in")
        candidate_expiry = time.time() + expires_in if isinstance(expires_in, (int, float)) else None
        block = dict(credentials.get("claudeAiOauth") or {})
        block["refreshToken"] = refresh_token
        regressed = prior_expiry is not None and candidate_expiry is not None and candidate_expiry <= prior_expiry
        if isinstance(access_token, str) and access_token and not regressed:
            block["accessToken"] = access_token
        if (
            isinstance(access_token, str)
            and access_token
            and candidate_expiry is not None
            and candidate_expiry > 0
            and not regressed
        ):
            block["expiresAt"] = int(candidate_expiry * 1000)
        credentials["claudeAiOauth"] = block
    else:
        candidate_expiry = _jwt_exp(access_token) if isinstance(access_token, str) else None
        regressed = prior_expiry is not None and candidate_expiry is not None and candidate_expiry <= prior_expiry
        tokens = dict(credentials.get("tokens") or {})
        tokens["refresh_token"] = refresh_token
        if isinstance(access_token, str) and access_token and not regressed:
            tokens["access_token"] = access_token
        id_token = response.get("id_token")
        if isinstance(id_token, str) and id_token and not regressed:
            tokens["id_token"] = id_token
        credentials["tokens"] = tokens
        credentials["last_refresh"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    if not isinstance(access_token, str) or not access_token:
        # The caller still persists a newly rotated refresh token before surfacing this error.
        raise ValueError("refresh response contained no access token")
    if regressed:
        raise ValueError("refresh response would regress the access-token expiry")
    return credentials


def _write_atomic(destination: Path, data: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".refresh.", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _write_worker_mirror(provider: Provider, credentials: dict[str, Any]) -> None:
    """Publish an access-only copy; the agent container never mounts the real refresh token."""
    if provider.mirror is None:
        return
    output = dict(credentials)
    if provider.name == "claude":
        block = dict(output.get("claudeAiOauth") or {})
        block.pop("refreshToken", None)
        if not block.get("accessToken"):
            return
        output["claudeAiOauth"] = block
    else:
        tokens = dict(output.get("tokens") or {})
        if not tokens.get("access_token"):
            return
        tokens["refresh_token"] = CODEX_REFRESH_PLACEHOLDER
        output["tokens"] = tokens
    _write_atomic(provider.mirror, output)


def _last_success(marker: Path) -> float | None:
    try:
        value = json.loads(marker.read_text()).get("at")
        return float(value) if isinstance(value, (int, float)) else None
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return None


def _effective_skew(skew_seconds: int, expiry: float, last_success: float | None) -> float:
    """Never spend more than half a freshly issued token's lifetime refreshing early."""
    if last_success is None or expiry <= last_success:
        return float(skew_seconds)
    return min(float(skew_seconds), max(1.0, (expiry - last_success) / 2))


def refresh_if_due(
    provider: Provider,
    skew_seconds: int,
    *,
    force: bool = False,
    minimum_interval_seconds: int = 600,
) -> str:
    provider.credentials.parent.mkdir(parents=True, exist_ok=True)
    credential_name = provider.credentials.name.lstrip(".")
    lock_path = provider.credentials.with_name(f".{credential_name}.refresh.lock")
    success_path = provider.credentials.with_name(f".{credential_name}.refresh.last-success")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            credentials = json.loads(provider.credentials.read_text())
        except FileNotFoundError:
            return "waiting"
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"cannot read {provider.credentials}: {error}") from error

        # Publish immediately after login/startup as well as after a rotation. This lets the
        # read-only worker volume become usable without waiting for the token to approach expiry.
        _write_worker_mirror(provider, credentials)

        now = time.time()
        expiry = _expires_at(provider, credentials)
        last_success = _last_success(success_path)
        effective_skew = _effective_skew(skew_seconds, expiry, last_success) if expiry is not None else skew_seconds
        if not force and expiry is not None and expiry > now + effective_skew:
            return "current"
        if not force and expiry is None:
            raise ValueError(f"cannot determine {provider.name} access-token expiry; log in again")
        if not force and last_success is not None and now < last_success + minimum_interval_seconds:
            return "cooldown"

        payload, old_refresh_token = _refresh_request(provider, credentials)
        response = requests.post(provider.token_url, json=payload, timeout=15)
        if not response.ok:
            # Do not echo the body: an authentication service is allowed to include request
            # details, and refresh tokens must never reach container logs.
            raise RuntimeError(f"OAuth endpoint returned HTTP {response.status_code}")
        try:
            body = response.json()
        except requests.JSONDecodeError as error:
            raise RuntimeError("OAuth endpoint returned invalid JSON") from error
        if not isinstance(body, dict):
            raise RuntimeError("OAuth endpoint returned a non-object response")

        try:
            updated = _merge_response(provider, credentials, body, old_refresh_token)
        except ValueError:
            # If the exchange rotated the refresh token but omitted another required field, preserve
            # that token so the next attempt does not replay the consumed credential.
            if body.get("refresh_token"):
                _write_atomic(provider.credentials, credentials)
                _write_worker_mirror(provider, credentials)
            raise
        _write_atomic(provider.credentials, updated)
        _write_worker_mirror(provider, updated)
        _write_atomic(success_path, {"at": time.time()})
        return "refreshed"


def _positive_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer (got {raw!r})") from error
    if value < 1:
        raise ValueError(f"{name} must be positive (got {value})")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=("claude", "codex"))
    args = parser.parse_args()

    provider = _provider(args.provider)
    try:
        poll_seconds = _positive_env("TAUCETI_REFRESH_POLL_SECONDS", 60)
        skew_seconds = _positive_env("TAUCETI_REFRESH_SKEW_SECONDS", 5400)
        minimum_interval = _positive_env("TAUCETI_REFRESH_MIN_INTERVAL_SECONDS", 600)
        maximum_backoff = _positive_env("TAUCETI_REFRESH_MAX_BACKOFF_SECONDS", 900)
    except ValueError as error:
        parser.error(str(error))
    run_once = os.environ.get("TAUCETI_REFRESH_ONCE") == "1"
    stop = threading.Event()
    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda _signum, _frame: stop.set())

    print(f"{provider.name}-refresh: monitoring {provider.credentials}", flush=True)
    backoff = poll_seconds
    while not stop.is_set():
        try:
            result = refresh_if_due(provider, skew_seconds, minimum_interval_seconds=minimum_interval)
            if result == "waiting":
                print(f"{provider.name}-refresh: waiting for {provider.credentials}", flush=True)
            elif result == "refreshed":
                print(f"{provider.name}-refresh: credential refreshed", flush=True)
            backoff = poll_seconds
        except (OSError, RuntimeError, ValueError, requests.RequestException) as error:
            print(f"{provider.name}-refresh: {error}; retrying in {backoff}s", file=sys.stderr, flush=True)
            if run_once:
                return 1
            if stop.wait(backoff):
                break
            backoff = min(maximum_backoff, max(poll_seconds, backoff * 2))
            continue
        if run_once:
            return 0
        stop.wait(poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
