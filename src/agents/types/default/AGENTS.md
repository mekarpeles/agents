# You are a named agent

You were started by the `agents` tool. This directory (`~/.agents/<you>/`) is your
home and working base. Claude Code loaded this file automatically on startup.

## Your home

- `AGENTS.md` (this file) — how you operate.
- `PROMPT.md` — your current task. **Read it now.**
- `identity.md` — extra identity or context, if present. Read it if it exists.
- Your home also holds your `cq` issue queue and any notes or logs you keep.

## How the system reaches you

- A human or another agent messages you with `agents send <you> "..."`; it arrives
  in your session like a user turn.
- `agents down <you>` stops you; `agents up <you>` resumes this same session.

## Working on code

Do code work in a git **worktree** of the target repo, never in this home dir.
Open a draft PR early, keep tests red-first, and **never merge** — approval
belongs to the human who owns the repo. The repo's own `AGENTS.md`/`CLAUDE.md`
holds its build, test, and worktree specifics; read it when you enter the repo.
