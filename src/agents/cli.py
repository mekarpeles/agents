"""agents — register, run, and message named Claude Code agents.

Three layers, each with one job:
    registry   agents.db — names, team, groups, the durable sessionId binding
    host       hosts.py — where claude runs: tmux (default), herdr, or foreground
    transport  transport.py — deliver a message to a running agent's native socket

Commands (selector: <name> | -t <team> | -g <group> | --type <type>):
    register <name> [-t team] [-g groups] [--cwd D] [--identity F] [--prompt T] [--session-id ID]
    up     [<selector>] [--host tmux|herdr|foreground] [--prompt T] [--no-inject]
    down   [<selector>]
    attach [<name> | -t <team>] [--host H]
    detach [<name> | -t <team>] [--host H]
    group  add|rm <name> <group...>   |   team add|rm <name> [team]
    send <name> <message...> [--from who]
    ls [-t [team]]   |   find [query]   |   rm <name>   |   migrate   |   help

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
import time

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


def _caller_agent():
    """The registered agent making this call, resolved from $CLAUDE_CODE_SESSION_ID
    (the same identity `whoami` uses). None when the caller is a human shell or a
    session the registry doesn't know."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    return db.get_by_session(sid) if sid else None


def cmd_register(args):
    # When an *agent* registers another agent, the new agent reports to it by
    # default (the caller is the lead) — unless an explicit --team was given.
    team = args.team
    lead = None
    if team is None:
        caller = _caller_agent()
        if caller and caller.get("name") != args.name:
            team = lead = caller["name"]
    db.register(args.name, agent_type=args.type, role=args.role,
                workspace=args.workspace, cwd=args.cwd, initial_prompt=args.prompt,
                identity_path=args.identity, session_id=args.session_id,
                team=team)
    db.set_archived(args.name, False)  # (re-)registering reactivates an archived agent
    home.ensure_home(args.name, cwd=args.cwd, agent_type=args.type,
                     identity_path=args.identity, initial_prompt=args.prompt,
                     session_id=args.session_id)
    bound = f" (bound to session {args.session_id})" if args.session_id else ""
    reports = f" (reports to {lead})" if lead else ""
    print(f"agents: registered '{args.name}' ({args.type}); "
          f"home: {home.home_dir(args.name)}{bound}{reports}")
    return 0


def cmd_group(args):
    """Add/remove this agent's groups — the labels (circles) it belongs to. An
    agent may be in many; groups select and filter, they don't colocate."""
    if not db.get(args.name):
        return _err(f"'{args.name}' is not a registered agent")
    (db.add_groups if args.op == "add" else db.remove_groups)(args.name, *args.groups)
    cur = db.groups_of(db.get(args.name))
    print(f"agents: '{args.name}' groups: {','.join(cur) or '(none)'}")
    return 0


def cmd_team(args):
    """Set (add) or clear (rm) the team — the lead an agent reports to. One team
    per agent; the team shares one tmux session on `up`."""
    if not db.get(args.name):
        return _err(f"'{args.name}' is not a registered agent")
    if args.op == "add" and not args.team:
        return _err("usage: agents team add <name> <team>")
    db.set_team(args.name, args.team if args.op == "add" else None)
    cur = db.get(args.name).get("team")
    print(f"agents: '{args.name}' team: {cur or '(none)'}")
    return 0


def _rehome_transcripts(old_cwd, new_cwd):
    """Move an agent's Claude transcripts from the project dir keyed by `old_cwd`
    to the one keyed by `new_cwd`. Claude derives a session's project dir from the
    cwd string (see `session.project_dir` — every non-alphanumeric char → '-'), so
    when an agent's cwd changes (its cwd was its home dir and the home moved on
    rename) the transcript has to follow or `claude --resume` looks in the wrong,
    empty dir. Returns the count moved."""
    old_slug = session.project_dir(old_cwd)
    new_slug = session.project_dir(new_cwd)
    if not os.path.isdir(old_slug) or os.path.realpath(old_slug) == os.path.realpath(new_slug):
        return 0
    os.makedirs(new_slug, exist_ok=True)
    moved = 0
    for fn in os.listdir(old_slug):
        dst = os.path.join(new_slug, fn)
        if not os.path.exists(dst):
            os.rename(os.path.join(old_slug, fn), dst)
            moved += 1
    try:
        os.rmdir(old_slug)  # tidy up if now empty (the whole dir was this agent's)
    except OSError:
        pass
    return moved


