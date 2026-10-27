"""agents — per-agent home directory.

Each agent gets ~/.agents/<name>/: its base, and the drop-zone a caller populates
before `agents up` starts claude there.

    AGENTS.md        how the agent operates. Seeded once from an agent type;
                     Claude Code auto-loads it on startup. Replace it before `up`
                     to give the agent its own instructions.
    PROMPT.md        the agent's current task (from --prompt, or dropped in).
    identity.md      optional extra identity (from --identity); read if present.
    cwd              the directory claude runs in — the home dir by default, so
                     AGENTS.md loads; --cwd overrides it to a project checkout.
    last-session-id  the Claude sessionId to --resume.
"""

import os
import shutil

from agents import db

TYPES_DIR = os.path.join(os.path.dirname(__file__), "types")


def home_dir(name):
    return os.path.join(db.STATE_DIR, name)


def available_types():
    """Bundled agent types (each a types/<name>/AGENTS.md)."""
    try:
        return sorted(d for d in os.listdir(TYPES_DIR)
                      if os.path.exists(os.path.join(TYPES_DIR, d, "AGENTS.md")))
    except OSError:
        return ["default"]


def type_agents_md(agent_type):
    """Path to a bundled agent type's AGENTS.md (falls back to the default type)."""
    for t in (agent_type or "default", "default"):
        p = os.path.join(TYPES_DIR, t, "AGENTS.md")
        if os.path.exists(p):
            return p
    return None


def ensure_home(name, cwd=None, agent_type="default", identity_path=None,
                initial_prompt=None, session_id=None):
    """Create/seed an agent's home dir. Returns its path. Idempotent.

    AGENTS.md and identity.md are seeded once and never overwritten — the caller
    (or the agent) owns them after. PROMPT.md is rewritten whenever a prompt is
    given.
    """
    home = home_dir(name)
    os.makedirs(home, exist_ok=True)

    if session_id:
        write_session_id(name, session_id)

    # AGENTS.md — seed once from the chosen type; never clobber a custom one the
    # caller may have dropped in.
    dst = os.path.join(home, "AGENTS.md")
    if not os.path.exists(dst):
        src = type_agents_md(agent_type)
        if src:
            shutil.copy2(src, dst)

    # identity.md — optional extra context; seed once, never overwrite.
    if identity_path:
        idst = os.path.join(home, "identity.md")
        if not os.path.exists(idst):
            isrc = os.path.expanduser(identity_path)
            if not os.path.exists(isrc):
                raise FileNotFoundError(f"identity file not found: {isrc}")
            shutil.copy2(isrc, idst)

    if initial_prompt is not None:
        with open(os.path.join(home, "PROMPT.md"), "w") as f:
            f.write(initial_prompt)

    # cwd — recorded on first start, reused forever after. Defaults to the home
    # dir so the agent's AGENTS.md loads at startup.
    cwd_path = os.path.join(home, "cwd")
    if os.path.exists(cwd_path):
        if open(cwd_path).read().strip():
            return home
    with open(cwd_path, "w") as f:
        f.write(cwd or home)
    return home


def agent_cwd(name):
    try:
        return open(os.path.join(home_dir(name), "cwd")).read().strip() or None
    except OSError:
        return None


def read_session_id(name):
    try:
        return open(os.path.join(home_dir(name), "last-session-id")).read().strip() or None
    except OSError:
        return None


def write_session_id(name, session_id):
    with open(os.path.join(home_dir(name), "last-session-id"), "w") as f:
        f.write(session_id)


def initial_prompt(name):
    try:
        return open(os.path.join(home_dir(name), "PROMPT.md")).read().strip() or None
    except OSError:
        return None
