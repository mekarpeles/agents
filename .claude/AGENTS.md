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
export AGENTS_CLAUDE_CMD="python3 /path/to/a/fake_claude.py"   # not the real claude
agents register throwaway --type ada && agents up throwaway
```

## Conventions

- **Terminology is settled — use it:** a set of agents is a **group** (`-g`), never
  "team" or "workspace" (the SQL column is `workspace` only because `group` is a
  reserved word). The two read commands are **`ls`** (the registry) and **`find`**
  (Claude's session history). A kind of agent is a **type** (`-t`): `ada`, `lead`.
- **Agent types** live in `src/agents/types/<type>/AGENTS.md` (the doctrine an
  agent is seeded with). Distinct from a running agent's **`PROMPT.md`** (its task).
- **Work is tracked in GitHub issues**, not in markdown TODOs. If you find a plan
  written into a doc or comment, move it to an issue.

## Gotchas (learned the hard way)

- **`agents up` runs `claude --dangerously-skip-permissions`.** A supervising
  agent running under Claude Code auto-mode is *denied* from launching it
  ("Create Unsafe Agents") — run `agents up` from a human terminal. See the
  open issue about making this opt-in.
- **claudio is gone, vendored as `transport.py`.** Do not re-add a `claudio`
  dependency; the socket protocol is self-contained there.
- **Layout is src-layout:** the package is `src/agents/`, imported as `agents`.
- **The host only hosts.** Messaging is always the native socket, regardless of
  tmux/herdr/foreground; `down` is host-agnostic (SIGTERM the bound pid).
