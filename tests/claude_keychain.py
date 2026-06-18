#!/usr/bin/env python3
"""macOS Keychain fallback for the pacer: when <config>/.credentials.json is absent on darwin, read
Claude Code's OAuth blob from the login Keychain (read-only), mark it from_keychain so the pacer never
refreshes it, and treat a locked Keychain (errSecInteractionNotAllowed=36) as 'unavailable'."""
import importlib.machinery, importlib.util, json, os, sys, tempfile, types
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

OAUTH = {"claudeAiOauth": {"accessToken": "KC", "refreshToken": "R", "expiresAt": 1}}

class FakeRun:
    """Stand-in for subprocess.run over `security find-generic-password`. Records the commands it saw and
    replays a scripted (returncode, stdout) per call, so we can assert the -a→service-only fallback."""
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

    # 3. A locked Keychain (36) is treated as 'no creds', not an error.
    tc.subprocess.run = FakeRun([(36, "")])
    check("locked keychain → None", tc._claude_keychain_creds(), None)

    # 4. Not found at all → None.
    tc.subprocess.run = FakeRun([(44, ""), (44, "")])
    check("item absent → None", tc._claude_keychain_creds(), None)

    # 5. The pacer's _claude_creds falls back to the Keychain and marks it from_keychain (so the
    #    401 path never refreshes it).
    tmp = Path(tempfile.mkdtemp())   # an empty home: no .credentials.json on disk
    cfg = types.SimpleNamespace(home=tmp, quota_cache=tmp)
    q = tc.Quota(cfg)
    tc.subprocess.run = FakeRun([(0, json.dumps(OAUTH))])
    oauth, from_kc = q._claude_creds()
    check("pacer falls back to keychain creds", oauth, OAUTH["claudeAiOauth"])
    check("keychain creds are marked from_keychain", from_kc, True)

    # 6. A real on-disk file wins and is NOT from_keychain (no security call at all).
    (tmp / ".claude").mkdir()
    (tmp / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "FILE"}}))
    fr = FakeRun([(0, json.dumps(OAUTH))]); tc.subprocess.run = fr
    oauth, from_kc = q._claude_creds()
    check("file creds win over keychain", oauth, {"accessToken": "FILE"})
    check("file creds are not from_keychain", from_kc, False)
    check("file present → no security call", fr.calls, [])

    # 7. A file present but missing claudeAiOauth (partial/legacy) must NOT shadow the Keychain.
    (tmp / ".claude" / ".credentials.json").write_text(json.dumps({"other": 1}))
    tc.subprocess.run = FakeRun([(0, json.dumps(OAUTH))])
    oauth, from_kc = q._claude_creds()
    check("file without claudeAiOauth falls back to keychain", oauth, OAUTH["claudeAiOauth"])
    check("fallback creds are from_keychain", from_kc, True)

    # 8. Interactive read (for bubble seeding): a locked Keychain runs `security unlock-keychain`, then
    #    the retry succeeds. (-a→36, service-only→36, unlock, -a→blob.)
    fr = FakeRun([(36, ""), (36, ""), (0, ""), (0, json.dumps(OAUTH))]); tc.subprocess.run = fr
    check("interactive read unlocks then reads", tc._claude_keychain_creds_interactive(), OAUTH)
    check("interactive read runs unlock-keychain", fr.calls[2], ["security", "unlock-keychain"])

    # 9. Bubble seeding on macOS with no on-disk file: materialize the Keychain blob into a private
    #    .credentials.json and point this run's $CLAUDE_CONFIG_DIR at it.
    seed_home, seed_state = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())   # empty home: no file
    cfg2 = types.SimpleNamespace(home=seed_home, state=seed_state)
    env = {}
    tc.subprocess.run = FakeRun([(0, json.dumps(OAUTH))])
    staged = tc._seed_claude_creds_for_bubble(cfg2, env)
    check("seed returns the staged dir", staged, seed_state / "bubble-claude-creds")
    check("staged file carries the keychain blob", json.loads((staged / ".credentials.json").read_text()), OAUTH)
    check("seed points $CLAUDE_CONFIG_DIR at it", env.get("CLAUDE_CONFIG_DIR"), str(staged))
    check("staged creds file is 0600", oct(os.stat(staged / ".credentials.json").st_mode & 0o777), "0o600")

    # 10. A real on-disk file is used as-is — no Keychain read, env untouched.
    (seed_home / ".claude").mkdir()
    (seed_home / ".claude" / ".credentials.json").write_text(json.dumps(OAUTH))
    fr = FakeRun([(0, json.dumps(OAUTH))]); tc.subprocess.run = fr; env2 = {}
    check("seed is a no-op when the file exists", tc._seed_claude_creds_for_bubble(cfg2, env2), None)
    check("seed makes no security call when file exists", fr.calls, [])
    check("seed leaves env untouched when file exists", "CLAUDE_CONFIG_DIR" in env2, False)

    # 10b. A file present but without claudeAiOauth (partial/legacy) falls through to the Keychain.
    (seed_home / ".claude" / ".credentials.json").write_text(json.dumps({"other": 1}))
    tc.subprocess.run = FakeRun([(0, json.dumps(OAUTH))]); env4 = {}
    staged2 = tc._seed_claude_creds_for_bubble(cfg2, env4)
    check("malformed file falls through to keychain", staged2 is not None, True)
    check("fallthrough seeds the keychain blob", json.loads((staged2 / ".credentials.json").read_text()), OAUTH)

    # 11. Off darwin with no file: a no-op (bubble seeds nothing, as before) — never reads a Keychain.
    tc.sys.platform = "linux"
    fr = FakeRun([(0, json.dumps(OAUTH))]); tc.subprocess.run = fr
    cfg3 = types.SimpleNamespace(home=Path(tempfile.mkdtemp()), state=Path(tempfile.mkdtemp()))
    check("seed is a no-op off darwin", tc._seed_claude_creds_for_bubble(cfg3, {}), None)
    check("seed makes no security call off darwin", fr.calls, [])
    tc.sys.platform = "darwin"

    # 12. macOS, no file, and the Keychain has nothing → a clear Die rather than a silent empty seed.
    cfg4 = types.SimpleNamespace(home=Path(tempfile.mkdtemp()), state=Path(tempfile.mkdtemp()))
    tc.subprocess.run = FakeRun([(44, ""), (44, "")])
    raised = False
    try:
        tc._seed_claude_creds_for_bubble(cfg4, {})
    except tc.Die:
        raised = True
    check("seed raises Die when no creds anywhere", raised, True)
finally:
    tc.subprocess.run, tc.sys.platform = orig_run, orig_platform
    if orig_user is None:
        os.environ.pop("USER", None)
    else:
        os.environ["USER"] = orig_user

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
