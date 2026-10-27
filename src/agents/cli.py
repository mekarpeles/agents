"""agents — register, run, and message named Claude Code agents.

Three layers, each with one job:
    registry   agents.db — names, group membership, the durable sessionId binding
    host       hosts.py — where claude runs: tmux (default), herdr, or foreground
    transport  transport.py — deliver a message to a running agent's native socket

Commands:
    register <name> [-g group] [--cwd D] [--identity F] [--prompt T] [--session-id ID]
    up     [<name> | -g <group>] [--host tmux|herdr|foreground] [--prompt T] [--no-inject]
    down   [<name> | -g <group>]
    attach [<name> | -g <group>] [--host H]
    detach [<name> | -g <group>] [--host H]
    send <name> <message...> [--from who]
    ls   |   find [query]   |   rm <name>   |   migrate   |   help

Delivery is native (no daemon, no tmux send-keys), and binding is by sessionId, so
a resume never mis-attributes a session. Agents run under Claude Code auto-mode.
"""

import argparse
import os
import shlex
import sys
import threading

from agents import db, history, home, hosts, session, transport as native


def _err(msg):
    """Print 'agents: <msg>' to stderr and return exit code 1."""
    print(f"agents: {msg}", file=sys.stderr)
    return 1


def _claude_cmd(name, resume_id):
    # Autonomous agents bypass the folder-trust and permission prompts, so a
    # freshly launched session starts (and writes its sessionId) without a human
    # keypress — otherwise `up` can't bind it. AGENTS_CLAUDE_CMD overrides the
    # whole command (tests point it at a fake claude).
    default = "claude --dangerously-skip-permissions"
    cmd = shlex.split(os.environ.get("AGENTS_CLAUDE_CMD", default))
    if resume_id:
        cmd += ["--resume", resume_id]
    return cmd


def _bind_and_inject(name, pid, is_first_start, inject, sid_hint=None):
    """Bind the launched pid -> sessionId, then inject the startup prompt.

    sid_hint short-circuits detection: on resume we already know the sessionId
    (it survives --resume), so a detached host needs no pid-based lookup.
    """
    sid = sid_hint or (session.session_id_for_pid(pid) if pid else None)
    if not sid:
        print(f"agents: warning — could not detect sessionId for '{name}' "
              f"(pid {pid}); not bound", file=sys.stderr)
        return None
    db.set_session(name, sid)
    home.write_session_id(name, sid)
    if not inject:
        return sid
    # claude started in the home dir, so AGENTS.md is already loaded; the nudge
    # just names the agent and points at PROMPT.md, then delivers the task.
    parts = []
    if is_first_start:
        parts.append(f'You are "{name}". Your home ({home.home_dir(name)}) holds '
                     f'AGENTS.md (how you operate) and PROMPT.md (your task).')
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
    db.register(args.name, agent_type=args.type, role=args.role,
                workspace=args.workspace, cwd=args.cwd, initial_prompt=args.prompt,
                identity_path=args.identity, session_id=args.session_id)
    home.ensure_home(args.name, cwd=args.cwd, agent_type=args.type,
                     identity_path=args.identity, initial_prompt=args.prompt,
                     session_id=args.session_id)
    bound = f" (bound to session {args.session_id})" if args.session_id else ""
    print(f"agents: registered '{args.name}' ({args.type}); "
          f"home: {home.home_dir(args.name)}{bound}")
    return 0


def _up_one(name, host, prompt=None, cwd=None, identity=None, inject=True):
    """Launch (or resume) one agent on `host`. Returns an exit code."""
    reg = db.get(name)
    if reg is None:
        db.register(name, cwd=cwd)   # one-shot up of an unregistered name
        reg = db.get(name)
    acwd = home.agent_cwd(name) or reg.get("cwd") or cwd or os.getcwd()
    home.ensure_home(name, cwd=acwd, identity_path=identity,
                     initial_prompt=prompt or reg.get("initial_prompt"))
    group = reg.get("workspace")

    resume_id = home.read_session_id(name)
    if resume_id:
        try:
            rec = native.resolve(resume_id)
            return _err(f"'{name}' is already running (session {resume_id[:8]}, "
                        f"pid {rec.get('pid')}). Use `agents send {name} ...`, "
                        f"`agents attach {name}`, or `agents down {name}` first.")
        except native.SessionNotFound:
            pass  # not running — resume if there's a transcript
        if not session.transcript_exists(resume_id, acwd):
            print(f"agents: no resumable transcript for '{name}' ({resume_id[:8]}); "
                  f"starting fresh.", file=sys.stderr)
            resume_id = None
    is_first_start = resume_id is None
    cmd = _claude_cmd(name, resume_id)
    env = dict(os.environ, AGENTS_AGENT_NAME=name)

    try:
        launched = host.start(name, cmd, acwd, env, group=group)
    except (hosts.Unsupported, RuntimeError) as e:
        return _err(str(e))

    if launched.proc is not None:
        # Inline host (foreground): bind in the background, then wait on claude.
        threading.Thread(
            target=_bind_and_inject,
            args=(name, launched.pid, is_first_start, inject),
            kwargs={"sid_hint": resume_id}, daemon=True).start()
        return launched.proc.wait()

    _bind_and_inject(name, launched.pid, is_first_start, inject, sid_hint=resume_id)
    where = f"group '{group}'" if group else "detached"
    print(f"agents: '{name}' started in {host.name} ({where}); "
          f"attach: agents attach {name}")
    return 0


