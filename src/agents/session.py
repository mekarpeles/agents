"""agents — bind a launched process to its Claude Code sessionId.

When we launch `claude` ourselves we know the child pid, so we can read that
session's own record at ~/.claude/sessions/<pid>.json and take its sessionId
directly. This is exact — no "newest jsonl modified in this cwd" guessing, which
is what mis-binds agents in cmux (see the session-misattribution bug).
"""

import json
import os
import re
import time

SESSIONS_DIR = os.path.expanduser(
    os.environ.get("CLAUDE_SESSIONS_DIR", "~/.claude/sessions")
)
PROJECTS_DIR = os.path.expanduser(
    os.environ.get("CLAUDE_PROJECTS_DIR", "~/.claude/projects")
)


def project_dir(cwd):
    """The directory Claude stores a cwd's transcripts under. Claude derives it by
    replacing *every* non-alphanumeric character in the absolute cwd with '-' (so
    `/Users/mek/.agents/x` → `-Users-mek--agents-x` — the dot becomes a dash too,
    not just the slashes), without collapsing runs. Anything keying on the project
    dir (resume lookup, moving a transcript on rename) must use this, not a bare
    slash substitution."""
    return os.path.join(PROJECTS_DIR, re.sub(r"[^A-Za-z0-9]", "-", cwd))


def transcript_exists(session_id, cwd=None):
    """True if a resumable transcript (<sessionId>.jsonl) exists for this id.

    Claude writes it lazily (after the first exchange), so a brand-new or empty
    session has none and cannot be --resume'd. Checks the cwd's project dir first,
    then any project dir.
    """
    import glob
    if cwd and os.path.exists(os.path.join(project_dir(cwd), f"{session_id}.jsonl")):
        return True
    return bool(glob.glob(os.path.join(PROJECTS_DIR, "*", f"{session_id}.jsonl")))


def record_for_pid(pid):
    path = os.path.join(SESSIONS_DIR, f"{pid}.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def session_id_for_pid(pid, timeout=15.0, interval=0.2):
    """Wait for the session record of `pid` to appear; return its sessionId.

    Claude writes ~/.claude/sessions/<pid>.json shortly after launch. Returns
    None if it never shows (e.g. the process died immediately).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = record_for_pid(pid)
        if rec and rec.get("sessionId"):
            return rec["sessionId"]
        if not _alive(pid):
            return None
        time.sleep(interval)
    return None


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
