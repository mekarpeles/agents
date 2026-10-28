"""agents — SQLite registry of named Claude Code agents.

A minimal fork of cmux's registry. One table, `agents`, is the persistent
catalog (survives up/down). It keeps the authored launch metadata plus the
durable binding to a Claude session:

    session_id  — the Claude Code sessionId this agent resumes. Binding by
                  sessionId (not pid, not "newest jsonl in cwd") is what makes
                  resume correct and avoids cmux's session-misattribution bug.
    team        — the name of the lead this agent reports to (a team is named
                  after its lead agent). An ada's team points at its lead; a lead
                  usually has no team. Single-valued — you report to one lead.
    workspace   — this agent's groups, comma-separated. Groups are many per agent
                  (circles/labels), unlike team. Stored in one column (named
                  `workspace` because `group` is a SQL reserved word).

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
                archived        INTEGER,
                team            TEXT
            )
        """)
        # migrate older dbs that predate a column
        cols = {r[1] for r in c.execute("PRAGMA table_info(agents)")}
        if "type" not in cols:
            c.execute("ALTER TABLE agents ADD COLUMN type TEXT")
        if "archived" not in cols:
            c.execute("ALTER TABLE agents ADD COLUMN archived INTEGER")
        if "team" not in cols:
            c.execute("ALTER TABLE agents ADD COLUMN team TEXT")


def _groups_csv(groups):
    """Normalize a groups value (list, or comma/space string, or None) to a
    comma-separated string, or None to leave it unchanged on update."""
    if groups is None:
        return None
    if isinstance(groups, str):
        groups = groups.replace(",", " ").split()
    seen, out = set(), []
    for g in groups:
        g = g.strip()
        if g and g not in seen:
            seen.add(g)
            out.append(g)
    return ",".join(out)


def groups_of(row):
    """The groups an agent belongs to, as a list (empty if none)."""
    csv = row.get("workspace") if isinstance(row, dict) else row
    return [g for g in (csv or "").split(",") if g]


def register(name, agent_type=None, role=None, workspace=None, cwd=None,
             initial_prompt=None, identity_path=None, session_id=None, team=None):
    """Register or update an agent (upsert on name). Does not launch anything.

    agent_type (ada, lead, …) is the queryable kind of agent; workspace holds the
    agent's groups (comma-separated, many per agent); team is the lead it reports
    to (one). session_id, when given, binds the agent to an existing Claude session
    (adopt one agents did not start) — the registry equivalent of --resume.
    """
    init()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    workspace = _groups_csv(workspace)
    with _conn() as c:
        c.execute("""
            INSERT INTO agents
                (name, type, role, workspace, cwd, initial_prompt, identity_path,
                 session_id, registered_at, team)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                type=coalesce(excluded.type, agents.type),
                role=coalesce(excluded.role, agents.role),
                workspace=coalesce(excluded.workspace, agents.workspace),
                cwd=coalesce(excluded.cwd, agents.cwd),
                initial_prompt=coalesce(excluded.initial_prompt, agents.initial_prompt),
                identity_path=coalesce(excluded.identity_path, agents.identity_path),
                session_id=coalesce(excluded.session_id, agents.session_id),
                team=coalesce(excluded.team, agents.team)
        """, (name, agent_type, role, workspace, cwd, initial_prompt,
              identity_path, session_id, ts, team))


def set_session(name, session_id):
    """Bind an agent to its live Claude sessionId and stamp started_at."""
    init()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _conn() as c:
        c.execute("UPDATE agents SET session_id=?, started_at=? WHERE name=?",
                  (session_id, ts, name))


def set_team(name, team):
    """Set (or clear, with team=None) the lead an agent reports to."""
    init()
    with _conn() as c:
        c.execute("UPDATE agents SET team=? WHERE name=?", (team, name))


def rename(old, new):
    """Rename an agent and keep the reporting tree intact: change its own name and
    repoint every report whose team was `old` to `new`. One transaction. The
    caller is responsible for the filesystem side (moving the home dir and any
    Claude transcript) and for ensuring `new` is free."""
    init()
    with _conn() as c:
        c.execute("UPDATE agents SET name=? WHERE name=?", (new, old))
        c.execute("UPDATE agents SET team=? WHERE team=?", (new, old))


def set_groups(name, groups):
    """Replace an agent's groups (a list or comma string; [] / '' clears them)."""
    init()
    csv = _groups_csv(groups) or ""
    with _conn() as c:
        c.execute("UPDATE agents SET workspace=? WHERE name=?", (csv, name))


def add_groups(name, *groups):
    """Add groups to an agent, keeping the ones it already has."""
    row = get(name)
    set_groups(name, groups_of(row) + list(groups) if row else list(groups))


def remove_groups(name, *groups):
    """Remove groups from an agent (leaving the rest)."""
    row = get(name)
    if row:
        drop = set(groups)
        set_groups(name, [g for g in groups_of(row) if g not in drop])


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


def select(group=None, agent_type=None, team=None, include_archived=False):
    """Agents filtered by group, type, and/or team; all if none given.

    The one slicer behind `ls`, `up`, and `down`. A type is a kind (ada, lead); a
    group is one of an agent's labels (membership, not equality — agents have
    many); a team is a lead plus the agents reporting to it, so `team=<lead>`
    matches the lead itself (name) and its reports (team column).

    Archived agents are excluded unless include_archived — so `ls`, group/type/team
    ops, and tab-completion all skip them by default.
    """
    init()
    where, params = [], []
    if agent_type is not None:
        where.append("type=?")
        params.append(agent_type)
    if team is not None:
        where.append("(team=? OR name=?)")  # the lead (name) + its reports (team)
        params += [team, team]
    if not include_archived:
        where.append("(archived IS NULL OR archived=0)")
    sql = "SELECT * FROM agents"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY team IS NULL, team, type, name"
    with _conn() as c:
        rows = [dict(r) for r in c.execute(sql, params).fetchall()]
    if group is not None:  # membership, not a column match — filter in Python
        rows = [r for r in rows if group in groups_of(r)]
    return rows


def all_groups():
    """Every group name in use across agents (for completion)."""
    gs = set()
    for a in all_agents():
        gs.update(groups_of(a))
    return sorted(gs)


def all_teams():
    """Every team (lead name) in use — leads by type, and any team referenced."""
    ts = set()
    for a in all_agents():
        if a.get("team"):
            ts.add(a["team"])
        if (a.get("type") or "") == "lead":
            ts.add(a["name"])
    return sorted(ts)


def all_agents():
    return select()


def in_workspace(ws):
    return select(group=ws)


def remove(name):
    """De-register (home dir is preserved on disk)."""
    init()
    with _conn() as c:
        c.execute("DELETE FROM agents WHERE name=?", (name,))
