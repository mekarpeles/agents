"""agents — register and run named Claude Code agents, without tmux or a daemon.

A minimal fork of cmux: it keeps the persistent registry, the per-agent home
directory, and up/down — but delivery is native (claudio.native) and the pty
host is pluggable (foreground / herdr), so there is no tmux dependency and no
per-agent message-injection daemon.

    agents register <name> [--role R] [--workspace W] [--cwd D]
                           [--identity path] [--prompt text] [--session-id ID]
    agents up <name> [--host foreground|herdr] [--prompt text] [--no-inject]

Agents launch under Claude Code auto-mode, so there is no permission-prompt
handling here (no --allowed-tools pre-auth, no trust-dialog unblocking): that
machinery is obsolete.
    agents down <name>
    agents ls
    agents send <name> <message...>
    agents rm <name>
"""

import argparse
import os
import sys
import threading

from agents_lib import db, home, hosts, session

try:
    from claudio import native, registry as claudio_registry
except ImportError:  # pragma: no cover - claudio is a runtime dependency
    native = None
    claudio_registry = None


def _require_claudio():
    if native is None:
        print("agents: claudio is not installed (pip install claudio)",
              file=sys.stderr)
        sys.exit(1)


def _claude_cmd(name, resume_id):
    claude_bin = os.environ.get("AGENTS_CLAUDE_CMD", "claude")
    cmd = claude_bin.split() if " " in claude_bin else [claude_bin]
    if resume_id:
        cmd += ["--resume", resume_id]
    return cmd


def _bind_and_inject(name, pid, is_first_start, inject):
    """Bind the launched pid -> sessionId, then inject the startup prompt."""
    sid = session.session_id_for_pid(pid)
    if not sid:
        print(f"agents: warning — could not detect sessionId for '{name}' "
              f"(pid {pid}); not bound", file=sys.stderr)
        return None
    db.set_session(name, sid)
    home.write_session_id(name, sid)
    if claudio_registry is not None:
        # Register the AGENT's socket, not the launcher's — read it from the
        # agent session's own record (env here belongs to the launching process).
        rec = session.record_for_pid(pid) or {}
        claudio_registry.register(
            name, session_id=sid,
            socket=rec.get("messagingSocketPath"), pid=pid,
        )  # "save socket on startup"

    if not inject:
        return sid
    parts = []
    idp = home.identity_path(name)
    if is_first_start:
        parts.append(f'Session: "{name}" — home: {home.home_dir(name)}.')
    elif idp:
        parts.append(f'Resuming "{name}" (ref: {idp}).')
    else:
        parts.append(f'Resuming "{name}".')
    prompt = home.initial_prompt(name)
    if prompt:
        parts.append(prompt)
    try:
        native.send(sid, "\n\n".join(parts), sender="agents")
    except native.DeliveryError as e:
        print(f"agents: warning — could not inject startup prompt: {e}",
              file=sys.stderr)
    return sid


def cmd_register(args):
    db.register(args.name, role=args.role, workspace=args.workspace,
                cwd=args.cwd, initial_prompt=args.prompt,
                identity_path=args.identity, session_id=args.session_id)
    home.ensure_home(args.name, cwd=args.cwd, identity_path=args.identity,
                     initial_prompt=args.prompt, session_id=args.session_id)
    bound = f" (bound to session {args.session_id})" if args.session_id else ""
    print(f"agents: registered '{args.name}'{bound}")
    return 0


