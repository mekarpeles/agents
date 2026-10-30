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


def _parse_meta_assign(tokens):
    """Parse `key=value` tokens (or a whole `{json}` object) into a dict, for
    setting metadata. A token with no `=` is an error — dropping a key is `meta
    rm`, not an empty value."""
    import json
    out = {}
    for t in tokens or []:
        t = t.strip()
        if t.startswith("{"):
            out.update(json.loads(t))
            continue
        k, sep, v = t.partition("=")
        if not sep:
            raise ValueError(f"metadata must be key=value (got {t!r})")
        out[k.strip()] = v
    return out


def _parse_meta_query(tokens):
    """Parse `key` / `key=value` tokens into (key, value|None) filter pairs: a bare
    key matches existence (metadata.<key> is present), key=value matches equality."""
    out = []
    for t in tokens or []:
        t = t.strip()
        if not t:
            continue
        k, sep, v = t.partition("=")
        out.append((k.strip(), v if sep else None))
    return out


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
    # A just-launched session may need a moment to register its socket — tmux
    # execs claude so it's immediate, but herdr runs it as a grandchild of the
    # pane, so poll briefly. A failed nudge must never crash the launch: the agent
    # is already running and bound, so at worst it misses a reminder.
    body = "\n\n".join(parts)
    for _ in range(50):  # ~5s
        try:
            native.send(sid, body, sender="agents")
            return sid
        except (native.SessionNotFound, native.DeliveryError):
            time.sleep(0.1)
    print(f"agents: warning — could not inject startup prompt for '{name}' "
          f"(session {sid[:8]} not reachable yet)", file=sys.stderr)
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
    try:
        meta = _parse_meta_assign(getattr(args, "meta", None))
    except ValueError as e:
        return _err(str(e))
    db.register(args.name, agent_type=args.type, role=args.role,
                workspace=args.workspace, cwd=args.cwd, initial_prompt=args.prompt,
                identity_path=args.identity, session_id=args.session_id,
                team=team, meta=meta or None)
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


def cmd_meta(args):
    """Get/set/remove an agent's structured metadata — a JSON object of queryable
    keys (pr, issue, project, …) that resolve agent <-> issue <-> PR without
    encoding it in the name. `set` merges key=value pairs (preserving the rest);
    `rm` drops keys; `get` prints the object."""
    import json
    if not db.get(args.name):
        return _err(f"'{args.name}' is not a registered agent")
    if args.op == "get":
        print(json.dumps(db.meta_of(db.get(args.name)), indent=2))
        return 0
    if args.op == "rm":
        if not args.items:
            return _err("usage: agents meta rm <name> <key>...")
        db.remove_meta(args.name, [k.split("=", 1)[0].strip() for k in args.items])
    else:  # set
        try:
            updates = _parse_meta_assign(args.items)
        except ValueError as e:
            return _err(str(e))
        if not updates:
            return _err("usage: agents meta set <name> key=value...")
        db.set_meta(args.name, updates)
    cur = db.meta_of(db.get(args.name))
    print(f"agents: '{args.name}' meta: {json.dumps(cur) if cur else '(none)'}")
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


def _resolve_known_session(target):
    """(sid, cwd, err) for an rm/archive target that names an *existing* session —
    live or in history, by display name, exact sessionId, or id-prefix. Ambiguous
    prefix → err (so a destructive --purge never hits the wrong one); nothing
    matched → (None, None, None)."""
    hits = {}  # sid -> cwd ; exact id/name wins outright
    for rec in native.list_sessions():
        s = rec.get("sessionId") or ""
        if rec.get("name") == target or s == target:
            return s, rec.get("cwd"), None
        if len(target) >= 4 and s.startswith(target):
            hits[s] = rec.get("cwd")
    for r in history.find_sessions(include_gone=True):
        s = r["session_id"]
        if s == target:
            return s, r["cwd"], None
        if len(target) >= 6 and s.startswith(target):
            hits.setdefault(s, r["cwd"])
    if len(hits) > 1:
        return None, None, (f"'{target}' matches {len(hits)} sessions "
                            f"({', '.join(x[:8] for x in hits)}) — use more characters")
    if hits:
        sid, cwd = next(iter(hits.items()))
        return sid, cwd, None
    return None, None, None


def _host_key(reg, host, team_override=None):
    """The host grouping an agent colocates into — a tmux session or a herdr
    workspace. An explicit --team always wins. Otherwise the two hosts differ:

    - **tmux** colocates by *team* — a lead and its reports share the lead's
      session; a teamless agent gets its own standalone session (`agents-<name>`).
    - **herdr** gives each **lead its own space** and each **report a tab** in its
      lead's space, so a lead (or a teamless agent) keys to its own *name*, and a
      report keys to its *team* (its lead's space).

    Groups are labels, not sessions, so they never key the host."""
    if team_override:
        return team_override
    if reg is None:
        return None
    if getattr(host, "name", None) == "herdr":
        if reg.get("type") == "lead" or not reg.get("team"):
            return reg.get("name")          # lead / teamless → its own space
        return reg.get("team")              # report → a tab in its lead's space
    return reg.get("team")                  # tmux: the team is the session


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
            db.unignore_session(sid)  # adopting restores it, like launching an archived agent
        else:
            db.register(name, cwd=cwd)   # brand-new agent
        reg = db.get(name)
    if reg.get("archived"):
        db.set_archived(name, False)  # launching an archived agent restores it
    acwd = home.agent_cwd(name) or reg.get("cwd") or cwd or os.getcwd()
    home.ensure_home(name, cwd=acwd, identity_path=identity,
                     initial_prompt=prompt or reg.get("initial_prompt"),
                     session_id=reg.get("session_id"))  # carry an adopted binding into the home
    group = _host_key(reg, host, team)  # host colocation key (tmux session / herdr space)

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
                or getattr(args, "team", None) or getattr(args, "meta", None))
    if args.name and selector:
        return "specify an agent name OR a selector, not both"
    if not args.name and not selector:
        return ("specify an agent name or a selector "
                "(-g <group>, -t <team>, --type, --meta key[=value])")
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
    meta_q = _parse_meta_query(getattr(args, "meta", None))
    rows = db.select(group=args.group, agent_type=getattr(args, "type", None),
                     team=getattr(args, "team", None), meta=meta_q or None)
    if not rows:
        return None, "no agents match that selection"
    return [r["name"] for r in rows], None


