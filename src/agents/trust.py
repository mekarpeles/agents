"""agents — pre-accept Claude Code's workspace-trust dialog for an agent's cwd.

Launching `claude` in a directory it hasn't seen pops a "trust the files in this
folder?" dialog that blocks an unattended start — so `up` never catches the pid's
sessionId and can't bind the agent (the ada-1581 stall). Clicking "trust" just
sets hasTrustDialogAccepted for that path in Claude's config; we set it directly
so a gentle permission mode (acceptEdits/auto) can still launch without a human
keypress. (--dangerously-skip-permissions skips the dialog on its own; this makes
every other mode launch just as cleanly.)
"""

import json
import os


def _config_path():
    return os.environ.get("CLAUDE_CONFIG_PATH", os.path.expanduser("~/.claude.json"))


def ensure_trusted(cwd):
    """Mark `cwd` trusted in Claude's config. Returns True if it changed the file.

    Read-modify-write, preserving everything else; a no-op (returns False) when the
    config is missing/unreadable or the path is already trusted. Best-effort: a
    failure to write is swallowed so it never blocks a launch.
    """
    path = _config_path()
    cwd = os.path.abspath(cwd)
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return False
    if not isinstance(cfg, dict):
        return False
    proj = cfg.setdefault("projects", {}).setdefault(cwd, {})
    if proj.get("hasTrustDialogAccepted") is True:
        return False
    proj["hasTrustDialogAccepted"] = True
    try:
        tmp = path + ".agents-tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        return False
    return True
