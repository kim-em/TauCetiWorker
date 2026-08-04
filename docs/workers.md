# Persistent workers

Persistent workers are declarative. You describe the workers you want in
`workers.toml`, and a manager process reconciles reality against that file:
starting missing workers, stopping removed ones, restarting changed ones, and
backing off repeated failures. They do not belong to a terminal session.

## Quickstart

Nothing needs to exist first. `workers add` creates the config, writes the
definition, and starts a manager:

```bash
tauceti workers add                        # an enabled worker1, the whole cascade
tauceti workers add reviewer --only review # a focused, named worker
tauceti workers                            # desired and actual state
tauceti workers logs --follow reviewer     # its durable console log
```

Day-to-day controls:

```bash
tauceti workers disable reviewer   # persist desired stopped state
tauceti workers enable reviewer    # and start it again
tauceti workers restart reviewer   # without changing desired state
tauceti workers remove reviewer    # drop the definition and stop it
```

## Editing the file directly

`workers add` cannot express every field, and it rewrites the file in canonical
form, dropping comments and hand formatting. To keep those, or to set a field
`add` does not cover, edit the file and apply it:

```bash
tauceti workers edit          # $VISUAL, else $EDITOR, else vi
tauceti workers apply --check # validate without reconciling
tauceti workers apply         # reconcile, starting a manager if needed
```

`workers edit` does not start a manager on its own; `workers apply` is the
explicit follow-up. A later `enable`, `disable`, `add`, or `remove` normalizes
the file again, so comments do not survive one.

A minimal file, matching `workers.toml.example`:

```toml
version = 1

[[workers]]
id = "worker1"
enabled = true

[[workers]]
id = "worker2"
enabled = true
agent = "codex"
only = ["rebase", "review"]
ignore_quota = true
```

You can edit `workers.toml` while the manager is running. It reloads the file
each cycle. If your edit does not parse, the manager keeps the last good
generation, leaves running workers alone, and says so once:

```
tauceti workers: invalid configuration; keeping last good generation: ...
```

## Where the config lives

The first of these that is set wins:

| Source | Path |
| --- | --- |
| `tauceti workers --config PATH` | exactly that file |
| `$TAUCETI_WORKERS_CONFIG` | exactly that file |
| `$TAUCETI_CONFIG_HOME` | `$TAUCETI_CONFIG_HOME/workers.toml` |
| `$XDG_CONFIG_HOME` | `$XDG_CONFIG_HOME/tauceti/workers.toml` |
| macOS default | `~/Library/Application Support/tauceti/workers.toml` |
| otherwise | `~/.config/tauceti/workers.toml` |

`--config` belongs to `tauceti workers` itself, not to the action, so it goes
before the action name: `tauceti workers --config ./workers.toml apply`.

Writes take an `flock` on a sibling `workers.toml.lock`, then replace the file
atomically, so concurrent CLI and dashboard mutations serialize instead of
clobbering each other.

## `workers.toml` reference

The file has exactly two top-level keys: `version`, which must be `1`, and
`workers`, an array of tables. Any other top-level key is an error, as is any
unrecognized field inside a `[[workers]]` table. Duplicate ids are rejected.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `id` | string | required | `[a-z0-9-]+`, at most 40 characters; namespaces the worker's state, checkout, and logs |
| `enabled` | bool | `true` | Desired running state. `false` stops the worker without forgetting it |
| `agent` | string | `"auto"` | `auto`, `codex`, `claude`, `deepseek`, or `minimax` |
| `only` | string list | `[]` | Work phases: `rebase`, `bump`, `progress`, `fix-ci`, `fix`, `review`, `roadmap`. Empty means the whole cascade |
| `sandbox` | string | `"host"` | `host` or `bubble` |
| `ignore_quota` | bool | `false` | Skip the pacer. Provider hard limits still apply |
| `roadmap_only` | string | unset | The single roadmap area for roadmap rounds. `""` means all areas; unset means a fresh random area each round |
| `roadmap_skip` | string list | `[]` | Roadmap areas to exclude. `roadmap_only` wins on overlap |
| `roadmap_extra_identities` | string list | `[]` | Extra GitHub logins whose claimed intentions count as this worker's own |
| `respect_claims` | bool | `true` | Whether to avoid intentions others have claimed |
| `source` | string | unset | Supplementary repository directory or URL. Requires `roadmap` in `only` and a non-empty `roadmap_only` |
| `author_model` | string | unset | Exact authoring model. Requires an `agent` other than `auto` |
| `author_effort` | string | unset | Authoring reasoning effort. Requires an `agent` other than `auto` |
| `pace` | string | unset | Pacing curve as `time%:budget%` points, for example `0:10,50:70,90:90` |
| `stream` | bool | `false` | Keep the agent transcript in the console log instead of a separate file |
| `isolate_home` | bool | `false` | Force per-worker credential isolation even for the id `default` |
| `restart` | string | `"always"` | `always`, `on-failure` (only after a nonzero exit), or `never` |

Changing any field changes the worker's fingerprint, and the manager restarts
exactly the workers whose fingerprint moved.

## `workers add` flags

`add` takes an optional id, defaulting to the next free `workerN`, and writes an
entry with `enabled = true`.