def cmd_up(args):
    _require_claudio()
    reg = db.get(args.name)
    if reg is None:
        # Allow a one-shot up of an unregistered name (registers on the fly).
        db.register(args.name, cwd=args.cwd)
        reg = db.get(args.name)

    cwd = home.agent_cwd(args.name) or reg.get("cwd") or os.getcwd()
    home.ensure_home(args.name, cwd=cwd, identity_path=args.identity,
                     initial_prompt=args.prompt or reg.get("initial_prompt"))

    resume_id = home.read_session_id(args.name)
    if resume_id:
        # Already running? Don't start a duplicate on the same session.
        try:
            rec = native.resolve(resume_id)
            print(f"agents: '{args.name}' is already running "
                  f"(session {resume_id[:8]}, pid {rec.get('pid')}). "
                  f"Use `agents send {args.name} ...` or `agents down {args.name}` first.",
                  file=sys.stderr)
            return 1
        except native.SessionNotFound:
            pass  # not running — ok to resume if there's a transcript
        # No resumable transcript (e.g. the bound session never had an exchange)?
        if not session.transcript_exists(resume_id, cwd):
            print(f"agents: no resumable transcript for '{args.name}' "
                  f"({resume_id[:8]}); starting a fresh session.", file=sys.stderr)
            resume_id = None
    is_first_start = resume_id is None
    cmd = _claude_cmd(args.name, resume_id)

    env = dict(os.environ, CLAUDIO_AGENT_NAME=args.name)
    inject = not args.no_inject

    if args.host == "herdr":
        pid = hosts.launch_herdr(cmd, cwd, env, label=args.name)
        _bind_and_inject(args.name, pid, is_first_start, inject)
        print(f"agents: '{args.name}' started in herdr (detached)")
        return 0

    # foreground: attach claude to this terminal; bind/inject in the background.
    proc = hosts.launch_foreground(cmd, cwd, env)
    t = threading.Thread(target=_bind_and_inject,
                         args=(args.name, proc.pid, is_first_start, inject),
                         daemon=True)
    t.start()
    return proc.wait()


def cmd_down(args):
    _require_claudio()
    sid = home.read_session_id(args.name)
    if not sid:
        print(f"agents: '{args.name}' has no bound session", file=sys.stderr)
        return 1
    try:
        rec = native.resolve(sid)
    except native.SessionNotFound:
        print(f"agents: '{args.name}' is not running")
        return 0
    pid = rec.get("pid")
    try:
        os.kill(pid, 15)  # SIGTERM
        print(f"agents: stopped '{args.name}' (pid {pid})")
    except ProcessLookupError:
        print(f"agents: '{args.name}' already stopped")
    return 0


def cmd_ls(args):
    agents = db.all_agents()
    if not agents:
        print("agents: none registered")
        return 0
    live = {}
    if native is not None:
        for r in native.list_sessions():
            live[r.get("sessionId")] = r
    print(f"{'NAME':<16} {'STATUS':<8} {'WORKSPACE':<12} {'SESSION':<10} ROLE")
    for a in agents:
        sid = a.get("session_id")
        rec = live.get(sid)
        status = (rec.get("status") if rec else "down")
        print(f"{a['name']:<16} {status:<8} {(a.get('workspace') or ''):<12} "
              f"{(sid or '')[:8]:<10} {(a.get('role') or '')}")
    return 0


def _sender(explicit):
    """Resolve the 'from' attribution: --from, else $CLAUDIO_AGENT_NAME (set when
    the caller is itself an agent), else the OS login name. Never the bare
    'agents' — that reads like a peer agent and confuses the receiver.
    """
    import getpass
    if explicit:
        return explicit
    if os.environ.get("CLAUDIO_AGENT_NAME"):
        return os.environ["CLAUDIO_AGENT_NAME"]
    try:
        return getpass.getuser()
    except Exception:
        return "cli"


def cmd_send(args):
    _require_claudio()
    reg = db.get(args.name)
    sid = (reg or {}).get("session_id") or home.read_session_id(args.name)
    if not sid:
        print(f"agents: '{args.name}' has no bound session", file=sys.stderr)
        return 1
    sender = _sender(args.sender)
    try:
        native.send(sid, " ".join(args.message), sender=sender)
    except (native.SessionNotFound, native.DeliveryError) as e:
        print(f"agents: {e}", file=sys.stderr)
        return 1
    print(f"agents: delivered to '{args.name}'")
    return 0


