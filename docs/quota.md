# Quota and pacing

The README covers the pacing rule and the controls. This page covers where the
usage numbers come from and how the pacer behaves at the edges.

## Credential sources

The pacer reads the credential files the official CLIs already maintain,
`~/.claude/.credentials.json` and `~/.codex/auth.json`, and queries each
provider's usage endpoint. It honors `$CLAUDE_CONFIG_DIR`, so switching between
a personal and a work Claude account is paced correctly.

Reading quota never spends anything, with the one exception described under
"window bootstrap" below. `tauceti status`, the dashboard, and an auto selection
that lands on Codex make no model request at all.

## macOS and the login Keychain

On macOS, Claude Code keeps its credentials in the login Keychain rather than in
a file. The pacer reads them from the Keychain, read-only. It never refreshes
the Keychain, because that would log out your interactive `claude`. So on token
expiry it simply reports Claude unavailable for the cycle, and your next `claude`
run refreshes the Keychain so the pacer can read it again. One
`--ignore-quota --agent claude` round does the same.

A locked Keychain, which is what you get headless or over SSH, reports
unavailable with a hint to `security unlock-keychain` first.

Bubble uses the credential file directly where Claude stores credentials in a
file; on macOS it receives the Keychain credential through the private handoff
described in [the sandbox notes](sandbox.md).

## Reading Claude's two windows

Claude's session and weekly windows reset on separate clocks, so they are read
independently and neither is inferred from the other. Each window's raw state is
kept before any pacing is applied.

The structured `limits` array is authoritative for each window. Legacy flat keys
are used only when `limits` omits that window; they cannot override a structured
entry.

A missing window or invalid data, such as an unreadable reset timestamp or
non-numeric usage, stops the provider and reports the specific problem:

```
weekly limit missing from usage response
session reset timestamp invalid
```

rather than a generic "usage unknown". An unreadable constraint is not the same
as no constraint.

## The window bootstrap

There is one gap where the endpoint reports a window with no usage and no reset
clock: right after that window rolls. Only a Claude request can open the new
window, so `tauceti` makes one small `claude -p` turn to do it, drops the cached
usage, and re-reads. The fresh telemetry, not the request, then decides whether a
round runs.

The bootstrap runs only under these conditions:

- It happens at the launch stage of a round that has already found work, so a
  poll that finds nothing to run costs nothing.
- Every other window must be active with real headroom. A window that is at
  budget, over pace, exhausted, missing, or unreadable forbids the bootstrap.
- It respects your pace curve. Under a curve whose budget stays at 0 for the
  first stretch of a window, say `--pace 0:0,90:0,100:95`, a fresh window may not
  be opened at all, and the status says so (`pace budget stays 0% through 90% of
  the window`) rather than quietly opening one to manufacture a clock.
- It is claimed under a lock in a shared ledger beside your credentials *before*
  the request goes out, so every worker on that account, whatever its worker id,
  checkout, or isolated `$HOME`, makes at most one request per window period,
  even if one of them crashes mid-flight.

If the window still is not reporting afterwards, the status reads
`session bootstrap attempted; awaiting fresh usage` and the worker stays parked.

## Why "strictly under"

A provider is available while `used%` is strictly under the budget for the
elapsed fraction of the window. Strictly, because the request being decided
costs something: sitting exactly on the budget
(`session at budget (used 50% = 50% budget)`) is a pause, not a green light.

If usage cannot be read at all, the provider is treated as unavailable rather
than assumed free.