| Flag | Sets |
| --- | --- |
| `worker_id` | `id`; omit it for the next free `workerN` |
| `--agent AGENT` | `agent` |
| `--only TASKS` | `only`, as a comma-separated list |
| `--sandbox {host,bubble}` | `sandbox` |
| `--ignore-quota` | `ignore_quota` |
| `--roadmap-only AREA` | `roadmap_only` |
| `--roadmap-skip AREAS` | `roadmap_skip`, as a comma-separated list |
| `--source PATH_OR_URL` | `source` |
| `--author-model MODEL` | `author_model` |
| `--author-effort EFFORT` | `author_effort` |
| `--pace CURVE` | `pace` |
| `--stream` | `stream` |
| `--isolate-home` | `isolate_home` |

`add` cannot set `roadmap_extra_identities`, `respect_claims`, or `restart`, and
always writes `enabled = true`. Use `workers edit` for those.

## Actions

| Action | What it does |
| --- | --- |
| _(none)_ | Same as `status` |
| `status [--json] [--watch]` | Desired and actual state. Exits nonzero if the manager is offline or a wanted worker is not alive |
| `apply [--check]` | Validate the whole file, then reconcile. `--check` validates only |
| `add [ID] [flags]` | Append an enabled definition and reconcile |
| `enable ID` / `disable ID` | Persist desired running or stopped state |
| `restart ID` | Restart one worker without changing desired state |
| `remove ID` | Drop the definition and stop the worker |
| `logs ID [--follow] [--lines N]` | The durable console log. `--follow` continues across worker restarts |
| `tmux [--no-attach]` | Build the optional tmux viewing workspace |
| `manager [--interval N]` | Run the reconciler in the foreground |
| `manager-stop [--leave-workers]` | Stop the detached manager, and by default its workers |
| `service ACTION` | `install`, `uninstall`, `start`, `stop`, `restart`, or `status` for the native user service |
| `edit [--editor CMD]` | Open `workers.toml` in an editor |
| `import LEGACY [--force]` | One-shot migration from the legacy `workers.conf` format |

Every mutating action (`apply`, `add`, `enable`, `disable`, `remove`, `restart`)
starts a manager if none is running, and returns only once that manager answers
a ping.

## The manager, and running past logout

`workers apply` and friends start a detached manager owned by the current login
session. For operation that survives logout and comes back after a reboot,
install the native user service:

```bash
tauceti workers service install
tauceti workers service status
```

That is a systemd user service on Linux and NixOS, or a LaunchAgent on macOS.
Two platform caveats:

- On Linux systems that stop the user manager after the last logout,
  `loginctl enable-linger "$USER"` keeps user services running with no login
  session.
- A macOS LaunchAgent survives terminal and SSH-session loss, but starts only
  after a graphical login. Installation says so explicitly when no GUI login
  domain is active.

At startup the worker preserves an explicit `SSL_CERT_FILE` or discovers the
host's system CA bundle, including NixOS's `/etc/ssl/certs/ca-certificates.crt`,
so quota checks keep working when uv's standalone Python defaults to a different
OpenSSL trust store. A shell-provided Nix bundle carried into the service stays
a revalidated candidate rather than a pinned trust path, so garbage collection
cannot silently disable fallback discovery.

The reconciler and the worker control sockets are portable Unix code. No Linux
`/proc` interface is required.

## State on disk

| Path | Contents |
| --- | --- |
| `<state>/<id>.json` | Per-worker status, heartbeated every two seconds |
| `<state>/manager.log` | The detached manager's own console |
| `<state>/logs/<id>/work-*.log` | Durable per-run console logs |
| `<runtime>/manager.sock`, `w-<id>.sock` | Control sockets, mode 0600 |

`<state>` is `$TAUCETI_WORKERS_STATE_DIR`, else `$XDG_STATE_HOME/tauceti/workers`,
else `~/Library/Application Support/tauceti/state/workers` on macOS, else
`~/.local/state/tauceti/workers`. `<runtime>` is `$TAUCETI_RUNTIME_DIR`, else
`$XDG_RUNTIME_DIR/tauceti`, else `/tmp/tauceti-$(id -u)`; it must be owned by you
and inaccessible to other users, or the manager refuses to start.

Each managed worker publishes a structured state such as `waiting-quota`,
`surveying`, `running`, or `backoff`, along with its current phase and target and
the path to its logfile. That is what `tauceti workers status` and the
dashboard's workers view read.

## tmux is a viewer, not the supervisor

`tauceti workers tmux` opens one dashboard window plus one log-following window
per enabled worker. Killing that session does not stop any worker, and running
the command again rebuilds the view from the configuration and the durable logs.
tmux is optional; workers run without it.

## Migrating from `workers.conf`

`workers.conf` is a legacy line-oriented format: one `tauceti work --loop`
command per line, each with an explicit `--worker-id`. It is only ever read by
the one-shot import, which refuses to overwrite an existing `workers.toml`
without `--force`:

```bash
tauceti workers import workers.conf
```

Anything the legacy parser does not recognize is an error naming the file, line,
and token, so a partial migration is never silently accepted.
