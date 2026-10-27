# Working on the `agents` project

`agents` is a system-level CLI for managing Claude Code agents (register / run /
message them). This file is for whoever works **on** the tool. Start here, then:

- **README.md** — what it does, for users.
- **docs/architecture.md** — how it's built (layers, modules, primitives).
- **GitHub issues** (`gh issue list`) — what's left to do. **Plans and specs live
  in issues, never in docs or code comments.**

## Setup & tests

Pure Python, **no runtime dependencies**. Editable install + tests:

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q      # the whole suite must stay green
```

The tmux host test skips itself when `tmux` isn't installed; everything else is
hermetic (tmp state dirs, a fake Claude session — never touches `~/.agents` or
`~/.claude`).

## Smoke-testing safely

Never exercise commands against the real registry or a live agent. Isolate state,
the tmux server, and the Claude binary (mirrors cmux's hard-won discipline):

```bash
export AGENTS_STATE_DIR=$(mktemp -d) CLAUDE_SESSIONS_DIR=$(mktemp -d) TMUX_TMPDIR=$(mktemp -d)
export CLAUDE_CONFIG_PATH=$(mktemp)   # trust.py writes ~/.claude.json on `up` — isolate it!
export AGENTS_CLAUDE_CMD="python3 /path/to/a/fake_claude.py"   # not the real claude
agents register throwaway --type ada && agents up throwaway
```

`CLAUDE_CONFIG_PATH` matters: `up` calls `trust.py` to pre-accept the workspace
trust in `~/.claude.json`; without the override a smoke test rewrites your real
Claude config.

## Conventions

- **Terminology is settled — use it:** a set of agents is a **group** (`--group`,
  short `-g`), never "team" or "workspace" (the SQL column is `workspace` only
  because `group` is a reserved word). A kind of agent is a **type** (`--type`,
  short `-t`): `ada`, `lead`. Lead with the long form in docs/help — the bare
  `-g`/`-t` confused the maintainer. The reads: **`ls`** (registry, with status
  lights + archive), **`find`** (Claude session history), **`whoami`** (this
  session, by `$CLAUDE_CODE_SESSION_ID`).
- **Agent types** live in `src/agents/types/<type>/AGENTS.md` (the doctrine an
  agent is seeded with). Distinct from a running agent's **`PROMPT.md`** (its task).
- **Work is tracked in GitHub issues**, not in markdown TODOs. If you find a plan
  written into a doc or comment, move it to an issue.

## Gotchas (learned the hard way)

- **Permission mode is configurable; the default is gentle.** `up` launches
  `claude --permission-mode <mode>` (default `acceptEdits`) and pre-trusts the cwd
  via `trust.py` so it still starts unattended. Only `--mode bypassPermissions`
  (the `--dangerously-skip-permissions` opt-in) is un-gated — and a *supervising*
  agent under Claude Code auto-mode is still denied from launching that one
  ("Create Unsafe Agents"), so start bypass agents from a human terminal.
- **`rm` archives, it doesn't delete.** It sets the `archived` flag (hidden from
  `ls`; `up`/`register` restore it); `rm --purge` is the hard delete. `db.select`
  excludes archived by default, so selectors and completion skip them too.
- **`whoami` keys on `$CLAUDE_CODE_SESSION_ID`, not `$AGENTS_AGENT_NAME`.** Every
  Claude session exports its id and it equals the registry's bound `session_id`;
  `db.get_by_session` reverse-looks-it-up. Don't reintroduce name-via-env identity.
- **claudio is gone, vendored as `transport.py`.** Do not re-add a `claudio`
  dependency; the socket protocol is self-contained there.
- **Layout is src-layout:** the package is `src/agents/`, imported as `agents`.
- **The host only hosts.** Messaging is always the native socket, regardless of
  tmux/herdr/foreground; `down` is host-agnostic (SIGTERM the bound pid).
