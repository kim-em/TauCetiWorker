"""tauceti_worker.intentions — discover cross-contributor claims on the intentions board.

A claim is an open issue in the roadmap repo labelled `intention` + `roadmap/<area>` that has
been claimed (the intentions bot assigns the claimant). Roadmap workers avoid targets claimed
by *other* contributors; claims by the operator's own identity (or operator-configured extra
identities) are not avoided. Phase 1: the avoid-list is the claim's prose scope, not matched
against machine-readable target ids. Coordination is cooperative and fail-open.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .config import log, roadmap_extra_identities
from .constants import ROADMAP
from .github import GitHub, me

# A claim's scope is untrusted text from an arbitrary issue body, injected into the worker prompt;
# bound its length and (in claimed_block) quote it so it reads as data, not instructions.
MAX_SCOPE_CHARS = 200


@dataclass
class Claim:
    number: int
    url: str
    holders: list[str]  # GitHub logins of the assignees (the claimants)
    scope: str  # prose description of what they're working on


def own_identities() -> set[str]:
    """The set of logins whose claims this worker will NOT avoid: its own `gh auth` identity plus
    any operator-configured extras. Lowercased for case-insensitive comparison."""
    return {me().lower()} | set(roadmap_extra_identities())


# GitHub issue forms render each field as a `### <label>` section; pull the "Items in scope" one.
_SCOPE_RE = re.compile(r"^###\s+Items in scope\s*$\n(.*?)(?=^###\s|\Z)", re.S | re.I | re.M)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
# GitHub issue forms render an unanswered field as "_No response_"; treat that as no scope.
_PLACEHOLDER_RE = re.compile(r"^_?no response_?$", re.I)


def parse_scope(body: str) -> str:
    """Extract the 'Items in scope' section of an Intention issue body, falling back to the whole
    body. HTML-comment markers are stripped and whitespace collapsed to a single line. An empty or
    `_No response_` placeholder answer yields ""; the caller then falls back to the issue title."""
    if not body:
        return ""
    m = _SCOPE_RE.search(body)
    text = m.group(1) if m else body
    text = _HTML_COMMENT_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return "" if _PLACEHOLDER_RE.match(text) else text


def select_foreign(issues: list[dict], own: set[str]) -> list[Claim]:
    """Pure filter: from raw intention-issue dicts, keep those claimed (assigned) by someone outside
    `own`. Unassigned issues (registered but unclaimed) and our-own claims are dropped. `own` must be
    lowercased. Kept separate from the network fetch so it's unit-testable with fixtures."""
    claims: list[Claim] = []
    for it in issues:
        holders = [a.get("login", "") for a in (it.get("assignees") or []) if a.get("login")]
        if not holders:
            continue  # registered but unclaimed -> still up for grabs
        if any(h.lower() in own for h in holders):
            continue  # ours (or an operator-named identity)
        scope = parse_scope(it.get("body") or "") or (it.get("title") or "").strip()
        claims.append(Claim(number=int(it.get("number") or 0), url=it.get("url") or "", holders=holders, scope=scope))
    return claims


def foreign_claims(gh: GitHub, area: str, own: set[str]) -> list[Claim]:
    """Open intention issues for `area` that are claimed by someone outside `own` (see select_foreign)."""
    issues = gh.issue_list(
        ROADMAP,
        labels=["intention", f"roadmap/{area}"],
        fields=["number", "url", "title", "body", "assignees"],
    )
    return select_foreign(issues, own)


def claimed_block(claims: list[Claim]) -> str:
    """Render foreign claims as the `__CLAIMED__` avoid-list for the prompt; "none" when empty.

    The scope is untrusted issue text, so it's truncated and JSON-quoted into a single token: the
    prompt frames these quoted strings as data (targets to avoid), never as instructions. Trusted
    metadata (issue number, claimant logins) sits outside the quoted scope."""
    if not claims:
        return "none"
    lines = []
    for c in claims:
        who = ", ".join(f"@{h}" for h in c.holders)
        scope = c.scope[:MAX_SCOPE_CHARS] or "(no description provided)"
        lines.append(f"- #{c.number} (claimed by {who}): {json.dumps(scope)}")
    return "\n".join(lines)


def claimed_avoid_list(gh: GitHub, area: str) -> str:
    """The `__CLAIMED__` string for `area`: foreign-claim scopes to avoid. Fails open to "none" on
    any error, so a transient API hiccup never blocks authoring (cooperative, soft coordination)."""
    try:
        return claimed_block(foreign_claims(gh, area, own_identities()))
    except Exception as e:  # noqa: BLE001 - fail open on anything (soft, cooperative coordination)
        log(f"roadmap: could not fetch claims for {area} ({e}); proceeding without claim exclusions")
        return "none"