def cmd_rename(args):
    """Rename an agent end to end: the registry row, its home dir, the reports that
    pointed at it as their lead, and — when its cwd was its home dir — the Claude
    transcript, so `up` still resumes. Refuses while the agent is live (its tmux
    session and session binding would be orphaned) unless --force stops it first."""
    old, new = args.old, args.new
    if not db.get(old):
        return _err(f"'{old}' is not a registered agent")
    if old == new:
        return _err("the new name is the same as the old one")
    if "/" in new or new in (".", ".."):
        return _err(f"'{new}' is not a valid agent name")
    if db.get(new):
        return _err(f"'{new}' already exists — pick another name")
    old_home, new_home = home.home_dir(old), home.home_dir(new)
    if os.path.exists(new_home):
        return _err(f"home dir {new_home} already exists — remove it or pick "
                    f"another name")

    status, sid = _ensure_stopped_for_restart(old, getattr(args, "force", False))
    if status == "needs_force":
        return _err(f"'{old}' is running; stop it first (`agents down {old}`) or "
                    f"`agents rename -f {old} {new}` to stop and rename it")
    if status == "stuck":
        return _err(f"'{old}' did not stop in time — run `agents down {old}` then "
                    f"retry the rename")

    # cwd that was the home dir has to move with the home (and so does the
    # transcript); a cwd pointing at a real project checkout is name-independent
    # and stays put — renaming such an agent never touches Claude's transcript.
    reg = db.get(old)
    cwd = reg.get("cwd") or home.agent_cwd(old)
    cwd_is_home = bool(cwd) and os.path.realpath(cwd) == os.path.realpath(old_home)

    db.rename(old, new)  # name + repoint reports (one transaction)
    if os.path.isdir(old_home):
        os.rename(old_home, new_home)

    new_cwd, moved = cwd, 0
    if cwd_is_home:
        new_cwd = new_home
        try:
            with open(os.path.join(new_home, "cwd"), "w") as f:
                f.write(new_cwd)
        except OSError:
            pass
        if sid:
            moved = _rehome_transcripts(cwd, new_cwd)
    # repoint the name-embedding columns (cwd if it moved, identity_path if it
    # pointed inside the old home); register coalesces, leaving the rest untouched.
    ident = reg.get("identity_path")
    if ident and ident.startswith(old_home):
        ident = ident.replace(old_home, new_home, 1)
    db.register(new, cwd=new_cwd, identity_path=ident)

    extra = f"; moved {moved} transcript file(s)" if moved else ""
    tail = " (it was stopped to rename)" if status == "stopped" else ""
    print(f"agents: renamed '{old}' → '{new}'{extra}{tail}")
    if status == "stopped":
        print(f"        `agents up {new}` to resume it.")
    return 0


def _existing_session(target):
    """An unregistered target that already names a session: (sid, cwd, label).

    Everything is a session, so `up <target>` should resume an existing one rather
    than spawn a fresh agent. Matches a live session (by display name or
    id/prefix) or a past session in history (by id/prefix). (None, None, target)
    when the target names nothing yet — a brand-new agent.
    """
    for rec in native.list_sessions():
        s = rec.get("sessionId") or ""
        if rec.get("name") == target or s == target or (target and s.startswith(target)):
            return s, rec.get("cwd"), rec.get("name") or s[:8]
    for r in history.find_sessions(include_gone=False):
        s = r["session_id"]
        if s == target or (len(target) >= 6 and s.startswith(target)):
            return s, r["cwd"], s[:8]
    return None, None, target


def _host_key(reg, team_override=None):
    """The tmux session an agent colocates in: its team (lead + reports share one
    session), or an explicit team when bringing a whole team up. None → the host
    gives it a standalone session (`agents-<name>`). Groups are labels, not
    sessions, so they never key the host."""
    return team_override or (reg.get("team") if reg else None)


