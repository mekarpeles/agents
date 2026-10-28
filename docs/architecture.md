# agents — architecture

How the system is built. (Work to be done lives in **GitHub issues**, not here.)

`agents` registers, runs, and messages named Claude Code agents. It has **no
runtime dependencies** — pure Python, stdlib only — because Claude Code now does
native inter-session messaging, so there is no daemon, no tmux send-keys, and no
bracketed-paste injection to carry.

## Three layers

Each layer owns one job; delivery is independent of where an agent runs.

```
  registry   agents.db (SQLite/WAL)  — names, team, groups, type, archive, sessionId
  host       hosts.py                — where claude runs: tmux | herdr | foreground
  transport  transport.py            — deliver to a running agent's native socket
```

## Modules

| module | responsibility |
|---|---|
| `cli.py` | argparse + orchestration — the only module that spans all layers |
| `db.py` | SQLite registry + `select(group, type, team)`, the one slicer |
| `config.py` | persistent `~/.agents/config.json`: permission mode + default host |
| `home.py` | per-agent home dir + agent types |
| `trust.py` | pre-accept Claude's workspace-trust dialog for an agent's cwd |
| `completion.py` | zsh/bash tab-completion scripts (`completion`, hidden `_complete`) |
| `session.py` | bind a launched pid → its Claude `sessionId` |
| `hosts.py` | pluggable pty hosts: `Host` base + `Tmux`/`Herdr`/`Foreground` |
| `transport.py` | vendored native socket delivery (self-contained, stdlib-only) |
| `history.py` | Claude session discovery behind `find` |
| `migrate.py` | one-way cmux → agents migration |
| `types/<t>/AGENTS.md` | bundled agent-type templates (`default`, `ada`) |

Dependencies flow one way: `cli → {db, home, hosts, session, transport, history,
migrate}`, `home → db`; everything else is a leaf. No cycles.

## Everything is a session

The session id is the universal handle, the way a file descriptor is in Unix.
Registration just puts a friendly name and a home dir on top of a session; it is
never a gate. So `ls` lists every live session (named or not), and `up`/`down`/
`send` resolve any identifier shown there — a registered name, a live session's
display name, or a sessionId/prefix (`cli._resolve_target`, `cli._existing_session`)
— to its session. `up <loose-session>` adopts it into the registry and resumes it
rather than spawning a fresh agent.

## Primitives

- **Agent** — a registry row: `name, type, team, groups, cwd, session binding, home`.
- **Team** — a lead plus the agents reporting to it; the `team` column on a report
  holds its lead's name (a team is named after its lead). One team per agent. The
  team is the **colocation unit**: `up --team X` materializes it as one tmux
  session named X (the lead plus every report, a window each), and `ls -t` draws
  it as a tree. A *lead + its ADAs* is exactly a team.
- **Group** — a label an agent carries, zero or more (the `workspace` column,
  comma-separated; `group` is a SQL reserved word). Groups **select and filter**
  (`select(group=…)` tests membership, not equality); they do not colocate.
- **Type** — the kind (`ada`, `lead`, …); seeds the home `AGENTS.md`, drives slicing.
- **Home** — `~/.agents/<name>/`: `AGENTS.md` (doctrine) · `PROMPT.md` (task) ·
  `identity.md` (optional) · `cwd` · `last-session-id`.
- **Host** — where claude runs; one pluggable seam (`hosts.py`).
- **Transport** — native socket delivery, host-agnostic.
- **Selector** — `name | --team team | --group group | --type type` (short `-t` /
  `-g`), the one way to target a set of agents. The selectors intersect.
- **Session binding** — the `sessionId`, which makes resume correct *and* lets a
  session identify itself (`whoami`).
- **Archive** — `rm` soft-hides rather than deleting. For a registered agent it
  sets the `archived` flag (`select` skips archived rows, so `ls`/selectors/
  completion ignore them until `-a`); `rm --purge` deletes the row. For an
  *unregistered* session (any id/prefix `find` lists) it writes a tombstone to the
  `ignored_sessions` table — keyed by sessionId, the "everything is a session" dual
  of the flag — so `find`/`ls` hide it until `--archived`; `--restore` lifts it,
  adopting it (`up`) lifts it, and `rm --purge` also deletes its transcript (making
  it non-resumable). Claude exposes no per-session delete (only project-wide
  `claude purge`), and never prunes `history.jsonl`, so the tombstone is ours.

## Binding by sessionId (not cwd-recency)

