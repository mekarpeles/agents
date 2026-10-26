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
4. **Groups** *(coming soon)* — gather related agents into a group and spin them
   up together with tmux or herdr.
5. **Persistent per-agent setup** — a durable home directory, skills, auth, and
   per-agent tooling for each agent.
6. **Archive or remove** agents when they are no longer needed.

The command is `agents` (with `ag` as a shorthand alias):

```bash
agents register pam --role "registry owner" --workspace pam --identity ./pam.md
agents register ada --cwd ~/Projects/ada --session-id <id>   # adopt an existing session
agents up pam                 # launch attached to this terminal (resumes if bound)
agents up pam --host herdr    # launch detached in a herdr pane
agents send pam "status?"     # pipe a message into the running session
agents find pam               # search past sessions to resume or adopt
agents ls                     # registered agents + live status
agents list                   # alias of ls
agents down pam               # stop the running session
agents rm pam                 # de-register (home dir preserved)
agents help                   # same as `agents --help`; bare `agents` also prints help
```

`list`/`ls`, `search`/`find`, and `help`/`--help` are aliases, and an unknown
command is rejected (it never becomes an agent name), so `agents list` and
`agents up --help` can't silently register junk agents named `list` / `--help`.

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

## Install

```bash
./install.sh         # installs `agents` (and its `ag` shortcut)
# or, from a clone, for development:
pip install -e .
```

State lives under `~/.agents/` (override with `AGENTS_STATE_DIR`).

## Roadmap

The registry is one table today (agents). The vision is a small graph on top of it:

- **Workspaces** — a table grouping one or more agents so you can bring a whole
  set up together and `agents attach <workspace>` into it (tmux-backed).
- **Roles** — a graph of edges over the agents table (`ada reports-to pam`), so
  coordination and escalation have a durable shape, not an ad-hoc convention.
- **Name resolution** — one resolver across the identifiers that different
  callers need: `session_id` (durable key), the harness name the TUI uses, your
  logical name, and the live socket/pid (resolved on demand, never stored stale).

These are experimental and not built yet; the near-term focus is making the
basics above rock-solid.

## Tests

```bash
python3 -m pytest tests/ -q
```

See [DESIGN.md](DESIGN.md) for the architecture.
