"""agents — per-agent home directory (cmux-compatible layout).

Each agent gets ~/.agents/<name>/ with the same files cmux writes under
~/.cmux/<name>/:

    identity.md         who the agent is (seeded once from --identity)
    initial-prompt.md   the first-start prompt (inlined on launch)
    cwd                 the working directory the session lives in
    last-session-id     the Claude sessionId to --resume

The split follows the PAM boundary: these files are *durable* agent facts. The
old ephemeral bits (socket, daemon pid/log, tmux target) are gone — Claude Code
owns the socket, and there is no daemon.
"""

import os
import shutil

from agents_lib import db


def home_dir(name):
    return os.path.join(db.STATE_DIR, name)


def ensure_home(name, cwd=None, identity_path=None, initial_prompt=None):
    """Create/seed an agent's home dir. Returns its path. Idempotent."""
    home = home_dir(name)
    os.makedirs(home, exist_ok=True)

    # identity.md — seed once, never overwrite (the agent owns it after start).
    if identity_path:
        dst = os.path.join(home, "identity.md")
        if not os.path.exists(dst):
            src = os.path.expanduser(identity_path)
            if not os.path.exists(src):
                raise FileNotFoundError(f"identity file not found: {src}")
            shutil.copy2(src, dst)

    if initial_prompt is not None:
        with open(os.path.join(home, "initial-prompt.md"), "w") as f:
            f.write(initial_prompt)

    # cwd — recorded on first start, reused forever after so resume always
    # happens from the directory the session actually lives in.
    cwd_path = os.path.join(home, "cwd")
    if os.path.exists(cwd_path):
        recorded = open(cwd_path).read().strip()
        if recorded:
            return home
    resolved = cwd or os.getcwd()
    with open(cwd_path, "w") as f:
        f.write(resolved)
    return home


def agent_cwd(name):
    path = os.path.join(home_dir(name), "cwd")
    try:
        return open(path).read().strip() or None
    except OSError:
        return None


def read_session_id(name):
    path = os.path.join(home_dir(name), "last-session-id")
    try:
        return open(path).read().strip() or None
    except OSError:
        return None


def write_session_id(name, session_id):
    with open(os.path.join(home_dir(name), "last-session-id"), "w") as f:
        f.write(session_id)


def identity_path(name):
    p = os.path.join(home_dir(name), "identity.md")
    return p if os.path.exists(p) else None


def initial_prompt(name):
    p = os.path.join(home_dir(name), "initial-prompt.md")
    try:
        return open(p).read().strip() or None
    except OSError:
        return None
