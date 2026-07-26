"""tauceti_worker.agents — prompt filling, the host/bubble checkout, and the agent launch: the host
argv path and the repo-scoped bubble sandbox path (plus per-worker $HOME isolation)."""

from __future__ import annotations

import functools
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotations only; importing at runtime would invert the layer order
    from .work_units import RoundOpts, Worker

from .config import Config, Die, log
from .constants import CLAUDE_CMD, OPENROUTER_MODELS, PI_RUN, REVIEW, REVIEW_DAILY_CAP, ROADMAP, TAUCETI
from .github import me
from .paths import HERE
from .quota import (
    _claude_keychain_creds_interactive,
    _read_json_file,
    _safe_exists,
    _write_json_atomic,
    claude_dir,
    mirror_creds,
)

# ============================================================================
# Agents — prompt filling, the host checkout, and the agent launch. The host argv lists are a frozen
# contract — keep them byte-for-byte stable (the historical `( cd … ) 9>&-` and `env -u` shell
# mechanics map to cwd=, close_fds=True, and a pruned env). claim.sh / git-safe-push /
# gh-safe-pr-create are put on the agent's PATH so it can push.
# ============================================================================


def fill_prompt(path: Path, **subs) -> str:
    out = Path(path).read_text()
    for k, v in subs.items():
        out = out.replace(f"__{k}__", str(v))
    return out


def prepare_checkout(cfg: Config) -> bool:
    """Clean checkout of TauCeti main; keep .lake for fast rebuilds, drop every other leftover."""
    co = cfg.checkout
    if not (co / ".git").is_dir():
        co.parent.mkdir(parents=True, exist_ok=True)
        log(f"cloning {TAUCETI} → {co} (first run)")
        if subprocess.run(["git", "clone", "-q", f"https://github.com/{TAUCETI}", str(co)]).returncode:
            return False

    def g(*a) -> int:
        return subprocess.run(["git", "-C", str(co), *a]).returncode

    if g("fetch", "-q", "origin"):
        return False
    # -f discards a prior round's leftover edits and lands us on main in one step; a plain
    # switch/checkout would refuse on a dirty tree (two noisy errors) and could leave HEAD on
    # the old branch with only main's content. Bail if even the forced checkout fails.
    if g("checkout", "-q", "-f", "-B", "main", "origin/main"):
        return False
    g("clean", "-fdxq", "-e", ".lake")
    return True


def _fetch_shallow(url: str, dir: Path) -> bool:
    """Clone or refresh a worker-owned shallow checkout and make its origin fetch-only."""
    if (dir / ".git").is_dir():
        ok = (
            subprocess.run(["git", "-C", str(dir), "fetch", "-q", "--depth", "1", "origin", "HEAD"]).returncode == 0
            and subprocess.run(["git", "-C", str(dir), "reset", "-q", "--hard", "FETCH_HEAD"]).returncode == 0
        )
        clean = subprocess.run(["git", "-C", str(dir), "clean", "-fdxq"]).returncode == 0
        no_push = subprocess.run(["git", "-C", str(dir), "config", "remote.origin.pushurl", "no_push"]).returncode == 0
        return ok and clean and no_push
    import shutil

    shutil.rmtree(dir, ignore_errors=True)
    dir.parent.mkdir(parents=True, exist_ok=True)
    cloned = subprocess.run(["git", "clone", "-q", "--depth", "1", "--", url, str(dir)]).returncode == 0
    return (
        cloned and subprocess.run(["git", "-C", str(dir), "config", "remote.origin.pushurl", "no_push"]).returncode == 0
    )


def fetch_ref(repo: str, dir: Path) -> bool:
    """Worker-owned throwaway shallow mirror of repo's default branch (reset hard, clean)."""
    return _fetch_shallow(f"https://github.com/{repo}", dir)


def fetch_git_source(url: str, dir: Path) -> bool:
    """Clone or refresh a worker-owned shallow snapshot of a source Git repository."""
    return _fetch_shallow(url, dir)


def host_agent_argv(prompt: str, work_model: str) -> tuple[list[str], dict]:
    """The exact argv + env for the host work agent. HERE is on PATH so the agent
    resolves git-safe-push / gh-safe-pr-create / claim.sh; close_fds=True replaces `9>&-`."""
    env = {**os.environ, "PATH": f"{HERE / 'scripts'}:{os.environ.get('PATH', '')}"}
    if work_model == "codex":
        argv = ["codex", "exec", "--sandbox", "danger-full-access", "--skip-git-repo-check", prompt]
    elif work_model in OPENROUTER_MODELS:
        argv = [PI_RUN, "openrouter", OPENROUTER_MODELS[work_model], "--prompt", prompt]
    else:  # claude (Opus); ANTHROPIC_API_KEY unset so it bills the Max plan
        env.pop("ANTHROPIC_API_KEY", None)
        base = shlex_split(CLAUDE_CMD) or ["claude"]  # empty / whitespace-only falls back, not a broken argv
        argv = [*base, "-p", prompt, "--model", "opus", "--dangerously-skip-permissions"]
    return argv, env


def run_agent_host(cwd: Path, prompt: str, work_model: str, logdir: Path) -> int:
    argv, env = host_agent_argv(prompt, work_model)
    if os.environ.get("TAUCETI_AGENT_ECHO"):
        print(f"HOST cwd={cwd}\n  " + " ".join(_shq(a) for a in argv))
        return 0
    return run_agent_proc(argv, env=env, cwd=cwd, logdir=logdir, label=f"agent-{work_model}")


