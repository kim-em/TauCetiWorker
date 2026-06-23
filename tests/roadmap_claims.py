#!/usr/bin/env python3
"""Roadmap claim discovery: the worker avoids targets other contributors have claimed on the
intentions board, but not unclaimed intentions or claims held by its own (or operator-named)
identities.

These exercise the pure pieces (no network): parse_scope (pull "Items in scope" from an Intention
issue body), select_foreign (ours/theirs/unassigned filtering), and claimed_block (prompt rendering).

Exit 0 = all cases agree; 1 = a mismatch.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import intentions as it  # noqa: E402

fails = 0


def check(name, ok):
    global fails
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}")


# --- parse_scope -----------------------------------------------------------
form_body = """### Roadmap area

OneParameterSemigroups

### Items in scope

representation theorems:
- Bernstein
- Bochner (finite-dim)

### Notes

_No response_
"""
check("parse_scope pulls the Items in scope section", it.parse_scope(form_body)
      == "representation theorems: - Bernstein - Bochner (finite-dim)")
check("parse_scope strips HTML-comment markers",
      it.parse_scope("### Items in scope\n\nfoo <!--tauceti-claim:v1 {\"x\":1}--> bar") == "foo bar")
check("parse_scope falls back to whole body when no section", it.parse_scope("just a sentence") == "just a sentence")
check("parse_scope empty -> empty", it.parse_scope("") == "")


# --- select_foreign --------------------------------------------------------
issues = [
    {"number": 1, "url": "u1", "title": "A", "body": "### Items in scope\n\nobject API",
     "assignees": [{"login": "kim-em"}]},                       # ours
    {"number": 2, "url": "u2", "title": "B", "body": "### Items in scope\n\nrepresentation theorems",
     "assignees": [{"login": "mrdouglasny"}]},                  # foreign
    {"number": 3, "url": "u3", "title": "C", "body": "unclaimed", "assignees": []},  # registered, unclaimed
    {"number": 4, "url": "u4", "title": "D", "body": "### Items in scope\n\npaired",
     "assignees": [{"login": "Kim-Em"}, {"login": "someone"}]},  # co-assigned incl. ours (case-insensitive)
]
own = {"kim-em"}
foreign = it.select_foreign(issues, own)
nums = sorted(c.number for c in foreign)
check("only the foreign, assigned claim is kept", nums == [2])
check("foreign claim carries holders", foreign and foreign[0].holders == ["mrdouglasny"])
check("foreign claim carries parsed scope", foreign and foreign[0].scope == "representation theorems")

# extra identities widen "ours": mrdouglasny becomes ours too
check("extra identities suppress a foreign claim",
      it.select_foreign(issues, {"kim-em", "mrdouglasny"}) == [])


# --- claimed_block ---------------------------------------------------------
check("claimed_block empty -> none", it.claimed_block([]) == "none")
block = it.claimed_block(foreign)
check("claimed_block lists holder + number + scope",
      block == "- (claimed by @mrdouglasny, #2) representation theorems")


print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
