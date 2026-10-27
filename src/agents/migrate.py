"""Migrate cmux state (~/.cmux) into the agents system (~/.agents).

Reads cmux's SQLite registry and per-agent home dirs and mirrors them into
agents' own store. **Non-destructive**: it never modifies or deletes anything
under ~/.cmux — cmux keeps running unchanged, so the fleet is safe during the
transition and you can migrate agents one at a time.

Does NOT import cmux (keeps agents cmux-free); it reads ~/.cmux/agents.db
directly, read-only.

What maps across:
    cmux agents.db row   -> agents.db row (name, role, workspace, initial_prompt)
    ~/.cmux/<name>/cwd              -> agents cwd
    ~/.cmux/<name>/last-session-id  -> agents session_id (the durable binding)
    ~/.cmux/<name>/identity.md etc. -> copied (or symlinked) into ~/.agents/<name>/

Dropped on purpose: no_inject/unblock/allowed_tools (permission machinery — gone
under Claude auto-mode), tmux/daemon/socket bookkeeping (ephemeral). workflow_path
is reported but not migrated (agents has no workflow-injection yet).
"""

import os
import shutil
import sqlite3

from agents import db, home

CMUX_STATE_DIR = os.environ.get("CMUX_STATE_DIR", os.path.expanduser("~/.cmux"))

# Durable home-dir contents worth carrying over (everything else in a cmux home
# — *.sock, *.daemon.log, *.daemon.pid, MIGRATE.md — is ephemeral).
DURABLE_FILES = ["identity.md", "initial-prompt.md", "cwd", "last-session-id"]
DURABLE_DIRS = [".cq"]


def cmux_db_path():
    return os.path.join(CMUX_STATE_DIR, "agents.db")


def read_cmux_agents():
    """Read the cmux agents table read-only. Returns [] if there is no cmux DB."""
    path = cmux_db_path()
    if not os.path.exists(path):
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM agents").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _read(home_dir, fname):
    try:
        return open(os.path.join(home_dir, fname)).read().strip() or None
    except OSError:
        return None


def plan():
    """Compute the migration plan (pure; makes no changes)."""
    items = []
    for a in read_cmux_agents():
        name = a["name"]
        src_home = os.path.join(CMUX_STATE_DIR, name)
        items.append({
            "name": name,
            "role": a.get("role"),
            "workspace": a.get("workspace"),
            "cwd": _read(src_home, "cwd"),
            "initial_prompt": a.get("initial_prompt"),
            "session_id": _read(src_home, "last-session-id"),
            "src_home": src_home,
            "has_home": os.path.isdir(src_home),
            "has_identity": os.path.exists(os.path.join(src_home, "identity.md")),
            "workflow_path": a.get("workflow_path"),  # reported, not migrated
            "already_migrated": db.get(name) is not None,
        })
    return items


def _copy_home(src_home, dst_home):
    os.makedirs(dst_home, exist_ok=True)
    for fn in DURABLE_FILES:
        s = os.path.join(src_home, fn)
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(dst_home, fn))
    for d in DURABLE_DIRS:
        s = os.path.join(src_home, d)
        if os.path.isdir(s):
            shutil.copytree(s, os.path.join(dst_home, d), dirs_exist_ok=True)


def migrate(apply=False, link=False, names=None):
    """Execute (or dry-run) the migration. Returns the list of plan items, each
    annotated with 'action'. apply=False (default) changes nothing.

    link=True symlinks ~/.agents/<name> -> ~/.cmux/<name> instead of copying
    (single source of truth during transition; only safe if the agent won't run
    under both systems at once). Default copies the durable files.
    """
    results = []
    for item in plan():
        name = item["name"]
        if names and name not in names:
            continue
        if item["already_migrated"]:
            item["action"] = "skip (already in agents.db)"
            results.append(item)
            continue
        dst_home = home.home_dir(name)
        item["action"] = (
            f"{'link' if link else 'copy'} home -> {dst_home}; "
            f"register (session_id={item['session_id']})"
        )
        if apply:
            if item["has_home"]:
                if link:
                    if not os.path.lexists(dst_home):
                        os.makedirs(os.path.dirname(dst_home), exist_ok=True)
                        os.symlink(item["src_home"], dst_home)
                else:
                    _copy_home(item["src_home"], dst_home)
            else:
                os.makedirs(dst_home, exist_ok=True)
            identity = os.path.join(dst_home, "identity.md")
            db.register(
                name, role=item["role"], workspace=item["workspace"],
                cwd=item["cwd"], initial_prompt=item["initial_prompt"],
                identity_path=identity if os.path.exists(identity) else None,
            )
            if item["session_id"]:
                db.set_session(name, item["session_id"])
        results.append(item)
    return results
