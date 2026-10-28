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
4. **Teams & groups** — a **team** is a lead and the agents reporting to it;
   `up --team <lead>` brings the whole team up colocated in one tmux session (a
   window per member). **Groups** are labels an agent can carry several of
   (circles), for selecting and filtering (`--group <g>`).
5. **Persistent per-agent setup** — a durable home directory, skills, auth, and
   per-agent tooling for each agent.
6. **Remove / archive** an agent when it's no longer needed — `rm` hides it from
   `ls` but keeps it (`ls -a` to see it, `up`/`register` to restore);
   `rm --purge` deletes the registry row. The home directory is always kept.

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

Once installed, **`agents upgrade`** updates the CLI in place (clone + `pipx`
reinstall); `agents upgrade --testing <branch>` installs a branch instead of main.

## Usage

The command is `agents` (`agents configure` can add a short `ag` alias, if that
name is free on your system):

```bash
agents register pam --group web,edge --role "edge" --identity ./pam.md
agents register ada1 --type ada --team agents-lead --group web   # reports to a lead
agents register ada --cwd ~/Projects/ada --session-id <id>   # adopt an existing session
agents up pam                 # start in tmux (detached); resumes if already bound
agents up --team agents-lead  # bring up a team (lead + reports) in one tmux session
agents up --group web         # bring up every agent labelled 'web'
agents up ada --host foreground   # or run attached to this terminal
agents attach pam             # attach to its tmux window (Ctrl-b d to detach)
agents attach --team agents-lead  # attach to the team's shared session
agents send pam "status?"     # message a registered agent
agents send -s <session> "hi" # …or any live session by id (no registry needed)
agents group add pam infra    # add/remove group labels (pam may be in many)
agents team add ada1 agents-lead   # set (or `team rm ada1` to clear) its team
agents rename ada1 ada-solr   # rename (home, reports & transcript follow; -f if live)
agents whoami                 # who am I? — an agent identifies itself (--json too)
agents find pam               # search past sessions to resume or adopt
agents ls                     # every active session (named or not); aligned columns
agents ls -t                  # team/tree view: reports nested under their lead
agents down pam               # stop it   (agents down --team agents-lead stops the team)
agents rm pam                 # remove (archive): hidden from ls, kept (ls -a shows it)
agents configure              # permission mode, default host, tmux, tab-completion
agents upgrade                # update the agents CLI itself (from GitHub)
```

Launched agents run in a configurable **permission mode** — the default,
`acceptEdits`, auto-accepts file edits but still gates riskier operations; their
workspace is pre-trusted so `up` can start them unattended. `agents configure`
(also run by the installer) sets the mode and the default host, and offers to
install tmux; `--mode bypassPermissions` is the explicit, un-gated opt-in.

`up` and `down` take an agent name or a selector: `--team <lead>` (short `-t`), `--group
<group>` (short `-g`), or `--type <type>`. A **team** colocates — `up --team X`
brings the lead X and everyone reporting to it up in one tmux session; a **group**
is just a label for selecting (an agent can be in several). `ls` lists your
registered agents **and** every live Claude session that isn't one. **Everything is
a session:** `up`, `down`, and `send` all take a live (or past) session's displayed
name or id, not just a registered agent — `up <session>` resumes it (adopting it
into the registry), `down`/`send` act on it (`send -s <id>` targets by id), and all
three tab-complete those sessions. `ls` shows a status light — **green** up,
**orange** down, **red** archived — with aligned `NAME SESSION STATUS TYPE GROUPS
DESC` columns; `ls -t` switches to a **team tree** (reports nested under their
lead). It hides archived agents unless you pass `-a`/`--all`, and filters by
`--group`/`--type`/`-t <team>`/`--running`/`--down`/`--archived` (which also drop
the unregistered sessions). `attach`/`detach` take a name or `--team`; attaching a
live agent that's running *outside* tmux (foreground/adopted) restarts it in a tmux
window, so it refuses unless you pass `-f`/`--force` (the same gate `rename` uses —
`send` is the no-interrupt alternative). Hosts:
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

## Teams, groups & types

Three ways to organize and target agents, all of which `ls`/`up`/`down` accept
and intersect:

- a **team** (`--team`, short `-t`) is a lead plus the agents reporting to it —
  the colocation unit (one tmux session) and the reporting relationship `ls -t`
  draws as a tree. One team per agent. A team is named after its lead.
- a **group** (`--group`, short `-g`) is a label; an agent can carry several
  (comma-separated on `register`, or `agents group add|rm <name> <g>…`). Groups
  select and filter, they don't colocate.
- a **type** (`--type`) is a kind of agent (`lead`, `ada`, …).

When an **agent** registers another agent (identified by its own
`$CLAUDE_CODE_SESSION_ID`, the way `whoami` works), the new agent reports to it by
default — the caller becomes its lead — unless an explicit `--team` is given. So a
lead spinning up its own reports gets the team wired up for free.

```bash
agents ls -t                             # team tree: reports nested under leads
agents ls -t payments                    # just the 'payments' team's tree
agents ls --type lead                    # every lead
agents ls --group web                    # every agent labelled 'web'
agents up --team payments-lead           # bring a team up together (lead + reports)
agents up --group web                    # bring up everything labelled 'web'
agents up --type lead                    # bring every lead up
agents down --team payments-lead         # stop the team
agents team add ada-7 payments-lead      # ada-7 now reports to that lead
agents group add ada-7 web,search        # …and carries the web + search labels
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

Tab-complete subcommands and your registered agent names (and `--group` groups,
`--type` types) — completion reads the live registry on each `<TAB>`. The installer and
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
