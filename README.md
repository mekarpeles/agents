<p align="center">
  <img src="Ag.jpeg" alt="agents" width="220">
</p>

<h1 align="center">agents</h1>

`agents` is a system-level command line tool, designed for both agents and
humans, for managing AI agents.

1. **Message any running agent** — a native socket API pipes a message directly
   into an agent's running session, ideal for nudging idle agents or wiring them
   into workflows.
2. **Find and resume past sessions** — search previous sessions and resume the
   one you want.
3. **Human-readable nicknames** — assign a friendly name to any agent.
4. **Groups** — gather related agents into a group and bring the whole set up
   together with `up -g <group>` (one tmux session, a window per agent).
5. **Persistent per-agent setup** — a durable home directory, skills, auth, and
   per-agent tooling for each agent.
6. **Remove** an agent when it's no longer needed (`rm` de-registers it; the home
   directory is kept).

## Install

Requires [Claude Code](https://claude.ai/code) installed and authenticated.

```bash
curl -fsSL https://raw.githubusercontent.com/mekarpeles/agents/main/install.sh | bash
```

Re-run with `--upgrade` to update in place (`… | bash -s -- --upgrade`). Installs
`agents` (and the `ag` shortcut) plus [cq](https://github.com/mekarpeles/cq) via
pipx; macOS needs [Homebrew](https://brew.sh). State lives under `~/.agents/`
(override with `AGENTS_STATE_DIR`).

## Usage

The command is `agents` (with `ag` as a shorthand alias):

```bash
agents register pam -g web --role "edge" --identity ./pam.md
agents register ada --cwd ~/Projects/ada --session-id <id>   # adopt an existing session
agents up pam                 # start in tmux (detached); resumes if already bound
agents up -g web              # bring up every agent in the 'web' group
agents up ada --host foreground   # or run attached to this terminal
agents attach pam             # attach to its tmux window (Ctrl-b d to detach)
agents send pam "status?"     # pipe a message into the running session
agents find pam               # search past sessions to resume or adopt
agents ls                     # registered agents + live status
agents down pam               # stop it   (agents down -g web  stops the group)
agents rm pam                 # de-register (home dir preserved)
```

`up`, `down`, and `ls` take an agent name or the `-g <group>` / `-t <type>`
selectors; `attach`/`detach` take a name or `-g`. Hosts: `tmux` (default),
`herdr`, `foreground` (`--host`). Aliases: `ls`/`list`, `find`/`search`,
`a`/`attach`, `d`/`detach`. An unknown command is rejected (it never becomes an
agent name), so `agents list` and `agents up --help` can't register junk agents.

## Agent home & types

`register` creates `~/.agents/<name>/` — the agent's base, and a drop-zone you
populate before `up`:

- **`AGENTS.md`** — how the agent operates. Seeded from an **agent type**
  (`--type ada`, `--type default`); claude starts in this dir, so Claude Code
  loads it automatically. Replace it to give the agent your own instructions.
- **`PROMPT.md`** — the agent's task (`--prompt "..."`, or write the file).
- **`identity.md`** — optional extra identity (`--identity file.md`).

Types are bundled in the repo under `src/agents/types/`. Claude runs in the home
dir by default (so `AGENTS.md` loads); pass `--cwd <repo>` to run it in a project
checkout instead.

## Groups & types

A **group** (`-g`) is a set of agents; a **type** (`-t`) is a kind of agent
(`lead`, `ada`, …). `ls`, `up`, and `down` take both and intersect them — so a
group with a `lead` and its `ada`s is managed as one unit:

```bash
agents ls -t lead                 # every lead
agents ls -t ada                  # every ADA
agents ls -g payments             # the whole 'payments' group
agents ls -g payments -t ada      # that group's ADAs
agents up -g payments             # bring the group up together (lead + ADAs)
agents up -t lead                 # bring every lead up
agents down -g payments           # stop the group
```

## Finding sessions

`agents find [query]` searches the sessions Claude Code has recorded on this
machine — matching on the AI-generated title, the working directory, and every
prompt — so you can pick one up again:

```
SESSION                               STATE     MSGS  CWD          TITLE
57b3775b-3839-4817-8c70-e4eabbae9c3f  running    260  Projects     Session discovery tool
dc33a929-b44e-448f-b1d5-adaa9cbd2681  idle       698  Projects     PAM agent restart
```

`STATE` is `running` (a live process is attached), `idle` (resumable transcript
on disk, no process), or `gone` (transcript cleaned up — shown only with
`--all`). `--json` emits one object per line; `-n` caps the count.

## Tests

```bash
python3 -m pytest tests/ -q
```

See [docs/architecture.md](docs/architecture.md) for how it's built, and the
[issue tracker](https://github.com/mekarpeles/agents/issues) for what's planned.
