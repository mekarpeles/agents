"""agents — persistent, non-per-agent configuration (~/.agents/config.json).

Settings that apply to every agent rather than one: the permission mode launched
agents run under, and the default pty host. `agents configure` writes this file;
the installer runs it once so a fresh machine is ready.

The file is a convenience default, not a lock — AGENTS_CLAUDE_CMD still overrides
the whole launch command (tests rely on it), and --host overrides the host per
invocation.
"""

import json
import os

# Claude Code's --permission-mode choices. 'acceptEdits' is the gentle default:
# launched agents auto-accept file edits but still gate the riskier operations.
# 'bypassPermissions' is the YOLO opt-in (equivalent to --dangerously-skip-
# permissions) — deliberate, never the default.
PERMISSION_MODES = ("acceptEdits", "auto", "manual", "bypassPermissions")

DEFAULTS = {
    "permission_mode": "acceptEdits",
    "host": "tmux",
    "up_mode": "foreground",
}

# How a bare `up` launches, when no -F/-d/-a/--host is given:
#   foreground — inline in the current terminal (works without tmux; the default)
#   detach     — detached in `host` (tmux/herdr), not attached
#   attach     — started in `host` and attached to immediately
UP_MODES = ("foreground", "detach", "attach")


def config_path():
    state = os.environ.get("AGENTS_STATE_DIR", os.path.expanduser("~/.agents"))
    return os.path.join(state, "config.json")


def load():
    """The saved config merged over the defaults (defaults for anything unset)."""
    cfg = dict(DEFAULTS)
    try:
        with open(config_path()) as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except (FileNotFoundError, ValueError, OSError):
        pass
    return cfg


def save(cfg):
    """Persist only the known keys; returns the saved dict."""
    keep = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(keep, f, indent=2)
    os.replace(tmp, path)
    return keep


def launch_flags(mode):
    """The claude flags for a permission mode. bypassPermissions maps to the
    documented --dangerously-skip-permissions (which also skips the trust
    dialog); every other mode is passed through --permission-mode."""
    if mode == "bypassPermissions":
        return ["--dangerously-skip-permissions"]
    if mode not in PERMISSION_MODES:
        mode = DEFAULTS["permission_mode"]
    return ["--permission-mode", mode]
