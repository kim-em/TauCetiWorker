#!/usr/bin/env python3
"""A shell command that writes a file says so, above the truncated command text.

Both agents sometimes edit Lean through a shell form rather than a structured edit tool: a
`python3 - <<'EOF'` heredoc that rewrites a module, a `cat > file <<EOF`, a `sed -i`. The renderer
truncates a command at TOOL_INPUT_LIMIT, so for a long heredoc the target path is often the part
that gets cut, and the transcript records no file modification at all. Reading such a log cannot
answer "what did this round write" or "did it build AFTER its last edit".

Naming the paths costs one line and, being placed first, survives the truncation.

This is best effort by construction: it annotates a transcript and gates nothing, so the bar is that
it does not MISLEAD. A missed write costs a little audit detail; a spurious one is worse, so the
patterns exclude fd duplications, /dev sinks, and bare words that are not plausibly files.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker.transcript import TOOL_INPUT_LIMIT, _command_input, _written_paths

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


WRITES = [
    ("cat > /tmp/pr-body.md <<'EOF'\nbody\nEOF", ["/tmp/pr-body.md"]),
    ("echo note >> TauCeti/notes.md", ["TauCeti/notes.md"]),
    ("lake build 2>&1 | tee /tmp/build.log", ["/tmp/build.log"]),
    ("sed -i 's/a/b/' TauCeti/Baz.lean", ["TauCeti/Baz.lean"]),
    ("python3 - <<'EOF'\nopen('TauCeti/Algebra/Foo.lean','w').write(s)\nEOF", ["TauCeti/Algebra/Foo.lean"]),
    ('python3 - <<"EOF"\nPath("TauCeti/Bar.lean").write_text(s)\nEOF', ["TauCeti/Bar.lean"]),
]
# Commands that write nothing. A false positive here is the failure mode that matters.
READS = [
    "lake build 2>&1 | tail -5",
    "lake exe axioms 2>&1 | tail -20",
    "grep -rn foo TauCeti/ 2>/dev/null | head",
    "rg -n 'index_comp' .lake/packages/mathlib | head -20",
    "gh pr list --repo TauCetiProject/TauCeti --state open --limit 100 --json number,title",
    "git diff --stat && git status --short",
    'if [ -s /opt/round/kiro.key ]; then export KIRO_API_KEY="$(cat /opt/round/kiro.key)"; fi',
    "find TauCeti -name '*.lean' | sort",
]


def main():
    for cmd, want in WRITES:
        got = _written_paths(cmd)
        check(f"{want} from {cmd.splitlines()[0][:44]!r}", got == want)
    for cmd in READS:
        got = _written_paths(cmd)
        check(f"no write claimed for {cmd[:46]!r}", got == [])

    # The annotation leads, so truncation cannot eat it: a heredoc far larger than the limit still
    # reports its target.
    big = "python3 - <<'EOF'\nopen('TauCeti/Huge.lean','w').write('''" + "x" * (TOOL_INPUT_LIMIT * 3) + "''')\nEOF"
    rendered = _command_input(big)
    check("a truncated heredoc still names its target", rendered.startswith("writes:\n- TauCeti/Huge.lean\n"))
    check("the rendering stays bounded", len(rendered.encode()) <= TOOL_INPUT_LIMIT + 200)
    check("the command text is still shown", "python3 - <<" in rendered)

    # A read-only command renders exactly as before, with no header.
    plain = _command_input("lake build 2>&1 | tail -5")
    check("a read-only command is unchanged", plain == "lake build 2>&1 | tail -5")

    # apply_patch keeps its existing summary, body omitted, and gains no writes: header.
    patch = "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: TauCeti/New.lean\n+content\n*** End Patch\nEOF"
    out = _command_input(patch)
    check("apply_patch still summarises", out.startswith("apply_patch (body omitted)"))
    check("apply_patch still lists its paths", "*** Add File: TauCeti/New.lean" in out)
    check("apply_patch body is not reproduced", "+content" not in out)

    # Many writes in one command are capped.
    many = "; ".join(f"echo x > f{i}.lean" for i in range(50))
    check("the path list is capped", len(_written_paths(many)) <= 12)

    print("FAIL" if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
