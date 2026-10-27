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
                type            TEXT,
                role            TEXT,
                workspace       TEXT,
                cwd             TEXT,
                initial_prompt  TEXT,
                identity_path   TEXT,
                session_id      TEXT,
                registered_at   TEXT,
                started_at      TEXT,
                archived        INTEGER
            )
        """)
        # migrate older dbs that predate a column
        cols = {r[1] for r in c.execute("PRAGMA table_info(agents)")}
        if "type" not in cols:
            c.execute("ALTER TABLE agents ADD COLUMN type TEXT")
        if "archived" not in cols:
            c.execute("ALTER TABLE agents ADD COLUMN archived INTEGER")


def register(name, agent_type=None, role=None, workspace=None, cwd=None,
             initial_prompt=None, identity_path=None, session_id=None):
    """Register or update an agent (upsert on name). Does not launch anything.

    agent_type (ada, lead, …) is the queryable kind of agent; workspace is the
    group column. session_id, when given, binds the agent to an existing Claude
    session (adopt one agents did not start) — the registry equivalent of --resume.
    """
    init()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _conn() as c:
        c.execute("""
            INSERT INTO agents
                (name, type, role, workspace, cwd, initial_prompt, identity_path,
                 session_id, registered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                type=coalesce(excluded.type, agents.type),
                role=coalesce(excluded.role, agents.role),
                workspace=coalesce(excluded.workspace, agents.workspace),
                cwd=coalesce(excluded.cwd, agents.cwd),
                initial_prompt=coalesce(excluded.initial_prompt, agents.initial_prompt),
                identity_path=coalesce(excluded.identity_path, agents.identity_path),
                session_id=coalesce(excluded.session_id, agents.session_id)
        """, (name, agent_type, role, workspace, cwd, initial_prompt,
              identity_path, session_id, ts))


def set_session(name, session_id):
    """Bind an agent to its live Claude sessionId and stamp started_at."""
    init()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _conn() as c:
        c.execute("UPDATE agents SET session_id=?, started_at=? WHERE name=?",
                  (session_id, ts, name))


def set_archived(name, archived):
    """Mark an agent archived (soft-hidden from ls/selectors) or restore it."""
    init()
    with _conn() as c:
        c.execute("UPDATE agents SET archived=? WHERE name=?",
                  (1 if archived else 0, name))


def get(name):
    init()
    with _conn() as c:
        row = c.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None


def get_by_session(session_id):
    """The agent bound to a Claude sessionId, or None. The reverse of the
    binding, so a session can identify itself from $CLAUDE_CODE_SESSION_ID."""
    init()
    with _conn() as c:
        row = c.execute("SELECT * FROM agents WHERE session_id=?",
                        (session_id,)).fetchone()
        return dict(row) if row else None


def select(group=None, agent_type=None, include_archived=False):
    """Agents filtered by group and/or type; all if neither given.

    The one slicer behind `ls`, `up`, and `down`: a group is a set of agents, a
    type is a kind (ada, lead), and the two together are e.g. a group's ADAs.
    (`group` is the `workspace` column — `group` is a SQL reserved word.)

    Archived agents are excluded unless include_archived — so `ls`, group/type
    ops, and tab-completion all skip them by default.
    """
    init()
    where, params = [], []
    if group is not None:
        where.append("workspace=?")
        params.append(group)
    if agent_type is not None:
        where.append("type=?")
        params.append(agent_type)
    if not include_archived:
        where.append("(archived IS NULL OR archived=0)")
    sql = "SELECT * FROM agents"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY workspace IS NULL, workspace, type, name"
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def all_agents():
    return select()


def in_workspace(ws):
    return select(group=ws)


def remove(name):
    """De-register (home dir is preserved on disk)."""
    init()
    with _conn() as c:
        c.execute("DELETE FROM agents WHERE name=?", (name,))