def _up_one(name, host, prompt=None, cwd=None, identity=None, inject=True,
            team=None):
    """Launch, resume, or adopt-and-resume one session on `host`. Exit code.

    A registered name uses its home; an unregistered name that already matches an
    existing session (live or in history) is adopted and resumed; anything else is
    a brand-new agent.
    """
    reg = db.get(name)
    if reg is None:
        sid, scwd, _ = _existing_session(name)
        if sid:
            db.register(name, session_id=sid, cwd=scwd or cwd)  # adopt an existing session
        else:
            db.register(name, cwd=cwd)   # brand-new agent
        reg = db.get(name)
    if reg.get("archived"):
        db.set_archived(name, False)  # launching an archived agent restores it
    acwd = home.agent_cwd(name) or reg.get("cwd") or cwd or os.getcwd()
    home.ensure_home(name, cwd=acwd, identity_path=identity,
                     initial_prompt=prompt or reg.get("initial_prompt"),
                     session_id=reg.get("session_id"))  # carry an adopted binding into the home
    group = _host_key(reg, team)  # tmux session key: the team (colocation), not groups

    resume_id = home.read_session_id(name)
    if resume_id:
        try:
            rec = native.resolve(resume_id)
            return _err(f"'{name}' is already running (session {resume_id[:8]}, "
                        f"pid {rec.get('pid')}). Use `agents attach {name}` to open "
                        f"it (moved into tmux if it isn't already), "
                        f"`agents send {name} ...` to message it, or "
                        f"`agents down {name}` to stop it.")
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
    """Validate 'exactly one of <name> or a selector'. Returns error|None.

    The single rule behind every targeting command — set ops (`up`/`down`) read it
    through `_selection`, single ops (`attach`/`detach`) call it directly. A
    selector is --group, --type, or --team (short -g / -t for team).
    """
    selector = (getattr(args, "group", None) or getattr(args, "type", None)
                or getattr(args, "team", None))
    if args.name and selector:
        return "specify an agent name OR a selector, not both"
    if not args.name and not selector:
        return "specify an agent name or a selector (-g <group>, -t <team>, --type)"
    return None


def _selection(args):
    """The agents a set command targets: a bare <name>, or the agents matching a
    selector (--group / --type / --team). Returns (names, error). A --team names a
    lead and brings up/stops the lead plus everyone reporting to it."""
    err = _one_target(args)
    if err:
        return None, err
    if args.name:
        return [args.name], None
    rows = db.select(group=args.group, agent_type=getattr(args, "type", None),
                     team=getattr(args, "team", None))
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
    # `up --team X` colocates the lead + its reports in one tmux session named X,
    # so pass the team through as the host key even for the lead (whose own team
    # column is empty). A group/type selection keeps each agent's own team.
    rc = 0
    for n in names:
        rc |= _up_one(n, host, inject=not args.no_inject,
                      team=getattr(args, "team", None))
    return rc


def _resolve_target(target):
    """Resolve a name/id shown in `ls` to (session_id, label).

    Checks registered agents first, then live sessions — by display name, exact
    sessionId, or sessionId prefix — so anything `ls` lists (registered or a loose
    live session like 'projects-70' or 'dc33a929') can be acted on. (None, target)
    when nothing matches.
    """
    reg = db.get(target)
    if reg:
        return (reg.get("session_id") or home.read_session_id(target)), target
    sid = home.read_session_id(target)
    if sid:
        return sid, target
    for rec in native.list_sessions():
        s = rec.get("sessionId") or ""
        if rec.get("name") == target or s == target or (target and s.startswith(target)):
            return s, rec.get("name") or s[:8]
    return None, target


def _resolve_session_id(value):
    """Resolve a bare session identifier (for `send -s`) to a full sessionId:
    (sid, label, err). Accepts a full sessionId, a unique id prefix, an exact live
    name, or a pid — matched against live sessions, so the same values `ls` prints
    (an 8-char prefix) work here too. A prefix matching more than one live session
    returns an err (ambiguous). When nothing live matches, the value is passed
    through unchanged so the transport does the authoritative resolve (and raises
    its own SessionNotFound if it is truly gone)."""
    pref = []
    for rec in native.list_sessions():
        s = rec.get("sessionId") or ""
        if value == s or value == rec.get("name") or value == str(rec.get("pid")):
            return s, s[:8], None
        if len(value) >= 4 and s.startswith(value):
            pref.append(s)
    uniq = list(dict.fromkeys(pref))
    if len(uniq) > 1:
        return None, None, (
            f"'{value}' matches {len(uniq)} live sessions "
            f"({', '.join(x[:8] for x in uniq)}) — use more characters")
    if uniq:
        return uniq[0], uniq[0][:8], None
    return value, value[:8], None