def _detach_host(pty):
    """The host a detached/attached `up` uses: the configured host if it can host a
    detached pane (tmux/herdr), else tmux. `foreground` can't, so it falls back."""
    return pty if hosts.get(pty).group else "tmux"


def _resolve_host(args, n_targets):
    """Pick the host for this `up`, whether to attach after, and a note if a request
    had to be overridden. Returns (host, attach, note).

    No flag → the configured `up_mode` (foreground | detach | attach). The flags
    override it: `--host` names the host outright, `-F` forces foreground, `-d`
    forces a detached pty host, `-a` a detached pty host it then attaches to.
    Guardrails win last: foreground needs a controlling terminal and runs one agent,
    so a selection of agents or no tty (a script, or an agent spawning a report via
    a tool — neither has a tty) forces a detached pty host instead. A real terminal
    is enough even inside a Claude session, so we gate on the tty, not the caller."""
    cfg = config.load()
    pty = cfg["host"]
    want_attach = False
    if getattr(args, "host", None):
        host_name = args.host
        want_attach = bool(getattr(args, "attach", False))
    elif getattr(args, "foreground", False):
        host_name = "foreground"
    elif getattr(args, "detach", False):
        host_name = _detach_host(pty)
    elif getattr(args, "attach", False):
        host_name, want_attach = _detach_host(pty), True
    elif cfg.get("up_mode") == "detach":
        host_name = _detach_host(pty)
    elif cfg.get("up_mode") == "attach":
        host_name, want_attach = _detach_host(pty), True
    else:  # foreground (the default up_mode)
        host_name = "foreground"

    note = None
    selection = n_targets > 1 or bool(getattr(args, "team", None)
                                      or getattr(args, "group", None)
                                      or getattr(args, "type", None))
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if host_name == "foreground" and (selection or not interactive):
        why = ("it's a selection of agents" if selection
               else "there's no terminal to run it in")
        host_name, want_attach = _detach_host(pty), False
        note = f"can't run foreground ({why}); detached on {host_name}"
    if want_attach and (host_name == "foreground" or not interactive):
        want_attach = False  # already inline, or nothing here to attach to
    return hosts.get(host_name), want_attach, note


def _attach_after_up(name, host):
    """Attach to an agent that `up -a` just started on a pty host. tmux attach
    execs (replaces this process); herdr focuses its tab."""
    time.sleep(0.3)  # let the pane/tab register before we hand off
    if host.name == "tmux":
        _attach_live_window(host, name)  # execs on success; returns False if not found
        return _err(f"'{name}' started but no tmux window was found to attach")
    try:
        host.attach(name, group=_host_key(db.get(name), host))
    except (hosts.Unsupported, RuntimeError) as e:
        return _err(str(e))
    return 0


def cmd_up(args):
    names, err = _selection(args)
    if err:
        return _err(err)
    host, attach, note = _resolve_host(args, len(names))
    if note:
        print(f"agents: {note}", file=sys.stderr)
    if args.name:  # a bare name keeps prompt/cwd/identity and may start anew
        rc = _up_one(args.name, host, prompt=args.prompt, cwd=args.cwd,
                     identity=args.identity, inject=not args.no_inject)
        if rc == 0 and attach:
            return _attach_after_up(args.name, host)
        return rc
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
    key = _host_key(db.get(args.name), host, team)
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
    a host, rename): such an op interrupts a live agent, so it refuses unless forced.
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


def _promote_to_host(name, host, team):
    """Make a live agent attachable on `host`: stop its current process and resume
    the same session there — a tmux window or a herdr tab, in its team's session/
    workspace or a standalone one. Lossless — we hold the sessionId, so `up
    --resume` reattaches the exact transcript. You cannot bind a second terminal to
    a process already running elsewhere, so moving it is the honest way to keep the
    'up ⇒ attachable' invariant across hosts."""
    sid, _ = _resolve_target(name)
    if sid:
        try:
            rec = native.resolve(sid)
        except native.SessionNotFound:
            rec = None
        if rec and rec.get("pid"):
            print(f"agents: '{name}' is running elsewhere (pid {rec['pid']}); "
                  f"moving it into {host.name} (resuming {sid[:8]})…")
            try:
                os.kill(rec["pid"], 15)  # SIGTERM; the session resumes from disk
            except ProcessLookupError:
                pass
            if not _await_stopped(sid):
                return _err(f"'{name}' did not stop in time — run `agents down "
                            f"{name}` then `agents attach {name}`.")
    return _up_one(name, host, inject=False, team=team)  # resume detached on host


def _attach_live_window(host, name):
    """If `name`'s live session runs in a tmux pane, `exec tmux attach` to that
    exact window (never returns). Otherwise return False. Finds the window by the
    agent's live pid — wherever it actually runs (a standalone `agents-<name>`
    session or a team session), regardless of what its registry team now says."""
    sid, _ = _resolve_target(name)
    if not (sid and _is_running(sid)):
        return False
    try:
        pid = native.resolve(sid).get("pid")
    except native.SessionNotFound:
        return False
    loc = host.locate_pid(pid) if pid else None
    if loc:
        host.attach_window(loc)  # execvp — does not return
    return False


