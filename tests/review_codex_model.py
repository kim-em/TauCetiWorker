#!/usr/bin/env python3
"""The review engine takes its codex model from --codex-model (default gpt-5.6-sol, with a gpt-5.6-terra
fallback). The worker forwards an operator override from $TAUCETI_CODEX_MODEL — reusing the same env
_codex_model() reads for authoring, so one var pins both — but ONLY when codex is a reviewer and the
var is set, so the engine's seamless default/fallback stays in play otherwise. Verify the decision
helper and that the flag actually threads into the real review_in_bubble command."""

import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import agents  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(f"[{'OK ' if cond else 'XX '}] {name}")
    if not cond:
        fails += 1


# --- the pure decision helper -----------------------------------------------------------------------
f = agents._codex_review_model_override
os.environ.pop("TAUCETI_CODEX_MODEL", None)
check("unset -> None", f("codex") is None)
os.environ["TAUCETI_CODEX_MODEL"] = "gpt-5.6-terra"
check("set + codex -> value", f("codex") == "gpt-5.6-terra")
check("set + claude -> None (not a codex reviewer)", f("claude") is None)
check("set + 'claude,codex' -> value", f("claude,codex") == "gpt-5.6-terra")

# --- end-to-end: the flag threads into the real review_in_bubble inner command ----------------------
captured = {}


def fake_run_in_bubble(w, target, prompt, opts, mounts=None, inner_cmd=None, cred_model=None):
    captured["inner"] = inner_cmd
    captured["cred"] = cred_model
    return 0


agents.run_in_bubble = fake_run_in_bubble
agents.fetch_ref = lambda repo, d: True  # no network
agents.me = lambda: "tester"  # no gh call

tmp = Path(tempfile.mkdtemp())
os.environ["TAUCETI_REVIEW_ENGINE_DIR"] = str(tmp / "engine")  # skip the engine fetch
w = types.SimpleNamespace(cfg=types.SimpleNamespace(state=tmp / "state", store_dir=tmp / "store"))
opts = types.SimpleNamespace()

os.environ.pop("TAUCETI_CODEX_MODEL", None)
agents.review_in_bubble(w, 470, "abc123", "codex", opts)
check("bubble: unset -> no --codex-model, engine default stands", "--codex-model" not in captured["inner"])
check("bubble: codex reviewer still seeds codex creds", captured["cred"] == "codex")

os.environ["TAUCETI_CODEX_MODEL"] = "gpt-5.6-terra"
agents.review_in_bubble(w, 470, "abc123", "codex", opts)
check("bubble: set -> --codex-model gpt-5.6-terra forwarded", "--codex-model gpt-5.6-terra" in captured["inner"])

agents.review_in_bubble(w, 470, "abc123", "claude", opts)
check("bubble: claude reviewer -> no codex flag even when set", "--codex-model" not in captured["inner"])

os.environ.pop("TAUCETI_REVIEW_ENGINE_DIR", None)
os.environ.pop("TAUCETI_CODEX_MODEL", None)
print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
