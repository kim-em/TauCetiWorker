#!/usr/bin/env python3
"""Honor $CLAUDE_CONFIG_DIR: the pacer reads Claude creds from it, isolation copies from there and
repoints it at the per-worker copy, and the bubble path fails fast when it can't reach it."""
import importlib.machinery, importlib.util, json, os, shutil, sys, tempfile, types
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

home = Path("/home/example")
os.environ.pop("CLAUDE_CONFIG_DIR", None)
check("default falls back to <home>/.claude", tc.claude_dir(home), home / ".claude")
os.environ["CLAUDE_CONFIG_DIR"] = "/custom/work-claude"
check("env var wins", tc.claude_dir(home), Path("/custom/work-claude"))

# Isolation: copy creds from the operator's config dir, then repoint $CLAUDE_CONFIG_DIR at the copy so
# both the pacer (claude_dir) and the spawned claude read the isolated creds.
tmp = Path(tempfile.mkdtemp())
wid = f"claude-config-dir-test-{os.getpid()}"
shutil.rmtree(tc.HERE / "state" / wid, ignore_errors=True)   # a prior interrupted run mustn't taint us
try:
    real, cfgdir = tmp / "realhome", tmp / "work-claude"
    cfgdir.mkdir(parents=True)
    (cfgdir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "T"}}))
    (cfgdir / "CLAUDE.md").write_text("x")
    (real / ".codex").mkdir(parents=True)
    os.environ["HOME"] = str(real)
    os.environ["CLAUDE_CONFIG_DIR"] = str(cfgdir)
    iso_home = tc.isolate_home(wid)
    iso_claude = iso_home / ".claude"
    check("isolation copies creds from the config dir", (iso_claude / ".credentials.json").exists(), True)
    check("isolation symlinks the config surface", (iso_claude / "CLAUDE.md").is_symlink(), True)
    check("isolation repoints $CLAUDE_CONFIG_DIR at the copy", os.environ["CLAUDE_CONFIG_DIR"], str(iso_claude))
    check("claude_dir agrees with the spawned claude", tc.claude_dir(iso_home), iso_claude)
    creds = json.loads((tc.claude_dir(iso_home) / ".credentials.json").read_text())
    check("isolated creds are the operator's", creds["claudeAiOauth"]["accessToken"], "T")
    check("isolation records the source", (iso_claude / ".tauceti-creds-source").read_text().strip(), str(cfgdir))
finally:
    shutil.rmtree(tc.HERE / "state" / wid, ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)

# Bubble fail-fast: with a non-default $CLAUDE_CONFIG_DIR, run_in_bubble must Die before staging because
# bubble seeds Claude creds from <home>/.claude, not the config dir. (--isolate-home repoints the var TO
# <home>/.claude, so that case must pass the check — verified via the ensure_bubble_home sentinel below.)
sentinel = RuntimeError("reached ensure_bubble_home")
tc.ensure_bubble_home = lambda cfg: (_ for _ in ()).throw(sentinel)   # passing the check lands here
w = types.SimpleNamespace(cfg=types.SimpleNamespace(home=Path("/home/example"), state=Path("/tmp"), wid="w"))
opts = types.SimpleNamespace(work_model="claude")

os.environ["CLAUDE_CONFIG_DIR"] = "/custom/work-claude"
try:
    tc.run_in_bubble(w, "review", "PROMPT", opts)
    check("bubble + non-default config dir raises", "no raise", "tc.Die")
except tc.Die as e:
    check("bubble + non-default config dir raises Die", "/custom/work-claude" in str(e), True)
except RuntimeError:
    check("bubble + non-default config dir raises (got sentinel instead)", "sentinel", "tc.Die")

os.environ["CLAUDE_CONFIG_DIR"] = str(Path("/home/example") / ".claude")   # what --isolate-home sets
try:
    tc.run_in_bubble(w, "review", "PROMPT", opts)
    check("bubble + matching config dir passes the check", "no raise/sentinel", "sentinel")
except tc.Die:
    check("bubble + matching config dir must NOT Die", "raised Die", "sentinel")
except RuntimeError:
    check("bubble + matching config dir passes the check", True, True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
