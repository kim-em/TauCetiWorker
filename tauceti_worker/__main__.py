"""`python -m tauceti_worker …` — the child-process entry used by self_argv() for spawned
rounds and heartbeats, and a convenient equivalent of the `tauceti` console script."""

from __future__ import annotations

from .cli import cli_main

if __name__ == "__main__":
    raise SystemExit(cli_main())
