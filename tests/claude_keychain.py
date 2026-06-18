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
finally:
    tc.subprocess.run, tc.sys.platform = orig_run, orig_platform
    if orig_user is None:
        os.environ.pop("USER", None)
    else:
        os.environ["USER"] = orig_user

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