def cmd_attach(args):
    host = _host(args)
    # A whole-team attach: the host opens the team's session/workspace as a unit.
    if getattr(args, "team", None):
        return _attach_detach(args, "attach")
    if not args.name:
        return _err("specify an agent name or --team <team>")
    name = args.name
    key = _host_key(db.get(name), host)  # its tmux session / herdr space
    # Already attachable on this host? tmux: a live pane found by pid (execs into
    # it). herdr: an existing tab to focus. Either way, no restart.
    if host.name == "tmux":
        if _attach_live_window(host, name):
            return 0  # unreachable: attach_window execs
    elif host.has_window(name, group=key):
        return _attach_detach(args, "attach")
    # Not on this host: live elsewhere (another host, or foreground/adopted) → move
    # it in, which restarts it, so refuse unless --force (the shared gate `rename`
    # uses); or down → just resume it here. Moving is lossless (resume by id).
    status, _ = _ensure_stopped_for_restart(name, getattr(args, "force", False))
    if status == "needs_force":
        return _err(f"'{name}' is running elsewhere; attaching moves (restarts) it "
                    f"into {host.name}. `agents send {name} ...` to message it "
                    f"without interrupting, or `agents attach -f {name}` to move it.")
    if status == "stuck":
        return _err(f"'{name}' did not stop in time — run `agents down "
                    f"{name}` then `agents attach {name}`.")
    rc = _promote_to_host(name, host, key)
    if rc != 0:
        return rc
    time.sleep(0.3)  # let the new pane/tab register before we hand off
    if host.name == "tmux" and _attach_live_window(host, name):
        return 0
    return _attach_detach(args, "attach")  # focus the new herdr tab / tmux session


def cmd_detach(args):
    return _attach_detach(args, "detach")


# The status light: green = up, orange = down, red = archived.
# Distinguished by SHAPE first, so it reads without color (colorblind-safe): a
# filled green light for up, a hollow circle (just an outline over the terminal
# background) for down, an ✗ for archived. Color is only an enhancement.
_STATUS_GLYPH = {"up": ("●", "32"), "down": ("○", ""), "archived": ("✗", "2")}


def _dot(status):
    """The status glyph — ● up (green), ○ down (hollow), ✗ archived. The shape
    carries the meaning; color is dropped when stdout isn't a terminal."""
    glyph, code = _STATUS_GLYPH.get(status, ("○", ""))
    return f"\033[{code}m{glyph}\033[0m" if code and sys.stdout.isatty() else glyph


