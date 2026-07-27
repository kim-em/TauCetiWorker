#!/usr/bin/env python3
"""The fix-CI prompt diagnoses from CI logs with targeted commands before its one complete final gate."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROMPT = (REPO / "prompts" / "fix-ci.md").read_text()

fails = 0


def check(name, ok):
    global fails
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}")


final_heading = "## Final gate before pushing"
check("prompt has an explicit final-gate section", final_heading in PROMPT)
diagnosis, final_gate = PROMPT.split(final_heading, 1)
lake = '"${TAUCETI_LAKE:-lake}"'

# Logs must drive diagnosis before any local Lean work, and local iteration should stay targeted.
log_i = diagnosis.find("gh run view <run-id>")
module_i = diagnosis.find(f"{lake} build TauCeti.<Module>")
check("failed CI logs are inspected before targeted local compilation", 0 <= log_i < module_i)
check("diagnosis suggests a targeted module build", module_i >= 0)
check(
    "diagnosis suggests a targeted single-file Lean check", f"{lake} env lean TauCeti/Path/To/Module.lean" in diagnosis
)
check(
    "diagnosis names the individual audit/lint checks",
    all(
        cmd in diagnosis
        for cmd in (
            f"{lake} exe axioms",
            f"{lake} exe module-system",
            '"${TAUCETI_TRUSTED_RUN:-env}" bash scripts/lint-env.sh',
        )
    ),
)
check("diagnosis does not fetch caches or run the complete suite", f"{lake} exe cache get" not in diagnosis)
check("old front-loaded whole-suite instruction is gone", "run the WHOLE suite" not in diagnosis)

# The expensive complete suite belongs only to the final gate, in CI order.
commands = [
    f"{lake} exe cache get",
    f"{lake} build --iofail",
    f"{lake} exe axioms",
    f"{lake} exe module-system",
    '"${TAUCETI_TRUSTED_RUN:-env}" bash scripts/lint-env.sh',
]
positions = [final_gate.find(f"\n{cmd}\n") for cmd in commands]
check("final gate contains the complete CI command sequence", all(i >= 0 for i in positions))
check(
    "final gate keeps the CI commands in order",
    positions == sorted(positions) and len(set(positions)) == len(positions),
)
check("final gate uses CI's fail-on-info build mode", f"\n{lake} build\n" not in final_gate)
check(
    "every Lake command uses the host-safe absolute launcher",
    "TAUCETI_LAKE" in diagnosis and "TAUCETI_LAKE" in final_gate,
)
check("lint runs through the trusted-main launcher on host", "TAUCETI_TRUSTED_RUN" in final_gate)
check(
    "complete suite is explicitly reserved for the final gate",
    "complete CI suite only here" in final_gate and "Only after the targeted failure is fixed" in final_gate,
)

# An out-of-diff lint failure can be a real semantic effect of the PR, not automatic proof of staleness.
check(
    "lint guidance names every global-environment cause",
    all(term in diagnosis for term in ("`simp` lemma", "instance", "import", "attribute")),
)
check(
    "lint guidance explains current-main trusted configuration",
    "current main's trusted configuration" in diagnosis,
)
check("lint guidance no longer declares the branch likely stale", "likely behind main" not in PROMPT)

# Every authoring prompt must use the absolute host launcher. Bubble gets the same prompt and falls
# back to ordinary `lake`, so this also pins the shared-prompt compatibility contract.
for prompt_name in ("fix.md", "fix-ci.md", "bump.md", "rebase.md", "roadmap.md"):
    prompt = (REPO / "prompts" / prompt_name).read_text()
    command_lines = [
        line.strip() for line in prompt.splitlines() if line.strip().startswith(("lake ", '"${TAUCETI_LAKE'))
    ]
    check(f"{prompt_name} uses the host-safe Lake launcher", lake in prompt)
    check(
        f"{prompt_name} has no bare Lake command",
        all(line.startswith(lake) for line in command_lines),
    )

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