def _down_one(name):
    """Stop one agent or live session: SIGTERM the pid resolved from its sessionId.

    Host-agnostic — when claude exits, its tmux window/herdr pane closes on its
    own, so there is nothing host-specific to tear down here.
    """
    sid, label = _resolve_target(name)
    if not sid:
        if db.get(name):  # registered, just never bound to a session
            print(f"agents: '{name}' is not running")
            return 0
        return _err(f"'{name}' is not a registered agent or live session")
    try:
        rec = native.resolve(sid)
    except native.SessionNotFound:
        print(f"agents: '{label}' is not running")
        return 0
    pid = rec.get("pid")
    try:
        os.kill(pid, 15)  # SIGTERM
        print(f"agents: stopped '{label}' (pid {pid})")
    except ProcessLookupError:
        print(f"agents: '{label}' already stopped")
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
    """attach/detach are the same single-target op on a host: a named agent (its
    team session, or a standalone one), or a whole team's session. `op` is
    'attach' or 'detach'. A team is the tmux session; groups are labels, so you
    attach by name or by --team, never by --group."""
    host = _host(args)
    team = getattr(args, "team", None)
    if team and args.name:
        return _err("specify an agent name OR --team, not both")
    if not team and not args.name:
        return _err("specify an agent name or --team <team>")
    key = team if team else (db.get(args.name) or {}).get("team")
    try:
        getattr(host, op)(None if team else args.name, group=key)
    except (hosts.Unsupported, RuntimeError) as e:
        return _err(str(e))
    return 0


def _is_running(sid):
    """True if a live session currently answers for `sid`."""
    try:
        native.resolve(sid)
        return True
    except native.SessionNotFound:
        return False


def _await_stopped(sid, timeout=4.0):
    """Wait until no live session answers for `sid` (its socket is gone)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            native.resolve(sid)
        except native.SessionNotFound:
            return True
        time.sleep(0.1)
    try:
        native.resolve(sid)
        return False
    except native.SessionNotFound:
        return True


def _ensure_stopped_for_restart(name, force):
    """Shared gate for ops that restart an agent's session in place (attach into
    tmux, rename): such an op interrupts a live agent, so it refuses unless forced.
    Returns (status, sid):
      'ok'          — not running (or no session); proceed.
      'stopped'     — was running; --force stopped it; proceed.
      'needs_force' — running and no --force; the caller refuses with its own hint.
      'stuck'       — --force, but it would not stop; the caller errors.
    """
    sid, _ = _resolve_target(name)
    if not (sid and _is_running(sid)):
        return "ok", sid
    if not force:
        return "needs_force", sid
    _down_one(name)
    return ("stopped" if _await_stopped(sid) else "stuck"), sid


def _promote_to_tmux(name, host, team):
    """Make a live-but-not-tmux agent attachable: stop its current process and
    resume the same session inside a tmux window (its team's session, or a
    standalone one). Lossless — we hold the sessionId, so `up --resume` reattaches
    the exact transcript. You cannot bind a second terminal to an already-running
    foreground process, so moving it is the honest way to keep the 'up ⇒
    attachable' invariant."""
    sid, _ = _resolve_target(name)
    if sid:
        try:
            rec = native.resolve(sid)
        except native.SessionNotFound:
            rec = None
        if rec and rec.get("pid"):
            print(f"agents: '{name}' is running outside tmux (pid {rec['pid']}); "
                  f"moving it into a tmux window (resuming {sid[:8]})…")
            try:
                os.kill(rec["pid"], 15)  # SIGTERM; the session resumes from disk
            except ProcessLookupError:
                pass
            if not _await_stopped(sid):
                return _err(f"'{name}' did not stop in time — run `agents down "
                            f"{name}` then `agents attach {name}`.")
    return _up_one(name, host, inject=False, team=team)  # resume detached in tmux


