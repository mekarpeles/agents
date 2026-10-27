# agents — architecture

How the system is built. (Work to be done lives in **GitHub issues**, not here.)

`agents` registers, runs, and messages named Claude Code agents. It has **no
runtime dependencies** — pure Python, stdlib only — because Claude Code now does
native inter-session messaging, so there is no daemon, no tmux send-keys, and no
bracketed-paste injection to carry.

## Three layers

Each layer owns one job; delivery is independent of where an agent runs.

```
  registry   agents.db (SQLite/WAL)  — names, group, type, the sessionId binding
  host       hosts.py                — where claude runs: tmux | herdr | foreground
  transport  transport.py            — deliver to a running agent's native socket
```

## Modules

| module | responsibility |
|---|---|
| `cli.py` | argparse + orchestration — the only module that spans all layers |
| `db.py` | SQLite registry + `select(group, type)`, the one slicer |
| `home.py` | per-agent home dir + agent types |
| `session.py` | bind a launched pid → its Claude `sessionId` |
| `hosts.py` | pluggable pty hosts: `Host` base + `Tmux`/`Herdr`/`Foreground` |
| `transport.py` | vendored native socket delivery (self-contained, stdlib-only) |
| `history.py` | Claude session discovery behind `find` |
| `migrate.py` | one-way cmux → agents migration |
| `types/<t>/AGENTS.md` | bundled agent-type templates (`default`, `ada`) |

Dependencies flow one way: `cli → {db, home, hosts, session, transport, history,
migrate}`, `home → db`; everything else is a leaf. No cycles.

## Primitives

- **Agent** — a registry row: `name, type, group, cwd, session binding, home`.
- **Group** — a set of agents (the `workspace` column; `group` is a SQL reserved
  word). Materialized at runtime as one tmux session. A *lead + its ADAs* is just
  a group whose members have types.
- **Type** — the kind (`ada`, `lead`, …); seeds the home `AGENTS.md`, drives slicing.
- **Home** — `~/.agents/<name>/`: `AGENTS.md` (doctrine) · `PROMPT.md` (task) ·
  `identity.md` (optional) · `cwd` · `last-session-id`.
- **Host** — where claude runs; one pluggable seam (`hosts.py`).
- **Transport** — native socket delivery, host-agnostic.
- **Selector** — `name | -g group | -t type`, the one way to target agents.
- **Session binding** — the `sessionId`, which makes resume correct.

## Binding by sessionId (not cwd-recency)

cmux guessed an agent's session as "newest `.jsonl` in this cwd," which another
session in the same cwd could steal (the misattribution bug). `agents` launches
`claude` as a child, reads `~/.claude/sessions/<pid>.json`, and takes that
record's `sessionId` directly. A resume passes `--resume <sessionId>`; the id
survives, so a resume needs no detection. (The tmux host `exec`s claude so the
pane pid *is* claude's.)

## Home directory — the handoff

`register` creates `~/.agents/<name>/` and seeds it; the caller drops in whatever
the agent needs before `up`:

- `AGENTS.md` — how the agent operates, seeded from an agent **type**; Claude Code
  auto-loads it because claude starts **in the home dir** by default.
- `PROMPT.md` — the task (`--prompt`, or written directly).
- `cwd` — where claude runs; the home dir unless `--cwd` points at a project.

On `up`, the injected nudge just names the agent and points at `PROMPT.md`; the
operating instructions arrive through `AGENTS.md` for free.

## Hosts

A host launches claude and returns its pid. `down` is host-agnostic: it SIGTERMs
the pid resolved from the sessionId, and the window/pane closes when claude exits.

- **tmux** (default) — detached. A group is a tmux session (`agents-<name>` when
  standalone, the group name when grouped), each agent a window. `attach`/`detach`
  map to `tmux attach` / `detach-client`.
- **herdr** — a detached herdr tab in a herdr workspace (needs a running herdr).
- **foreground** — claude attached to the current terminal; no group, no attach.

## Commands, by category

| category | commands | reads/acts on |
|---|---|---|
| **Query** | `ls` (registry), `find` (Claude session history) | — |
| **Provision** | `register`, `migrate` | home, type, tooling |
| **Manage** | `rm` | the registry row |
| **Communicate** | `send` | a running agent's socket |
| **Operate** | `up`, `down`, `attach`, `detach` | the live process |

`ls` and `find` are both reads but on different objects: `ls` lists agents you
manage; `find` searches every Claude session on the machine so you can adopt one
(`register --session-id`). `up`/`down`/`ls` all take the `name | -g | -t` selector.

## Launch mode

`up` runs `claude --dangerously-skip-permissions` so a freshly launched agent
clears the folder-trust and permission prompts and writes its sessionId without a
human keypress (otherwise `up` can't bind it). `AGENTS_CLAUDE_CMD` overrides the
whole command (tests point it at a fake claude).