def _clip(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


_ALL_TEAMS = "*"  # `ls -t` with no value: tree of every team

# (field key, header, max width) in display order: NAME SESSION STATUS TYPE GROUPS DESC
# The header line per agent. By default the META column is just a count of how
# many metadata/group fields an agent has; `--meta` expands them (stacked beneath,
# see _detail_lines) and `--meta <key>` surfaces that key's value inline here.
_HDR_COLS = [("name", "NAME", 30), ("sid", "SESSION", 8),
             ("type", "TYPE", 6), ("meta", "META", 24)]

_META_ORDER = ("project", "issue", "pr", "epics")   # the common keys lead


def _detail_lines(groups, meta, limit=None):
    """An agent's metadata and groups as stacked `key: value` lines (one per line)
    for the ls block — e.g. 'project: ol', 'epics: [13923, 13924]', 'groups:
    [solr, tags]'. Groups are folded in here as just another key. With `limit`, at
    most that many are shown and the rest collapse into a '… +N more' line."""
    out = []
    keys = ([k for k in _META_ORDER if k in meta]
            + sorted(k for k in meta if k not in _META_ORDER))
    for k in keys:
        v = meta[k]
        out.append(f"{k}: [{', '.join(str(x) for x in v)}]" if isinstance(v, list)
                   else f"{k}: {v}")
    if groups:
        out.append(f"groups: [{', '.join(groups)}]")
    if limit is not None and len(out) > limit:
        out = out[:limit] + [f"… +{len(out) - limit} more"]
    return out


def _trunc(s, n):
    """Truncate to n columns, preserving leading whitespace (tree indentation)."""
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _print_agents(rows):
    """Print each agent as a header line (name/session/status/type + a
    terminal-width-capped desc) with its metadata and groups stacked as `key:
    value` lines indented beneath it, so nothing overflows the screen."""
    w = {}
    for key, hdr, cap in _HDR_COLS:
        w[key] = min(cap, max(len(hdr), *(len(str(r.get(key, ""))) for r in rows)))
    fixed = 2 + sum(w[k] for k, _, _ in _HDR_COLS) + 2 * len(_HDR_COLS)
    total = shutil.get_terminal_size(fallback=(120, 40)).columns
    desc_w = max(20, total - fixed - 2)
    print(("  " + "  ".join(f"{hdr:<{w[key]}}" for key, hdr, _ in _HDR_COLS)
           + f"  {'DESC':<{desc_w}}").rstrip())
    for r in rows:
        name = _trunc(r.get("name", ""), w["name"])
        cells = [f"{name:<{w['name']}}"] + [
            f"{_clip(str(r.get(key, '')), w[key]):<{w[key]}}"
            for key, _, _ in _HDR_COLS[1:]]
        desc = _clip(str(r.get("desc", "")), desc_w)
        print((f"{_dot(r['status'])} " + "  ".join(cells) + "  " + desc).rstrip())
        # Keep the tree's vertical bars running down through the stacked meta
        # (detail_prefix carries them); flat view has none, so just indent.
        pad = "  " + r.get("detail_prefix", "  ")
        avail = max(10, total - len(pad))
        for line in r.get("details", []):
            print(pad + _clip(line, avail))


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
        # Detail lines sit in this node's child region, so they carry the same
        # continuation prefix — the tree's │ bars keep running past the meta.
        rows.append({**e, "name": cell, "detail_prefix": child_prefix})
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
    registered agent's task). The default is a tree (reports nested under their
    lead; loose live sessions as roots); `--flat` lists them in a flat table.
    `-t <team>` narrows to one team, and narrowing to a registered subset
    (-g/--type/--down/--archived) drops the unregistered sessions."""
    tree = not getattr(args, "flat", False)
    specific = args.team if args.team not in (None, _ALL_TEAMS) else None
    show_archived = args.all or args.archived
    # --meta modes: no flag -> META is a count; bare --meta -> expand all stacked;
    # --meta <key> -> filter by it AND show just that key inline in the META cell.
    meta_tokens = getattr(args, "meta", None)
    meta_keys = [t for t in (meta_tokens or []) if t is not None]
    expand = bool(meta_tokens) and any(t is None for t in meta_tokens)
    inline_keys = [] if expand else meta_keys
    meta_q = _parse_meta_query(meta_keys)
    agents = db.select(group=args.group, agent_type=args.type, team=specific,
                       include_archived=show_archived, meta=meta_q or None)
    live = {r.get("sessionId"): r for r in native.list_sessions()}

    if getattr(args, "json", False):
        import json as _json
        for a in agents:
            sid = a.get("session_id")
            status = ("archived" if a.get("archived")
                      else "up" if sid in live else "down")
            if args.running and status != "up":
                continue
            if args.down and status != "down":
                continue
            if args.archived and status != "archived":
                continue
            hd = home.home_dir(a["name"])
            print(_json.dumps({
                "name": a["name"], "type": a.get("type") or None,
                "team": a.get("team") or None, "groups": db.groups_of(a),
                "session_id": sid, "status": status,
                "cwd": a.get("cwd"), "home": hd if os.path.isdir(hd) else None,
                "desc": a.get("role"), "recap": a.get("recap"),
                "meta": db.meta_of(a),
            }))
        return 0

    titles = {r["session_id"]: r["title"]
              for r in history.find_sessions(include_gone=True)}
    narrowed = bool(args.group or args.type or specific or args.down
                    or args.archived or meta_q)

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
        # The one-liner says "where this agent is", freshest first: an agent-set
        # recap, else the operator's purpose (role), else its task, and only as a
        # last resort the Claude transcript title (which for recovered agents is
        # often a stale cmux message, not a useful description).
        desc = (a.get("recap") or a.get("role")
                or home.initial_prompt(a["name"]) or titles.get(sid) or "")
        desc = " ".join(desc.split())   # one line — collapse any newlines/runs
        meta_d = db.meta_of(a)
        full = _detail_lines(db.groups_of(a), meta_d)
        if inline_keys:                      # --meta <key>: surface that key
            scal, lst = [], []
            for tok in inline_keys:
                k = tok.split("=", 1)[0].strip()
                if k not in meta_d:
                    continue
                v = meta_d[k]
                if isinstance(v, list):      # a list (epics) -> stack it, full width
                    lst.append(f"{k}: [{', '.join(map(str, v))}]")
                else:                        # a scalar (pr, issue) -> inline
                    scal.append(f"{k}: {v}")
            meta_col, details = "  ".join(scal), lst
        elif expand:                         # bare --meta: expand all, stacked below
            meta_col, details = (str(len(full)) if full else ""), full
        else:                                # default: just the field count
            meta_col, details = (str(len(full)) if full else ""), []
        entries.append({"status": status, "name": a["name"], "sid": (sid or "")[:8],
                        "type": a.get("type") or "", "meta": meta_col, "desc": desc,
                        "team": a.get("team") or "", "details": details})

    # Also list live sessions that aren't registered agents (as tree roots, or
    # flat rows) — unless we're narrowing the registry, which they can't match.
    if not narrowed:
        ignored = db.ignored_sessions()
        for sid, rec in live.items():
            if sid in bound:
                continue
            archived = sid in ignored
            if archived and not show_archived:
                continue
            desc = titles.get(sid) or os.path.basename((rec.get("cwd") or "").rstrip("/"))
            desc = " ".join(desc.split())
            entries.append({"status": "archived" if archived else "up",
                            "name": rec.get("name") or "—",
                            "sid": (sid or "")[:8], "type": "", "meta": "",
                            "desc": desc, "team": "", "details": []})

    if not entries:
        print("agents: no agents match" if (narrowed or args.running)
              else "agents: none registered")
        return 0
    _print_agents(_tree_rows(entries) if tree else entries)
    return 0


def cmd_find(args):
    rows = history.find_sessions(
        args.query, deep=args.all, ignored=db.ignored_sessions(),
        show_archived=args.archived, limit=args.limit or 20)
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
        state = ("archived" if r.get("archived")
                 else "running" if r["session_id"] in running
                 else "idle" if r["present"] else "gone")
        cwd = os.path.basename(r["cwd"].rstrip("/")) or r["cwd"]
        print(f"{r['session_id']:<36} {state:<8} {r['msgs']:>5}  "
              f"{clip(cwd, 22):<22} {clip(r['title'], 56)}")
    print("\nresume:  claude --resume <SESSION>   "
          "(or: agents register <name> --cwd <cwd> --session-id <SESSION>; "
          "agents up <name>)", file=sys.stderr)
    return 0


def _sender(explicit):
    """The 'from' attribution: --from if given, else the calling *agent's*
    registered name (by $CLAUDE_CODE_SESSION_ID, like whoami), else — for a human
    terminal with no agent session — the OS user ($USER). Identity comes from the
    session, never an env var: an agent self-attributes by its registered name, and
    a person at a terminal sends as themselves.
    """
    import getpass
    if explicit:
        return explicit
    caller = _caller_agent()
    if caller:
        return caller["name"]
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


def _ago(iso):
    """A compact 'how long ago' for an ISO-8601 Z timestamp (e.g. '3h', '2d')."""
    if not iso:
        return ""
    import calendar
    try:
        t = calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))  # struct is UTC
    except (ValueError, TypeError):
        return ""
    s = max(0, int(time.time() - t))
    for n, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


def cmd_checkin(args):
    """Record a short recap of where an agent is — opt-in, agent-driven, never
    computed. By default the *calling* agent checks itself in (resolved from
    $CLAUDE_CODE_SESSION_ID, like whoami); --agent names another. The line is
    stored (with a timestamp) and appended to the home's recap.log journal."""
    name = args.agent
    if not name:
        reg = _caller_agent()
        if not reg:
            return _err("not a registered agent — run from an agent session, or "
                        "name one with --agent <name>")
        name = reg["name"]
    if not db.get(name):
        return _err(f"'{name}' is not a registered agent")
    text = " ".join(args.text).strip()
    if not text:
        return _err("usage: agents checkin [--agent NAME] <recap text>")
    ts = db.set_recap(name, text)
    home.append_recap(name, text, ts)
    print(f"agents: '{name}' recap set — {text}")
    return 0


