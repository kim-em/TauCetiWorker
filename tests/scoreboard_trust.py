#!/usr/bin/env python3
"""gh_meta must accept a scoreboard regardless of the comment author's repo association.

`author_association` is viewer-dependent: a reviewer who is a PRIVATE org member reads as MEMBER to
themselves but as CONTRIBUTOR (or NONE) to an outside contributor. The old trust filter
({OWNER, MEMBER, COLLABORATOR}) therefore silently discarded legitimate scoreboards for every
unprivileged contributor — Bryan's PR #470 had a real kim-em scoreboard with four blocking rubrics that
his worker treated as "no scoreboard at this head", so `fix` never ran. gh_meta now identifies the
scoreboard by the <!--tauceti-scoreboard--> marker alone and parses the newest one's meta. (Safe: this
meta only drives the worker's own review/fix eligibility; merges are gated by the write-restricted
TauCetiData records, not this comment.)

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import json
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def sb(head, updated, assoc="CONTRIBUTOR"):
    """A scoreboard comment: the marker + a tauceti-meta block, authored with `assoc` association."""
    meta = json.dumps({"head_sha": head, "states": {"reuse": "blocking_request"}})
    return {
        "body": f"<!--tauceti-scoreboard-->\nscores here\n<!--tauceti-meta:v1 {meta}-->",
        "updated_at": updated,
        "author_association": assoc,
    }


def plain(updated, assoc="MEMBER"):
    """A non-scoreboard comment (no marker) — must be ignored even from a 'trusted' author."""
    meta = json.dumps({"head_sha": "FORGED"})
    return {"body": f"just chatting <!--tauceti-meta:v1 {meta}-->", "updated_at": updated, "author_association": assoc}


def make_rs(comments):
    cfg = types.SimpleNamespace(sbcache=Path(tempfile.mkdtemp()) / "sb")
    gh = types.SimpleNamespace(issue_comments=lambda pr: comments)
    return tc.ReviewState(cfg, gh)


# 1) A scoreboard from a CONTRIBUTOR (the outside-contributor view of a private member) is trusted.
m = make_rs([sb("HEAD1", "2026-06-26T13:49:44Z", assoc="CONTRIBUTOR")]).gh_meta(470)
check("CONTRIBUTOR-authored scoreboard is parsed", m.data.get("head_sha") == "HEAD1")
check("CONTRIBUTOR-authored scoreboard is fresh", m.provenance == "fresh")

# 2) Even a NONE-association author is trusted (we deliberately don't gate on association anymore).
m = make_rs([sb("HEAD2", "2026-06-26T13:49:44Z", assoc="NONE")]).gh_meta(471)
check("NONE-authored scoreboard is parsed", m.data.get("head_sha") == "HEAD2")

# 3) Newest marked scoreboard (by updated_at) wins, regardless of author association ordering.
m = make_rs(
    [
        sb("OLD", "2026-06-26T10:00:00Z", assoc="MEMBER"),
        sb("NEW", "2026-06-26T14:00:00Z", assoc="CONTRIBUTOR"),
    ]
).gh_meta(472)
check("newest marked scoreboard wins", m.data.get("head_sha") == "NEW")

# 4) A comment WITHOUT the scoreboard marker is ignored even if it carries a meta block and a MEMBER author.
m = make_rs([plain("2026-06-26T15:00:00Z", assoc="MEMBER")]).gh_meta(473)
check("non-marker comment is not treated as a scoreboard", m.data == {} and m.provenance == "missing")

# 4b) ...and a real scoreboard alongside a (newer) non-marker comment still wins.
m = make_rs([sb("REAL", "2026-06-26T12:00:00Z"), plain("2026-06-26T16:00:00Z")]).gh_meta(474)
check("marker comment used despite a newer plain comment", m.data.get("head_sha") == "REAL")

# 5) No scoreboard at all -> empty/missing.
m = make_rs([plain("2026-06-26T09:00:00Z")]).gh_meta(475)
check("no scoreboard -> missing", m.data == {} and m.provenance == "missing")

# 6) Fetch failure (issue_comments None) with no cache -> fetch_failed sentinel.
m = make_rs(None).gh_meta(476)
check("fetch failure -> fetch_failed", m.data == {} and m.provenance == "fetch_failed")

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
