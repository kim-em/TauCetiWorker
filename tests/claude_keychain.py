#!/usr/bin/env python3
"""macOS Keychain support. The pacer reads Claude Code's OAuth blob from the login Keychain (read-only,
keychain-first, marked from_keychain so the 401 path never refreshes a token shared with the operator's
claude). For bubble rounds, where the in-container claude needs a .credentials.json, the credential is
materialized from the Keychain INTO the configured CLAUDE_CONFIG_DIR when the file is missing/stale."""
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_loader("tauceti", importlib.machinery.SourceFileLoader("tauceti", str(REPO / "tauceti")))
tc = importlib.util.module_from_spec(spec); sys.modules["tauceti"] = tc; spec.loader.exec_module(tc)

fails = 0
def check(name, got, expect):
    global fails
    ok = got == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got}")
    if not ok:
        print(f"      expected: {expect}"); fails += 1

OAUTH = {"claudeAiOauth": {"accessToken": "KC", "refreshToken": "R", "expiresAt": 1}}            # expiresAt in the past
FRESH = {"claudeAiOauth": {"accessToken": "F", "refreshToken": "R", "expiresAt": 9999999999999}}  # far future

class FakeRun:
    """Stand-in for subprocess.run over `security`. Records the commands it saw and replays a scripted
    (returncode, stdout) per call, so we can assert the -a→service-only fallback and the unlock retry."""
    def __init__(self, results):
        self.results, self.calls, self.i = results, [], 0
    def __call__(self, cmd, *a, **k):
        self.calls.append(cmd)
        rc, out = self.results[min(self.i, len(self.results) - 1)]; self.i += 1
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")

