#!/usr/bin/env python3
"""Selector parity harness (plan milestone 3b).

Checks the riskiest jq→Python ports — the pure-over-the-PR-list selectors that don't depend on the
per-PR scoreboard ledger — by running the EXACT round.sh jq strings against a snapshot of `gh pr list`
and comparing to tauceti's Python classifications. Ledger-dependent kinds (review/fix/abandon) are
validated separately by live behavior; this harness covers the mechanical list filtering.

Usage:
  tests/parity_selectors.py            # live-fetch a snapshot from GitHub, compare
  tests/parity_selectors.py FIXTURE    # compare against a saved `gh pr list ... --json ...` JSON file

Exit 0 = all selectors agree; 1 = a mismatch (prints the diff).
"""
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# Load the `tauceti` single-file program as a module (no .py extension; main() is guarded).
spec = importlib.util.spec_from_loader("tauceti", importlib.machinery.SourceFileLoader("tauceti", str(REPO / "tauceti")))
tc = importlib.util.module_from_spec(spec)
sys.modules["tauceti"] = tc   # dataclasses resolves annotations via sys.modules[cls.__module__]
spec.loader.exec_module(tc)

FIELDS = ["number", "headRefOid", "headRefName", "headRepositoryOwner", "headRepository",
          "isDraft", "statusCheckRollup", "author", "mergeable"]
ME = tc.ME
TAUCETI = tc.TAUCETI


def jq(data, expr, args=None):
    cmd = ["jq", "-c"] + (args or []) + [expr]
    p = subprocess.run(cmd, input=json.dumps(data), text=True, capture_output=True)
    if p.returncode != 0:
        raise SystemExit(f"jq failed: {p.stderr}")
    return [json.loads(l) for l in p.stdout.splitlines() if l.strip()]


def fetch_live():
    p = subprocess.run(["gh", "pr", "list", "--repo", TAUCETI, "--state", "open", "--limit", "200",
                        "--json", ",".join(FIELDS)], text=True, capture_output=True)
    if p.returncode != 0:
        raise SystemExit(f"gh failed: {p.stderr}")
    return json.loads(p.stdout)


def main():
    if len(sys.argv) > 1:
        data = json.loads(Path(sys.argv[1]).read_text())
    else:
        data = fetch_live()
    prs = [tc.PRInfo.from_json(d) for d in data]
    fails = 0

    def check(name, jq_expr, py_set, jq_args=None):
        nonlocal fails
        ref = sorted(r["number"] for r in jq(data, jq_expr, jq_args))
        got = sorted(py_set)
        ok = ref == got
        print(f"[{'OK ' if ok else 'XX '}] {name:14} jq={ref} py={got}")
        if not ok:
            fails += 1

    # n_reviewable: non-draft AND a build check SUCCESS (round.sh line 816-817).
    check("reviewable*",
          '.[] | select(.isDraft|not) '
          '| select([.statusCheckRollup[]? | select(.name=="build")] | any(.conclusion=="SUCCESS"))',
          [p.number for p in prs if not p.is_draft and p.build_success])

    # rebaseable: mine, non-draft, CONFLICTING (round.sh 832-835).
    check("rebaseable",
          '.[] | select(.isDraft|not) | select(.author.login=="%s") | select(.mergeable=="CONFLICTING")' % ME,
          [p.number for p in prs if not p.is_draft and p.author == ME and p.mergeable == "CONFLICTING"])

    # red_ci: mine, build check FAILED-ish (round.sh 878-882).
    fail_set = '"FAILURE","ERROR","TIMED_OUT","CANCELLED","STARTUP_FAILURE","ACTION_REQUIRED"'
    check("red_ci",
          '.[] | select(.author.login=="%s") '
          '| select([.statusCheckRollup[]? | select(.name=="build") '
          '| select(.conclusion | IN(%s))] | any)' % (ME, fail_set),
          [p.number for p in prs if p.author == ME and p.build_failed])

    # bump: a hopscotch/lkg-bump PR (bot-authored) whose build is red.
    check("bump (hopscotch)",
          '.[] | select(.headRefName|startswith("hopscotch/")) '
          '| select([.statusCheckRollup[]? | select(.name=="build") '
          '| select(.conclusion | IN(%s))] | any)' % fail_set,
          [p.number for p in prs if p.head_ref.startswith("hopscotch/") and p.build_failed])

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} selector mismatch(es)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
