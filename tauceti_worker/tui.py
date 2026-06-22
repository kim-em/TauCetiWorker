"""tauceti_worker.tui — split from the monolithic worker (behaviour-preserving)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .agents import _shq
from .config import Config, _only_label, _skip_label, roadmap_areas, roadmap_only, roadmap_skip
from .constants import ALLOWED_TASKS, KIND_BY_NAME, KIND_KEYS, MAX_OPEN_PRS, TAUCETI
from .github import GitHub
from .paths import entry_cmd
from .quota import Quota, _read_json_file, quota_line
from .review_state import ReviewState
from .survey import Candidate, Counters, Survey, survey

# --- command stubs (filled in by later milestones) ---------------------------


def _sample(cands: list[Candidate], n: int = 3) -> str:
    nums = [f"#{c.pr}" for c in cands]
    head = " ".join(nums[:n])
    return head + (f" +{len(nums) - n}" if len(nums) > n else "")


def render_survey(
    sv: Survey,
    console,
    quota_snap: dict | None = None,
    selected: str | None = None,
    expanded: set | None = None,
    areas: list | None = None,
) -> None:
    """Render the survey table. `selected` (a kind name) is highlighted as the arrow-key cursor;
    `expanded` (a set of kind names) get their candidate PRs listed beneath them, one per row, with
    the PR title — and an expanded `roadmap` lists the `areas` instead. Pass None for all
    (e.g. from `tauceti status`) for the plain table."""
    from rich.markup import escape  # PR titles / areas / errors are external text — escape before markup
    from rich.panel import Panel
    from rich.table import Table

    if sv.github_failed:
        console.print(
            Panel(
                "[bold red]GitHub fetch failed[/] — survey unavailable.\n" + escape("\n".join(sv.errors)),
                title="tauceti",
            )
        )
        return

    expanded = expanded or set()
    titles = {p.number: p.title for p in sv.open_prs}
    only = escape(sv.roadmap_only)
    skip = set(sv.roadmap_skip)
    skip_note = f"  (skip: {escape(', '.join(sv.roadmap_skip))})" if sv.roadmap_skip else ""

    header = (
        f"[bold]{TAUCETI}[/]   worker: {sv.worker_id}   "
        f"open: {sv.n_open_nondraft} non-draft, {sv.n_reviewable} build-green"
    )
    if quota_snap is not None:
        header += "\nquota: " + quota_line(quota_snap)
    console.print(Panel(header, title="tauceti"))

    t = Table(show_header=True, header_style="bold")
    # The "#" is the key you press in the TUI to run one round of that kind (matches KIND_KEYS).
    t.add_column("#", justify="right")
    t.add_column("KIND")
    t.add_column("READY", justify="right")
    t.add_column("SAMPLE / NOTE")
    for name in ALLOWED_TASKS:
        num = KIND_BY_NAME[name]
        is_sel = name == selected
        style = "reverse" if is_sel else None  # the arrow-key cursor
        kind_cell = ("▸ " if is_sel else "  ") + name  # marker survives even without color
        if name == "roadmap":
            if sv.roadmap_backpressure:
                t.add_row(
                    num,
                    kind_cell,
                    "⛔",
                    f"backpressure: {sv.n_mine_open}/{MAX_OPEN_PRS} open  (only: {only}){skip_note}",
                    style=style,
                )
            else:
                t.add_row(num, kind_cell, "∞", f"only: {only}{skip_note}", style=style)
            if name in expanded:  # list the areas, marking the active one and any skipped ones
                if not areas:
                    t.add_row("", "    └ (areas unavailable)", "", "", style="dim")
                else:
                    # In "auto" mode no row matches (no area is pinned — a random one is picked per
                    # round), so no "← only" marker shows; that is the intended display.
                    for a in ["(all areas)"] + areas:
                        active = a == sv.roadmap_only or (a == "(all areas)" and sv.roadmap_only in ("", "any"))
                        tag = "[dim]← only[/]" if active else ("[dim](skipped)[/]" if a in skip else "")
                        t.add_row("", f"    └ {escape(a)}", "", tag)
            continue
        wk = sv.kind(name)
        ready = str(wk.count) if wk.count else "—"
        extra = f"  (+{len(wk.suppressed)} past-budget)" if wk.suppressed else ""
        if name == "review" and sv.review_capped:
            extra += (
                f"  ({len(sv.review_capped)} daily-capped: {','.join('#' + str(p) for p, _ in sv.review_capped[:5])})"
            )
        note = (_sample(wk.actionable) or "") + extra
        if name == "bump" and not wk.actionable and not wk.suppressed:
            note = "no broken bump-mathlib PR"
        t.add_row(num, kind_cell, ready, note, style=style)
        if name in expanded:  # tree-style PR list under the expanded kind
            rows = [(c, False) for c in wk.actionable] + [(c, True) for c in wk.suppressed]
            if not rows:
                t.add_row("", "    └ (none)", "", "", style="dim")
            for c, supp in rows:
                title = titles.get(c.pr) or c.reason or ""
                tag = " [dim](past-budget)[/]" if supp else ""
                t.add_row("", f"    └ #{c.pr}", "", f"[dim]{escape(title)}[/]" + tag)
    console.print(t)

    nxt = sv.next_auto_stage
    console.print(f"\nNext auto round would: [bold]{nxt.upper() if nxt else 'NOTHING (no eligible work)'}[/]")


def launch_cmd(
    only: str | None,
    model_dial: str,
    host: bool,
    loop: bool,
    roadmap_only: str | None = None,
    roadmap_skip: list[str] | None = None,
) -> list[str]:
    """Build the exact `tauceti work` command a TUI action runs/spawns (also shown via 'copy command').
    `roadmap_only`/`roadmap_skip` (the current roadmap dials) are embedded as --roadmap-only/--roadmap-skip
    so the copied/logged command reproduces what [o]/[x] set instead of silently reverting to the shell
    default."""
    cmd = entry_cmd() + ["work"]
    if loop:
        cmd.append("--loop")
    if only:
        cmd += ["--only", only]
    if model_dial != "auto":
        cmd += ["--agent", model_dial]
    if host:
        cmd.append("--host")
    if roadmap_only is not None:
        cmd += ["--roadmap-only", roadmap_only]
    if roadmap_skip:
        cmd += ["--roadmap-skip", ",".join(roadmap_skip)]
    return cmd


def _prefs_path(cfg: Config) -> Path:
    """Where the dashboard remembers its dials. A stable per-user config file (honoring
    $XDG_CONFIG_HOME, default ~/.config/tauceti/), NOT the per-worker state dir: `state` lives under
    HERE — i.e. inside the installed tree when `uv tool install`ed — which a `uv tool upgrade` can
    discard. These are operator preferences that should outlive the install, and be shared whether you
    run `./tauceti` from a clone or the installed `tauceti`."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else (cfg.home / ".config")
    return root / "tauceti" / "dashboard.json"


