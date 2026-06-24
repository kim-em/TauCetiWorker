#!/usr/bin/env python3
"""The quota cache must be keyed by the credentials it was fetched under.

When the operator rotates to a different account, the cached usage is for the WRONG account. Keying the
entry on a credential fingerprint makes a rotation read as a cache miss immediately, instead of serving
stale numbers until the (1-hour, for claude) TTL lapses, which used to leave workers sleeping on another
account's exhausted-looking quota. Dependency-free; no network.
"""

import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


# A Quota with only its cache_dir wired up — enough to exercise the raw cache layer offline.
q = tc.Quota.__new__(tc.Quota)
q.cache_dir = Path(tempfile.mkdtemp())

# Distinct accounts ⇒ distinct fingerprints; same token ⇒ same fingerprint; no creds ⇒ None.
fp_gmail = tc.Quota._fingerprint("gmail-token")
fp_qim = tc.Quota._fingerprint("qim-token")
check("different tokens fingerprint differently", fp_gmail != fp_qim, True)
check("same token is stable", tc.Quota._fingerprint("qim-token"), fp_qim)
check("no creds ⇒ None", tc.Quota._fingerprint(None), None)
check("empty ⇒ None", tc.Quota._fingerprint("", None), None)

# Store under the gmail account.
q._store_raw("claude", {"five_hour": {"utilization": 36.0}}, fp_gmail)

# Same account, within TTL ⇒ hit.
check("same fp within TTL ⇒ hit", q._cached_raw("claude", fp_gmail), {"five_hour": {"utilization": 36.0}})

# Rotated to qim ⇒ the gmail entry is for the wrong account ⇒ miss (the bug this fixes).
check("rotated fp ⇒ miss (re-fetch)", q._cached_raw("claude", fp_qim), None)

# A None fingerprint (creds vanished) never matches a stored entry ⇒ miss.
check("None fp ⇒ miss", q._cached_raw("claude", None), None)

# Expired entry ⇒ miss even with the matching fingerprint.
stale = {"fetched_at": int(time.time()) - tc.QUOTA_TTL["claude"] - 1, "fp": fp_gmail, "payload": {"x": 1}}
(q.cache_dir / "quota-claude.json").write_text(tc.json.dumps(stale))
check("matching fp but past TTL ⇒ miss", q._cached_raw("claude", fp_gmail), None)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