def _one_target(args):
    """Validate 'exactly one of <name> or a -g/-t selector'. Returns error|None.

    The single rule behind every targeting command — set ops (`up`/`down`) read it
    through `_selection`, single ops (`attach`/`detach`) call it directly.
    """
    selector = getattr(args, "group", None) or getattr(args, "type", None)
    if args.name and selector:
        return "specify an agent name OR a selector, not both"
    if not args.name and not selector:
        return "specify an agent name or -g <group>"
    return None


def _selection(args):
    """The agents a set command targets: a bare <name>, or the agents matching
    -g <group> / -t <type>. Returns (names, error)."""
    err = _one_target(args)
    if err:
        return None, err
    if args.name:
        return [args.name], None
    rows = db.select(group=args.group, agent_type=getattr(args, "type", None))
    if not rows:
        return None, "no agents match that selection"
    return [r["name"] for r in rows], None


def cmd_up(args):
    host = hosts.get(args.host)
    names, err = _selection(args)
    if err:
        return _err(err)
    if args.name:  # a bare name keeps prompt/cwd/identity and may start anew
        return _up_one(args.name, host, prompt=args.prompt, cwd=args.cwd,
                       identity=args.identity, inject=not args.no_inject)
    if not host.group:
        return _err(f"{host.name} can't bring up a selection — use --host tmux")
    rc = 0
    for n in names:
        rc |= _up_one(n, host, inject=not args.no_inject)
    return rc


def _down_one(name):
    """Stop one agent: SIGTERM the live pid resolved from its sessionId.

    Host-agnostic — when claude exits, its tmux window/herdr pane closes on its
    own, so there is nothing host-specific to tear down here.
    """
    sid = home.read_session_id(name)
    if not sid:
        return _err(f"'{name}' has no bound session")
    try:
        rec = native.resolve(sid)
    except native.SessionNotFound:
        print(f"agents: '{name}' is not running")
        return 0
    pid = rec.get("pid")
    try:
        os.kill(pid, 15)  # SIGTERM
        print(f"agents: stopped '{name}' (pid {pid})")
    except ProcessLookupError:
        print(f"agents: '{name}' already stopped")
    return 0


def cmd_down(args):
    names, err = _selection(args)
    if err:
        return _err(err)
    rc = 0
    for n in names:
        rc |= _down_one(n)
    return rc


def _attach_detach(args, op):
    """attach/detach are the same single-target op on a host: a named agent's
    window, or a whole group's session. `op` is 'attach' or 'detach'."""
    host = hosts.get(args.host)
    err = _one_target(args)
    if err:
        return _err(err)
    group = args.group or (db.get(args.name) or {}).get("workspace")
    try:
        getattr(host, op)(None if args.group else args.name, group=group)
    except (hosts.Unsupported, RuntimeError) as e:
        return _err(str(e))
    return 0


def cmd_attach(args):
    return _attach_detach(args, "attach")


def cmd_detach(args):
    return _attach_detach(args, "detach")


def cmd_ls(args):
    agents = db.select(group=args.group, agent_type=args.type)
    if not agents:
        print("agents: no agents match" if (args.group or args.type)
              else "agents: none registered")
        return 0
    live = {r.get("sessionId"): r for r in native.list_sessions()}
    print(f"{'NAME':<16} {'TYPE':<8} {'STATUS':<8} {'GROUP':<12} {'SESSION':<10} ROLE")
    for a in agents:
        sid = a.get("session_id")
        status = live[sid].get("status") if sid in live else "down"
        print(f"{a['name']:<16} {(a.get('type') or ''):<8} {status:<8} "
              f"{(a.get('workspace') or ''):<12} {(sid or '')[:8]:<10} "
              f"{a.get('role') or ''}")
    return 0


def cmd_find(args):
    rows = history.find_sessions(
        args.query, include_gone=args.all, limit=args.limit or 20)
    running = {r.get("sessionId") for r in native.list_sessions()}

    if args.json:
        import json as _json
        for r in rows:
            print(_json.dumps({**r, "running": r["session_id"] in running}))
        return 0

    if not rows:
        print("agents: no matching sessions")
        return 0

    def clip(s, n):
        s = " ".join((s or "").split())
        return s if len(s) <= n else s[: n - 1] + "…"

    print(f"{'SESSION':<36} {'STATE':<8} {'MSGS':>5}  {'CWD':<22} TITLE")
    for r in rows:
        state = ("running" if r["session_id"] in running
                 else "idle" if r["present"] else "gone")
        cwd = os.path.basename(r["cwd"].rstrip("/")) or r["cwd"]
        print(f"{r['session_id']:<36} {state:<8} {r['msgs']:>5}  "
              f"{clip(cwd, 22):<22} {clip(r['title'], 56)}")
    print("\nresume:  claude --resume <SESSION>   "
          "(or: agents register <name> --cwd <cwd> --session-id <SESSION>; "
          "agents up <name>)", file=sys.stderr)
    return 0


