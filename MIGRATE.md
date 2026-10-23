# Migrating from cmux to agents

## Why nothing breaks the moment claudio v2 lands

cmux depends on claudio's **old** API (`claudio.run`, the socket inbox, pairing).
claudio v2 removes all of that, so installing v2 *where cmux can import it* breaks
cmux. The migration avoids that by keeping the two on separate claudio lines:

| | installs claudio from | pipx venv |
|---|---|---|
| **cmux** | `claudio.git` (**main**) | the existing shared `cmux` venv |
| **agents** | `claudio.git@**v2**` | its own `agents` venv |

Consequences:

- **PR #12 merges into `v2`, not `main`.** cmux installs claudio `main`, so the
  merge does **not** touch the running fleet. cmux keeps working.
- cmux breaks only if you (a) merge `v2` → `main`, or (b) force-reinstall claudio
  v2 into cmux's shared venv. **Do neither until the fleet has migrated.**
- `agents` and `cmux` run side by side during the transition. Run any given agent
  under **one** of them at a time (never both — two processes resuming the same
  session is the hazard).

## The migration command

`agents migrate` reads cmux's SQLite registry (`~/.cmux/agents.db`) and per-agent
home dirs and mirrors them into `~/.agents`. It is **read-only against `~/.cmux`** —
it never edits or deletes cmux state, so it is safe to run with the fleet up.

```bash
agents migrate                 # dry-run: print the plan, change nothing
agents migrate --apply         # execute: copy homes + populate ~/.agents/agents.db
agents migrate --apply pam ada # migrate only named agents (one at a time)
agents migrate --apply --link  # symlink homes instead of copying (single source)
```

What maps across:

| cmux | agents |
|---|---|
| `agents.db` row (name, role, workspace, initial_prompt) | `agents.db` row |
| `~/.cmux/<name>/cwd` | `cwd` |
| `~/.cmux/<name>/last-session-id` | `session_id` (the durable binding) |
| `~/.cmux/<name>/{identity.md, initial-prompt.md, .cq/}` | copied into `~/.agents/<name>/` |

Dropped on purpose: `no_inject` / `unblock` / `allowed_tools` (permission
machinery — obsolete under Claude auto-mode) and all tmux/daemon/socket
bookkeeping (ephemeral). `workflow_path` is reported but not migrated (agents has
no workflow-injection step yet).

## Watch for: shared session_ids

The dry-run flags any `session_id` bound to **more than one agent**
(`SHARED session_id xN`). That is the cmux session-misattribution bug frozen into
the data — several agents `--resume` the same Claude session. Migration mirrors
cmux faithfully, so the collision carries over. Resolve each before relying on it:
find the real owner (`grep -l "You are <NAME>" ~/.claude/projects/*/*.jsonl`),
keep that binding, and clear the others (`agents` won't guess which is right).

## Suggested rollout

1. `agents migrate` (dry-run) — review the plan; note `no session_id` and
   `SHARED session_id` agents.
2. Repair shared session_ids in `~/.cmux` first (so the mirror is clean).
3. `agents migrate --apply` for a pilot agent; `agents up <name>` under agents;
   confirm `agents send` reaches it. Take it `down` in cmux first.
4. Roll through the fleet by attrition. cmux stays installed until the last agent
   moves.
5. Only then retire cmux and (optionally) merge claudio `v2` → `main`.
