"""agents — pty hosts (where the `claude` process lives).

Because messaging is now native (claudio.native writes to the session's own
socket), the terminal host is decoupled from delivery: it only has to keep an
interactive `claude` process alive in a pty. That is the one thing a plain
`subprocess` can't do well on its own, and the one thing tmux used to be dragged
in for. So `agents` delegates it to whatever host is available rather than
hard-wiring tmux:

    foreground  run claude attached to the current terminal (we wait; sessionId
                is bound in the background). Good for interactive `agents up`.
    herdr       run claude in a new herdr pane (herdr holds the pty; detached).
                Experimental — needs a running herdr session.

A host launches a command and yields a pid; everything else (home dir, sessionId
binding, native messaging) is host-independent.
"""

import json
import os
import shutil
import subprocess


def launch_foreground(cmd, cwd, env):
    """Spawn claude attached to the current terminal. Returns the Popen.

    stdio is inherited, so claude drives the current tty. The caller gets the
    pid immediately (to bind the sessionId) and then waits on the process.
    """
    return subprocess.Popen(cmd, cwd=cwd, env=env)


def launch_herdr(cmd, cwd, env, label=None):
    """Launch claude in a new herdr tab/pane. Returns the pane's pid, or None.

    Best-effort: creates a tab in the current herdr workspace and runs the
    command in its pane. The pane pid is read back from `herdr pane` JSON.
    """
    if not shutil.which("herdr") or os.environ.get("HERDR_ENV") != "1":
        raise RuntimeError("herdr host requested but not running inside herdr")
    ws = os.environ.get("HERDR_WORKSPACE_ID")
    r = subprocess.run(
        ["herdr", "tab", "create", "--workspace", ws, "--cwd", cwd,
         "--label", label or "agent", "--no-focus"],
        capture_output=True, text=True,
    )
    pane_id = _json_get(r.stdout, ("result", "root_pane", "pane_id"))
    if not pane_id:
        raise RuntimeError(f"herdr tab create failed: {r.stdout}{r.stderr}")
    subprocess.run(["herdr", "pane", "run", pane_id, " ".join(cmd)],
                   capture_output=True, text=True)
    # The pane pid is the shell; the launched claude is its child. We return the
    # pane pid as a hint; sessionId binding scans ~/.claude/sessions regardless.
    g = subprocess.run(["herdr", "pane", "get", pane_id],
                       capture_output=True, text=True)
    return _json_get(g.stdout, ("result", "pane", "pid"))


def _json_get(text, path):
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    for k in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj
