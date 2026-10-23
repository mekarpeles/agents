"""agents — SQLite registry of named Claude Code agents.

A minimal fork of cmux's registry. One table, `agents`, is the persistent
catalog (survives up/down). It keeps the authored launch metadata plus the
durable binding to a Claude session:

    session_id  — the Claude Code sessionId this agent resumes. Binding by
                  sessionId (not pid, not "newest jsonl in cwd") is what makes
                  resume correct and avoids cmux's session-misattribution bug.

SQLite (WAL) because many agents may read/write concurrently — the exact race a
JSON read-modify-write registry loses (cmux's sessions.json wipe incidents).
"""

import os
import sqlite3
import time

STATE_DIR = os.environ.get("AGENTS_STATE_DIR", os.path.expanduser("~/.agents"))
DB_PATH = os.path.join(STATE_DIR, "agents.db")


def _conn():
    os.makedirs(STATE_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                name            TEXT PRIMARY KEY,
                role            TEXT,
                workspace       TEXT,
                cwd             TEXT,
                initial_prompt  TEXT,
                identity_path   TEXT,
                session_id      TEXT,
                registered_at   TEXT,
                started_at      TEXT
            )
        """)


def register(name, role=None, workspace=None, cwd=None, initial_prompt=None,
             identity_path=None):
    """Register or update an agent (upsert on name). Does not launch anything."""
    init()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _conn() as c:
        c.execute("""
            INSERT INTO agents
                (name, role, workspace, cwd, initial_prompt, identity_path,
                 registered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                role=coalesce(excluded.role, agents.role),
                workspace=coalesce(excluded.workspace, agents.workspace),
                cwd=coalesce(excluded.cwd, agents.cwd),
                initial_prompt=coalesce(excluded.initial_prompt, agents.initial_prompt),
                identity_path=coalesce(excluded.identity_path, agents.identity_path)
        """, (name, role, workspace, cwd, initial_prompt, identity_path, ts))


def set_session(name, session_id):
    """Bind an agent to its live Claude sessionId and stamp started_at."""
    init()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _conn() as c:
        c.execute("UPDATE agents SET session_id=?, started_at=? WHERE name=?",
                  (session_id, ts, name))


def get(name):
    init()
    with _conn() as c:
        row = c.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None


def all_agents():
    init()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM agents ORDER BY workspace IS NULL, workspace, name"
        ).fetchall()
        return [dict(r) for r in rows]


def remove(name):
    """De-register (home dir is preserved on disk, like cmux rm)."""
    init()
    with _conn() as c:
        c.execute("DELETE FROM agents WHERE name=?", (name,))