def load_dashboard_prefs(cfg: Config) -> dict:
    """The dashboard's last-used dials (model, sandbox, roadmap only/skip), so the picker reopens where
    the operator left it."""
    return _read_json_file(_prefs_path(cfg)) or {}


def save_dashboard_prefs(cfg: Config, prefs: dict) -> None:
    p = _prefs_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(prefs, indent=2))


def _dashboard_app(cfg, loader=None):
    """Build the Textual dashboard App. Split from cmd_tui so a headless test can drive it with
    App.run_test(); `loader` (a callable returning (survey, quota_snap, areas)) defaults to a live
    GitHub fetch but is stubbed in tests. ALL Textual imports live here so every non-TUI path stays
    stdlib + rich only — the same lazy-import discipline `rich` already follows."""
    from rich.panel import Panel
    from rich.text import Text
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.screen import ModalScreen
    from textual.widgets import DataTable, Footer, Input, Label, OptionList, Static
    from textual.widgets.option_list import Option

    MODELS = ["auto", "codex", "claude", "deepseek", "minimax"]

    class WorkGrid(DataTable):
        # Never take focus: the App owns ↑/↓/←/→ (move + expand/collapse). If the table grabbed focus
        # on mount it would swallow the arrows as its own scroll, which is exactly the lag-era feel we
        # are replacing. Modals (OptionList / Input) are focusable, so their arrows still work there.
        can_focus = False

    def default_loader():
        gh = GitHub()
        sv = survey(cfg, gh, ReviewState(cfg, gh), Counters(cfg), deep=True)
        _, q = Quota(cfg).choose(None)
        areas = roadmap_areas(gh)  # [] on failure; the area picker falls back to free text
        return sv, q, areas

    load = loader or default_loader

    # Restore the operator's last-used dials. An explicit TAUCETI_ROADMAP_ONLY/SKIP in the environment
    # (exported, or passed by a parent) always wins over the saved one; otherwise apply the saved value
    # to the env now so BOTH the survey display and any round launched from here inherit it. This is the
    # ONLY place a saved value is read — it is dashboard-scoped: a bare `tauceti work` never loads prefs
    # (cmd_work resolves these from the env + the live area list only), so the saved pref cannot leak
    # into a CLI run; rounds launched from here carry it via explicit --roadmap-only/--roadmap-skip flags.
    prefs = load_dashboard_prefs(cfg)
    if "TAUCETI_ROADMAP_ONLY" not in os.environ and isinstance(prefs.get("roadmap_only"), str):
        os.environ["TAUCETI_ROADMAP_ONLY"] = prefs["roadmap_only"]
    if "TAUCETI_ROADMAP_SKIP" not in os.environ and isinstance(prefs.get("roadmap_skip"), str):
        os.environ["TAUCETI_ROADMAP_SKIP"] = prefs["roadmap_skip"]

    class OnlyPicker(ModalScreen):
        """Arrow-key picker over the roadmap areas — the modal cousin of the dashboard cursor."""

        BINDINGS = [Binding("escape", "cancel", "cancel"), Binding("q", "cancel", "cancel")]

        def __init__(self, options, current):
            super().__init__()
            self._options = options
            self._current = current

        def compose(self) -> ComposeResult:
            ol = OptionList(*[Option(o) for o in self._options])
            yield Vertical(
                Label("roadmap only (single area)", classes="title"),
                ol,
                Label("↑/↓ move · Enter select · Esc cancel", classes="hint"),
                id="dialog",
            )

        def on_mount(self) -> None:
            ol = self.query_one(OptionList)
            if self._current in self._options:
                ol.highlighted = self._options.index(self._current)
            ol.focus()

        def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
            self.dismiss(self._options[event.option_index])

        def action_cancel(self) -> None:
            self.dismiss(None)

    class TextPrompt(ModalScreen):
        """Free-text input for the roadmap dials (the single-area fallback when the area list can't be
        fetched, and the comma-separated skip list)."""

        BINDINGS = [Binding("escape", "cancel", "cancel")]

        def __init__(self, prompt, current):
            super().__init__()
            self._prompt = prompt
            self._current = current or ""

        def compose(self) -> ComposeResult:
            yield Vertical(
                Label(self._prompt, classes="title"),
                Input(value=self._current),
                Label("Enter accept · Esc cancel", classes="hint"),
                id="dialog",
            )

        def on_mount(self) -> None:
            self.query_one(Input).focus()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            self.dismiss(event.value)

        def action_cancel(self) -> None:
            self.dismiss(None)

    class CommandScreen(ModalScreen):
        """Show the exact `tauceti work` commands the current dials would run; the one-round form is
        also placed on the clipboard so it can be pasted into another shell."""

        BINDINGS = [
            Binding("escape", "close", "close"),
            Binding("enter", "close", "close"),
            Binding("q", "close", "close"),
            Binding("space", "close", "close"),
        ]

        def __init__(self, one, loop, copied):
            super().__init__()
            self._one, self._loop, self._copied = one, loop, copied

        def compose(self) -> ComposeResult:
            body = Text()
            body.append("one round:\n", style="bold")
            body.append("  " + self._one + "\n\n")
            body.append("loop:\n", style="bold")
            body.append("  " + self._loop)
            if self._copied:
                body.append("\n\ncopied the one-round command to the clipboard", style="green")
            yield Vertical(Static(body), Label("press any key to close", classes="hint"), id="dialog")

        def on_key(self, event) -> None:
            self.dismiss(None)

        def action_close(self) -> None:
            self.dismiss(None)

    class Dashboard(App):
        CSS = """
        Screen { layers: base; }
        #hdr { height: auto; }
        #tbl { height: 1fr; }
        #status { height: auto; padding: 1 0 0 1; color: $text-muted; }
        ModalScreen { align: center middle; }
        #dialog {
            width: 80%; max-width: 110; height: auto; padding: 1 2;
            border: round $accent; background: $surface;
        }
        #dialog OptionList { height: auto; max-height: 18; border: none; }
        #dialog Input { margin: 1 0; }
        .title { text-style: bold; }
        .hint { color: $text-muted; padding-top: 1; }
        """
        BINDINGS = [
            Binding("up", "cursor_up", "move", show=False),
            Binding("down", "cursor_down", "move", show=False),
            Binding("k", "cursor_up", "up", show=False),
            Binding("j", "cursor_down", "down", show=False),
            Binding("right", "expand", "expand"),
            Binding("left", "collapse", "collapse", show=False),
            Binding("enter", "run_selected", "run one"),
            Binding("l", "loop_auto", "loop"),
            Binding("L", "loop_selected", "loop sel", show=False),
            Binding("m", "cycle_model", "model"),
            Binding("s", "toggle_sandbox", "sandbox"),
            Binding("o", "set_only", "only"),
            Binding("x", "set_skip", "skip"),
            Binding("r", "refresh", "refresh"),
            Binding("c", "copy_cmd", "copy"),
            Binding("q", "quit", "quit"),
        ] + [Binding(k, f"run_kind('{k}')", f"#{k}", show=False) for k in KIND_KEYS]

        TITLE = "tauceti"

        def __init__(self):
            super().__init__()
            self.cfg = cfg
            self.model_dial = prefs["model"] if prefs.get("model") in MODELS else "auto"
            self.host = bool(prefs.get("host", False))
            self.sel = 0
            self.expanded = set()
            self.areas = None
            self.sv = None
            self.quota = None
            self.err = None
            self.loading = True
            self._sel_init = False
            self._hdr_row = {}
            self._load_seq = 0  # guards against a slow refresh landing after a newer one
            self._saved_only = prefs.get("roadmap_only") if isinstance(prefs.get("roadmap_only"), str) else None
            self._only_set_by_user = False  # only an area chosen via [o] is persisted (not an env override)
            self._saved_skip = prefs.get("roadmap_skip") if isinstance(prefs.get("roadmap_skip"), str) else None
            self._skip_set_by_user = False  # only a skip set via [x] is persisted (not an env override)
            self.followup = None  # ("exec", cmd) read by cmd_tui once the alt-screen is gone

        def compose(self) -> ComposeResult:
            yield Static(id="hdr")
            yield WorkGrid(id="tbl", cursor_type="row")
            yield Static(id="status")
            yield Footer()

        def on_mount(self) -> None:
            t = self.query_one(DataTable)
            t.add_columns("#", "KIND", "READY", "SAMPLE / NOTE")
            self._render()
            self._refresh()  # first survey fetch (background thread)
            self.set_interval(90, self._refresh)  # keep it live without per-keypress refetches

        # ---- survey load: a background thread, because gh shells out (this was the old lag) ----------
        def _refresh(self) -> None:
            self._load_seq += 1  # stamp on the main thread, then hand the token to the worker
            self._load(self._load_seq)

        @work(thread=True, exclusive=True, group="survey")
        def _load(self, seq: int) -> None:
            try:
                sv, q, areas = load()
                self.call_from_thread(self._loaded, seq, sv, q, areas, None)
            except Exception as e:  # a fetch error must never tear the UI down
                self.call_from_thread(self._loaded, seq, None, None, None, repr(e))

        def _loaded(self, seq, sv, q, areas, err) -> None:
            if seq != self._load_seq:  # a newer refresh superseded this one — ignore the stale result
                return
            self.loading = False
            self.err = err
            if sv is not None:
                self.sv = sv
            if q is not None:
                self.quota = q
            if areas:
                self.areas = areas
            if self.sv is not None and not self._sel_init:  # default the cursor to what auto would run
                nxt = self.sv.next_auto_stage
                self.sel = ALLOWED_TASKS.index(nxt) if nxt in ALLOWED_TASKS else 0
                self._sel_init = True
            self._render()

        # ---- rendering: pure local state, so cursor moves never touch the network -------------------
        def _render(self) -> None:
            self._render_header()
            self._render_table()
            self._render_status()

        def _render_header(self) -> None:
            sv = self.sv
            if sv is None:
                msg = (
                    Text("loading…") if self.loading else Text("GitHub fetch failed — " + (self.err or ""), style="red")
                )
                self.query_one("#hdr", Static).update(Panel(msg, title="tauceti"))
                return
            head = Text()
            head.append(TAUCETI, style="bold")
            head.append(
                f"   worker: {sv.worker_id}   open: {sv.n_open_nondraft} non-draft, {sv.n_reviewable} build-green"
            )
            if self.quota is not None:
                head.append("\nquota: ")
                head.append_text(Text.from_markup(quota_line(self.quota)))
            if sv.github_failed:
                head.append("\nGitHub fetch failed — survey unavailable", style="red")
            self.query_one("#hdr", Static).update(Panel(head, title="tauceti"))

        def _render_table(self) -> None:
            t = self.query_one(DataTable)
            t.clear()
            self._hdr_row = {}
            sv = self.sv
            if sv is None or sv.github_failed:
                return
            titles = {p.number: p.title for p in sv.open_prs}
            sel_name = ALLOWED_TASKS[self.sel]
            for name in ALLOWED_TASKS:
                num = KIND_BY_NAME[name]
                is_sel = name == sel_name
                marker = "▸ " if is_sel else "  "

                def cell(s, sel=is_sel):
                    return Text(s, style="reverse bold" if sel else "")

                if name == "roadmap":
                    skip = set(sv.roadmap_skip)
                    skip_note = f"  (skip: {', '.join(sv.roadmap_skip)})" if sv.roadmap_skip else ""
                    if sv.roadmap_backpressure:
                        ready = "⛔"
                        note = (
                            f"backpressure: {sv.n_mine_open}/{MAX_OPEN_PRS} open  (only: {sv.roadmap_only}){skip_note}"
                        )
                    else:
                        ready = "∞"
                        note = f"only: {sv.roadmap_only}{skip_note}"
                    self._hdr_row[name] = t.row_count
                    t.add_row(cell(num), cell(marker + name), cell(ready), cell(note))
                    if name in self.expanded:
                        if not self.areas:
                            t.add_row(Text(""), Text("    └ (areas unavailable)", style="dim"), Text(""), Text(""))
                        else:
                            # "auto" mode pins no area (a random one is picked per round), so no row
                            # matches and no "← only" marker shows — the intended display.
                            for a in ["(all areas)"] + self.areas:
                                active = a == sv.roadmap_only or (a == "(all areas)" and sv.roadmap_only in ("", "any"))
                                tag = "← only" if active else ("(skipped)" if a in skip else "")
                                t.add_row(
                                    Text(""),
                                    Text("    └ " + a, style="dim"),
                                    Text(""),
                                    Text(tag, style="dim"),
                                )
                    continue
                wk = sv.kind(name)
                ready = str(wk.count) if wk.count else "—"
                extra = f"  (+{len(wk.suppressed)} past-budget)" if wk.suppressed else ""
                note = (_sample(wk.actionable) or "") + extra
                if name == "bump" and not wk.actionable and not wk.suppressed:
                    note = "no broken bump-mathlib PR"
                self._hdr_row[name] = t.row_count
                t.add_row(cell(num), cell(marker + name), cell(ready), cell(note))
                if name in self.expanded:
                    rows = [(c, False) for c in wk.actionable] + [(c, True) for c in wk.suppressed]
                    if not rows:
                        t.add_row(Text(""), Text("    └ (none)", style="dim"), Text(""), Text(""))
                    for c, supp in rows:
                        title = titles.get(c.pr) or c.reason or ""
                        tag = "  (past-budget)" if supp else ""
                        t.add_row(
                            Text(""), Text(f"    └ #{c.pr}", style="dim"), Text(""), Text(title + tag, style="dim")
                        )
            row = self._hdr_row.get(sel_name, 0)
            if t.row_count:
                t.move_cursor(row=row, animate=False)  # scroll the selected kind into view (no focus needed)

        def _render_status(self) -> None:
            sandbox = "host" if self.host else "bubble"
            nxt = self.sv.next_auto_stage if self.sv else None
            nxt_s = nxt.upper() if nxt else "NOTHING (no eligible work)"
            s = Text()
            s.append("model=", style="bold")
            s.append(self.model_dial)
            s.append("   sandbox=", style="bold")
            s.append(sandbox)
            s.append("   roadmap only=", style="bold")
            s.append(_only_label(self.sv))
            s.append("   skip=", style="bold")
            s.append(_skip_label(self.sv))
            s.append("\nnext auto round would: ", style="bold")
            s.append(nxt_s, style="bold")
            self.query_one("#status", Static).update(s)

        # ---- cursor + expansion (instant, local) ----------------------------------------------------
        def action_cursor_up(self) -> None:
            self._sel_init = True  # a deliberate move; don't let the first survey snap it back
            self.sel = (self.sel - 1) % len(ALLOWED_TASKS)
            self._render_table()

        def action_cursor_down(self) -> None:
            self._sel_init = True
            self.sel = (self.sel + 1) % len(ALLOWED_TASKS)
            self._render_table()

        def action_expand(self) -> None:
            name = ALLOWED_TASKS[self.sel]
            if name == "roadmap" and not self.areas:
                self._refresh()  # the area list arrives with the next survey
            self.expanded.add(name)
            self._render_table()

        def action_collapse(self) -> None:
            self.expanded.discard(ALLOWED_TASKS[self.sel])
            self._render_table()

        # ---- dials ----------------------------------------------------------------------------------
        def action_cycle_model(self) -> None:
            self.model_dial = MODELS[(MODELS.index(self.model_dial) + 1) % len(MODELS)]
            self._render_status()
            self._save_prefs()

        def action_toggle_sandbox(self) -> None:
            self.host = not self.host
            self._render_status()
            self._save_prefs()

        def _save_prefs(self) -> None:
            # Persist the roadmap dials only when the operator set them via [o]/[x]; a transient
            # TAUCETI_ROADMAP_ONLY/SKIP override must not become sticky across future runs. Otherwise
            # leave the saved values as-is.
            data = {"model": self.model_dial, "host": self.host}
            if self._only_set_by_user:
                self._saved_only = roadmap_only()
            if self._saved_only is not None:
                data["roadmap_only"] = self._saved_only
            if self._skip_set_by_user:
                self._saved_skip = ",".join(roadmap_skip())
            if self._saved_skip is not None:
                data["roadmap_skip"] = self._saved_skip
            save_dashboard_prefs(self.cfg, data)

        def action_refresh(self) -> None:
            self.notify("refreshing…", timeout=2)
            self._refresh()

        def _apply_only(self, value: str) -> None:
            os.environ["TAUCETI_ROADMAP_ONLY"] = value
            self._only_set_by_user = True
            if self.sv is not None:  # keep the roadmap row in step with the status bar immediately
                self.sv.roadmap_only = roadmap_only() or "any"
            self._save_prefs()
            self._render()

        def action_set_only(self) -> None:
            cur = roadmap_only() or ""  # None (auto) → "" so the picker/prompt starts at all-areas
            if self.areas:
                opts = ["(all areas)"] + self.areas

                def done(pick):
                    if pick is not None:
                        self._apply_only("" if pick == "(all areas)" else pick)

                self.push_screen(OnlyPicker(opts, cur if cur else "(all areas)"), done)
            else:

                def done(val):
                    if val is not None:
                        self._apply_only(val.strip())

                self.push_screen(TextPrompt("roadmap only — single area (blank = all areas)", cur), done)

        def _apply_skip(self, value: str) -> None:
            # Normalize through the same parse roadmap_skip() uses, so the env carries a canonical list.
            areas = sorted({tok for tok in (t.strip() for t in value.split(",")) if tok})
            os.environ["TAUCETI_ROADMAP_SKIP"] = ",".join(areas)
            self._skip_set_by_user = True
            if self.sv is not None:  # keep the roadmap row in step with the status bar immediately
                self.sv.roadmap_skip = roadmap_skip()
            self._save_prefs()
            self._render()

        def action_set_skip(self) -> None:
            cur = ",".join(roadmap_skip())

            def done(val):
                if val is not None:
                    self._apply_skip(val)

            self.push_screen(TextPrompt("roadmap skip — comma-separated areas (blank = none)", cur), done)

        # ---- launch ---------------------------------------------------------------------------------
        def action_copy_cmd(self) -> None:
            one = " ".join(
                _shq(a) for a in launch_cmd(None, self.model_dial, self.host, False, roadmap_only(), roadmap_skip())
            )
            loop = " ".join(
                _shq(a) for a in launch_cmd(None, self.model_dial, self.host, True, roadmap_only(), roadmap_skip())
            )
            copied = True
            try:
                self.copy_to_clipboard(one)
            except Exception:
                copied = False
            self.push_screen(CommandScreen(one, loop, copied))

        def _launch(self, *, loop: bool, only_selected: bool) -> None:
            only = ALLOWED_TASKS[self.sel] if only_selected else None
            cmd = launch_cmd(only, self.model_dial, self.host, loop, roadmap_only(), roadmap_skip())
            if loop:  # detached: the TUI is a launcher, not a supervisor
                pid, logf = _launch_detached(self.cfg, cmd)
                self.notify(f"pid {pid}  →  {logf}\nstop it:  kill {pid}", title="loop launched", timeout=10)
            else:  # foreground: hand the tty over by replacing this process
                self.followup = ("exec", cmd)
                self.exit()

        def action_run_selected(self) -> None:
            self._launch(loop=False, only_selected=True)

        def action_run_kind(self, key: str) -> None:
            self.sel = ALLOWED_TASKS.index(KIND_KEYS[key])
            self._launch(loop=False, only_selected=True)

        def action_loop_auto(self) -> None:
            self._launch(loop=True, only_selected=False)

        def action_loop_selected(self) -> None:
            self._launch(loop=True, only_selected=True)

    return Dashboard()