cmux guessed an agent's session as "newest `.jsonl` in this cwd," which another
session in the same cwd could steal (the misattribution bug). `agents` launches
`claude` as a child, reads `~/.claude/sessions/<pid>.json`, and takes that
record's `sessionId` directly. A resume passes `--resume <sessionId>`; the id
survives, so a resume needs no detection. (The tmux host `exec`s claude so the
pane pid *is* claude's.)

The same binding runs in reverse for identity: every Claude session exports
`$CLAUDE_CODE_SESSION_ID` (equal to its bound sessionId), so `whoami` reads that
and reverse-looks-up the registry (`db.get_by_session`) — no injected agent name,
and it works for adopted sessions too.

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

- **tmux** (default) — detached. A **team** is a tmux session (`agents-<name>`
  when standalone, the team name for a member), each agent a window — `cli`
  passes the team as the host's session key, so the lead and its reports share
  one session (`_host_key`). Groups never key the host. `attach` finds the agent's
  **real window by its live pid** (`locate_pid`), the way `down` does — so it works
  wherever the agent actually runs even if its registry team now names a different
  session (a team-tagged agent stays in the standalone session it was launched into
  until a team `up`). Only when the agent runs outside tmux (foreground/adopted, no
  pane) does attach fall back to restarting it into a tmux window — refused while
  live unless `-f` (the shared restart gate). `detach` maps to `detach-client`.
- **herdr** — a detached herdr tab in a herdr workspace (needs a running herdr).
- **foreground** — claude attached to the current terminal; no team, no attach.

## Commands, by category

| category | commands | reads/acts on |
|---|---|---|
| **Query** | `ls` (registry), `find` (sessions), `whoami` (this session) | — |
| **Provision** | `register`, `migrate`, `configure` | home, type, tooling, config |
| **Organize** | `group` (add/rm labels), `team` (set/clear lead), `rename` | the registry row |
| **Manage** | `rm` (archive agent *or* session; `--restore` lifts; `--purge` deletes) | a registry row or a session tombstone |
| **Communicate** | `send` | a running agent's socket |
| **Operate** | `up`, `down`, `attach`, `detach` | the live process |

`ls` lists agents you manage **and** every live session that isn't one — it joins
the registry, the live sockets (`transport.list_sessions`), and history titles, so
anything `up` is visible and addressable (`send -s <id>`). It prints aligned
`NAME SESSION STATUS TYPE GROUPS DESC` columns with a colored status light, and
`ls -t` nests reports under their lead as a tree. `find` searches every Claude
session on the machine (live or not) so you can adopt one (`register
--session-id`); by default it matches session *metadata* (title, cwd, the prompts
you typed) off the cached index, `-a` makes it a *deep* search that greps full
transcript bodies, and `--archived` reveals sessions you tombstoned with `rm`.
`up` and `down` take the `name | --team | --group | --type`
selector; `ls` filters by `--group`/`--type`/`-t <team>` and by status
(`--running`/`--down`/`--archived`, archived hidden unless `-a`; those filters also
drop the unregistered sessions).

## Rename

`rename <old> <new>` is a whole-identity move, because an agent's name is embedded
in more than the registry key. It renames the row and **repoints every report**
whose `team` was `<old>` (one transaction, `db.rename`), moves the home dir, and
fixes the name-bearing columns (`identity_path`, and `cwd` *only when it was the
home dir*). The subtle part is Claude's transcript: Claude keys a session's project
dir purely off the cwd string, so a **default-cwd** agent (cwd == home dir) would
lose `--resume` when its home moves — `rename` moves the transcript to the new cwd
slug to keep it resumable. An agent whose cwd is a real project checkout is
name-independent: its cwd and transcript stay put, and the rename can't break
resume.

Renaming restarts nothing by itself, but a *live* agent's tmux session (`agents-<name>`)
and binding would be orphaned, so `rename` **refuses while the agent is up** unless
`-f`/`--force`, which stops it first (lossless — the sessionId resumes from disk).
This refuse-unless-force gate is shared with `attach`: moving a live out-of-tmux
agent into a tmux window also restarts it, so both go through
`_ensure_stopped_for_restart` (`ok` / `stopped` / `needs_force` / `stuck`).

## Who's registering — auto-lead

`register` resolves its caller the same way `whoami` does (`$CLAUDE_CODE_SESSION_ID`
→ `db.get_by_session`). When the caller is itself a registered agent, the new agent
`team`-defaults to the caller — a lead that spins up its own reports wires the tree
up without an extra `team add`. An explicit `--team` always wins, and an agent
re-registering itself never becomes its own lead.

## Launch mode & trust

A freshly launched agent has to clear two gates *without a human keypress* — the
workspace-trust dialog and the per-tool permission prompts — or it never writes a
sessionId and `up` can't bind it. `agents` clears each separately so the default
need not be the blunt one:

- **Trust** — `trust.py` pre-accepts the dialog by setting `hasTrustDialogAccepted`
  for the agent's cwd in `~/.claude.json` (exactly what clicking "trust" does), so
  *any* permission mode launches unattended.
- **Permission mode** — `config.launch_flags()` turns the configured mode into the
  claude flag. The default is **`acceptEdits`** (`--permission-mode acceptEdits`):
  auto-accept file edits, still gate the risky operations. **`bypassPermissions`**
  (`--dangerously-skip-permissions`, the YOLO opt-in) is explicit, never default.

`agents configure` writes the mode and host; `AGENTS_CLAUDE_CMD` still overrides the
whole command (tests point it at a fake claude).
