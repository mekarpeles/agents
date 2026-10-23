"""agents — register and run named Claude Code agents, without tmux or a daemon.

A minimal fork of cmux: it keeps the persistent registry, the per-agent home
directory, and up/down — but delivery is native (claudio.native) and the pty
host is pluggable (foreground / herdr), so there is no tmux dependency and no
per-agent message-injection daemon.

    agents register <name> [--role R] [--workspace W] [--cwd D]
                           [--identity path] [--prompt text]
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
                identity_path=args.identity)
    home.ensure_home(args.name, cwd=args.cwd, identity_path=args.identity,
                     initial_prompt=args.prompt)
    print(f"agents: registered '{args.name}'")
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


def cmd_send(args):
    _require_claudio()
    reg = db.get(args.name)
    sid = (reg or {}).get("session_id") or home.read_session_id(args.name)
    if not sid:
        print(f"agents: '{args.name}' has no bound session", file=sys.stderr)
        return 1
    sender = os.environ.get("CLAUDIO_AGENT_NAME", "agents")
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


def cmd_help(args):
    build_parser().print_help()
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="agents", description=__doc__.splitlines()[0])
    # Not required: a bare `agents` prints help instead of an argparse error.
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("register", help="register an agent (does not launch)")
    s.add_argument("name")
    s.add_argument("--role")
    s.add_argument("--workspace")
    s.add_argument("--cwd")
    s.add_argument("--identity")
    s.add_argument("--prompt")
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
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("rm", help="de-register an agent (keeps home dir)")
    s.add_argument("name")
    s.set_defaults(func=cmd_rm)

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
