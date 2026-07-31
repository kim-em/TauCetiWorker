#!/usr/bin/env python3
"""Review-command failures retain useful public-safe diagnostics and enrich stuck issues."""

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import github as gh_mod
from tauceti_worker.review_diagnostics import (
    clear_review_failure,
    public_review_failure,
    read_review_failure,
    record_review_failure,
    recover_review_failures,
    sanitize_failure,
)

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


with tempfile.TemporaryDirectory() as raw:
    state = Path(raw)
    log = state / "review.log"
    log.write_text("setup\nNot logged in · Please run /login\n")
    value = record_review_failure(
        state,
        worker="worker7",
        pr=1388,
        head="deadbeef",
        provider="claude",
        code=1,
        log_file=log,
    )
    attempt = value["attempts"][-1]
    check("log tail classified", attempt["category"], "reviewer-auth")
    check("log path reduced to basename", attempt["log"], "review.log")
    check("public diagnostic names provider", "via `claude`" in public_review_failure(value), True)

    for i in range(4):
        record_review_failure(
            state,
            worker="worker7",
            pr=1388,
            head="deadbeef",
            provider="codex",
            code=i + 2,
            reason=f"review #1388 exited with status {i + 2}: engine error: attempt {i}",
        )
    check("history capped at three", len(read_review_failure(state, 1388)["attempts"]), 3)
    clear_review_failure(state, 1388)
    check("successful review clears diagnostic", read_review_failure(state, 1388), {})

    old_log = state / "review-1500-20260730-185500.log"
    old_log.write_text("clone setup\nfatal: could not resolve host github.com\n")
    recovered = recover_review_failures(state, state, worker="worker7", pr=1500, head="cafebabe")
    check("legacy log is recovered", len(recovered["attempts"]), 1)
    check("legacy log is classified", recovered["attempts"][0]["category"], "checkout-or-network")
    check("legacy log records provenance", recovered["attempts"][0]["recovered"], True)
    check("legacy log does not invent an exit code", "(exit unknown)" in public_review_failure(recovered), True)

secret = "OPENAI_API_KEY=sk-secretvalue123 /Users/alice/work https://token@example.com/repo"
clean = sanitize_failure(secret)
check("API key redacted", "secretvalue" in clean, False)
check("home user redacted", "alice" in clean, False)
check("credential URL redacted", "token@" in clean, False)

adversarial = {
    "schema": "tauceti.review-failure/v1",
    "attempts": [
        {
            "at": "now ](https://example.com)",
            "provider": "codex` token",
            "code": 1,
            "category": "review-engine`",
            "summary": (
                "Authorization: Basic dXNlcjpwYXNzd29yZA== "
                "ANTHROPIC_API_KEY = supersecretvalue "
                '{"api_key":"jsonsecret"} client_secret: oauthsecret '
                "cookie=sessionsecret eyJhbGciOiJIUzI1NiJ9.payload.signature"
            ),
        }
    ],
}
public = public_review_failure(adversarial)
for leaked in (
    "dXNlcjpwYXNzd29yZA",
    "supersecretvalue",
    "jsonsecret",
    "oauthsecret",
    "sessionsecret",
    "eyJhbGci",
    "https://example.com",
    "codex` token",
):
    check(f"public output excludes {leaked}", leaked in public, False)
check(
    "unknown public fields fail closed",
    public,
    "- unknown time: `review-command` via `unknown` (exit 1): review command failed",
)

e2big = {
    "attempts": [
        {
            "at": "2026-07-30T23:00:00Z",
            "provider": "codex",
            "code": 1,
            "category": "review-engine",
            "summary": "OSError: [Errno 7] Argument list too long: 'codex' SECRET=do-not-copy",
        }
    ]
}
public = public_review_failure(e2big)
check("E2BIG has fixed public diagnostic", "review prompt exceeded the OS argument limit" in public, True)
check("E2BIG raw summary stays private", "do-not-copy" in public, False)


class FakeGitHub(gh_mod.GitHub):
    def __init__(self, existing=None):
        super().__init__("TauCetiProject/TauCeti")
        self.existing = existing or []
        self.calls = []

    def _gh(self, args):
        self.calls.append(args)
        if args[:2] == ["issue", "list"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.existing))
        return SimpleNamespace(returncode=0, stdout="")


diagnostic = "- 2026-07-30T18:55:00Z: `reviewer-auth` via `claude` (exit 1): Not logged in"
client = FakeGitHub()
client.ensure_stuck_issue(1388, "its review errored", diagnostic)
check("missing issue is created", client.calls[-1][:2], ["issue", "create"])
body = client.calls[-1][client.calls[-1].index("--body") + 1]
check("created issue carries diagnostic", diagnostic in body, True)
check("created issue asks for infrastructure repair", "infrastructure repair" in body, True)

existing_body = gh_mod.GitHub._stuck_issue_body(1388, "its review errored", diagnostic)
client = FakeGitHub([{"number": 1504, "title": "Review stuck: PR #1388", "body": existing_body}])
client.ensure_stuck_issue(1388, "its review errored", diagnostic)
check("unchanged issue is not edited", len(client.calls), 1)

client = FakeGitHub([{"number": 1504, "title": "Review stuck: PR #1388", "body": "old"}])
client.ensure_stuck_issue(1388, "its review errored", diagnostic)
check("existing issue is enriched", client.calls[-1][:2], ["issue", "edit"])
check("right issue is edited", client.calls[-1][2], "1504")

other_diagnostic = "- 2026-07-30T18:56:00Z: `review-command` via `codex` (exit 1): review command failed"
existing_body = gh_mod.GitHub._stuck_issue_body(1388, "its review errored", diagnostic)
client = FakeGitHub([{"number": 1504, "title": "Review stuck: PR #1388", "body": existing_body}])
client.ensure_stuck_issue(1388, "its review errored again", other_diagnostic)
check("peer diagnostic does not clobber existing evidence", len(client.calls), 1)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