def cmd_more(args):
    """The full record for one agent — everything the system holds, beyond the
    scannable `ls` line: purpose, recap, task, home, cwd, session, meta, status."""
    reg = db.get(args.name)
    if not reg:
        return _err(f"'{args.name}' is not a registered agent")
    name = reg["name"]
    sid = reg.get("session_id")
    live = {r.get("sessionId") for r in native.list_sessions()}
    status = ("archived" if reg.get("archived")
              else "up" if sid in live else "down")
    hd = home.home_dir(name)
    info = {
        "name": name, "type": reg.get("type"), "team": reg.get("team"),
        "groups": db.groups_of(reg), "status": status,
        "desc": reg.get("role"), "recap": reg.get("recap"),
        "recap_at": reg.get("recap_at"), "initial_prompt": home.initial_prompt(name),
        "home": hd if os.path.isdir(hd) else None,
        "cwd": reg.get("cwd") or home.agent_cwd(name),
        "session_id": sid, "meta": db.meta_of(reg),
    }
    if args.json:
        import json as _json
        print(_json.dumps(info))
        return 0
    print(f"{name}  [{status}]"
          + (f"  type {info['type']}" if info["type"] else "")
          + (f"  team {info['team']}" if info["team"] else ""))
    if info["groups"]:
        print(f"  groups   {','.join(info['groups'])}")
    if info["desc"]:
        print(f"  desc     {info['desc']}")
    if info["recap"]:
        when = _ago(info["recap_at"])
        print(f"  recap    {info['recap']}" + (f"  ({when} ago)" if when else ""))
    if info["meta"]:
        import json as _json
        print(f"  meta     {_json.dumps(info['meta'])}")
    print(f"  cwd      {info['cwd']}")
    print(f"  home     {info['home'] or '(none)'}")
    print(f"  session  {sid or '(unbound)'}")
    if info["initial_prompt"]:
        print(f"  task     {info['initial_prompt'].strip().splitlines()[0][:72]}")
    hist = home.recap_log(name, limit=5)
    if len(hist) > 1:
        print("  history:")
        for ts, text in hist[:-1]:
            print(f"    {_ago(ts):>4} ago  {text}")
    return 0


def _transcript_for(name):
    """Locate an agent's Claude transcript (.jsonl): the cwd's project dir first,
    then any project dir (an agent whose cwd moved). Returns a path or None."""
    reg = db.get(name)
    if not reg:
        return None
    sid = reg.get("session_id") or home.read_session_id(name)
    if not sid:
        return None
    cwd = reg.get("cwd") or home.agent_cwd(name) or home.home_dir(name)
    p = history.transcript_path(cwd, sid)
    if os.path.exists(p):
        return p
    import glob
    hits = glob.glob(os.path.join(session.PROJECTS_DIR, "*", f"{sid}.jsonl"))
    return hits[0] if hits else None


def _render_transcript_line(r):
    """A transcript event as a compact human line, or None to skip it (mode flags,
    titles, etc.). Defensive — unknown shapes fall back to a short raw blob."""
    t = r.get("type")
    if t not in ("user", "assistant"):
        return None
    content = (r.get("message") or {}).get("content")
    parts = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for b in content:
            bt = b.get("type")
            if bt == "text":
                parts.append(b.get("text", ""))
            elif bt == "thinking":
                parts.append("💭 " + (b.get("thinking", "")[:80]))
            elif bt == "tool_use":
                parts.append(f"⚙ {b.get('name', 'tool')}")
            elif bt == "tool_result":
                parts.append("↩ tool_result")
    text = " ".join(p.strip() for p in parts if p and p.strip())
    text = " ".join(text.split())  # collapse whitespace/newlines
    if not text:
        return None
    role = "you" if t == "user" else "asst"
    return f"{role}: {text[:200]}"


def cmd_session(args):
    """Tail (or head) an agent's Claude session transcript. Human-rendered by
    default (role: text, one line per turn), --raw for the exact JSONL. --tail
    (default) shows the last N, --head the first N, -f follows live. Works whether
    the agent is up or down — it reads the transcript file, not the live pane (that
    is what `attach` is for)."""
    path = _transcript_for(args.name)
    if not path:
        if not db.get(args.name):
            return _err(f"'{args.name}' is not a registered agent")
        return _err(f"'{args.name}' has no transcript yet "
                    "(it hasn't run, or its session isn't bound)")
    import json as _json
    n = args.lines

    def rendered(line):
        """The display form of a raw JSONL line, or None to drop it. In --raw mode
        every line is kept verbatim; otherwise only message turns, humanized."""
        if args.raw:
            return line.rstrip("\n")
        try:
            return _render_transcript_line(_json.loads(line))
        except (ValueError, TypeError):
            return None

    if args.follow:
        # Stream new events as they're appended (Ctrl-C to stop).
        with open(path) as f:
            f.seek(0, os.SEEK_END)
            try:
                while True:
                    line = f.readline()
                    if not line:
                        time.sleep(0.3)
                        continue
                    out = rendered(line)
                    if out is not None:
                        print(out)
            except KeyboardInterrupt:
                return 0
    # Render/filter first, THEN take head/tail — so -n counts shown lines (e.g.
    # conversation turns), not raw events, which often trail off into metadata.
    with open(path) as f:
        shown = [out for out in (rendered(line) for line in f) if out is not None]
    for out in (shown[:n] if args.head else shown[-n:]):
        print(out)
    return 0


