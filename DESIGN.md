# agents design

**Status:** prototype, 2026-10-23.

## Goal

Keep the useful core of cmux — a durable catalog of named agents, a per-agent
home directory, and the ability to spin a Claude session up/down/resume — while
removing the entire machinery cmux only carried in order to *deliver messages*:
the per-agent claudio daemon, tmux, bracketed-paste injection, the Enter-retry
"submit" loop, and tmux-cursor idle detection.

That machinery is obsolete because Claude Code now has **native inter-session
messaging**: every session publishes its own socket and delivers queued messages
to itself when idle. `agents` delivers through `claudio.native` and never touches
a terminal to send.

## Layers

```
   agents        registry (agents.db) + home dirs + up/down + sessionId binding
      |
      v
   claudio.native   resolve a session, write the native wire protocol
      |
      v
   Claude Code   ~/.claude/sessions/<pid>.json + /tmp/cc-socks/<pid>.sock
```

`agents` depends on `claudio`; `claudio` depends on neither. (This mirrors the
PAM invariant that the lower layer must not import the higher one.)

## The three things agents still owns

1. **Registry** — `agents.db` (SQLite/WAL). One `agents` table: authored metadata
   (`role`, `workspace`, `cwd`, `initial_prompt`, `identity_path`) plus the
   durable `session_id` binding. SQLite because many
   agents write concurrently — the race cmux's JSON `sessions.json` lost twice.

2. **Home directory** — `~/.agents/<name>/`, same files as `~/.cmux/<name>/`
   (`identity.md`, `initial-prompt.md`, `cwd`, `last-session-id`). Durable agent
   facts only; the old ephemeral socket/daemon/tmux files are gone.

3. **Up / down / resume** — launch `claude` (fresh, or `--resume <session_id>`),
   bind it, inject the startup prompt natively, and on `down` SIGTERM the live
   pid resolved from the bound sessionId.

## Binding by sessionId (not cwd-recency)

cmux detects an agent's session as "the newest `.jsonl` modified in this cwd
since a pre-launch snapshot." When another session runs in the same cwd it steals
the binding (the misattribution bug). `agents` launches `claude` as a child, so
it **knows the child pid**, reads `~/.claude/sessions/<pid>.json`, and takes that
record's `sessionId` directly. Resume uses `--resume <sessionId>`, and the new
record under the new pid carries the same sessionId. No guessing.

## The terminal host is now just a pty host

Delivery no longer needs the terminal, so the host is decoupled and pluggable
(`agents_lib/hosts.py`):

- **foreground** (default) — `claude` attached to the current terminal; the CLI
  binds the sessionId in a background thread and waits. Good for interactive use.
- **herdr** — a new herdr pane holds the pty; detached. (Mek already runs herdr,
  so this is the natural replacement for tmux. Experimental: pane→pid binding is
  best-effort.)

tmux is no longer required anywhere. Adding a `tmux` host later is trivial and
isolated to this module.

## Not built yet (prototype scope)

- herdr host pid-binding is best-effort (reads the pane shell pid; falls back to
  scanning the sessions dir). A clean path is for the host to report the launched
  claude pid directly.
- `down` is a single SIGTERM; no graceful-drain or orphan sweep yet.
- No workspace bring-up-all, clone, or import-sessions commands (cmux parity work).
- Migration of the 38 live agents: copy `~/.cmux/<name>/` → `~/.agents/<name>/`
  and re-key the registry; by-attrition, never moving a running agent's dir.
