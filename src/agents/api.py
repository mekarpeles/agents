"""agents — the Python library surface.

`pip install agents` gives you both the `agents` CLI and this importable API, so a
program can drive the registry directly instead of shelling out:

    import agents
    agents.register("tess", type="ada", team="saul", meta={"pr": "14212"})
    for a in agents.ls(group="project:ol"):
        print(a["name"], a["status"], a["meta"])
    agents.checkin("tess", "builder patched")
    agents.send("saul", "tess is up")

Every function returns plain data (dicts / lists / scalars) and never prints — the
CLI's cmd_* wrap these with argument parsing and output. This layer covers the
registry, metadata/recap, messaging, and identity/resolution; launching and
attaching terminals stay in the CLI (they drive a pty host), so for now use the
`agents up` / `agents attach` commands for those.
"""

import os

from agents import db, home, session
from agents import transport as native


def _session_for(name):
    """The sessionId bound to a registry name (row binding, else the home file)."""
    row = db.get(name)
    return (row.get("session_id") if row else None) or home.read_session_id(name)


def register(name, *, type=None, team=None, groups=None, cwd=None, role=None,
             desc=None, prompt=None, identity=None, session_id=None, meta=None):
    """Register (or update) an agent and seed its home. Returns the stored row."""
    db.register(name, agent_type=type, role=role or desc, workspace=groups,
                cwd=cwd, initial_prompt=prompt, identity_path=identity,
                session_id=session_id, team=team, meta=meta)
    db.set_archived(name, False)
    home.ensure_home(name, cwd=cwd, agent_type=type or "default",
                     identity_path=identity, initial_prompt=prompt,
                     session_id=session_id)
    return get(name)


def unregister(name, *, purge=False):
    """Archive an agent (hidden from ls, restorable), or hard-delete with purge."""
    if purge:
        db.remove(name)
    else:
        db.set_archived(name, True)


def get(name):
    """One agent's row as a dict (with parsed groups + meta), or None."""
    row = db.get(name)
    if not row:
        return None
    return {**row, "groups": db.groups_of(row), "meta": db.meta_of(row)}


def ls(*, group=None, type=None, team=None, meta=None, include_archived=False):
    """Registered agents matching the filters, each with live `status` and parsed
    groups/meta. `meta` is a list of (key, value|None) filters (value None = the
    key exists; a list-valued key matches by membership)."""
    live = {r.get("sessionId") for r in native.list_sessions()}
    out = []
    for a in db.select(group=group, agent_type=type, team=team,
                       include_archived=include_archived, meta=meta):
        sid = a.get("session_id")
        status = ("archived" if a.get("archived")
                  else "up" if sid in live else "down")
        out.append({**a, "groups": db.groups_of(a), "meta": db.meta_of(a),
                    "status": status})
    return out


def whoami(session_id=None):
    """The agent bound to a sessionId (default: $CLAUDE_CODE_SESSION_ID), or None."""
    sid = session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        return None
    row = db.get_by_session(sid)
    return get(row["name"]) if row else None


def whois(name):
    """Resolve a registry name to its live Claude-land SendMessage address, as data
    (the bridge for using SendMessage directly). None if the agent isn't live."""
    sid = _session_for(name)
    if not sid:
        return None
    try:
        rec = native.resolve(sid)
    except native.SessionNotFound:
        return None
    if not rec:
        return None
    sock = rec.get("messagingSocketPath") or ""
    return {
        "name": name, "session_id": rec.get("sessionId"),
        "claude_name": rec.get("name"), "former_names": rec.get("formerNames") or [],
        "socket": sock, "sendmessage_to": f"uds:{sock}" if sock else rec.get("name"),
        "pid": rec.get("pid"), "status": rec.get("status"),
        "meta": db.meta_of(db.get(name) or {}),
    }


def send(name, message, *, sender="agents"):
    """Deliver a message to an agent over its native socket. Raises LookupError if
    the agent has no bound session (never started / unbound)."""
    sid = _session_for(name)
    if not sid:
        raise LookupError(f"'{name}' has no bound session")
    native.send(sid, message, sender=sender)


def checkin(name, text):
    """Record an agent's recap line (stamped + journaled). Returns the timestamp."""
    ts = db.set_recap(name, text)
    home.append_recap(name, text, ts)
    return ts


def recap_history(name, limit=None):
    """The agent's recap journal as a list of (timestamp, text), oldest first."""
    return home.recap_log(name, limit=limit)


def meta_get(name):
    """An agent's metadata dict (empty if none)."""
    return db.meta_of(db.get(name) or {})


def meta_set(name, updates):
    """Merge key->value pairs into an agent's metadata (value None drops a key).
    Returns the resulting metadata."""
    db.set_meta(name, updates)
    return meta_get(name)


def set_groups(name, groups):
    db.set_groups(name, groups)


def set_team(name, team):
    db.set_team(name, team)


# --- durability / schema ---------------------------------------------------

def snapshot():
    """Write the JSON registry snapshot now; returns its path."""
    return db.snapshot()


def reindex():
    """Rebuild the sqlite registry from the JSON snapshot. Returns (agents,
    tombstones) restored."""
    return db.reindex_from_snapshot()


def schema_version():
    """The current registry schema version (migrating the db if needed)."""
    return db.schema_version()


def migrate_schema():
    """Apply any pending schema migrations; returns (old_version, new_version).
    (Named migrate_schema, not migrate, to leave the `agents.migrate` cmux-import
    module unshadowed.)"""
    return db.migrate_now()
