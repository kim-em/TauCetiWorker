"""tauceti_worker.github — split from the monolithic worker (behaviour-preserving)."""

from __future__ import annotations

import functools
import json
import re
import subprocess
import time
from pathlib import Path

from .config import Die, log
from .constants import (
    _GH_PRIMARY_RE,
    _GH_SECONDARY_RE,
    CONTEST_CLAIM_EMOJI,
    GH_INROUND_WAIT,
    GH_SECONDARY_BASE,
    TAUCETI,
)


@functools.lru_cache(maxsize=1)
def me() -> str:
    """The GitHub login the worker is authenticated as (gh). Its PRs are the ones the worker tends
    (fix / fix-ci / rebase). Never hardcoded: whoever set up `gh auth` is who the worker acts as."""
    r = gh_run(["gh", "api", "user", "--jq", ".login"])  # waits out a rate limit rather than failing setup
    login = (r.stdout or "").strip()
    if not login:
        raise Die("could not determine the authenticated GitHub account (run `gh auth login`)")
    return login


# ============================================================================
# run() — subprocess helpers. close_fds=True (Python default) means children never
# inherit the round.lock fd, retiring round.sh's hand-managed `9>&-` fd-leak fix.
# ============================================================================


class GitHubError(Exception):
    """A `gh` call failed (distinct from 'ran fine, returned no rows')."""


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict | None = None,
    capture: bool = True,
    check: bool = False,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=capture,
        input=input_text,
        check=check,
    )


def _parse_iso8601(s: str | None) -> int | None:
    """ISO 8601 (e.g. GitHub's '2026-06-19T08:03:48Z') → epoch seconds, or None on a bad value."""
    if not s:
        return None
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


# ============================================================================
# GitHub REST rate limits. A 403 mid-round wastes the agent's work, so we wait the limit out and retry
# rather than failing the round (see GH_* constants). gh prints the limit kind on stdout/stderr.
# Two kinds, handled differently because of the ROUND_TIMEOUT hard cap on a child round:
#  - SECONDARY ("abuse") limits clear after a short, unspecified cooldown → wait IN PLACE and retry,
#    bounded by GH_INROUND_WAIT so the wait can't blow the round timeout.
#  - PRIMARY limits clear only at the hourly bucket reset → too long to wait under the round cap, so
#    surface immediately and let the loop preflight (cmd_loop, no hard cap) wait the reset out before
#    relaunching. (GitHub buckets core and graphql resets independently; the preflight watches both.)
# ============================================================================


def _gh_rate_kind(text: str) -> str | None:
    """Classify a failed `gh` call from its combined stdout+stderr: 'secondary' | 'primary' | None.
    Secondary first — its message also contains 'rate limit', so the primary regex would match it too."""
    if _GH_SECONDARY_RE.search(text):
        return "secondary"
    if _GH_PRIMARY_RE.search(text):
        return "primary"
    return None


def github_budget() -> dict | None:
    """Per-bucket (remaining, reset_epoch) from GitHub's rate_limit endpoint, keyed 'core' and 'graphql'
    — the two buckets a round spends (REST and the progress-guard GraphQL query). That endpoint is itself
    exempt from the budget, so probing it is free. None on any read failure (caller proceeds rather than
    block on a flaky probe)."""
    p = run(
        [
            "gh",
            "api",
            "rate_limit",
            "--jq",
            "{core:[.resources.core.remaining,.resources.core.reset],"
            "graphql:[.resources.graphql.remaining,.resources.graphql.reset]}",
        ]
    )
    if p.returncode != 0:
        return None
    try:
        d = json.loads(p.stdout)
        return {k: (int(v[0]), int(v[1])) for k, v in d.items()}
    except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
        return None


def _gh_secondary_wait(text: str, attempt: int) -> int:
    """Seconds to wait before retrying a SECONDARY-limited `gh` call: honor a Retry-After if gh echoed
    one, else exponential from GH_SECONDARY_BASE. >= 1s, clamped to the in-round budget."""
    ra = _parse_retry_after(_gh_retry_after(text))
    nap = int(ra) if ra is not None else GH_SECONDARY_BASE * (1 << min(attempt, 4))
    return max(1, min(nap, GH_INROUND_WAIT))


def _gh_retry_after(text: str) -> str | None:
    """gh occasionally echoes a `Retry-After: N` line for a secondary limit. Pull the value if present."""
    m = re.search(r"retry[- ]after:?\s*(\d+)", text, re.I)
    return m.group(1) if m else None