def cmd_tui(args) -> int:
    """Default view: a Textual dashboard of available work + a launcher. NOT a supervisor — launching a
    loop detaches it (coordination lives in GitHub, not here). Over a pipe / no TTY it prints a
    snapshot. Selecting a one-round action exits the dashboard and execs the round so it owns the tty."""
    from rich.console import Console

    console = Console()
    cfg = Config.resolve(None)

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        gh = GitHub()
        sv = survey(cfg, gh, ReviewState(cfg, gh), Counters(cfg), deep=True)
        _, q = Quota(cfg).choose(None)
        render_survey(sv, console, q)
        console.print("[dim](not a TTY — snapshot only; use `tauceti status [--json]` in scripts)[/]")
        return 0

    app = _dashboard_app(cfg)
    app.run()
    if app.followup and app.followup[0] == "exec":
        cmd = app.followup[1]
        console.print(f"\n  running: {' '.join(_shq(a) for a in cmd)}\n")
        os.execvp(cmd[0], cmd)  # replace this process; the round owns the tty
    return 0


def _launch_detached(cfg: Config, cmd: list[str]) -> tuple[int, Path]:
    """Spawn a loop in its own session, logging to a timestamped file; return (pid, logfile) so the
    caller can report how to watch and stop it. Detached because the TUI is a launcher, not a
    supervisor — the loop coordinates through GitHub, not this process."""
    cfg.logdir.mkdir(parents=True, exist_ok=True)
    logf = cfg.logdir / f"loop-{time.strftime('%Y%m%d-%H%M%S')}.log"
    f = open(logf, "ab")
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=f, stderr=f, start_new_session=True)
    return p.pid, logf