def cmd_schema(args):
    """Show the registry schema version, applying any pending migrations first.
    `agents upgrade` runs this against the freshly installed code so migrations
    land as part of the upgrade."""
    old, new = db.migrate_now()
    if new > old:
        print(f"agents: schema migrated v{old} → v{new}")
    else:
        print(f"agents: schema at v{new} (up to date)")
    return 0


def _subparser_map(parser):
    """The {name: subparser} map for a built top-level parser (incl. aliases)."""
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a.choices
    return {}


def _undent(doc):
    """A command docstring with its 4-space body indentation removed."""
    return "\n".join(l[4:] if l.startswith("    ") else l
                     for l in (doc or "").strip().splitlines())


def _man_overview():
    from agents import manuals
    return "\n".join([
        "agents — manuals",
        "",
        "Run `agents man <command>` to learn a command well: its flags (read live",
        "from the tool, so always current), what it's for, the conventions, and",
        "worked examples. Recommended before you first use a command.",
        "",
        "CONCEPTS",
        "  agents man herdr        workspaces & tabs (leads vs. adas)",
        "  agents man conventions  naming → type, and where each fact lives",
        "  agents man recap        keeping status current with `checkin`",
        "",
        "COMMANDS WITH MANUALS",
        "  " + "  ".join(manuals.CORE),
        "",
        manuals.ISSUE_NUDGE,
    ])


def cmd_man(args):
    """Comprehensive manual for a command (or concept): its synopsis and flags —
    read live from the parser so they never drift — plus authored conventions and
    worked examples. `agents man` with no argument prints the concept overview."""
    import textwrap
    from agents import manuals
    topic = args.topic
    if not topic:
        print(_man_overview())
        return 0
    if topic in manuals.TOPICS:
        print(manuals.TOPICS[topic])
        print("\n" + manuals.ISSUE_NUDGE)
        return 0
    sub = _subparser_map(build_parser())
    if topic not in sub:
        return _err(f"no manual for '{topic}'. Try `agents man` for the index, a "
                    "command name, or a concept (herdr, conventions, recap).")
    sp = sub[topic]
    func = sp.get_default("func")
    print(f"agents {topic}")
    if func and func.__doc__:
        print("\n" + _undent(func.__doc__))
    print("\n" + sp.format_help().rstrip())       # live synopsis + options
    man = manuals.MANUALS.get(topic)
    if man:
        if man.get("best"):
            print("\nBEST PRACTICES\n" + textwrap.fill(
                man["best"], width=78, initial_indent="  ", subsequent_indent="  "))
        if man.get("examples"):
            print("\nEXAMPLES")
            for cmd, comment in man["examples"]:
                print(f"  {cmd}")
                if comment:
                    print(f"      ⤷ {comment}")
        if man.get("see_also"):
            print("\nSEE ALSO  "
                  + "  ".join(f"agents man {s}" for s in man["see_also"]))
    else:
        print("\n(No worked examples authored yet — `agents man` lists the "
              "concepts. If you'd find examples here useful, that's worth an issue.)")
    print("\n" + manuals.ISSUE_NUDGE)
    return 0


def cmd_snapshot(args):
    """Force-write the JSON snapshot of the registry now (normally refreshed
    lazily, at most hourly). The non-sqlite backup `reindex` can rebuild from."""
    path = db.snapshot()
    print(f"agents: snapshot written → {path}")
    return 0


def cmd_reindex(args):
    """Rebuild the sqlite registry from the JSON snapshot — recovery for a lost or
    corrupt agents.db. The snapshot is at most ~an hour old, so changes since the
    last snapshot are not in it (it prints the snapshot's age so you can judge)."""
    import json as _json
    try:
        with open(db.SNAPSHOT_PATH) as f:
            when = _json.load(f).get("written_at")
    except OSError:
        return _err(f"no snapshot at {db.SNAPSHOT_PATH} — nothing to reindex from")
    n_agents, n_ignored = db.reindex_from_snapshot()
    age = _ago(when)
    print(f"agents: reindexed {n_agents} agent(s), {n_ignored} tombstone(s) "
          f"from snapshot written {when}" + (f" ({age} ago)" if age else ""))
    return 0


def cmd_whois(args):
    """Resolve a registry name to its live Claude-land SendMessage address.

    The name you give an agent (its registry name) is NOT the name Claude
    advertises natively — that one derives from the session's cwd and rotates, so
    `SendMessage`/`ListAgents` can't resolve the registry name directly. The
    sessionId is the stable join key: the registry binds registry-name -> sessionId,
    and Claude writes a live session record (name, formerNames, messagingSocketPath)
    keyed by that same sessionId. This walks name -> sessionId -> that record and
    prints the address `SendMessage` actually resolves, computed live so a rotation
    or rebind can't make it stale. `agents send <name>` remains the guaranteed
    channel; this is the bridge for when an agent wants to use `SendMessage` itself.
    """
    import json
    sid, _ = _resolve_target(args.name)
    rec = None
    if sid:
        try:
            rec = native.resolve(sid)
        except native.SessionNotFound:
            rec = None
    if rec is None:
        # A registered agent that's down (or never bound) has no SendMessage target
        # yet; anything else is just unknown.
        if not db.get(args.name):
            return _err(f"'{args.name}' is not a registered agent or live session")
        if args.json:
            print(json.dumps({"name": args.name, "session_id": sid, "live": False,
                              "meta": db.meta_of(db.get(args.name))}))
        else:
            print(f"{args.name}: registered but not live — no SendMessage target "
                  f"until it's up. `agents send {args.name}` will deliver once it is.")
        return 1
    sock = rec.get("messagingSocketPath") or ""
    to = f"uds:{sock}" if sock else rec.get("name")
    if args.json:
        reg = db.get(args.name)
        print(json.dumps({
            "name": args.name, "session_id": rec.get("sessionId"),
            "claude_name": rec.get("name"),
            "former_names": rec.get("formerNames") or [],
            "sendmessage_to": to, "socket": sock,
            "pid": rec.get("pid"), "status": rec.get("status"), "live": True,
            "meta": db.meta_of(reg) if reg else {},
        }))
        return 0
    print(f"{args.name} → SendMessage to: {to!r}")
    former = rec.get("formerNames") or []
    print(f"  claude name   {rec.get('name')}"
          + (f"  (was: {', '.join(map(str, former))})" if former else ""))
    print(f"  session       {rec.get('sessionId')}")
    print(f"  pid/status    {rec.get('pid')}  {rec.get('status')}")
    print(f"  guaranteed    agents send {args.name} \"...\"")
    return 0


