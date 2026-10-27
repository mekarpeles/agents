<p align="center">
  <img src="Ag.jpeg" alt="agents" width="220">
</p>

<h1 align="center">agents</h1>

`agents` is a system-level command line tool for managing AI agent sessions,
designed to be used by both agents and humans.

1. **Send message** to any running agent. A native socket API pipes a message
   directly into an agent's running session, ideal for nudging idle agents or
   wiring them into workflows.
2. **Find and resume past sessions** — search previous sessions and resume the
   one you want.
3. **Human-readable nicknames** — assign a friendly name to any agent.
4. **Groups** — gather related agents into a group and bring the whole set up
   together with `up --group <group>` (one tmux session, a window per agent).
5. **Persistent per-agent setup** — a durable home directory, skills, auth, and
   per-agent tooling for each agent.
6. **Remove** an agent when it's no longer needed (`rm` de-registers it; the home
   directory is kept).

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/mekarpeles/agents/main/install.sh | bash
```

Installs `agents` plus [cq](https://github.com/mekarpeles/cq) via pipx (macOS needs
[Homebrew](https://brew.sh)); re-run with `--upgrade` to update in place
(`… | bash -s -- --upgrade`). The installer first checks for
[Claude Code](https://claude.ai/code) and stops if it's missing — install and
authenticate `claude` first.

For an interactive install it then runs `agents configure` (permission mode, and
offers to install tmux, shell completion, and a short `ag` alias); a
non-interactive/agent install skips that — force it with `--configure`, or skip it
with `--no-configure`. State lives under `~/.agents/` (override with
`AGENTS_STATE_DIR`).

## Usage

The command is `agents` (`agents configure` can add a short `ag` alias, if that
name is free on your system):

```bash
agents register pam --group web --role "edge" --identity ./pam.md
agents register ada --cwd ~/Projects/ada --session-id <id>   # adopt an existing session
agents up pam                 # start in tmux (detached); resumes if already bound
agents up --group web         # bring up every agent in the 'web' group
agents up ada --host foreground   # or run attached to this terminal
agents attach pam             # attach to its tmux window (Ctrl-b d to detach)
agents send pam "status?"     # pipe a message into the running session
agents whoami                 # who am I? — an agent identifies itself (--json too)
agents find pam               # search past sessions to resume or adopt
agents ls                     # registered agents + status light (● up/down)
agents down pam               # stop it   (agents down --group web  stops the group)
agents rm pam                 # remove (archive): hidden from ls, kept (ls -a shows it)
agents configure              # permission mode, default host, tmux, tab-completion
```

Launched agents run in a configurable **permission mode** — the default,
`acceptEdits`, auto-accepts file edits but still gates riskier operations; their
workspace is pre-trusted so `up` can start them unattended. `agents configure`
(also run by the installer) sets the mode and the default host, and offers to
install tmux; `--mode bypassPermissions` is the explicit, un-gated opt-in.

`up` and `down` take an agent name or the `--group <group>` / `--type <type>`
selectors (short: `-g` / `-t`). `ls` shows a status light — **green** up,
**orange** down, **red** archived — hides archived agents unless you pass
`-a`/`--all`, and filters by `--group`/`--type`/`--running`/`--down`/`--archived`.
`attach`/`detach` take a name or `--group`. Hosts:
`tmux` (default), `herdr`, `foreground` (`--host`). Short aliases: `list` (`ls`),
`attach` (`a`), `detach` (`d`). An unknown command is rejected (it never becomes
an agent name), so `agents list` and `agents up --help` can't register junk agents.

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

Every Claude session knows its own id (`$CLAUDE_CODE_SESSION_ID`), and the registry
binds that id to an agent — so `agents whoami` identifies the session by the id it
already has (no injected name), resolving it to the registry row, home, and task.
It works for an adopted session too, not only ones `agents` launched; `--json` for
machine use, or `--session-id <id>` to look up another session.

## Groups & types

A **group** (`--group`, short `-g`) is a set of agents; a **type** (`--type`,
short `-t`) is a kind of agent (`lead`, `ada`, …). `ls`, `up`, and `down` take
both and intersect them — so a group with a `lead` and its `ada`s is managed as
one unit:

```bash
agents ls --type lead                    # every lead
agents ls --type ada                     # every ADA
agents ls --group payments               # the whole 'payments' group
agents ls --group payments --type ada    # that group's ADAs
agents up --group payments               # bring the group up together (lead + ADAs)
agents up --type lead                    # bring every lead up
agents down --group payments             # stop the group
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

## Shell completion

Tab-complete subcommands and your registered agent names (and `-g` groups, `-t`
types) — completion reads the live registry on each `<TAB>`. The installer and
`agents configure` offer to set this up; to do it yourself:

```bash
agents completion --install    # appends the source-line to ~/.zshrc or ~/.bashrc
```

Then `source` your rc (or open a new terminal) and `agents up <TAB>` offers the
agents you've registered. To wire it by hand instead, source the script directly:

```bash
source <(agents completion zsh)    # ~/.zshrc (needs compinit)
source <(agents completion bash)   # ~/.bashrc
```

## Tests

```bash
python3 -m pytest tests/ -q
```

See [docs/architecture.md](docs/architecture.md) for how it's built, and the
[issue tracker](https://github.com/mekarpeles/agents/issues) for what's planned.