def run_agent_proc(argv: list[str], *, env: dict, logdir: Path, label: str, cwd: Path | None = None) -> int:
    """Run an agent subprocess. The agent CLIs (codex/claude/pi) stream a very noisy conversation log;
    by default we redirect it to a timestamped file under logdir and print only the path, so the round
    output stays readable. Pass --stream (TAUCETI_STREAM=1) to watch it live on the terminal instead.
    On a non-zero exit we always tail the log so failures aren't silent."""
    cwds = str(cwd) if cwd is not None else None
    # Every supported agent receives its prompt in argv. Close stdin explicitly: Bubble reaches the
    # agent through a non-PTY SSH channel, so an inherited terminal becomes a non-TTY stream there;
    # Codex then treats it as additional prompt input and waits forever for EOF.
    if os.environ.get("TAUCETI_STREAM"):
        return subprocess.run(argv, cwd=cwds, env=env, stdin=subprocess.DEVNULL).returncode
    logdir.mkdir(parents=True, exist_ok=True)
    logf = logdir / f"{label}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    log(f"{label}: output → {logf}  (run with --stream to watch live)")
    with open(logf, "ab") as f:
        rc = subprocess.run(
            argv, cwd=cwds, env=env, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT
        ).returncode
    if rc != 0:
        log(f"{label}: exited {rc}; last lines of {logf.name}:")
        try:
            tail = logf.read_text(errors="replace").splitlines()[-20:]
            for line in tail:
                print("    " + line)
        except OSError:
            pass
    return rc


def run_to_logfile(argv: list[str], logf: Path, label: str) -> int:
    """Run a subprocess with stdout+stderr redirected to logf, keeping the worker's MAIN log clean. Used
    for the review engine, which prints a lot (git clones, the full scoreboard dump, per-rubric lines) —
    detail that belongs in a subsidiary per-review log, not the orchestration stream. TAUCETI_STREAM=1
    streams to the terminal instead. Tails logf to the main log on a non-zero exit so failures aren't
    silent. The caller logs a one-line pointer to logf so the detail is discoverable."""
    if os.environ.get("TAUCETI_STREAM"):
        return subprocess.run(argv).returncode
    logf.parent.mkdir(parents=True, exist_ok=True)
    log(f"  {label}: engine output → {logf}  (run with --stream to watch live)")
    with open(logf, "ab") as f:
        rc = subprocess.run(argv, stdout=f, stderr=subprocess.STDOUT).returncode
    if rc != 0:
        log(f"{label}: exited {rc}; last lines of {logf.name}:")
        try:
            for line in logf.read_text(errors="replace").splitlines()[-20:]:
                log("    " + line)
        except OSError:
            pass
    return rc


def _shq(s: str) -> str:
    import shlex

    return shlex.quote(s)


# ===== Bubble authoring path (opt in with --bubble; the host is the default) =======
# The checkout, lake build, and every git/gh call happen IN a repo-scoped bubble
# container. GitHub goes through bubble's auth proxy (the host gh token never
# enters); only the one credential the work model needs is seeded; no host config
# crosses the boundary. The in-container agent invocation is a frozen contract (see agent_inner_cmd).

BUBBLE_REPO = "git+https://github.com/kim-em/bubble.git"
BUBBLE_MIN_VERSION = "0.7.28"

# TauCeti's public, anonymous Lake artifact cache. Mathlib's separate cache is fetched by
# `lake exe cache get`; this one contains TauCeti's own main-built outputs.
TAUCETI_CACHE_DOMAIN = "pub-1825e93d97ca45b2a98d9ad45a5972f8.r2.dev"
TAUCETI_CACHE_CONFIG = f"""cache.defaultService = "tauceti-public"

[[cache.service]]
name = "tauceti-public"
kind = "s3"
artifactEndpoint = "https://{TAUCETI_CACHE_DOMAIN}/artifacts"
revisionEndpoint = "https://{TAUCETI_CACHE_DOMAIN}/revisions"
"""


def bubble_cmd() -> list[str]:
    """Resolve the Bubble CLI. The uvx fallback is suitable for dry-run capability probes only;
    real sandbox rounds require an installed executable because Bubble owns host-global services."""
    import shutil

    override = os.environ.get("TAUCETI_BUBBLE")
    if override:
        return shlex_split(override)
    if shutil.which("bubble"):
        return ["bubble"]
    return ["uvx", "--from", BUBBLE_REPO, "bubble"]