def _sender(explicit):
    """Resolve the 'from' attribution: --from, else $AGENTS_AGENT_NAME (set when
    the caller is itself an agent), else the OS login name. Never the bare
    'agents' — that reads like a peer agent and confuses the receiver.
    """
    import getpass
    if explicit:
        return explicit
    if os.environ.get("AGENTS_AGENT_NAME"):
        return os.environ["AGENTS_AGENT_NAME"]
    try:
        return getpass.getuser()
    except Exception:
        return "cli"


def cmd_send(args):
    reg = db.get(args.name)
    sid = (reg or {}).get("session_id") or home.read_session_id(args.name)
    if not sid:
        return _err(f"'{args.name}' has no bound session")
    sender = _sender(args.sender)
    try:
        native.send(sid, " ".join(args.message), sender=sender)
    except (native.SessionNotFound, native.DeliveryError) as e:
        return _err(str(e))
    print(f"agents: delivered to '{args.name}'")
    return 0


def cmd_rm(args):
    db.remove(args.name)
    print(f"agents: de-registered '{args.name}' (home dir preserved at "
          f"{home.home_dir(args.name)})")
    return 0


def cmd_migrate(args):
    from agents import migrate
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
    p = argparse.ArgumentParser(prog="agents", description=__doc__.splitlines()[0])
    # Not required: a bare `agents` prints help instead of an argparse error.
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("register", help="register an agent (does not launch)")
    s.add_argument("name")
    s.add_argument("--type", default="default",
                   help="agent type seeding AGENTS.md "
                        f"({', '.join(home.available_types())})")
    s.add_argument("--role")
    s.add_argument("-g", "--group", dest="workspace",
                   help="group this agent belongs to (agents up -g <group>)")
    s.add_argument("--cwd", help="project dir claude runs in (default: the home dir)")
    s.add_argument("--identity", help="optional identity.md to seed")
    s.add_argument("--prompt", help="initial task written to PROMPT.md")
    s.add_argument("--session-id", dest="session_id",
                   help="adopt an existing Claude sessionId (resume it on `up`)")
    s.set_defaults(func=cmd_register)

    s = sub.add_parser("up", help="launch (or resume) an agent, group, or type")
    s.add_argument("name", nargs="?")
    s.add_argument("-g", "--group", help="bring up every agent in this group")
    s.add_argument("-t", "--type", help="bring up every agent of this type")
    s.add_argument("--host", choices=hosts.CHOICES, default=hosts.DEFAULT,
                   help=f"pty host (default: {hosts.DEFAULT})")
    s.add_argument("--prompt")
    s.add_argument("--cwd")
    s.add_argument("--identity")
    s.add_argument("--no-inject", action="store_true")
    s.set_defaults(func=cmd_up)

    s = sub.add_parser("down", help="stop an agent, group, or type")
    s.add_argument("name", nargs="?")
    s.add_argument("-g", "--group", help="stop every agent in this group")
    s.add_argument("-t", "--type", help="stop every agent of this type")
    s.set_defaults(func=cmd_down)

    s = sub.add_parser("attach", aliases=["a"], help="attach to an agent or group")
    s.add_argument("name", nargs="?")
    s.add_argument("-g", "--group", help="attach to this group's session")
    s.add_argument("--host", choices=hosts.CHOICES, default=hosts.DEFAULT,
                   help=f"pty host (default: {hosts.DEFAULT})")
    s.set_defaults(func=cmd_attach)

    s = sub.add_parser("detach", aliases=["d"], help="detach from an agent or group")
    s.add_argument("name", nargs="?")
    s.add_argument("-g", "--group", help="detach from this group's session")
    s.add_argument("--host", choices=hosts.CHOICES, default=hosts.DEFAULT,
                   help=f"pty host (default: {hosts.DEFAULT})")
    s.set_defaults(func=cmd_detach)

    s = sub.add_parser("ls", aliases=["list"], help="list agents (filter by group/type)")
    s.add_argument("-g", "--group", help="only agents in this group")
    s.add_argument("-t", "--type", help="only agents of this type (e.g. lead, ada)")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("find", aliases=["search"],
                       help="find past Claude sessions to resume or adopt")
    s.add_argument("query", nargs="?", help="substring over title, cwd, prompts")
    s.add_argument("-a", "--all", action="store_true",
                   help="include sessions whose transcript is gone (not resumable)")
    s.add_argument("-n", "--limit", type=int, help="max results (default 20)")
    s.add_argument("--json", action="store_true", help="JSONL output")
    s.set_defaults(func=cmd_find)

    s = sub.add_parser("send", help="deliver a message to an agent")
    s.add_argument("name")
    s.add_argument("message", nargs="+")
    s.add_argument("--from", dest="sender",
                   help="sender name shown to the receiver "
                        "(default: $AGENTS_AGENT_NAME or your OS login)")
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