def cmd_attach(args):
    host = _host(args)
    # A whole-team attach, or a non-tmux host (herdr/foreground manages its own
    # panes): defer straight to the host's attach.
    if getattr(args, "team", None) or host.name != "tmux":
        return _attach_detach(args, "attach")
    if not args.name:
        return _err("specify an agent name or --team <team>")
    key = (db.get(args.name) or {}).get("team")  # the agent's tmux session key
    if host.has_window(args.name, group=key):
        return _attach_detach(args, "attach")
    # Up but not in tmux (foreground/adopted): attaching means restarting it in a
    # tmux window, which interrupts it — so a live agent is refused unless --force
    # (the same gate `rename` uses). A mid-task agent shouldn't be moved on a stray
    # attach.
    status, _ = _ensure_stopped_for_restart(args.name, getattr(args, "force", False))
    if status == "needs_force":
        return _err(f"'{args.name}' is running outside tmux; attaching restarts it. "
                    f"`agents send {args.name} ...` to message it without "
                    f"interrupting, or `agents attach -f {args.name}` to move it "
                    f"into tmux.")
    if status == "stuck":
        return _err(f"'{args.name}' did not stop in time — run `agents down "
                    f"{args.name}` then `agents attach {args.name}`.")
    rc = _promote_to_tmux(args.name, host, key)
    if rc != 0:
        return rc
    time.sleep(0.3)  # let the new pane register before we hand off to tmux
    return _attach_detach(args, "attach")


def cmd_detach(args):
    return _attach_detach(args, "detach")


# The status light: green = up, orange = down, red = archived.
_STATUS_COLOR = {"up": "32", "down": "38;5;208", "archived": "31"}


def _dot(status):
    """A colored status light (plain '●' when stdout isn't a terminal)."""
    code = _STATUS_COLOR.get(status, "0")
    return f"\033[{code}m●\033[0m" if sys.stdout.isatty() else "●"