def gh_run(argv: list[str], *, cwd: Path | None = None, max_wait: int = GH_INROUND_WAIT) -> subprocess.CompletedProcess:
    """Run a `gh` command, waiting out a SECONDARY GitHub rate limit IN PLACE and retrying so the limit
    costs a pause, not a discarded round (bounded by max_wait so it can't blow ROUND_TIMEOUT). A PRIMARY
    (hourly) limit is surfaced immediately — waiting an hour inside a round under the 90-min cap would
    just be SIGKILLed; the loop preflight waits that reset out instead. Any non-rate-limit failure is
    returned unchanged for the caller to handle as before."""
    waited = 0
    attempt = 0
    while True:
        p = run(argv, cwd=cwd)
        if p.returncode == 0:
            return p
        text = (p.stderr or "") + "\n" + (p.stdout or "")
        kind = _gh_rate_kind(text)
        if kind is None:
            return p
        if kind == "primary":
            log(
                "gh: primary rate limit — surfacing so the round backs off and the loop preflight "
                "waits out the hourly reset (waiting in-round would exceed the round timeout)"
            )
            return p
        nap = _gh_secondary_wait(text, attempt)
        if waited + nap > max_wait:
            log(
                f"gh: secondary rate limit, but the in-round wait budget is spent ({waited}s) — "
                f"surfacing the error so the round backs off"
            )
            return p
        log(f"gh: secondary rate limit — waiting {nap}s for it to clear, then retrying ({' '.join(argv[1:3])})")
        time.sleep(nap)
        waited += nap
        attempt += 1


# ============================================================================
# GitHub — every gh wrapper. JSON via `gh ... --json` + stdlib json (no jq).
# ============================================================================


