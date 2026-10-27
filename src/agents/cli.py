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
a resume never mis-attributes a session. Launched agents run in a configurable
permission mode (default: auto-accept edits) with their workspace pre-trusted, so
`up` can start them unattended — see `agents configure`.
"""

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import threading

from agents import (config, db, history, home, hosts, session, trust,
                    transport as native)


def _err(msg):
    """Print 'agents: <msg>' to stderr and return exit code 1."""
    print(f"agents: {msg}", file=sys.stderr)
    return 1


def _claude_cmd(name, resume_id):
    # A freshly launched session must start (and write its sessionId) without a
    # human keypress, or `up` can't bind it. The configured permission mode
    # supplies the flag; the workspace is pre-trusted separately (see trust.py) so
    # a gentle mode still clears the trust dialog. AGENTS_CLAUDE_CMD overrides the
    # whole command (tests point it at a fake claude).
    override = os.environ.get("AGENTS_CLAUDE_CMD")
    if override:
        cmd = shlex.split(override)
    else:
        cmd = ["claude", *config.launch_flags(config.load()["permission_mode"])]
    if resume_id:
        cmd += ["--resume", resume_id]
    return cmd


def _host(args):
    """The host for this invocation: --host if given, else the configured default."""
    return hosts.get(getattr(args, "host", None) or config.load()["host"])


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
    db.set_archived(args.name, False)  # (re-)registering reactivates an archived agent
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
    if reg.get("archived"):
        db.set_archived(name, False)  # launching an archived agent restores it
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
    trust.ensure_trusted(acwd)  # so claude's trust dialog can't stall the launch

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
    host = _host(args)
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
    host = _host(args)
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


# The status light: green = up, orange = down, red = archived.
_STATUS_COLOR = {"up": "32", "down": "38;5;208", "archived": "31"}


def _dot(status):
    """A colored status light (plain '●' when stdout isn't a terminal)."""
    code = _STATUS_COLOR.get(status, "0")
    return f"\033[{code}m●\033[0m" if sys.stdout.isatty() else "●"


def cmd_ls(args):
    # Archived agents are hidden unless asked for (-a / --archived).
    show_archived = args.all or args.archived
    agents = db.select(group=args.group, agent_type=args.type,
                       include_archived=show_archived)
    filtered = bool(args.group or args.type or args.running or args.down
                    or args.archived)
    live = {r.get("sessionId") for r in native.list_sessions()}
    rows = []
    for a in agents:
        if a.get("archived"):
            status = "archived"
        else:
            status = "up" if a.get("session_id") in live else "down"
        if args.running and status != "up":
            continue
        if args.down and status != "down":
            continue
        if args.archived and status != "archived":
            continue
        rows.append((a, status))
    if not rows:
        print("agents: no agents match" if filtered else "agents: none registered")
        return 0
    print(f"  {'NAME':<16} {'TYPE':<8} {'STATUS':<9} {'GROUP':<12} {'SESSION':<10} ROLE")
    for a, status in rows:
        sid = (a.get("session_id") or "")[:8]
        print(f"{_dot(status)} {a['name']:<16} {(a.get('type') or ''):<8} "
              f"{status:<9} {(a.get('workspace') or ''):<12} {sid:<10} "
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


def cmd_whoami(args):
    """Identify the agent this session is, by its Claude session id.

    Every Claude session knows its own id ($CLAUDE_CODE_SESSION_ID), and the
    registry binds that id to an agent — so a session identifies itself by the id
    it already has, not by anything `up` had to inject. That means it works for an
    adopted session too, not only ones `agents` launched.
    """
    sid = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        return _err("no session id to look up ($CLAUDE_CODE_SESSION_ID is unset) "
                    "— pass --session-id <id>")
    reg = db.get_by_session(sid)
    if not reg:
        return _err(f"session {sid[:8]} is not a registered agent "
                    f"(register it: agents register <name> --session-id {sid})")
    name = reg["name"]
    task = home.initial_prompt(name)
    info = {
        "name": name,
        "type": reg.get("type"),
        "group": reg.get("workspace"),
        "role": reg.get("role"),
        "cwd": home.agent_cwd(name),
        "session_id": sid,
        "home": home.home_dir(name),
        "archived": bool(reg.get("archived")),
    }
    if args.json:
        import json as _json
        print(_json.dumps(info))
        return 0
    tag = " ".join(p for p in (
        f"type {info['type']}" if info["type"] else "",
        f"group {info['group']}" if info["group"] else "") if p)
    print(f"You are '{name}'" + (f"  ({tag})" if tag else "")
          + ("  [archived]" if info["archived"] else ""))
    if info["role"]:
        print(f"  role     {info['role']}")
    print(f"  cwd      {info['cwd']}")
    print(f"  session  {sid[:8]}")
    print(f"  home     {info['home']}  (AGENTS.md + PROMPT.md)")
    if task:
        print(f"  task     {task.strip().splitlines()[0][:72]}")
    return 0


def cmd_rm(args):
    """Remove (archive) an agent: hidden from `ls` but kept so it can come back
    (`ls -a` to see it, `up`/`register` to restore). --purge deletes the row."""
    if not db.get(args.name):
        return _err(f"no agent named '{args.name}'")
    if args.purge:
        db.remove(args.name)
        print(f"agents: purged '{args.name}' from the registry "
              f"(home dir kept at {home.home_dir(args.name)})")
        return 0
    db.set_archived(args.name, True)
    print(f"agents: removed '{args.name}' — hidden from `ls` "
          f"(`agents ls -a` shows it; `agents up {args.name}` restores it)")
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


def _read_line(prompt):
    """Show `prompt` and read one line, or None if there's no way to ask.

    Uses stdin when it's a terminal — the normal, visible path for a direct
    `agents configure`. Falls back to /dev/tty only when stdin isn't a tty (under
    `curl … | bash`, stdin is the piped script). None means we couldn't ask.
    """
    try:
        if sys.stdin.isatty():
            return input(prompt)
        tty = open("/dev/tty", "r+")
        try:
            tty.write(prompt)
            tty.flush()
            return tty.readline()
        finally:
            tty.close()
    except (OSError, EOFError):
        return None


def _prompt_yes_no(question, default=True):
    """Ask a yes/no question; return a bool (the default if we can't ask)."""
    line = _read_line(question + (" [Y/n] " if default else " [y/N] "))
    if line is None:
        return default
    ans = line.strip().lower()
    return default if not ans else ans.startswith("y")


def _prompt_choice(question, choices, default):
    """Ask the user to pick one of `choices`; return the default if we can't ask
    or the answer isn't one of them."""
    line = _read_line(f"{question} [{'/'.join(choices)}] ({default}) ")
    if line is None:
        return default
    ans = line.strip()
    return ans if ans in choices else default


def _install_tmux():
    """Best-effort install of tmux via the platform package manager. Returns ok."""
    if shutil.which("brew"):
        cmd = ["brew", "install", "tmux"]
    elif shutil.which("apt-get"):
        cmd = ["sudo", "apt-get", "install", "-y", "tmux"]
    elif shutil.which("dnf"):
        cmd = ["sudo", "dnf", "install", "-y", "tmux"]
    else:
        return False
    return subprocess.run(cmd).returncode == 0


_COMPLETION_MARKER = "# agents shell completion"


def _shell_rc():
    """(shell, rc_path) for the user's login shell, or (None, None) if unknown."""
    shell = os.path.basename(os.environ.get("SHELL", ""))
    if shell == "zsh":
        return "zsh", os.path.expanduser("~/.zshrc")
    if shell == "bash":
        return "bash", os.path.expanduser("~/.bashrc")
    return None, None


def _completion_block(shell):
    if shell == "zsh":
        # Run compinit only if it hasn't already (e.g. by oh-my-zsh) — a second
        # compinit is slow and noisy.
        return (f"\n{_COMPLETION_MARKER}\n"
                "if ! whence compdef >/dev/null 2>&1; then "
                "autoload -Uz compinit && compinit; fi\n"
                "source <(agents completion zsh)\n")
    return f"\n{_COMPLETION_MARKER}\nsource <(agents completion bash)\n"


def _install_completion():
    """Append the completion source-line to the user's shell rc (idempotent)."""
    shell, rc = _shell_rc()
    if not rc:
        return _err("unknown shell; add `source <(agents completion zsh|bash)` "
                    "to your shell rc for tab-completion")
    existing = ""
    if os.path.exists(rc):
        try:
            existing = open(rc).read()
        except OSError as e:
            return _err(f"could not read {rc}: {e}")
    if _COMPLETION_MARKER in existing:
        print(f"agents: tab-completion already set up in {rc}")
        return 0
    if existing and not os.path.exists(rc + ".pre-agents"):
        try:  # one-time backup
            with open(rc + ".pre-agents", "w") as f:
                f.write(existing)
        except OSError:
            pass
    try:
        with open(rc, "a") as f:
            f.write(_completion_block(shell))
    except OSError as e:
        return _err(f"could not write {rc}: {e}")
    print(f"agents: tab-completion installed in {rc} — run `source {rc}` to use it now")
    return 0


def _install_ag_shortcut():
    """Create a short `ag` alias (a symlink next to `agents`), if `ag` is free.

    `ag` is commonly the_silver_searcher, so we never overwrite an existing one.
    """
    if shutil.which("ag"):
        print("agents: 'ag' is already a command on this system — leaving it alone")
        return 0
    agents_path = shutil.which("agents") or os.path.abspath(sys.argv[0])
    ag_path = os.path.join(os.path.dirname(agents_path), "ag")
    if os.path.lexists(ag_path):
        return 0
    try:
        os.symlink(agents_path, ag_path)
    except OSError as e:
        return _err(f"could not create 'ag' alias: {e}")
    print(f"agents: added short alias 'ag' -> {agents_path}")
    return 0


def cmd_configure(args):
    """Write ~/.agents/config.json (permission mode, host) and offer to install
    tmux. Interactive by default; flags set values without prompting, and
    --no-input accepts defaults (what the installer uses under pipe-to-bash)."""
    cfg = config.load()

    if args.mode:
        cfg["permission_mode"] = args.mode
    elif not args.no_input:
        gentle = cfg["permission_mode"] != "manual"
        gentle = _prompt_yes_no(
            "Let launched agents auto-accept file edits? (safer than full "
            "bypass; recommended)", gentle)
        cfg["permission_mode"] = "acceptEdits" if gentle else "manual"

    if args.host:
        cfg["host"] = args.host
    elif not args.no_input:
        cfg["host"] = _prompt_choice("Default host for launched agents?",
                                     hosts.CHOICES, cfg["host"])

    if cfg["host"] == "tmux" and not shutil.which("tmux"):
        if (not args.no_input) and _prompt_yes_no(
                "tmux is not installed (the default host). Install it now?", True):
            print("agents: installing tmux…")
            if _install_tmux():
                print("agents: tmux installed")
            else:
                print("agents: could not install tmux automatically — install it "
                      "yourself, or run agents with --host foreground",
                      file=sys.stderr)

    shell, rc = _shell_rc()
    did_install = False
    if shell and not args.no_input:
        try:
            installed = os.path.exists(rc) and _COMPLETION_MARKER in open(rc).read()
        except OSError:
            installed = False
        if not installed and _prompt_yes_no(
                f"Install agents tab-completion into {rc}?", True):
            did_install = _install_completion() == 0

    if not args.no_input and not shutil.which("ag"):
        if _prompt_yes_no("Add a short 'ag' alias for agents?", True):
            _install_ag_shortcut()

    saved = config.save(cfg)
    bypass = saved["permission_mode"] == "bypassPermissions"
    print(f"agents: configuration saved to {config.config_path()}")
    print(f"  permission mode: {saved['permission_mode']}"
          + ("  (⚠ bypasses all permission checks)" if bypass else ""))
    print(f"  host:            {saved['host']}")

    # A child process can't source the rc into the parent shell, so the equivalent
    # of "activate now" is to replace this process with a fresh login shell (which
    # re-reads the rc, completion and all). Only when interactive and not told to
    # skip (the installer passes --no-reload — exec'ing there would hijack it).
    if did_install and not args.no_reload and sys.stdout.isatty():
        if _prompt_yes_no(f"Reload {shell} now to activate completion?", True):
            sh = os.environ.get("SHELL") or shell
            print(f"agents: reloading {shell}…")
            sys.stdout.flush()
            os.execvp(sh, [sh, "-l"])  # replaces this process; does not return
    return 0


def cmd_complete(args):
    """Emit completion candidates for the shell scripts (one per line). Hidden."""
    if args.kind == "names":
        items = [a["name"] for a in db.all_agents()]
    elif args.kind == "groups":
        items = sorted({a["workspace"] for a in db.all_agents() if a.get("workspace")})
    elif args.kind == "types":
        items = home.available_types()
    else:
        items = []
    print("\n".join(items))
    return 0


def cmd_completion(args):
    """Print the shell completion script, or --install it into your shell rc."""
    if args.install:
        return _install_completion()
    if not args.shell:
        return _err("specify a shell (zsh|bash) or use --install")
    from agents import completion
    print(completion.script(args.shell))
    return 0


def cmd_help(args):
    build_parser().print_help()
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="agents", description=__doc__.splitlines()[0])
    # Shorthand so `agents --configure` works like the `configure` subcommand.
    p.add_argument("--configure", action="store_true",
                   help="run interactive configuration (same as `agents configure`)")
    # Not required: a bare `agents` prints help instead of an argparse error.
    # metavar collapses the usage brace-list (and hides the internal `_complete`).
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

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
    s.add_argument("--host", choices=hosts.CHOICES, default=None,
                   help="pty host (default: the configured host, tmux)")
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
    s.add_argument("--host", choices=hosts.CHOICES, default=None,
                   help="pty host (default: the configured host, tmux)")
    s.set_defaults(func=cmd_attach)

    s = sub.add_parser("detach", aliases=["d"], help="detach from an agent or group")
    s.add_argument("name", nargs="?")
    s.add_argument("-g", "--group", help="detach from this group's session")
    s.add_argument("--host", choices=hosts.CHOICES, default=None,
                   help="pty host (default: the configured host, tmux)")
    s.set_defaults(func=cmd_detach)

    s = sub.add_parser("list", aliases=["ls"],
                       help="list agents (filter by group/type/status)")
    s.add_argument("-g", "--group", help="only agents in this group")
    s.add_argument("-t", "--type", help="only agents of this type (e.g. lead, ada)")
    s.add_argument("--running", action="store_true", help="only agents with a live session")
    s.add_argument("--down", action="store_true", help="only stopped agents")
    s.add_argument("-a", "--all", action="store_true",
                   help="include archived agents (hidden by default)")
    s.add_argument("--archived", action="store_true", help="only archived agents")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("find",
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

    s = sub.add_parser("whoami",
                       help="identify this session's agent (by $CLAUDE_CODE_SESSION_ID)")
    s.add_argument("--session-id", dest="session_id",
                   help="look up this sessionId instead of the current session's")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_whoami)

    s = sub.add_parser("rm", help="remove an agent (archive; hidden from ls, kept)")
    s.add_argument("name")
    s.add_argument("--purge", action="store_true",
                   help="delete the registry row instead of archiving (keeps home dir)")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("migrate", help="migrate cmux state (~/.cmux) into agents")
    s.add_argument("names", nargs="*", help="limit to these agent names")
    s.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    s.add_argument("--link", action="store_true",
                   help="symlink homes instead of copying")
    s.set_defaults(func=cmd_migrate)

    s = sub.add_parser("configure",
                       help="set defaults (permission mode, host); install tmux")
    s.add_argument("--mode", choices=config.PERMISSION_MODES,
                   help="permission mode launched agents run under "
                        "(default: acceptEdits; bypassPermissions = YOLO)")
    s.add_argument("--host", choices=hosts.CHOICES, help="default pty host")
    s.add_argument("--no-input", action="store_true",
                   help="don't prompt; keep current values / accept defaults")
    s.add_argument("--no-reload", action="store_true",
                   help="don't offer to reload the shell after installing completion")
    s.set_defaults(func=cmd_configure)

    s = sub.add_parser("completion",
                       help="print (or --install) a shell completion script")
    s.add_argument("shell", nargs="?", choices=["zsh", "bash"],
                   help="shell to print the script for")
    s.add_argument("--install", action="store_true",
                   help="append the source line to your shell rc instead")
    s.set_defaults(func=cmd_completion)

    # hidden: the data source the completion scripts call on each <TAB>.
    # No `help=` kwarg, so argparse omits it from the command listing entirely.
    s = sub.add_parser("_complete")
    s.add_argument("kind", choices=["names", "groups", "types"])
    s.set_defaults(func=cmd_complete)

    # `help` as a real subcommand too, so `agents help` works like `agents --help`.
    s = sub.add_parser("help", help="show this help")
    s.set_defaults(func=cmd_help)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "configure", False) and not getattr(args, "cmd", None):
        sys.exit(cmd_configure(argparse.Namespace(
            mode=None, host=None, no_input=False, no_reload=False)))
    if not getattr(args, "cmd", None):
        parser.print_help()
        sys.exit(0)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
