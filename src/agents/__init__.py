"""agents — register and run named Claude Code agents without tmux or a daemon.

Delivery is native (agents.transport); the terminal is a pluggable pty host.

Importable as a library as well as a CLI — the stable operations are re-exported
here (see agents.api), so `import agents; agents.register(...)` / `agents.ls()` /
`agents.send(...)` work directly, returning plain data instead of printing.
"""

from agents.api import (  # noqa: E402,F401  (public library surface)
    register, unregister, get, ls, whoami, whois, send, checkin, recap_history,
    meta_get, meta_set, set_groups, set_team, snapshot, reindex, schema_version,
    migrate_schema,
)

# NB: `migrate` is deliberately NOT re-exported — `agents.migrate` is the
# cmux-import submodule, and a same-named function here would shadow it.
__all__ = [
    "register", "unregister", "get", "ls", "whoami", "whois", "send", "checkin",
    "recap_history", "meta_get", "meta_set", "set_groups", "set_team",
    "snapshot", "reindex", "schema_version", "migrate_schema",
]