def cmd_rm(args):
    db.remove(args.name)
    if claudio_registry is not None:
        claudio_registry.unregister(args.name)
    print(f"agents: de-registered '{args.name}' (home dir preserved at "
          f"{home.home_dir(args.name)})")
    return 0


def cmd_migrate(args):
    from agents_lib import migrate
    results = migrate.migrate(apply=args.apply, link=args.link,
                              names=args.names or None)
    if not results:
        print("agents: nothing to migrate (no cmux agents.db or no rows at "
              f"{migrate.cmux_db_path()})")
        return 0
    mode = "APPLY" if args.apply else "DRY-RUN (use --apply to execute)"
    # Detect session_ids bound to more than one agent — the cmux misattribution
    # bug frozen into the data. Migration mirrors cmux faithfully, so these carry
    # over; flag them so they can be repaired (one is the real owner).
    counts = {}
    for it in results:
        if it["session_id"]:
            counts[it["session_id"]] = counts.get(it["session_id"], 0) + 1
    shared = {sid for sid, n in counts.items() if n > 1}
    print(f"agents migrate — {mode}  [{len(results)} agent(s)]")
    for it in results:
        flags = []
        if not it["has_home"]:
            flags.append("no home dir")
        if not it["session_id"]:
            flags.append("no session_id")
        if it["session_id"] in shared:
            flags.append(f"SHARED session_id x{counts[it['session_id']]}")
        if it.get("workflow_path"):
            flags.append("workflow_path dropped")
        extra = f"  ({', '.join(flags)})" if flags else ""
        print(f"  {it['name']:<22} {it['action']}{extra}")
    if shared:
        print(f"\nWARNING: {len(shared)} session_id(s) are bound to multiple "
              f"agents (cmux misattribution). Resolve before/after --apply so each "
              f"agent keeps its real session.")
    if not args.apply:
        print("\nNon-destructive: ~/.cmux is never modified. Re-run with --apply.")
    return 0


def cmd_help(args):
    build_parser().print_help()
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="ag", description=__doc__.splitlines()[0])
    # Not required: a bare `agents` prints help instead of an argparse error.
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("register", help="register an agent (does not launch)")
    s.add_argument("name")
    s.add_argument("--role")
    s.add_argument("--workspace")
    s.add_argument("--cwd")
    s.add_argument("--identity")
    s.add_argument("--prompt")
    s.add_argument("--session-id", dest="session_id",
                   help="adopt an existing Claude sessionId (resume it on `up`)")
    s.set_defaults(func=cmd_register)

    s = sub.add_parser("up", help="launch (or resume) an agent")
    s.add_argument("name")
    s.add_argument("--host", choices=["foreground", "herdr"], default="foreground")
    s.add_argument("--prompt")
    s.add_argument("--cwd")
    s.add_argument("--identity")
    s.add_argument("--no-inject", action="store_true")
    s.set_defaults(func=cmd_up)

    s = sub.add_parser("down", help="stop a running agent")
    s.add_argument("name")
    s.set_defaults(func=cmd_down)

    s = sub.add_parser("ls", aliases=["list"], help="list registered agents")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("send", help="deliver a message to an agent")
    s.add_argument("name")
    s.add_argument("message", nargs="+")
    s.add_argument("--from", dest="sender",
                   help="sender name shown to the receiver "
                        "(default: $CLAUDIO_AGENT_NAME or your OS login)")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("rm", help="de-register an agent (keeps home dir)")
    s.add_argument("name")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("migrate", help="migrate cmux state (~/.cmux) into agents")
    s.add_argument("names", nargs="*", help="limit to these agent names")
    s.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    s.add_argument("--link", action="store_true",
                   help="symlink homes instead of copying")
    s.set_defaults(func=cmd_migrate)

    # `help` as a real subcommand too, so `agents help` works like `agents --help`.
    s = sub.add_parser("help", help="show this help")
    s.set_defaults(func=cmd_help)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "cmd", None):
        parser.print_help()
        sys.exit(0)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