def cmd_rm(args):
    """Remove (archive) an agent OR an unregistered session. For an agent: hidden
    from `ls` but kept (`ls -a` to see it, `up`/`register` to restore); --purge
    deletes the row. For a bare session id/prefix (anything `find` lists that isn't
    an agent): archive tombstones it so find/ls hide it (`find --archived` to see,
    --restore to lift); --purge also deletes its transcript (non-resumable)."""
    if db.get(args.name):
        return _rm_agent(args)
    # Not an agent name — treat the target as a raw session.
    sid, cwd, err = _resolve_known_session(args.name)
    if err:
        return _err(err)
    if not sid:
        return _err(f"'{args.name}' is not a registered agent or a known session")
    return _rm_session(args.name, sid, cwd, args)


def _rm_agent(args):
    if args.restore:
        db.set_archived(args.name, False)
        print(f"agents: restored '{args.name}'")
        return 0
    # Archiving or purging retires the agent, so stop it first if it is live —
    # otherwise `rm` leaves a hidden-but-running orphan the archived row no longer
    # lists. This is the inverse of `up` (which unarchives + launches), and it is
    # lossless: `down` is a SIGTERM and the session resumes from disk on `up`.
    sid, _ = _resolve_target(args.name)
    if sid and _is_running(sid):
        _down_one(args.name)
    if args.purge:
        db.remove(args.name)
        print(f"agents: purged '{args.name}' from the registry "
              f"(home dir kept at {home.home_dir(args.name)})")
        return 0
    db.set_archived(args.name, True)
    print(f"agents: removed '{args.name}' — hidden from `ls` "
          f"(`agents ls -a` shows it; `agents up {args.name}` restores it)")
    return 0


