<p align="center">
  <img src="Ag.jpeg" alt="agents" width="220">
</p>

<h1 align="center">agents</h1>

<p align="center">
  Register and run named Claude Code agents — <b>without tmux and without a per-agent daemon.</b>
</p>

A minimal fork of [cmux](https://github.com/mekarpeles/cmux): it keeps what only
this layer can do (a persistent registry, a per-agent home directory,
up/down/resume) and drops everything cmux only needed for message delivery,
because Claude Code delivers natively now.

The command is `agents` (with `ag` as a shorthand alias):

```bash
agents register pam --role "registry owner" --workspace pam --identity ./pam.md
agents register ada --cwd ~/Projects/ada --session-id <id>   # adopt an existing session
agents up pam                 # launch attached to this terminal (resumes if bound)
agents up pam --host herdr    # launch detached in a herdr pane
agents send pam "status?"     # native delivery via claudio
agents ls                     # registered agents + live status
agents list                   # alias of ls
agents down pam               # SIGTERM the running session
agents rm pam                 # de-register (home dir preserved)
agents help                   # same as `agents --help`; bare `agents` also prints help
```

`list`/`ls` and `help`/`--help` are aliases, and an unknown command is rejected
(it never becomes an agent name) — fixing the cmux footgun where `cmux list` and
`cmux up --help` silently registered junk agents named `list` / `--help`.

## How it differs from cmux

| | cmux | agents |
|---|---|---|
| Message delivery | claudio daemon + tmux send-keys + Enter-retry | **native** (`claudio.native` → session socket) |
| Idle detection | read tmux cursor position | Claude Code (`notify_idle`) |
| Session binding | newest `.jsonl` in cwd (mis-binds) | **sessionId of the pid we launched** |
| Terminal host | tmux (required) | pluggable: current terminal, or herdr |
| Permission prompts | unblock watcher + `--allowed-tools` pre-auth | **none** (Claude auto-mode) |
| Per-agent daemon | yes | **none** |
| Registry | `sessions.json` + `agents.db` | `agents.db` (SQLite/WAL) |

Home dirs keep the cmux layout (`identity.md`, `initial-prompt.md`, `cwd`,
`last-session-id`), so migration is a copy.

## Install

```bash
./install.sh                         # pipx: agents (+ claudio) and cq
# or, for development:
pip install -e /path/to/claudio      # the native messaging dependency
pip install -e /path/to/agents       # exposes `agents` (and the `ag` shortcut)
```

State lives under `~/.agents/` (override with `AGENTS_STATE_DIR`).

## Roadmap

The registry is one table today (agents). The vision is a small graph on top of it:

- **Workspaces** — a table grouping one or more agents so you can bring a whole
  set up together and `agents attach <workspace>` into it (tmux-backed).
- **Roles** — a graph of edges over the agents table (`ada reports-to pam`), so
  coordination and escalation have a durable shape, not an ad-hoc convention.
- **Name resolution** — one resolver across the identifiers that different
  callers need: `session_id` (durable key), harness name (what the TUI's
  SendMessage uses), your logical name, and the live socket/pid (resolved on
  demand, never stored stale).

These are experimental and not built yet; the near-term focus is making the
basics above rock-solid.

## Tests

```bash
PYTHONPATH=/path/to/claudio:. python3 -m pytest tests/ -q
```

See [DESIGN.md](DESIGN.md) for the architecture and the native-messaging boundary.