orig_run, orig_platform, orig_user = tc.subprocess.run, sys.platform, os.environ.get("USER")
os.environ["USER"] = "alice"
os.environ.pop("CLAUDE_CONFIG_DIR", None)
tc.sys.platform = "darwin"
try:
    # 1. The plain `-a $USER -w` hit parses to the same dict as the file would.
    tc.subprocess.run = FakeRun([(0, json.dumps(OAUTH))])
    check("keychain read parses the OAuth blob", tc._claude_keychain_creds(), OAUTH)
    check("keychain read uses -s/-a/-w", tc.subprocess.run.calls[0],
          ["security", "find-generic-password", "-s", "Claude Code-credentials", "-a", "alice", "-w"])

    # 2. errSecItemNotFound (44) for the -a search falls back to the service-only search.
    fr = FakeRun([(44, ""), (0, json.dumps(OAUTH))]); tc.subprocess.run = fr
    check("falls back to service-only search", tc._claude_keychain_creds(), OAUTH)
    check("fallback drops -a", fr.calls[1], ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"])

    # 3. A locked Keychain (36) is treated as 'no creds', not an error (the pacer read is non-interactive).
    tc.subprocess.run = FakeRun([(36, "")])
    check("locked keychain → None", tc._claude_keychain_creds(), None)

    # 4. Not found at all → None.
    tc.subprocess.run = FakeRun([(44, ""), (44, "")])
    check("item absent → None", tc._claude_keychain_creds(), None)

    # 5. macOS reads the Keychain FIRST (authoritative) and marks creds from_keychain, so the 401 path
    #    never refreshes (rotating the shared token would log out the operator's claude).
    tmp = Path(tempfile.mkdtemp())
    cfg = types.SimpleNamespace(home=tmp, quota_cache=tmp)
    q = tc.Quota(cfg)
    tc.subprocess.run = FakeRun([(0, json.dumps(OAUTH))])
    oauth, from_kc = q._claude_creds()
    check("pacer reads keychain creds", oauth, OAUTH["claudeAiOauth"])
    check("keychain creds are from_keychain", from_kc, True)

    # 6. The Keychain wins over a .credentials.json on macOS (a file there is only a mirror / stale export),
    #    so a credentials file we materialize for the bubble never makes the pacer refresh a shared token.
    (tmp / ".claude").mkdir()
    (tmp / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "FILE"}}))
    tc.subprocess.run = FakeRun([(0, json.dumps(OAUTH))])
    oauth, from_kc = q._claude_creds()
    check("keychain wins over a file on macOS", oauth, OAUTH["claudeAiOauth"])
    check("keychain-win creds are from_keychain", from_kc, True)

    # 7. An empty/unreadable Keychain falls back to the file, but on macOS that file is still a Keychain
    #    mirror sharing the one refresh token, so it stays non-refreshable (from_keychain) too.
    tc.subprocess.run = FakeRun([(44, ""), (44, "")])
    oauth, from_kc = q._claude_creds()
    check("empty keychain falls back to the file", oauth, {"accessToken": "FILE"})
    check("macOS file fallback is non-refreshable too", from_kc, True)

    # 8. Interactive read (for bubble seeding): a locked Keychain runs `security unlock-keychain`, then
    #    the retry succeeds. (-a→36, service-only→36, unlock, -a→blob.)
    fr = FakeRun([(36, ""), (36, ""), (0, ""), (0, json.dumps(FRESH))]); tc.subprocess.run = fr
    check("interactive read unlocks then reads", tc._claude_keychain_creds_interactive(), FRESH)
    check("interactive read runs unlock-keychain", fr.calls[2], ["security", "unlock-keychain"])

    # 9. Bubble seeding on macOS with no file: materialize the Keychain blob INTO the configured dir, 0600.
    seed_home = Path(tempfile.mkdtemp())
    cfg2 = types.SimpleNamespace(home=seed_home)
    tc.subprocess.run = FakeRun([(0, json.dumps(FRESH))])
    tc._ensure_claude_creds_for_bubble(cfg2)
    target = seed_home / ".claude" / ".credentials.json"
    check("materializes the keychain blob into the config dir", json.loads(target.read_text()), FRESH)
    check("materialized file is 0600", oct(os.stat(target).st_mode & 0o777), "0o600")

    # 10. macOS always re-mirrors from the authoritative Keychain (a stale refresh token in an unexpired
    #     file would fail mid-round), so an existing file is overwritten with the current Keychain blob.
    target.write_text(json.dumps(OAUTH))   # a stale mirror
    tc.subprocess.run = FakeRun([(0, json.dumps(FRESH))])
    tc._ensure_claude_creds_for_bubble(cfg2)
    check("existing file is re-mirrored from the keychain", json.loads(target.read_text()), FRESH)

    # 11. Keychain unreadable (locked/absent) but a credentials file exists → keep it (don't Die).
    target.write_text(json.dumps(FRESH))
    tc.subprocess.run = FakeRun([(44, ""), (44, "")])
    tc._ensure_claude_creds_for_bubble(cfg2)
    check("keychain unreadable keeps the existing file", json.loads(target.read_text()), FRESH)

    # 12. Off darwin: a no-op (the file is the store there) — never reads a Keychain.
    tc.sys.platform = "linux"
    fr = FakeRun([(0, json.dumps(FRESH))]); tc.subprocess.run = fr
    tc._ensure_claude_creds_for_bubble(types.SimpleNamespace(home=Path(tempfile.mkdtemp())))
    check("ensure is a no-op off darwin", fr.calls, [])
    tc.sys.platform = "darwin"

    # 13. macOS, no file, and the Keychain has nothing → a clear Die rather than a silent empty seed.
    tc.subprocess.run = FakeRun([(44, ""), (44, "")])
    raised = False
    try:
        tc._ensure_claude_creds_for_bubble(types.SimpleNamespace(home=Path(tempfile.mkdtemp())))
    except tc.Die:
        raised = True
    check("ensure raises Die when no creds anywhere", raised, True)
finally:
    tc.subprocess.run, tc.sys.platform = orig_run, orig_platform
    if orig_user is None:
        os.environ.pop("USER", None)
    else:
        os.environ["USER"] = orig_user

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