def _rm_session(label, sid, cwd, args):
    short = sid[:8]
    if args.restore:
        lifted = db.unignore_session(sid)
        print(f"agents: restored session {short}" if lifted
              else f"agents: session {short} was not archived")
        return 0
    db.ignore_session(sid, reason=args.reason)
    if args.purge:
        path = history.transcript_path(cwd, sid) if cwd else None
        gone = False
        if path and os.path.exists(path):
            try:
                os.remove(path)
                gone = True
            except OSError as e:
                return _err(f"archived session {short} but could not delete its "
                            f"transcript: {e}")
        tail = (" and deleted its transcript (no longer resumable)" if gone
                else " (no transcript on disk to delete)")
        print(f"agents: purged session {short}{tail}")
        return 0
    print(f"agents: archived session {short} — hidden from `find`/`ls` "
          f"(`agents find --archived` shows it; `agents rm {short} --restore` lifts it)")
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
    # Always back the registry up first — no matter what. An upgrade shouldn't be
    # able to lose the table or the data cached in it (recap, meta); this fresh
    # snapshot is the floor `reindex` can restore from if anything goes wrong.
    try:
        print(f"agents: backed up registry → {db.snapshot()}")
    except OSError as e:
        print(f"agents: warning — could not snapshot the registry first ({e}); "
              f"continuing (upgrade does not modify the db)", file=sys.stderr)
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
    # Apply the new version's schema migrations now. This process still runs the
    # OLD code, so we shell out to the freshly installed `agents` on PATH — its
    # MIGRATIONS are the new ones. The pre-upgrade snapshot above is the fallback.
    mig = subprocess.run(["agents", "schema"], capture_output=True, text=True)
    out = (mig.stdout or "").strip()
    if out:
        print(out)
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
        cfg["host"] = _prompt_choice("Pty host for attach/detach and -d/-a?",
                                     hosts.CHOICES, cfg["host"])

    if args.up_mode:
        cfg["up_mode"] = args.up_mode
    elif not args.no_input:
        cfg["up_mode"] = _prompt_choice(
            "How should a bare `up` launch an agent?",
            config.UP_MODES, cfg.get("up_mode", "foreground"))

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
    print(f"  up mode:         {saved['up_mode']}")

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
    p = _Parser(
        prog="agents",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            __doc__.splitlines()[0]
            + "\n\n★ Run `agents man <command>` to learn a command's flags, "
              "conventions, and examples —\n  recommended before first use. "
              "`agents man` alone covers the core concepts\n  (herdr workspaces, "
              "naming, metadata)."),
        epilog=("Confusing, wrong, or did you have to work around the tool? "
                "Please open an issue:\n  gh issue create -R mekarpeles/agents"))
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
    s.add_argument("--role", "--desc", dest="role",
                   help="operator's one-line description of the agent's purpose "
                        "(shown by `ls`/`more`)")
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
    s.add_argument("--meta", action="append", metavar="KEY=VALUE",
                   help="structured metadata (repeatable), e.g. --meta project=ol "
                        "--meta issue=14200 --meta pr=14212, or --meta '{...}'")
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

    s = sub.add_parser("meta", help="get/set/remove an agent's structured metadata")
    s.add_argument("op", choices=["get", "set", "rm"])
    s.add_argument("name")
    s.add_argument("items", nargs="*", metavar="KEY=VALUE",
                   help="set: key=value pairs (or a {json} object); rm: bare keys")
    s.set_defaults(func=cmd_meta)

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
    s.add_argument("--meta", action="append", metavar="KEY[=VALUE]",
                   help="bring up every agent whose metadata matches (repeatable; "
                        "e.g. --meta project=ol)")
    s.add_argument("--host", choices=hosts.CHOICES, default=None,
                   help="run on this host for this call (overrides -F/-d/-a and "
                        "the configured up_mode)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("-F", "--foreground", action="store_true",
                   help="run inline in this terminal (no tmux)")
    g.add_argument("-d", "--detach", action="store_true",
                   help="start detached in the configured host (tmux/herdr), "
                        "without attaching")
    g.add_argument("-a", "--attach", action="store_true",
                   help="start in the configured host and attach to it now")
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
    s.add_argument("--meta", action="append", metavar="KEY[=VALUE]",
                   help="stop every agent whose metadata matches (e.g. --meta project=ol)")
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
                       help="list agents (tree by default; --flat for a table; "
                            "filter by group/type/status)")
    s.add_argument("-g", "--group", help="only agents in this group")
    s.add_argument("-t", "--team", nargs="?", const=_ALL_TEAMS, default=None,
                   metavar="TEAM",
                   help="limit the tree to one team (reports under their lead)")
    s.add_argument("--flat", action="store_true",
                   help="flat table instead of the default tree view")
    s.add_argument("--type", help="only agents of this type (e.g. lead, ada)")
    s.add_argument("--meta", action="append", nargs="?", metavar="KEY[=VALUE]",
                   help="bare (`--meta`): expand all metadata stacked under each "
                        "agent. With a KEY (`--meta pr`) or KEY=VALUE: filter to "
                        "matches AND show that key inline in the META column. "
                        "Omit entirely and META is just a field count.")
    s.add_argument("--running", action="store_true", help="only agents with a live session")
    s.add_argument("--down", action="store_true", help="only stopped agents")
    s.add_argument("-a", "--all", action="store_true",
                   help="include archived agents (hidden by default)")
    s.add_argument("--archived", action="store_true", help="only archived agents")
    s.add_argument("--json", action="store_true",
                   help="machine-readable output (one JSON object per agent, incl. meta)")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("find",
                       help="find past Claude sessions to resume or adopt")
    s.add_argument("query", nargs="?", help="substring over title, cwd, prompts")
    s.add_argument("-a", "--all", action="store_true",
                   help="deep search: grep full transcript contents across every "
                        "session (incl. non-resumable); slower, searches everything")
    s.add_argument("--archived", action="store_true",
                   help="include sessions you archived with `rm` (hidden by default)")
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
                        "(default: your registered agent name, else your OS login)")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("whoami",
                       help="identify this session's agent (by $CLAUDE_CODE_SESSION_ID)")
    s.add_argument("--session-id", dest="session_id",
                   help="look up this sessionId instead of the current session's")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_whoami)

    s = sub.add_parser("whois",
                       help="resolve a registry name to its live SendMessage address")
    s.add_argument("name", help="the agent's registry name (or any selector `ls` shows)")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_whois)

    s = sub.add_parser("checkin",
                       help="record a short recap of where an agent is (agent-set)")
    s.add_argument("text", nargs="*", help="the recap line")
    s.add_argument("--agent", help="check in this agent (default: the calling agent)")
    s.set_defaults(func=cmd_checkin)

    s = sub.add_parser("more", help="show the full record for one agent")
    s.add_argument("name")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_more)

    s = sub.add_parser("session",
                       help="tail/head an agent's Claude session transcript")
    s.add_argument("name")
    s.add_argument("-n", "--lines", type=int, default=20,
                   help="how many events to show (default 20)")
    s.add_argument("--head", action="store_true",
                   help="show the first N events instead of the last")
    s.add_argument("--tail", action="store_true",
                   help="show the last N events (the default)")
    s.add_argument("-f", "--follow", action="store_true",
                   help="stream new events live (Ctrl-C to stop)")
    s.add_argument("--raw", action="store_true",
                   help="exact JSONL lines instead of the human render")
    s.set_defaults(func=cmd_session)

    s = sub.add_parser("snapshot",
                       help="force-write the JSON registry snapshot now")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("reindex",
                       help="rebuild the sqlite registry from the JSON snapshot")
    s.set_defaults(func=cmd_reindex)

    s = sub.add_parser("schema",
                       help="show/apply the registry schema version (migrations)")
    s.set_defaults(func=cmd_schema)

    s = sub.add_parser("man",
                       help="comprehensive manual for a command or concept "
                            "(flags + conventions + examples)")
    s.add_argument("topic", nargs="?",
                   help="a command (register, up, …) or concept "
                        "(herdr, conventions, recap); omit for the overview")
    s.set_defaults(func=cmd_man)

    s = sub.add_parser("rm", help="archive an agent or an unregistered session "
                                  "(hidden from ls/find, kept)")
    s.add_argument("name", help="an agent name, or a session id/prefix from `find`")
    s.add_argument("--purge", action="store_true",
                   help="hard delete: an agent's registry row (keeps home dir), or "
                        "a session's transcript (no longer resumable)")
    s.add_argument("--restore", action="store_true",
                   help="lift the archive instead of setting it")
    s.add_argument("--reason", help="note why a session was archived")
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
    s.add_argument("--host", choices=hosts.CHOICES,
                   help="the pty host attach/detach and -d/-a use (tmux/herdr)")
    s.add_argument("--up-mode", choices=config.UP_MODES,
                   help="how a bare `up` launches: foreground | detach | attach")
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
    rc = args.func(args)
    # Keep the non-sqlite backup fresh (lazily, ~hourly) — best-effort, after the
    # command's real work, so a registry mutation is captured but a failure here
    # never affects the command's result.
    db.maybe_snapshot()
    sys.exit(rc)


if __name__ == "__main__":
    main()