def _clip(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


_ALL_TEAMS = "*"  # `ls -t` with no value: tree of every team

# (field key, header, max width) in display order: NAME SESSION STATUS TYPE GROUPS DESC
_LS_COLS = [("name", "NAME", 28), ("sid", "SESSION", 8), ("status", "STATUS", 8),
            ("type", "TYPE", 8), ("groups", "GROUPS", 20), ("desc", "DESC", 44)]


def _trunc(s, n):
    """Truncate to n columns, preserving leading whitespace (tree indentation)."""
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _print_rows(rows):
    """Print rows (each a dict with the _LS_COLS keys + 'status') as an aligned
    table whose column widths fit the content, capped by _LS_COLS."""
    w = {}
    for key, hdr, cap in _LS_COLS:
        w[key] = min(cap, max(len(hdr), *(len(str(r.get(key, ""))) for r in rows)))
    print("  " + "  ".join(f"{hdr:<{w[key]}}" for key, hdr, _ in _LS_COLS).rstrip())
    for r in rows:
        cells = []
        for key, _, _ in _LS_COLS:
            val = (_trunc(r.get("name", ""), w["name"]) if key == "name"
                   else _clip(str(r.get(key, "")), w[key]))
            cells.append(f"{val:<{w[key]}}")
        print(f"{_dot(r['status'])} " + "  ".join(cells).rstrip())


def _tree_rows(entries):
    """Nest entries into a lead→reports forest: each agent whose team names
    another listed agent hangs under it; leads, standalones, and orphans are
    roots. Returns rows with the NAME cell carrying tree branches."""
    by_name = {e["name"]: e for e in entries}
    children, roots = {}, []
    for e in entries:
        t = e["team"]
        if t and t in by_name and t != e["name"]:
            children.setdefault(t, []).append(e)
        else:
            roots.append(e)
    rows = []

    def walk(e, prefix, is_last, depth):
        if depth == 0:
            cell, child_prefix = e["name"], ""
        else:
            cell = prefix + ("└─ " if is_last else "├─ ") + e["name"]
            child_prefix = prefix + ("   " if is_last else "│  ")
        rows.append({**e, "name": cell})
        kids = sorted(children.get(e["name"], []), key=lambda x: x["name"])
        for i, k in enumerate(kids):
            walk(k, child_prefix, i == len(kids) - 1, depth + 1)

    for r in sorted(roots, key=lambda e: (e["type"] != "lead", e["name"])):
        walk(r, "", True, 0)
    return rows


def cmd_ls(args):
    """List agents and every active session. Registered agents (any status) plus
    live Claude sessions that aren't registered — because anything that's `up` is
    addressable (`send -s <id>`). The DESC column is the session's AI title (or a
    registered agent's task). `-t`/`--team` switches to a tree view (reports nested
    under their lead); narrowing to a registered subset (-g/--type/--down/
    --archived) drops the unregistered sessions, which have none of those."""
    team_mode = args.team is not None
    specific = args.team if args.team not in (None, _ALL_TEAMS) else None
    show_archived = args.all or args.archived
    agents = db.select(group=args.group, agent_type=args.type, team=specific,
                       include_archived=show_archived)
    live = {r.get("sessionId"): r for r in native.list_sessions()}
    titles = {r["session_id"]: r["title"]
              for r in history.find_sessions(include_gone=True)}
    narrowed = bool(args.group or args.type or specific or args.down or args.archived)

    entries, bound = [], set()
    for a in agents:
        sid = a.get("session_id")
        bound.add(sid)
        status = ("archived" if a.get("archived")
                  else "up" if sid in live else "down")
        if args.running and status != "up":
            continue
        if args.down and status != "down":
            continue
        if args.archived and status != "archived":
            continue
        desc = titles.get(sid) or (home.initial_prompt(a["name"]) or "")
        entries.append({"status": status, "name": a["name"], "sid": (sid or "")[:8],
                        "type": a.get("type") or "", "groups": ",".join(db.groups_of(a)),
                        "desc": desc, "team": a.get("team") or ""})

    # Flat view also lists live sessions that aren't registered agents (team view
    # is a registry structure, so it shows only registered agents).
    if not team_mode and not narrowed:
        for sid, rec in live.items():
            if sid in bound:
                continue
            desc = titles.get(sid) or os.path.basename((rec.get("cwd") or "").rstrip("/"))
            entries.append({"status": "up", "name": rec.get("name") or "—",
                            "sid": (sid or "")[:8], "type": "", "groups": "",
                            "desc": desc, "team": ""})

    if not entries:
        print("agents: no agents match" if (narrowed or args.running or team_mode)
              else "agents: none registered")
        return 0
    _print_rows(_tree_rows(entries) if team_mode else entries)
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
    parts = args.message
    if args.session:  # target a live session id directly; the name slot is message
        sid, target, err = _resolve_session_id(args.session)
        if err:
            return _err(err)
        parts = ([args.name] if args.name else []) + args.message
    elif args.name:
        sid, target = _resolve_target(args.name)
        if not sid:
            return _err(f"'{args.name}' is not a registered agent or live session")
    else:
        return _err("specify an agent name or -s <session_id>")
    if not parts:
        return _err("nothing to send")
    sender = _sender(args.sender)
    try:
        native.send(sid, " ".join(parts), sender=sender)
    except (native.SessionNotFound, native.DeliveryError) as e:
        return _err(str(e))
    print(f"agents: delivered to '{target}'")
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
    groups = db.groups_of(reg)
    info = {
        "name": name,
        "type": reg.get("type"),
        "team": reg.get("team"),
        "groups": groups,
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
        f"team {info['team']}" if info["team"] else "",
        f"groups {','.join(groups)}" if groups else "") if p)
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