class GitHub:
    def __init__(self, repo: str = TAUCETI):
        self.repo = repo

    def _gh(self, args: list[str]) -> subprocess.CompletedProcess:
        return gh_run(["gh", *args])

    def pr_list(self, fields: list[str], *, author: str | None = None, state: str = "open") -> list[dict]:
        args = ["pr", "list", "--repo", self.repo, "--state", state, "--limit", "200", "--json", ",".join(fields)]
        if author:
            args += ["--author", author]
        p = self._gh(args)
        if p.returncode != 0:
            raise GitHubError(f"gh pr list failed: {p.stderr.strip()}")
        return json.loads(p.stdout or "[]")

    def issue_list(self, repo: str, *, labels: list[str] | None = None, fields: list[str], state: str = "open", limit: int = 200) -> list[dict]:
        """List issues in `repo` (explicit, since the client is bound to its own repo), filtered by
        ALL of `labels` (repeated --label = AND; gh handles slashes in label names). Each dict has
        the requested `fields`. Raises GitHubError on failure."""
        args = ["issue", "list", "--repo", repo, "--state", state, "--limit", str(limit), "--json", ",".join(fields)]
        for label in labels or []:
            args += ["--label", label]
        p = self._gh(args)
        if p.returncode != 0:
            raise GitHubError(f"gh issue list failed: {p.stderr.strip()}")
        return json.loads(p.stdout or "[]")

    def pr_view(self, pr: int, fields: list[str]) -> dict | None:
        p = self._gh(["pr", "view", str(pr), "--repo", self.repo, "--json", ",".join(fields)])
        if p.returncode != 0:
            return None
        return json.loads(p.stdout or "{}")

    def ensure_stuck_issue(self, pr: int, reason: str) -> None:
        """Ensure a tracking issue exists for a PR the automation can't make progress on (a permanent
        record so a human notices). Deduped by an exact title: one open issue per stuck PR across the
        whole fleet. Best-effort — a failure to create one is non-fatal (the per-round warning still
        fires); never raises."""
        title = f"Review stuck: PR #{pr}"
        try:
            p = self._gh(
                [
                    "issue",
                    "list",
                    "--repo",
                    self.repo,
                    "--state",
                    "open",
                    "--search",
                    f'in:title "{title}"',
                    "--json",
                    "number,title",
                    "--jq",
                    f'[.[] | select(.title == "{title}")] | length',
                ]
            )
            if p.returncode == 0 and (p.stdout or "0").strip() not in ("", "0"):
                return  # already tracked
            body = (
                f"The autonomous worker cannot make progress on #{pr}: {reason}\n\n"
                f"It can be neither merged (not all-green) nor auto-retired (its review rounds "
                f"are not advancing), so it needs a human to look. The worker re-checks each round "
                f"and will close this issue's PR-side concern once #{pr} merges or is closed.\n\n"
                f"<!--tauceti-review-stuck:{pr}-->"
            )
            self._gh(["issue", "create", "--repo", self.repo, "--title", title, "--body", body])
        except Exception:
            pass

    def issue_comments(self, pr: int) -> list[dict] | None:
        """All issue comments for a PR (paginated). None on fetch failure (distinct from empty)."""
        p = self._gh(["api", "--paginate", f"/repos/{self.repo}/issues/{pr}/comments?per_page=100"])
        if p.returncode != 0:
            return None
        try:
            return json.loads(p.stdout or "[]")
        except json.JSONDecodeError:
            return None

    def review_comments(self, pr: int) -> list[dict] | None:
        """All review (inline / thread) comments for a PR — distinct from issue_comments. A contested
        fix replies on a review thread, so the progress guard must count these too. None on failure."""
        p = self._gh(["api", "--paginate", f"/repos/{self.repo}/pulls/{pr}/comments?per_page=100"])
        if p.returncode != 0:
            return None
        try:
            return json.loads(p.stdout or "[]")
        except json.JSONDecodeError:
            return None

    def pr_progress_state(self, pr: int) -> dict | None:
        """{'head': <headRefOid>, 'ncomments': <issue + review-thread comments>} in ONE GraphQL request,
        replacing a `pr view` plus two paginated REST comment fetches. The progress guard runs this twice
        per guarded round per PR, so collapsing ~5 REST calls into 1 GraphQL call is the bulk of the
        round's GitHub spend. None on any failure (caller treats that as 'can't tell'). Review threads are
        fetched first:100; on the rare PR with more, we fall back to the exact paginated REST count so the
        guard never undercounts a comment that landed on a later thread."""
        owner, _, name = self.repo.partition("/")
        q = (
            "query($owner:String!,$name:String!,$pr:Int!){repository(owner:$owner,name:$name){"
            "pullRequest(number:$pr){headRefOid comments{totalCount}"
            "reviewThreads(first:100){totalCount nodes{comments{totalCount}}}}}}"
        )
        p = self._gh(
            ["api", "graphql", "-f", f"query={q}", "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"pr={pr}"]
        )
        if p.returncode != 0:
            return None
        try:
            d = json.loads(p.stdout)["data"]["repository"]["pullRequest"]
            threads = d["reviewThreads"]
            if threads["totalCount"] > 100:  # beyond one page — get the exact count via REST
                return self._pr_progress_state_rest(pr, d["headRefOid"])
            nc = d["comments"]["totalCount"] + sum(t["comments"]["totalCount"] for t in threads["nodes"])
            return {"head": d["headRefOid"], "ncomments": nc}
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def _pr_progress_state_rest(self, pr: int, head: str) -> dict | None:
        """Exact issue + review comment count via the paginated REST endpoints (the pre-GraphQL path),
        for the rare PR with >100 review threads. None if either fetch fails."""
        cs = self.issue_comments(pr)
        rcs = self.review_comments(pr)
        if cs is None or rcs is None:
            return None
        return {"head": head, "ncomments": len(cs) + len(rcs)}

    def api_jq(self, path: str, jq: str) -> str | None:
        p = self._gh(["api", path, "--jq", jq])
        if p.returncode != 0:
            return None
        return p.stdout.strip()

    def reactions(self, comment_id: int) -> list[dict] | None:
        """Reactions on a pull-request review (thread) comment. Each carries {content, created_at
        (whole-second ISO 8601), user, id}. None on fetch failure (distinct from no reactions)."""
        p = self._gh(["api", "--paginate", f"/repos/{self.repo}/pulls/comments/{comment_id}/reactions?per_page=100"])
        if p.returncode != 0:
            return None
        try:
            return json.loads(p.stdout or "[]")
        except json.JSONDecodeError:
            return None

    def fresh_claim_age(self, comment_id: int, emoji: str = CONTEST_CLAIM_EMOJI) -> int | None:
        """Seconds since the newest `emoji` reaction on this comment, or None if there is none (or the
        fetch failed — fail OPEN: a transient API error must not let a stale claim block work forever,
        and the worst case of a missed claim is a rare double-review, which we've accepted)."""
        rs = self.reactions(comment_id)
        if not rs:
            return None
        newest = 0
        for r in rs:
            if r.get("content") != emoji:
                continue
            ts = _parse_iso8601(r.get("created_at"))
            if ts is not None and ts > newest:
                newest = ts
        if not newest:
            return None
        return max(0, int(time.time()) - newest)

    def add_reaction(self, comment_id: int, emoji: str = CONTEST_CLAIM_EMOJI) -> bool:
        """Add `emoji` to a review comment (idempotent per (login, content)). False on failure — a
        claim we couldn't post just means a peer may double up, which is acceptable."""
        p = self._gh(
            ["api", "-X", "POST", f"/repos/{self.repo}/pulls/comments/{comment_id}/reactions", "-f", f"content={emoji}"]
        )
        return p.returncode == 0

    def remove_reaction(self, comment_id: int, emoji: str = CONTEST_CLAIM_EMOJI) -> bool:
        """Remove our own `emoji` reaction from a review comment (releases the claim). Looks up the
        reaction id for THIS login, then deletes it; a no-op (True) if we hold none."""
        rs = self.reactions(comment_id)
        if rs is None:
            return False
        mine = me()
        rid = next(
            (r["id"] for r in rs if r.get("content") == emoji and (r.get("user") or {}).get("login") == mine), None
        )
        if rid is None:
            return True
        p = self._gh(["api", "-X", "DELETE", f"/repos/{self.repo}/pulls/comments/{comment_id}/reactions/{rid}"])
        return p.returncode == 0


def _parse_retry_after(raw: str | None) -> float | None:
    """A `Retry-After` header is either delta-seconds or an HTTP-date; return seconds-from-now, clamped to
    [0, 3600] (the server can send bogus/huge/negative values). Returns None when absent/unparseable."""
    if not raw:
        return None
    raw = raw.strip()
    secs: float | None = None
    try:
        secs = float(raw)
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime

            secs = parsedate_to_datetime(raw).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return None
    if secs is None:
        return None
    return max(0.0, min(3600.0, secs))
