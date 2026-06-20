#!/usr/bin/env python3
"""In-flight review de-contention (the worker-side early skip).

The review engine posts a `<!--tauceti-review-in-progress {json}-->` marker on a head it is reviewing,
with an embedded `expires_at` so a crashed reviewer self-clears. De-contention is on the head ALONE (a
commit is reviewed once, regardless of model). The worker now reads the SAME marker during the survey so
it skips a head a peer already holds BEFORE paying the engine's build+launch cost — instead of
re-selecting that one PR every round and busy-looping. This harness pins the pure read decision (given a
PR's comments + a head, which providers hold it?) against the engine's rules, without touching the API.

Exit 0 = all cases agree; 1 = a mismatch.
"""
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

spec = importlib.util.spec_from_loader("tauceti", importlib.machinery.SourceFileLoader("tauceti", str(REPO / "tauceti")))
tc = importlib.util.module_from_spec(spec)
sys.modules["tauceti"] = tc
spec.loader.exec_module(tc)

NOW = 1_700_000_000
HEAD = "509d78605409abcdef0123456789abcdef012345"
OTHER = "0000000000000000000000000000000000000000"


def marker(head, providers, expires_at):
    """A PR issue comment carrying an in-progress marker, byte-for-byte like the engine posts."""
    payload = {"schema": "tauceti-review-in-progress/v1", "nonce": "n", "providers": providers,
               "head": head, "submitted_by": "", "started_at": expires_at - 1800,
               "expires_at": expires_at}
    body = (f"🔍 Review in progress — `{','.join(providers)}` reviewing `{head[:12]}`."
            f"\n<!--tauceti-review-in-progress {json.dumps(payload, separators=(',', ':'))}-->")
    return {"id": 1, "body": body}


# (name, comments, want_providers) — non-empty => a peer holds this head => the worker SKIPS.
CASES = [
    ("no comments",              [],                                                set()),
    ("fetch failed (fail-open)", None,                                              set()),
    ("plain comment, no marker", [{"id": 1, "body": "looks good to me"}],           set()),
    ("fresh codex marker",       [marker(HEAD, ["codex"], NOW + 1700)],             {"codex"}),
    ("fresh claude marker",      [marker(HEAD, ["claude"], NOW + 1700)],            {"claude"}),
    ("expired marker",           [marker(HEAD, ["codex"], NOW - 30)],               set()),
    ("marker on a stale head",   [marker(OTHER, ["codex"], NOW + 1700)],            set()),
    ("stale head + fresh head",  [marker(OTHER, ["codex"], NOW + 1700),
                                  marker(HEAD, ["claude"], NOW + 1700)],            {"claude"}),
    ("expired + fresh on head",  [marker(HEAD, ["codex"], NOW - 30),
                                  marker(HEAD, ["claude"], NOW + 1700)],            {"claude"}),
    ("malformed marker json",    [{"id": 1, "body": "<!--tauceti-review-in-progress {nope-->"}], set()),
    ("missing expires_at",       [{"id": 1, "body": '<!--tauceti-review-in-progress {"head":"%s","providers":["codex"]}-->' % HEAD}], set()),
]


def main() -> int:
    fails = 0
    for name, comments, want in CASES:
        got = tc.inflight_review_providers(comments, HEAD, NOW)
        ok = got == want
        fails += not ok
        verb = "SKIP" if got else "fire"
        print(f"[{'OK ' if ok else 'BAD'}] {name:<26} -> {verb} {sorted(got)} (want {sorted(want)})")
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