@functools.lru_cache(maxsize=1)
def _bubble_open_help() -> str:
    """The resolved Bubble's `open --help`, fetched once for all capability probes."""
    try:
        p = subprocess.run([*bubble_cmd(), "open", "--help"], capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (p.stdout or "") + (p.stderr or "")


def bubble_supports_allow_push() -> bool:
    """Does the resolved Bubble support repo-scoped fork pushes?"""
    return "--allow-push" in _bubble_open_help()


def bubble_supports_allow_domain() -> bool:
    """Does the resolved bubble support the per-bubble `--allow-domain` extension? TauCeti's Lake
    artifact cache lives on its own public R2 host, outside Bubble's built-in Lean allowlist."""
    return "--allow-domain" in _bubble_open_help()


def _host_home() -> Path:
    """The login user's real home, unaffected by per-worker ``$HOME`` isolation."""
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        return Path(os.path.expanduser("~"))


def bubble_cmd_is_disposable(cmd: list[str] | None = None) -> bool:
    """Whether Bubble would run from uv's disposable one-shot tool cache."""
    cmd = cmd or bubble_cmd()
    names = [Path(arg).name for arg in cmd]
    return bool(names) and (names[0] == "uvx" or names[:3] == ["uv", "tool", "run"])


def _bubble_version(cmd: list[str]) -> str:
    """Return the installed Bubble package version, or ``""`` when it cannot be read."""
    import re

    try:
        result = subprocess.run([*cmd, "--version"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"\bversion\s+([^\s,]+)", (result.stdout or result.stderr or ""))
    return match.group(1) if result.returncode == 0 and match else ""


def installed_bubble_version() -> str:
    """Return the version of the exact stable Bubble command this worker would execute."""
    return _bubble_version(bubble_cmd())


def bubble_version_meets_minimum(version: str, minimum: str = BUBBLE_MIN_VERSION) -> bool:
    """Compare stable three-component Bubble versions without adding a packaging dependency."""
    import re

    pattern = r"(\d+)\.(\d+)\.(\d+)(?:\+[0-9A-Za-z.-]+)?"
    found = re.fullmatch(pattern, version)
    required = re.fullmatch(pattern, minimum)
    if found is None or required is None:
        return False
    return tuple(map(int, found.groups()[:3])) >= tuple(map(int, required.groups()[:3]))


def _bubble_proxy_endpoint_healthy(*, newer_than: int | None = None, expected_version: str | None = None) -> bool:
    """Whether Bubble's host-global endpoint describes a live compatible daemon.

    Validate pid liveness, the fork-push capability, and (when known) the installed
    Bubble version. The version check restarts the daemon once after upgrades so
    fixes inside the proxy process take effect even when its protocol is unchanged.
    """
    import json

    endpoint_file = _host_home() / ".bubble" / "auth-proxy.endpoint"
    try:
        if newer_than is not None and endpoint_file.stat().st_mtime_ns <= newer_than:
            return False
        endpoint = json.loads(endpoint_file.read_text())
        tcp = endpoint["tcp"]
        host, port = tcp["host"], tcp["port"]
        capabilities = endpoint.get("capabilities")
        if not isinstance(capabilities, list) or "allow-push" not in capabilities:
            return False
        if expected_version is not None and endpoint.get("bubble_version") != expected_version:
            return False
        pid = endpoint.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False
        if not isinstance(host, str) or not host or not isinstance(port, int) or isinstance(port, bool):
            return False
        os.kill(pid, 0)
        return True
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return False


def _wait_bubble_proxy_endpoint_healthy(
    *, newer_than: int | None = None, expected_version: str | None = None, timeout: float = 30
) -> bool:
    """Wait for a freshly started daemon to publish and bind its endpoint."""
    import time

    deadline = time.monotonic() + timeout
    while True:
        if _bubble_proxy_endpoint_healthy(newer_than=newer_than, expected_version=expected_version):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _bubble_proxy_endpoint_mtime() -> int | None:
    try:
        return (_host_home() / ".bubble" / "auth-proxy.endpoint").stat().st_mtime_ns
    except OSError:
        return None


def _auth_proxy_lock_path() -> Path:
    import tempfile

    return Path(tempfile.gettempdir()) / f"tauceti-worker-auth-proxy-{os.getuid()}.lock"


def ensure_fork_proxy_current() -> None:
    """Keep bubble's git auth-proxy daemon in step with the installed `bubble` CLI; Die if it can't be.

    The proxy that enforces `--allow-push` runs as a long-lived launchd/systemd daemon. Upgrading the
    `bubble` CLI does NOT restart it, so a daemon started before kim-em/bubble#320 keeps rejecting fork
    pushes with `403 Repository mismatch` even though `bubble open --help` (and so bubble_supports_allow_push)
    advertises the flag — the fork round then silently falls back to canonical (a wrong-target PR for an
    account with canonical write) or fails outright (a read-only contributor). The CLI capability probe
    can't catch this: it inspects the binary, not the running daemon.

    A healthy endpoint explicitly advertises fork-push support, so unrelated Bubble upgrades do not churn
    the shared daemon. Missing, dead, or pre-capability endpoints are refreshed through Bubble's own
    `gh proxy start`. The refresh is serialized under a host-global file lock so concurrent TauCeti workers
    do not race; Bubble separately serializes all service installers. Fail-CLOSED throughout: if the refresh
    cannot publish a fresh endpoint we Die rather than burn a long round that cannot authenticate. Call this
    ONLY for rounds that push to a fork — a review-only worker must not be blocked by it."""
    import fcntl

    # The lock lives in the always-writable temp dir, per OS user, so acquiring it effectively never fails.
    lockpath = _auth_proxy_lock_path()
    lockf = None
    try:
        try:
            lockf = open(lockpath, "w")
            fcntl.flock(lockf, fcntl.LOCK_EX)
        except OSError:
            lockf = None  # extraordinary (temp dir unwritable) — fall through and refresh anyway
        cmd = bubble_cmd()
        if bubble_cmd_is_disposable(cmd):
            if _bubble_proxy_endpoint_healthy():
                return
            raise Die(
                "preflight: fork authoring needs Bubble installed at a stable path; the uvx fallback "
                "cannot safely own a host-global launchd/systemd daemon. Install dev-bubble or set "
                "$TAUCETI_BUBBLE to a stable Bubble executable, then re-run."
            )
        version = _bubble_version(cmd)
        if _bubble_proxy_endpoint_healthy(expected_version=version or None):
            return
        endpoint_mtime = _bubble_proxy_endpoint_mtime()
        try:
            subprocess.run([*cmd, "gh", "proxy", "start"], capture_output=True, text=True, timeout=120, check=True)
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or e.stdout or "").strip()[-500:]
            suffix = f"\n  Bubble said: {detail}" if detail else ""
            raise Die(
                "preflight: bubble's git auth-proxy daemon lacks fork-push support and could not be "
                f"refreshed: {e}{suffix}"
            ) from e
        except (OSError, subprocess.SubprocessError) as e:
            raise Die(
                "preflight: bubble's git auth-proxy daemon lacks fork-push support and could not be "
                f"refreshed: {e}\n"
                "  Restart it yourself with `bubble gh proxy start`, then re-run."
            ) from e
        if not _wait_bubble_proxy_endpoint_healthy(newer_than=endpoint_mtime, expected_version=version or None):
            raise Die(
                "preflight: `bubble gh proxy start` returned success but did not publish a reachable "
                "fresh auth-proxy endpoint with fork-push support. Check ~/.bubble/auth-proxy.log and "
                "/tmp/bubble-auth-proxy.log, then re-run."
            )
        log("bubble auth-proxy: restarted daemon with fork --allow-push support")
    finally:
        if lockf is not None:
            try:
                fcntl.flock(lockf, fcntl.LOCK_UN)
            finally:
                lockf.close()


def shlex_split(s: str) -> list[str]:
    import shlex

    return shlex.split(s)


def bubble_name(cfg: Config) -> str:
    return f"tauceti-worker-{cfg.wid}"


def bubble_home(cfg: Config) -> Path:
    env = os.environ.get("TAUCETI_BUBBLE_HOME")
    return Path(env) if env else (cfg.home / ".cache" / "tauceti-worker" / cfg.wid / "bubble")


def ensure_bubble_home(cfg: Config) -> dict:
    """Fail-closed hardening of the private Bubble home.

    Both shared Lean caches must be writable only through a per-round overlay. A persistent writable
    cache would let one untrusted agent poison artifacts restored by later rounds, so verify the actual
    config rather than trusting the legacy `.worker-init` sentinel.
    """
    import tomllib

    home = bubble_home(cfg)
    env = {**os.environ, "BUBBLE_HOME": str(home)}
    home.mkdir(parents=True, exist_ok=True)

    def overlay_configured() -> bool:
        try:
            with open(home / "config.toml", "rb") as f:
                return tomllib.load(f).get("security", {}).get("shared_cache") == "overlay"
        except (OSError, tomllib.TOMLDecodeError):
            return False

    if not overlay_configured():
        try:
            p = subprocess.run(
                [*bubble_cmd(), "security", "set", "shared-cache", "overlay"],
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise Die(f"could not configure Bubble's shared-cache overlay; refusing unsafe cache mounts: {e}") from e
        if p.returncode != 0 or not overlay_configured():
            detail = (p.stderr or p.stdout or "Bubble did not persist security.shared-cache=overlay").strip()[-300:]
            raise Die(
                "could not configure Bubble's shared-cache overlay; refusing to expose persistent "
                f"writable Lean caches to the agent. Bubble said: {detail}"
            )
    return env


def agent_cred_flags(work_model: str) -> list[str]:
    """Bubble flags seeding ONLY the work model's credential; all config and other models' creds stay out."""
    if work_model == "codex":
        return ["--codex-credentials", "--no-codex-config", "--no-claude-credentials", "--no-claude-config"]
    if work_model in OPENROUTER_MODELS:
        return ["--no-claude-credentials", "--no-claude-config", "--no-codex-credentials", "--no-codex-config"]
    return ["--claude-credentials", "--no-claude-config", "--no-codex-credentials", "--no-codex-config"]


def _codex_model() -> str:
    """The codex model to run in the bubble. $TAUCETI_CODEX_MODEL wins; else the host's configured
    model from ~/.codex/config.toml (what host rounds use); else a safe default."""
    m = os.environ.get("TAUCETI_CODEX_MODEL")
    if m:
        return m
    try:
        import tomllib

        configured = tomllib.loads((_host_home() / ".codex" / "config.toml").read_text()).get("model")
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return "gpt-5.6-terra"


def agent_inner_cmd(work_model: str) -> str:
    """The command bubble runs INSIDE the container (bash -lc); a frozen contract, kept byte-for-byte
    stable: the prompt is read from the read-only /opt/round mount; *_API_KEY emptied to force
    subscription auth."""
    import shlex

    if work_model == "codex":
        # Pin the model explicitly: the bubble seeds codex *credentials* but NOT host config
        # (--no-codex-config, for isolation), so in-container codex would otherwise fall back to its
        # built-in default (e.g. gpt-5.3-codex), which a ChatGPT-subscription account may not support.
        # shlex.quote the model id: it comes from env / ~/.codex/config.toml, not a literal, and is
        # spliced into a bash -lc string inside a credential-seeded, repo-write container.
        return (
            f"env OPENAI_API_KEY= ANTHROPIC_API_KEY= codex exec --model {shlex.quote(_codex_model())} "
            '--sandbox danger-full-access --skip-git-repo-check "$(cat /opt/round/prompt.txt)"'
        )
    if work_model in OPENROUTER_MODELS:
        return (
            'env ANTHROPIC_API_KEY= OPENAI_API_KEY= OPENROUTER_API_KEY="$(cat /opt/round/openrouter.key)" '
            f"pi --provider openrouter --model {shlex.quote(OPENROUTER_MODELS[work_model])} --print "
            '"$(cat /opt/round/prompt.txt)"'
        )
    return (
        'env ANTHROPIC_API_KEY= OPENAI_API_KEY= CLAUDECODE= claude -p "$(cat /opt/round/prompt.txt)" '
        "--dangerously-skip-permissions --model opus"
    )


def bubble_work_cmd(inner: str) -> str:
    """Trusted bootstrap run before the work agent inside Bubble.

    Bubble's noninteractive `--command` mode deliberately does not run a hook-generated build, so do
    both cache fetches explicitly: Mathlib's `lake exe cache`, then Lake's built-in cache for TauCeti's
    own outputs. Keep a Lake-cache miss and the preliminary build non-fatal: fix/fix-ci/bump/rebase
    rounds often start from a red tree, and repairing it is the agent's job. A Mathlib-cache failure is
    fatal because compiling Mathlib would consume the round. The exports remain in the environment
    inherited by the agent, so later `lake build`s restore from the same per-round cache too.
    """
    return (
        "set -e; "
        "export LAKE_CONFIG=/opt/round/lake-cache.toml LAKE_ARTIFACT_CACHE=true "
        "LAKE_RESTORE_ARTIFACTS=true; "
        "lake exe cache get || lake exe cache get; "
        f"if ! lake cache get --service tauceti-public --repo {TAUCETI}; then "
        "echo 'warning: TauCeti Lake cache miss; building missing outputs' >&2; "
        "fi; "
        "if ! timeout 1800 lake build; then "
        "echo 'warning: pre-agent lake build failed or timed out; the agent starts from a red tree' >&2; "
        "fi; "
        f"exec {inner}"
    )


def _bubble_pop(cfg: Config, env: dict) -> None:
    subprocess.run([*bubble_cmd(), "pop", bubble_name(cfg), "-f"], env=env, capture_output=True)


def _ensure_claude_creds_for_bubble(cfg: Config) -> None:
    """bubble seeds the in-container claude from <CLAUDE_CONFIG_DIR>/.credentials.json (it can't read the
    macOS Keychain). On Linux that file is the store, so leave it to bubble. On macOS, where Claude Code
    keeps creds in the Keychain, write the credential INTO the configured dir from the Keychain, every
    round: the Keychain is authoritative and may hold a token rotated by a host claude since we last
    wrote, and re-mirroring it keeps both the access AND refresh token current (a stale refresh token in
    an unexpired-looking mirror would fail mid-round). The read is interactive (unlock if locked). The
    pacer reads the Keychain directly (keychain-first), so it never refreshes this mirror. If the Keychain
    can't be read but a credentials file already exists, fall back to it; otherwise Die."""
    if sys.platform != "darwin":
        return  # Linux/Windows: the file is the store; bubble seeds it (or reports none)
    f = claude_dir(cfg.home) / ".credentials.json"
    blob = _claude_keychain_creds_interactive()
    if blob and blob.get("claudeAiOauth"):
        f.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(f, blob)  # 0600, temp + atomic rename (no partial-read or perms window)
        return
    if (_read_json_file(f) or {}).get("claudeAiOauth"):
        return  # Keychain unreadable but a credentials file exists; let bubble use it
    raise Die(
        "no Claude credentials to seed the bubble: none in "
        f'{f} and could not read the "Claude Code-credentials" login Keychain item. Unlock '
        "the Keychain and retry, or drop --bubble (the host claude reads the Keychain itself)."
    )


def run_in_bubble(
    w: Worker,
    target: str,
    prompt: str,
    opts: RoundOpts,
    mounts: list[str] | None = None,
    *,
    inner_cmd: str | None = None,
    cred_model: str | None = None,
    allow_push: str | None = None,
) -> int:
    """Open a fresh repo-scoped bubble for target, run a command inside it, pop it. By default runs the
    work agent (agent_inner_cmd) seeding the work model's credential; pass inner_cmd / cred_model to run
    something else in the same sandbox (e.g. the review engine — see review_in_bubble)."""
    import shlex

    cfg, wm = w.cfg, opts.work_model
    cred_model = cred_model or wm
    # OpenRouter agents run in the bubble: the image ships `pi` and allows openrouter.ai egress
    # (kim-em/bubble#299), and the key is staged 0600 at /opt/round/openrouter.key below.
    # bubble honors $CLAUDE_CONFIG_DIR for its own credential seeding (kim-em/bubble#317), reading it
    # from this subprocess's inherited env, so the in-bubble claude and the pacer agree on the account
    # with no extra plumbing here.
    env = ensure_bubble_home(cfg)
    rounddir = cfg.state / "bubble-round"
    import shutil

    shutil.rmtree(rounddir, ignore_errors=True)
    rounddir.mkdir(parents=True, exist_ok=True)
    (rounddir / "prompt.txt").write_text(prompt)
    (rounddir / "lake-cache.toml").write_text(TAUCETI_CACHE_CONFIG)
    # Stage the write wrappers (contract §1/§4): mounted read-only at /opt/round and put on PATH inside
    # the container, so the agent's ONLY push path is the branch-CAS git-safe-push.
    for f in ("git-safe-push", "gh-safe-pr-create", "claim.sh"):
        shutil.copy(HERE / "scripts" / f, rounddir / f)
        os.chmod(rounddir / f, 0o755)
    if wm in OPENROUTER_MODELS:  # OpenRouter key has no proxy — stage it 0600, mounted read-only
        keyf = rounddir / "openrouter.key"
        keyf.write_text(os.environ.get("OPENROUTER_API_KEY", ""))
        os.chmod(keyf, 0o600)

    _bubble_pop(cfg, env)  # clear any container a SIGKILLed prior round left behind

    mount_flags = ["--mount", f"{rounddir}:/opt/round:ro"]
    for m in mounts or []:
        mount_flags += ["--mount", m]

    # Fork-PR write support (kim-em/bubble#320): grant the in-container agent git fetch/push to the
    # contributor's own fork on top of the base-scoped GitHub access, so it can push an authored branch
    # (roadmap) or a fix to a fork-headed PR. The base repo keeps its allowlist-write-graphql scope; the
    # fork gets git only. (For a PR target, bubble also auto-derives the head fork, so this is belt-and-
    # suspenders for maintenance and the sole grant for authoring, which has no PR to derive from.)
    push_flags = ["--allow-push", allow_push] if allow_push else []

    # Push-arbiter env crossing into the container: /opt/round on PATH + the branch-CAS inputs the
    # agent's git-safe-push / gh-safe-pr-create need. \$PATH stays literal so it expands to the
    # CONTAINER PATH inside bubble's bash -lc. We do NOT forward TAUCETI_CLAIM_* (the claim+heartbeat
    # are host-side; the branch CAS is the [HARD] guarantee and needs no in-container claim).
    tcenv = "env PATH=/opt/round:$PATH"
    for var in (
        "TAUCETI_PUSH_REF",
        "TAUCETI_PUSH_EXPECT",
        "TAUCETI_PUSH_REMOTE",
        "TAUCETI_TARGET_MARKER",
        "TAUCETI_REQUIRE_TARGET_MARKER",
    ):
        val = os.environ.get(var)
        if val:
            tcenv += f" {var}={shlex.quote(val)}"
    command_inner = inner_cmd or agent_inner_cmd(wm)
    if inner_cmd is None:
        command_inner = f"bash -c {shlex.quote(bubble_work_cmd(command_inner))}"
    command = f"{tcenv} {command_inner}"

    # Only work-agent rounds compile TauCeti. Review/probe commands remain usable with older Bubble
    # releases and do not need access to the artifact-cache host.
    cache_flags = ["--allow-domain", TAUCETI_CACHE_DOMAIN] if inner_cmd is None else []

    argv = [
        *bubble_cmd(),
        "open",
        target,
        "--shell",
        "--local",
        "--name",
        bubble_name(cfg),
        "--ephemeral",
        "--github-security",
        "allowlist-write-graphql",
        *push_flags,
        *cache_flags,
        *mount_flags,
        *agent_cred_flags(cred_model),
        "--command",
        command,
    ]

    if os.environ.get("TAUCETI_AGENT_ECHO"):
        print("BUBBLE " + " ".join(_shq(a) for a in argv))
        return 0

    if allow_push and not _wait_bubble_proxy_endpoint_healthy(timeout=5):
        raise Die(
            "bubble auth-proxy became unavailable before the fork-writing round started; re-run to refresh it safely"
        )

    # Re-mirror the operator's fresh creds into the isolated home at the last moment before bubble seeds
    # the container (provider-neutral: covers codex too, and the --ignore-quota / review / probe paths that
    # never call the pacer). No-op when not isolated or on macOS.
    mirror_creds(cfg)
    # On macOS, Claude Code keeps creds in the Keychain, not a file; make sure the configured
    # CLAUDE_CONFIG_DIR holds a current credentials file so bubble can seed it (done after the echo path
    # so a dry-run never prompts the Keychain).
    if cred_model == "claude":
        _ensure_claude_creds_for_bubble(cfg)
    w.rc.add_cleanup(lambda: _bubble_pop(cfg, env))  # pop if we're killed mid-run
    try:
        if inner_cmd is None:  # the work agent — quiet/log it like the host path
            rc = run_agent_proc(argv, env=env, logdir=cfg.logdir, label=f"agent-{wm}")
        else:  # review engine / probe — leave its output inline
            rc = subprocess.run(argv, env=env).returncode
    finally:
        _bubble_pop(cfg, env)  # don't rely on --ephemeral alone, and pop even on an exception
    return rc


def _codex_review_model_override(reviewers: str) -> str | None:
    """An explicit codex review-model override from $TAUCETI_CODEX_MODEL, or None to let the engine use
    its own default (currently gpt-5.6-sol, with an automatic gpt-5.6-terra fallback for accounts that
    can't run it). Reuses the same env _codex_model() reads for authoring, so one TAUCETI_CODEX_MODEL
    pins BOTH authoring and review (parity with DEEPSEEK_MODEL / MINIMAX_MODEL). Forwarded only when
    codex is actually a reviewer; left unset by default so the engine's seamless fallback stays in play
    (passing --codex-model opts the engine out of it, which is what an explicit pin should mean)."""
    m = os.environ.get("TAUCETI_CODEX_MODEL")
    return m if (m and "codex" in [r.strip() for r in reviewers.split(",")]) else None


def review_in_bubble(w: Worker, pr: int, head: str, reviewers: str, opts: RoundOpts) -> int:
    """Run the tauceti-review engine INSIDE bubble — a hard container boundary around an engine that
    reads an untrusted PR diff and runs a model on it (and, once review gains tool use, runs that
    model's tools). The repo-scoped proxy can't reach a second repo, so we pre-stage everything the
    engine would otherwise fetch and run it OFFLINE: the engine itself, the roadmap, and the review
    store are host→container mounts; `--no-sync` makes the engine archive review records to the mounted
    outbox but NOT push (the bubble can't reach TauCetiData) — do_review drains that outbox to
    TauCetiData host-side afterwards. The only traffic that
    crosses the boundary is the engine's TauCeti code clone + PR API + scoreboard post (all scoped to
    TauCeti, already allowed by the proxy) and the reviewer model's provider egress. The engine has no
    Python deps, so we run the mounted source with the image's python3 — no uvx/uv/PyPI.

    The store is mounted READ-WRITE from the worker's persistent store_dir (not /tmp): it holds the
    scoreboard/thread comment ids the next round edits in place — an ephemeral store would post a
    duplicate scoreboard. The engine mount keeps its `.git` so the engine never falls back to a
    cross-repo `gh api` for its own rev. TAUCETI_REVIEW_ENGINE_DIR pins a local engine checkout
    (operator override / pre-merge testing); otherwise a shallow REVIEW clone is staged."""
    cfg = w.cfg
    eng = os.environ.get("TAUCETI_REVIEW_ENGINE_DIR")
    engine_dir = Path(eng) if eng else (cfg.state / "refs" / "review-engine")
    if not eng and not fetch_ref(REVIEW, engine_dir):  # keeps .git (no cross-repo rev fallback)
        raise Die(f"fetch {REVIEW} failed")
    roadmap_dir = cfg.state / "refs" / "roadmap"
    if not fetch_ref(ROADMAP, roadmap_dir):
        raise Die(f"fetch {ROADMAP} failed")
    store = cfg.store_dir
    store.mkdir(parents=True, exist_ok=True)
    mounts = [f"{engine_dir}:/opt/engine:ro", f"{roadmap_dir}:/opt/roadmap:ro", f"{store}:/opt/review-store:rw"]
    # No --rubrics-sha/--shadow (they'd re-fetch TauCetiReview). --no-mathlib for now; wiring
    # --mathlib-dir at the bubble's vendored .lake/packages/mathlib is the reuse-rubric refinement.
    # run_in_bubble prefixes `env PATH=… `, so the inner command must start with an executable, not the
    # `cd` shell builtin — carry the engine on PYTHONPATH instead of cwd (cwd is irrelevant: the engine
    # uses --repo-dir for its files and an absolute temp workdir).
    import shlex

    cm = _codex_review_model_override(reviewers)
    codex_flag = f" --codex-model {shlex.quote(cm)}" if cm else ""  # operator override; else engine default
    inner = (
        "env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/engine python3 -m runner.cli "
        f"{pr} --repo {TAUCETI} --repo-dir /opt/engine --roadmap-dir /opt/roadmap "
        f"--no-mathlib --no-sync --store /opt/review-store --post "
        f"--max-rounds-per-day {REVIEW_DAILY_CAP} "  # one value drives the survey prefilter + engine
        f"--reviewer {reviewers} --expect-head {head} --submitted-by {me()}{codex_flag}"
    )
    # target is the PR so bubble checks it out; prompt unused by the engine.
    return run_in_bubble(w, f"{TAUCETI}/pull/{pr}", "", opts, mounts=mounts, inner_cmd=inner, cred_model=reviewers)


def _worker_iso_home(wid: str, _base: Path | None = None) -> Path:
    """The per-worker isolated $HOME. On macOS it MUST be short: bubble runs the sandbox in a colima VM
    whose lima/incus unix sockets nest under $HOME, and the default location beneath the installed package
    (site-packages) pushes those socket paths past UNIX_PATH_MAX (104) — colima then refuses to start
    ("instance name … too long"). Anchor it at the real login user's home (via `pwd`, NOT $HOME, which a
    loop child has already had repointed) so the path is short and stable across re-isolations, and bound
    the per-worker component against the longest socket bubble nests under the home so that even a long
    --worker-id (or login name) can't reoverflow it — hashing (deterministically; a loop child must
    recompute the same path) only when the raw wid wouldn't fit, so ordinary ids stay readable. Linux uses
    native incus (no $HOME-nested sockets, no colima), so it keeps the in-tree location beside the worker's
    other state. The path must be a pure function of wid (no $HOME) so the early-return below recognises an
    already-isolated child."""
    if sys.platform != "darwin":
        return HERE / "state" / wid / "home"
    base = _base
    if base is None:
        try:
            import pwd

            base = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (ImportError, KeyError, OSError):
            base = Path(os.path.expanduser("~"))
    root = base / ".tauceti"
    # colima binds <home>/.colima/_lima/<profile>/ssh.sock.<16-digit id>; keep that whole path strictly
    # under UNIX_PATH_MAX (104) by bounding the per-worker component.
    sock_suffix = len("/.colima/_lima/colima-bubble-colima/ssh.sock.") + 16
    budget = (104 - 1 - sock_suffix) - len(str(root)) - 1  # max home length, minus root and its trailing "/"
    if len(wid) <= budget:
        return root / wid
    import hashlib

    digest = hashlib.sha1(wid.encode()).hexdigest()[:8]
    keep = max(1, budget - 9)  # leave room for "-" + 8 hex chars
    return root / f"{wid[:keep]}-{digest}"


def _seed_gh_token_for_isolation() -> None:
    """macOS only: export $GH_TOKEN from the operator's gh login BEFORE isolate_home repoints $HOME.

    gh stores its token in the login Keychain, which gh's keyring backend can resolve via
    $HOME/Library/Keychains on an affected setup. Once isolate_home sets $HOME to the worker's short
    isolated home, that lookup misses and the survey's `gh pr list` fails unauthenticated — the worker
    aborts the round (Bryan's report). The GH_CONFIG_DIR redirect recovers gh's host LIST but not the
    keychain-backed token. So while the real $HOME still reaches the Keychain, capture the token and
    export it: gh and gh's git credential helper both honour $GH_TOKEN ahead of the keychain, so child
    calls (the survey, host pushes, and the agent's own gh in host mode) stay authenticated under the
    isolated home. macOS is single-account by nature, so one token is correct.

    Scope/limits: github.com only (the sole host TauCeti uses; a GHES remote would need a separate
    $GH_ENTERPRISE_TOKEN). Captured once, at isolation — a long `--loop` inherits it for the run, which
    matches gh's own use of the static keychain token; if the operator rotates gh creds mid-loop,
    restart the worker to re-seed. No-op off macOS (Linux keeps its token in the redirected
    GH_CONFIG_DIR or a session keyring, neither $HOME-path-scoped), when the operator already exported a
    token, or when capture fails (logged, then left for `gh auth login` / `--worker-id default` — no
    worse than before this seed existed)."""
    if sys.platform != "darwin":
        return
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        return  # respect an operator-set token; don't shell out
    try:
        r = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError) as e:
        log(f"could not capture a gh token for the isolated home ({e}); gh may fail under the isolated $HOME")
        return
    tok = (r.stdout or "").strip()
    if r.returncode == 0 and tok:
        os.environ["GH_TOKEN"] = tok
    else:
        log(
            "could not capture a gh token for the isolated home (gh auth token failed); gh may fail "
            "under the isolated $HOME on macOS — run `gh auth login` or pass --worker-id default"
        )


def isolate_home(wid: str) -> Path:
    """Give this worker its OWN $HOME so its credentials can't race other workers or the operator (Codex
    review / the --isolate-home flag). Symlinks the read-only Claude tool/config surface from the real
    config dir; copies the mutable Claude/Codex auth files in ONCE, then records the source dirs in
    .tauceti-creds-source markers so mirror_creds() can re-mirror a fresher access token whenever the
    operator's external refresher rotates it. The worker itself never refreshes (never touches the
    single-use refresh token). The copy always lives at <home>/.claude and $CLAUDE_CONFIG_DIR is repointed
    there, so both the pacer and the spawned claude read the isolated creds even when the operator's real
    config dir is elsewhere. Returns the worker home and sets $HOME. Children inherit.

    macOS caveat: this isolates Codex creds, but NOT Claude's. Claude Code keeps its creds in the login
    Keychain — one per-login-user store, not $HOME/$CLAUDE_CONFIG_DIR-scoped — so the credential copy +
    repointing is a no-op there: the spawned claude reads the shared Keychain regardless, and the pacer's
    read-only Keychain fallback measures that same account. macOS is host-only/single-account by nature.
    On macOS the home also sits at a short path (see _worker_iso_home) and its colima/incus state is
    symlinked to the operator's, so bubble reuses the one host VM instead of building a throwaway per-worker
    one (and the sockets under it stay within UNIX_PATH_MAX)."""
    import shutil

    home = _worker_iso_home(wid)
    if Path(os.environ.get("HOME", "")) == home:
        return home  # already isolated (a loop child inherits the parent's $HOME) — don't re-copy or warn
    real = Path(os.environ.get("HOME", os.path.expanduser("~")))
    real_claude = claude_dir(real)  # honors the operator's $CLAUDE_CONFIG_DIR before we repoint it
    iso_claude = home / ".claude"
    iso_claude.mkdir(parents=True, exist_ok=True)
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        # Point the isolated home's colima/incus state at the operator's so bubble's `colima status`
        # (which reads $HOME/.colima) finds the host's already-running VM and skips building a throwaway
        # per-worker one — and the short home keeps the resulting socket paths within UNIX_PATH_MAX. The
        # shared host VM is the same one the 'default' worker uses, so this adds no new colima ownership.
        for rel in (".colima", Path(".config") / "incus"):
            link, target = home / rel, real / rel
            if link.is_symlink() and not link.exists():
                try:
                    link.unlink()  # stale dangling link (target since removed) — drop it before re-linking
                except OSError:
                    pass
            # Only link when the operator actually has state to share; otherwise leave it absent so bubble
            # creates a real dir here (a self-contained per-worker VM at this short path), never a dangling
            # symlink. Don't clobber an existing real dir or a good link.
            if target.exists() and not link.exists() and not link.is_symlink():
                try:
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(target)
                except OSError:
                    pass
    for item in ("skills", "swap-account", "bin", "config.json", "settings.json", "CLAUDE.md"):
        src, dst = real_claude / item, iso_claude / item
        if _safe_exists(src) and not dst.exists():
            try:
                dst.symlink_to(src)
            except OSError:
                pass
    for f in (".credentials.json", ".gist-id", ".gist-encryption-key"):
        src, dst = real_claude / f, iso_claude / f
        if _safe_exists(src) and not dst.exists():
            shutil.copy2(src, dst)
    # The initial copy is once-only; thereafter mirror_creds() RE-MIRRORS a fresher access token from the
    # source whenever the operator's external refresher rotates it (the worker never refreshes its own
    # tokens). So a reused --worker-id stays pinned to whatever account it was first seeded from. Record the
    # source and warn if it changes, rather than silently pacing/running the stale account.
    marker = iso_claude / ".tauceti-creds-source"
    if marker.exists():
        if marker.read_text().strip() != str(real_claude):
            log(
                f"WARNING: worker '{wid}' keeps Claude creds first copied from {marker.read_text().strip()} "
                f"(not {real_claude}); use a fresh --worker-id to switch accounts."
            )
    else:
        marker.write_text(str(real_claude))
    real_codex, iso_codex = real / ".codex", home / ".codex"
    src, dst = real_codex / "auth.json", iso_codex / "auth.json"
    if _safe_exists(src) and not dst.exists():
        shutil.copy2(src, dst)
    # Record the real ~/.codex so mirror_creds() can re-mirror the codex token too (the Claude marker only
    # names the Claude source). Written unconditionally so homes seeded before this marker existed get it
    # backfilled on their next isolate_home() run.
    codex_marker = iso_codex / ".tauceti-creds-source"
    if not codex_marker.exists():
        try:
            codex_marker.write_text(str(real_codex))
        except OSError:
            pass
    # Keep host-side gh and git working under the isolated $HOME: the survey's `gh pr list` and host
    # pushes run in this $HOME, but their config (unlike Claude/Codex tokens) doesn't refresh-race, so
    # point them back at the operator's real config rather than an empty isolated one. Respect a value
    # the operator already exported. Children inherit these, so the early-return path above is covered.
    # (On macOS this redirect recovers gh's host list but not its keychain-backed token — that needs the
    # $GH_TOKEN seed just below, before $HOME moves.)
    gh_cfg = real / ".config" / "gh"
    if gh_cfg.is_dir():
        os.environ.setdefault("GH_CONFIG_DIR", str(gh_cfg))
    git_cfg = real / ".gitconfig"
    if git_cfg.exists():
        os.environ.setdefault("GIT_CONFIG_GLOBAL", str(git_cfg))
    # Capture gh's keychain-backed token while the real $HOME still reaches the login Keychain (macOS),
    # so child gh/git calls authenticate via $GH_TOKEN once $HOME is repointed below. Must precede the
    # $HOME assignment — afterwards the Keychain lookup misses.
    _seed_gh_token_for_isolation()
    os.environ["HOME"] = str(home)
    os.environ["CLAUDE_CONFIG_DIR"] = str(iso_claude)  # so claude_dir() + the spawned claude agree
    log(f"isolated HOME={home} (worker '{wid}')")
    return home
