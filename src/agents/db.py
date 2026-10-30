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

import json
import os
import sqlite3
import time

STATE_DIR = os.environ.get("AGENTS_STATE_DIR", os.path.expanduser("~/.agents"))
DB_PATH = os.path.join(STATE_DIR, "agents.db")

# --- schema versioning -----------------------------------------------------
#
# The db carries its schema version in SQLite's `PRAGMA user_version`. The
# additive block in init() (CREATE TABLE + guarded ADD COLUMN) defines the
# BASELINE schema — v1 — and is idempotent, so it brings any db (fresh, or an old
# pre-versioning one that's still at user_version 0) up to the baseline. From then
# on, EVERY schema change is a numbered migration in MIGRATIONS (v2, v3, …): a
# function run exactly once, in order, that takes the open connection and may do
# anything (non-additive changes, data backfills — not just ADD COLUMN). init()
# applies pending migrations on any db access (so a resumed agent self-heals), and
# `agents upgrade` runs them explicitly against the freshly installed code and
# reports the result. Migrations must be idempotent-safe and fast.
#
# To add one: write migrate_vN(c), register it as MIGRATIONS[N] = migrate_vN.
BASELINE_VERSION = 1
MIGRATIONS = {}  # {version:int -> fn(conn)}; populated as the schema evolves


def _schema_target():
    return max([BASELINE_VERSION, *MIGRATIONS])


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
        # Migrations are ADDITIVE ONLY: add a column when it's missing, never drop
        # or rewrite one. That's what keeps an upgrade non-destructive — computed /
        # cached columns (recap, meta, session_id) survive because init() only ever
        # grows the schema. `agents upgrade` also snapshots first, as a safety net.
        cols = {r[1] for r in c.execute("PRAGMA table_info(agents)")}
        if "type" not in cols:
            c.execute("ALTER TABLE agents ADD COLUMN type TEXT")
        if "archived" not in cols:
            c.execute("ALTER TABLE agents ADD COLUMN archived INTEGER")
        if "team" not in cols:
            c.execute("ALTER TABLE agents ADD COLUMN team TEXT")
        if "meta" not in cols:
            # Structured metadata (a JSON object) — queryable keys like pr, issue,
            # project that resolve agent <-> issue <-> PR without encoding it in the
            # name. `project` lives here, not on `group` (group stays for labels).
            c.execute("ALTER TABLE agents ADD COLUMN meta TEXT")
        if "recap" not in cols:
            # A short, agent-set status line ("where we are") plus when it was set.
            # Opt-in and agent-driven (via `agents checkin`), never computed — so it
            # costs no tokens and only ever says what an agent chose to record.
            c.execute("ALTER TABLE agents ADD COLUMN recap TEXT")
            c.execute("ALTER TABLE agents ADD COLUMN recap_at TEXT")
        # Tombstones for *unregistered* Claude sessions the user wants hidden from
        # find/ls. Keyed by sessionId (not a name) because these sessions have no
        # agent row — the "everything is a session" dual of the archived flag.
        c.execute("""
            CREATE TABLE IF NOT EXISTS ignored_sessions (
                session_id   TEXT PRIMARY KEY,
                reason       TEXT,
                archived_at  TEXT
            )
        """)
        _apply_migrations(c)


def _apply_migrations(c):
    """Bring the schema up to date: adopt the additive baseline as v1, then run any
    numbered migrations past the db's current user_version, in order, bumping the
    version after each. Idempotent — already-applied versions are skipped."""
    v = c.execute("PRAGMA user_version").fetchone()[0]
    if v < BASELINE_VERSION:
        # Fresh db or a pre-versioning one: the additive block above already made it
        # the baseline schema, so stamp it as v1 without re-running those steps.
        c.execute(f"PRAGMA user_version = {BASELINE_VERSION}")
        v = BASELINE_VERSION
    for target in sorted(MIGRATIONS):
        if target > v:
            MIGRATIONS[target](c)
            c.execute(f"PRAGMA user_version = {target}")  # constant int, not user data
            v = target


def _raw_version():
    """The db's stored schema version, WITHOUT running migrations (for before/after
    reporting). 0 if the db doesn't exist yet."""
    if not os.path.exists(DB_PATH):
        return 0
    with _conn() as c:
        return c.execute("PRAGMA user_version").fetchone()[0]


def schema_version():
    """The current schema version after ensuring the db is migrated."""
    init()
    return _raw_version()


def migrate_now():
    """Apply any pending schema migrations and return (old_version, new_version)."""
    old = _raw_version()
    init()
    return old, _raw_version()


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


def meta_of(row):
    """An agent's structured metadata as a dict (empty if none or unparseable)."""
    raw = row.get("meta") if isinstance(row, dict) else row
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def _meta_json(meta):
    """Serialize a metadata dict to a JSON string, or None to leave it unchanged
    on an upsert (coalesce semantics, like the other optional columns)."""
    if meta is None:
        return None
    return json.dumps(meta) if meta else None


def register(name, agent_type=None, role=None, workspace=None, cwd=None,
             initial_prompt=None, identity_path=None, session_id=None, team=None,
             meta=None):
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
                 session_id, registered_at, team, meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                type=coalesce(excluded.type, agents.type),
                role=coalesce(excluded.role, agents.role),
                workspace=coalesce(excluded.workspace, agents.workspace),
                cwd=coalesce(excluded.cwd, agents.cwd),
                initial_prompt=coalesce(excluded.initial_prompt, agents.initial_prompt),
                identity_path=coalesce(excluded.identity_path, agents.identity_path),
                session_id=coalesce(excluded.session_id, agents.session_id),
                team=coalesce(excluded.team, agents.team),
                meta=coalesce(excluded.meta, agents.meta)
        """, (name, agent_type, role, workspace, cwd, initial_prompt,
              identity_path, session_id, ts, team, _meta_json(meta)))


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


def set_recap(name, text):
    """Record an agent's latest recap line and stamp when. Returns the timestamp."""
    init()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _conn() as c:
        c.execute("UPDATE agents SET recap=?, recap_at=? WHERE name=?",
                  (text, ts, name))
    return ts


