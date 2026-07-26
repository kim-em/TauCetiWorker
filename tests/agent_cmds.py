#!/usr/bin/env python3
"""M8: verify the host agent argv is byte-for-byte what round.sh's run_agent builds."""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

P = "DO THE WORK"
fails = 0


def check(name, argv, expect):
    global fails
    ok = argv == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {argv}")
    if not ok:
        print(f"      expected: {expect}")
        fails += 1


a, env = tc.host_agent_argv(P, "codex")
check("codex", a, ["codex", "exec", "--sandbox", "danger-full-access", "--skip-git-repo-check", P])

a, env = tc.host_agent_argv(P, "claude")
check("claude", a, ["claude", "-p", P, "--model", "opus", "--dangerously-skip-permissions"])
assert "ANTHROPIC_API_KEY" not in env, "claude env must drop ANTHROPIC_API_KEY (bills the Max plan)"
print("[OK ] claude env drops ANTHROPIC_API_KEY")

a, env = tc.host_agent_argv(P, "deepseek")
check("deepseek", a, [tc.PI_RUN, "openrouter", tc.OPENROUTER_MODELS["deepseek"], "--prompt", P])

# PATH must prepend HERE so the agent resolves git-safe-push / gh-safe-pr-create / claim.sh
assert env["PATH"].startswith(str(tc.HERE / "scripts") + ":"), "PATH must prepend the repo dir"
print("[OK ] PATH prepends repo dir for the safe-push/claim wrappers")

# Agent prompts are always passed in argv. In Bubble, an inherited terminal crosses SSH as a non-TTY
# stream; Codex then waits for more prompt text until EOF. Both output modes must close stdin.
saved_run = tc.agents.subprocess.run
saved_stream = os.environ.get("TAUCETI_STREAM")
calls = []
tc.agents.subprocess.run = lambda *a, **k: calls.append(k) or SimpleNamespace(returncode=0)
try:
    with tempfile.TemporaryDirectory() as td:
        os.environ["TAUCETI_STREAM"] = "1"
        tc.run_agent_proc(["agent"], env={}, logdir=Path(td), label="test")
        check("streamed agent stdin is closed", calls[-1].get("stdin"), tc.agents.subprocess.DEVNULL)
        os.environ.pop("TAUCETI_STREAM")
        tc.run_agent_proc(["agent"], env={}, logdir=Path(td), label="test")
        check("logged agent stdin is closed", calls[-1].get("stdin"), tc.agents.subprocess.DEVNULL)
finally:
    tc.agents.subprocess.run = saved_run
    if saved_stream is None:
        os.environ.pop("TAUCETI_STREAM", None)
    else:
        os.environ["TAUCETI_STREAM"] = saved_stream

# Per-worker HOME isolation must not hide the host Codex model: Bubble seeds credentials but deliberately
# excludes host config, so the worker has to pass the host's configured subscription-compatible model.
saved_host_home = tc.agents._host_home
saved_model = os.environ.pop("TAUCETI_CODEX_MODEL", None)
try:
    with tempfile.TemporaryDirectory() as td:
        host_home = Path(td)
        (host_home / ".codex").mkdir()
        (host_home / ".codex" / "config.toml").write_text('model = "gpt-5.6-sol"\n')
        tc.agents._host_home = lambda: host_home
        check("bubble Codex model comes from host home", tc.agents._codex_model(), "gpt-5.6-sol")
        (host_home / ".codex" / "config.toml").write_text("not valid [")
        check("bubble Codex model has a supported fallback", tc.agents._codex_model(), "gpt-5.6-terra")
        os.environ["TAUCETI_CODEX_MODEL"] = "operator-model"
        check("bubble Codex model operator override", tc.agents._codex_model(), "operator-model")
finally:
    tc.agents._host_home = saved_host_home
    if saved_model is None:
        os.environ.pop("TAUCETI_CODEX_MODEL", None)
    else:
        os.environ["TAUCETI_CODEX_MODEL"] = saved_model

# $TAUCETI_CLAUDE_CMD wraps/replaces the host claude executable; the standard flags are still appended,
# and an empty / whitespace-only value falls back to bare `claude` rather than a broken argv.
_saved = tc.agents.CLAUDE_CMD
tc.agents.CLAUDE_CMD = "my-wrapper --flag claude"
a, _ = tc.host_agent_argv(P, "claude")
check(
    "claude override",
    a,
    ["my-wrapper", "--flag", "claude", "-p", P, "--model", "opus", "--dangerously-skip-permissions"],
)
tc.agents.CLAUDE_CMD = "   "
a, _ = tc.host_agent_argv(P, "claude")
check("claude override blank falls back", a, ["claude", "-p", P, "--model", "opus", "--dangerously-skip-permissions"])
tc.agents.CLAUDE_CMD = _saved
print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)


def _m9():
    """M9: bubble inner commands + cred flags byte-for-byte vs round.sh."""
    fails = 0

    def eq(name, got, expect):
        nonlocal fails
        ok = got == expect
        print(f"[{'OK ' if ok else 'XX '}] {name}")
        if not ok:
            print(f"      got:      {got!r}\n      expected: {expect!r}")
            fails += 1

    eq(
        "inner codex",
        tc.agent_inner_cmd("codex"),
        'env OPENAI_API_KEY= ANTHROPIC_API_KEY= codex exec --sandbox danger-full-access --skip-git-repo-check "$(cat /opt/round/prompt.txt)"',
    )
    eq(
        "inner claude",
        tc.agent_inner_cmd("claude"),
        'env ANTHROPIC_API_KEY= OPENAI_API_KEY= CLAUDECODE= claude -p "$(cat /opt/round/prompt.txt)" --dangerously-skip-permissions --model opus',
    )
    eq(
        "inner deepseek",
        tc.agent_inner_cmd("deepseek"),
        'env ANTHROPIC_API_KEY= OPENAI_API_KEY= OPENROUTER_API_KEY="$(cat /opt/round/openrouter.key)" pi --provider openrouter --model %s --print "$(cat /opt/round/prompt.txt)"'
        % tc.OPENROUTER_MODELS["deepseek"],
    )
    eq(
        "creds codex",
        tc.agent_cred_flags("codex"),
        ["--codex-credentials", "--no-codex-config", "--no-claude-credentials", "--no-claude-config"],
    )
    eq(
        "creds claude",
        tc.agent_cred_flags("claude"),
        ["--claude-credentials", "--no-claude-config", "--no-codex-credentials", "--no-codex-config"],
    )
    eq(
        "creds deepseek",
        tc.agent_cred_flags("deepseek"),
        ["--no-claude-credentials", "--no-claude-config", "--no-codex-credentials", "--no-codex-config"],
    )
    return fails


if __name__ == "__main__":
    pass  # invoked below