def _clone_version(clone_dir, branch=None):
    """A human label for the version a clone landed on — the pyproject version plus
    the short commit, e.g. '0.1.0 (fc295fe)' (prefixed with the branch for
    `--testing`). Falls back to whatever is available (sha alone, or 'latest')."""
    r = subprocess.run(["git", "-C", clone_dir, "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True)
    sha = (r.stdout or "").strip() if r.returncode == 0 else ""
    pyver = ""
    try:
        import re
        m = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"',
                      open(os.path.join(clone_dir, "pyproject.toml")).read())
        pyver = m.group(1) if m else ""
    except OSError:
        pass
    label = " ".join(p for p in (pyver, f"({sha})" if sha else "") if p) or "latest"
    return f"{branch} {label}" if branch else label


def cmd_upgrade(args):
    """Upgrade the agents CLI from GitHub (clone + pipx reinstall).

    Simpler than cmux's upgrade: there's no dependency to re-inject because
    claudio is vendored. `--testing [branch]` installs a branch instead of main,
    so a fix can be validated on one machine without changing what a plain
    `agents upgrade` installs anywhere else.
    """
    import tempfile
    tmp = tempfile.mkdtemp()
    clonedir = os.path.join(tmp, "agents")
    clone = ["git", "clone", "--depth=1"]
    if args.branch:
        clone += ["--branch", args.branch]
    clone += ["https://github.com/mekarpeles/agents.git", clonedir]
    r = subprocess.run(clone, capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return _err(f"clone failed — {(r.stderr or '').strip()}")
    ver = _clone_version(clonedir, args.branch)
    if args.branch:
        print(f"agents: ⚠ installing from {args.branch!r}, not the stable release — "
              f"run `agents upgrade` (no flags) to return to main.")
    print(f"agents: pulling {ver} and upgrading via pipx into venv 'agents'…")
    # uv-backed pipx refuses to overwrite an existing venv without UV_VENV_CLEAR,
    # and may serve a stale cached build without UV_NO_CACHE. Capture pipx's own
    # chatter so the line above is the whole story; surface it only on failure.
    env = dict(os.environ, UV_VENV_CLEAR="1", UV_NO_CACHE="1")
    r2 = subprocess.run(["pipx", "install", "--force", clonedir], env=env,
                        capture_output=True, text=True)
    shutil.rmtree(tmp, ignore_errors=True)
    if r2.returncode != 0:
        detail = ((r2.stderr or "") + (r2.stdout or "")).strip()
        return _err("upgrade failed" + (f":\n{detail}" if detail else "") +
                    "\nIf pipx uses uv, run:\n"
                    "  UV_VENV_CLEAR=1 UV_NO_CACHE=1 pipx install --force "
                    "git+https://github.com/mekarpeles/agents.git")
    print(f"agents: upgraded to {ver}.")
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
        items = db.all_groups()
    elif args.kind == "teams":
        items = db.all_teams()
    elif args.kind == "types":
        items = home.available_types()
    elif args.kind == "targets":  # registered agents + live sessions (down/send)
        names = {a["name"] for a in db.all_agents()}
        live = {r.get("name") for r in native.list_sessions() if r.get("name")}
        items = sorted(names | live)
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


class _Parser(argparse.ArgumentParser):
    """Top-level parser that prints full help on an unknown command instead of a
    terse 'invalid choice' line (which also leaked `_complete` and every alias)."""

    def error(self, message):
        self.print_help(sys.stderr)
        sys.exit(2)


def build_parser():
    p = _Parser(prog="agents", description=__doc__.splitlines()[0])
    # Shorthand so `agents --configure` works like the `configure` subcommand.
    p.add_argument("--configure", action="store_true",
                   help="run interactive configuration (same as `agents configure`)")
    # Not required: a bare `agents` prints help instead of an argparse error.
    # metavar collapses the usage brace-list (and hides the internal `_complete`).
    # parser_class keeps subcommands on the default parser, so a missing-arg error
    # inside a subcommand still prints that subcommand's terse error, not full help.
    sub = p.add_subparsers(dest="cmd", metavar="<command>",
                           parser_class=argparse.ArgumentParser)

    s = sub.add_parser("register", help="register an agent (does not launch)")
    s.add_argument("name")
    s.add_argument("--type", default="default",
                   help="agent type seeding AGENTS.md "
                        f"({', '.join(home.available_types())})")
    s.add_argument("--role")
    s.add_argument("-g", "--group", dest="workspace",
                   help="group label(s) this agent belongs to; comma-separated for "
                        "several (agents up -g <group>)")
    s.add_argument("-t", "--team",
                   help="the lead this agent reports to (its team); up/down -t <team>")
    s.add_argument("--cwd", help="project dir claude runs in (default: the home dir)")
    s.add_argument("--identity", help="optional identity.md to seed")
    s.add_argument("--prompt", help="initial task written to PROMPT.md")
    s.add_argument("--session-id", dest="session_id",
                   help="adopt an existing Claude sessionId (resume it on `up`)")
    s.set_defaults(func=cmd_register)

    s = sub.add_parser("group", help="add or remove an agent's group labels")
    s.add_argument("op", choices=["add", "rm"])
    s.add_argument("name")
    s.add_argument("groups", nargs="+", metavar="group")
    s.set_defaults(func=cmd_group)

    s = sub.add_parser("team", help="set or clear the lead an agent reports to")
    s.add_argument("op", choices=["add", "rm"])
    s.add_argument("name")
    s.add_argument("team", nargs="?", help="lead name (required for add)")
    s.set_defaults(func=cmd_team)

    s = sub.add_parser("rename", help="rename an agent (home, reports, transcript follow)")
    s.add_argument("old")
    s.add_argument("new")
    s.add_argument("-f", "--force", action="store_true",
                   help="if it's running, stop it first, then rename")
    s.set_defaults(func=cmd_rename)

    s = sub.add_parser("up", help="launch (or resume) an agent, team, group, or type")
    s.add_argument("name", nargs="?")
    s.add_argument("-g", "--group", help="bring up every agent in this group")
    s.add_argument("-t", "--team", help="bring up a team (its lead + reports)")
    s.add_argument("--type", help="bring up every agent of this type")
    s.add_argument("--host", choices=hosts.CHOICES, default=None,
                   help="pty host (default: the configured host, tmux)")
    s.add_argument("--prompt")
    s.add_argument("--cwd")
    s.add_argument("--identity")
    s.add_argument("--no-inject", action="store_true")
    s.set_defaults(func=cmd_up)

    s = sub.add_parser("down", help="stop an agent, team, group, or type")
    s.add_argument("name", nargs="?")
    s.add_argument("-g", "--group", help="stop every agent in this group")
    s.add_argument("-t", "--team", help="stop a team (its lead + reports)")
    s.add_argument("--type", help="stop every agent of this type")
    s.set_defaults(func=cmd_down)

    s = sub.add_parser("attach", aliases=["a"], help="attach to an agent or team")
    s.add_argument("name", nargs="?")
    s.add_argument("-t", "--team", help="attach to this team's tmux session")
    s.add_argument("-f", "--force", action="store_true",
                   help="if it's running outside tmux, move it there without asking")
    s.add_argument("--host", choices=hosts.CHOICES, default=None,
                   help="pty host (default: the configured host, tmux)")
    s.set_defaults(func=cmd_attach)

    s = sub.add_parser("detach", aliases=["d"], help="detach from an agent or team")
    s.add_argument("name", nargs="?")
    s.add_argument("-t", "--team", help="detach from this team's tmux session")
    s.add_argument("--host", choices=hosts.CHOICES, default=None,
                   help="pty host (default: the configured host, tmux)")
    s.set_defaults(func=cmd_detach)

    s = sub.add_parser("list", aliases=["ls"],
                       help="list agents (-t for team/tree view; filter by group/type/status)")
    s.add_argument("-g", "--group", help="only agents in this group")
    s.add_argument("-t", "--team", nargs="?", const=_ALL_TEAMS, default=None,
                   metavar="TEAM",
                   help="team/tree view: reports nested under their lead "
                        "(optional TEAM limits to one team)")
    s.add_argument("--type", help="only agents of this type (e.g. lead, ada)")
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

    s = sub.add_parser("send", help="deliver a message to an agent or live session")
    s.add_argument("name", nargs="?")
    s.add_argument("message", nargs="*")
    s.add_argument("-s", "--session",
                   help="target a live session id directly (no registry lookup)")
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

    s = sub.add_parser("upgrade", help="upgrade the agents CLI from GitHub")
    s.add_argument("--testing", nargs="?", const="testing", default=None,
                   dest="branch", metavar="BRANCH",
                   help="install from a branch (default: 'testing') instead of main")
    s.set_defaults(func=cmd_upgrade)

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
    s.add_argument("kind", choices=["names", "groups", "teams", "types", "targets"])
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