def set_meta(name, updates):
    """Merge key->value pairs into an agent's metadata. A value of None drops that
    key. Read-modify-write of the JSON blob, so it preserves keys not mentioned."""
    init()
    row = get(name)
    cur = meta_of(row) if row else {}
    for k, v in updates.items():
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    with _conn() as c:
        c.execute("UPDATE agents SET meta=? WHERE name=?",
                  (json.dumps(cur) if cur else None, name))


def remove_meta(name, keys):
    """Drop the named keys from an agent's metadata."""
    set_meta(name, {k: None for k in keys})


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


def _meta_match(row, filters):
    """True if an agent's metadata satisfies every (key, value) filter. value None
    means "key is present with any value"; otherwise it's a stringified equality
    (so pr=14212 matches whether stored as 14212 or \"14212\")."""
    m = meta_of(row)
    for k, v in filters:
        if k not in m:
            return False
        if v is None:
            continue                       # bare key -> existence only
        cur = m.get(k)
        if isinstance(cur, list):          # list value -> membership (e.g. epics)
            if str(v) not in [str(x) for x in cur]:
                return False
        elif str(cur) != str(v):           # scalar -> stringified equality
            return False
    return True


def select(group=None, agent_type=None, team=None, include_archived=False,
           meta=None):
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
    if meta:  # list of (key, value|None) — JSON blob, so filter in Python too
        rows = [r for r in rows if _meta_match(r, meta)]
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


def ignore_session(session_id, reason=None):
    """Tombstone an unregistered session so find/ls hide it. Reversible with
    unignore_session; idempotent (a repeat keeps the first reason unless a new one
    is given)."""
    init()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _conn() as c:
        c.execute("""
            INSERT INTO ignored_sessions (session_id, reason, archived_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                reason=coalesce(excluded.reason, ignored_sessions.reason)
        """, (session_id, reason, ts))


def unignore_session(session_id):
    """Lift a session tombstone (un-archive). Returns True if one was present."""
    init()
    with _conn() as c:
        cur = c.execute("DELETE FROM ignored_sessions WHERE session_id=?",
                        (session_id,))
        return cur.rowcount > 0


def ignored_sessions():
    """The set of tombstoned (archived) sessionIds."""
    init()
    with _conn() as c:
        return {r[0] for r in c.execute("SELECT session_id FROM ignored_sessions")}


# --- snapshot / reindex ----------------------------------------------------
#
# SQLite (WAL) is the live registry, but a file can corrupt. So the whole table
# is mirrored to one plain-JSON file, `agents-snapshot.json`, next to the db. It's
# refreshed lazily — on any command, if the snapshot is missing or over an hour
# old (so the common case costs nothing) — and `reindex` can rebuild the db from
# it. One file, not per-agent: simpler, and the single writer is this process, so
# there's no cross-file race. The snapshot is at most ~an hour stale by design.

SNAPSHOT_PATH = os.path.join(STATE_DIR, "agents-snapshot.json")
SNAPSHOT_MAX_AGE = 3600  # seconds


def _all_rows():
    """Every agent row, archived included — the durable catalog, for snapshotting."""
    init()
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM agents")]


def _ignored_rows():
    init()
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM ignored_sessions")]


def snapshot(path=None):
    """Atomically write the whole registry to a JSON file. Returns its path."""
    path = path or SNAPSHOT_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "agents": _all_rows(),
        "ignored_sessions": _ignored_rows(),
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)  # atomic — never a half-written backup
    return path


def maybe_snapshot(path=None, max_age=SNAPSHOT_MAX_AGE):
    """Refresh the snapshot if it's missing or older than max_age. Best-effort:
    any failure is swallowed (a backup must never break a command). Returns True if
    it wrote one."""
    path = path or SNAPSHOT_PATH
    try:
        fresh = (os.path.exists(path)
                 and (time.time() - os.path.getmtime(path)) < max_age)
        if fresh:
            return False
        snapshot(path)
        return True
    except OSError:
        return False


def _upsert_full(c, row):
    """Insert or replace one agent row verbatim (preserving its timestamps) — the
    reindex primitive, distinct from register() which coalesces and re-stamps."""
    cols = ["name", "type", "role", "workspace", "cwd", "initial_prompt",
            "identity_path", "session_id", "registered_at", "started_at",
            "archived", "team", "meta", "recap", "recap_at"]
    vals = [row.get(k) for k in cols]
    placeholders = ",".join("?" * len(cols))
    c.execute(f"INSERT OR REPLACE INTO agents ({','.join(cols)}) "
              f"VALUES ({placeholders})", vals)


def reindex_from_snapshot(path=None):
    """Rebuild the agents table from the JSON snapshot (the non-sqlite source of
    truth). Restores every agent row and the session tombstones verbatim. Returns
    (agents_restored, ignored_restored). Raises if the snapshot is absent."""
    path = path or SNAPSHOT_PATH
    with open(path) as f:
        data = json.load(f)
    init()
    agents = data.get("agents", [])
    ignored = data.get("ignored_sessions", [])
    with _conn() as c:
        for row in agents:
            _upsert_full(c, row)
        for ig in ignored:
            c.execute("INSERT OR REPLACE INTO ignored_sessions "
                      "(session_id, reason, archived_at) VALUES (?, ?, ?)",
                      (ig.get("session_id"), ig.get("reason"), ig.get("archived_at")))
    return len(agents), len(ignored)
