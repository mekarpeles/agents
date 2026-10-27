"""Tests for the agents tool — registry, home dir, sessionId binding, injection.

Hermetic: AGENTS_STATE_DIR and the Claude sessions dir are redirected to tmp.
No real claude is launched (we bind against a fake session for the current pid).
"""

import importlib
import os

import pytest

from tests.conftest import FakeSession, write_session, wait_for


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated state + sessions dirs; reload modules so they pick up env."""
    state = tmp_path / "agents-state"
    sessions = tmp_path / "sessions"
    socks = tmp_path / "cc-socks"
    for d in (state, sessions, socks):
        d.mkdir()
    monkeypatch.setenv("AGENTS_STATE_DIR", str(state))

    from agents import db, home, session
    from agents import transport as native
    importlib.reload(db)
    importlib.reload(home)
    monkeypatch.setattr(session, "SESSIONS_DIR", str(sessions))
    monkeypatch.setattr(native, "SESSIONS_DIR", str(sessions))
    from agents import cli
    importlib.reload(cli)
    return {"state": str(state), "sessions": str(sessions), "socks": str(socks),
            "db": db, "home": home, "session": session, "native": native, "cli": cli}


def test_register_creates_home_and_row(env):
    env["db"].register("pam", role="registry owner", workspace="pam", cwd="/tmp/x")
    env["home"].ensure_home("pam", cwd="/tmp/x")
    row = env["db"].get("pam")
    assert row["role"] == "registry owner" and row["workspace"] == "pam"
    assert os.path.isdir(env["home"].home_dir("pam"))
    assert env["home"].agent_cwd("pam") == "/tmp/x"


def test_home_layout(env, tmp_path):
    env["home"].ensure_home("ada", cwd=str(tmp_path), initial_prompt="be ada")
    h = env["home"].home_dir("ada")
    assert os.path.exists(os.path.join(h, "PROMPT.md"))     # the task
    assert os.path.exists(os.path.join(h, "AGENTS.md"))     # seeded from a type
    assert os.path.exists(os.path.join(h, "cwd"))
    assert env["home"].initial_prompt("ada") == "be ada"
    env["home"].write_session_id("ada", "sid-xyz")
    assert env["home"].read_session_id("ada") == "sid-xyz"


def test_default_cwd_is_home(env):
    env["home"].ensure_home("solo")   # no --cwd -> runs in its own home dir
    assert env["home"].agent_cwd("solo") == env["home"].home_dir("solo")


def test_type_seeds_agents_md(env):
    env["home"].ensure_home("dev", agent_type="ada")
    text = open(os.path.join(env["home"].home_dir("dev"), "AGENTS.md")).read()
    assert "Atomic Development Agent" in text          # the ada doctrine
    env["home"].ensure_home("gen", agent_type="default")
    assert "named agent" in open(
        os.path.join(env["home"].home_dir("gen"), "AGENTS.md")).read()


def test_cwd_recorded_once_and_reused(env):
    env["home"].ensure_home("x", cwd="/first")
    env["home"].ensure_home("x", cwd="/second")  # must NOT overwrite
    assert env["home"].agent_cwd("x") == "/first"


def test_session_id_for_pid(env):
    write_session(env["sessions"], os.getpid(), "/tmp/x.sock", "sid-live", "me")
    assert env["session"].session_id_for_pid(os.getpid(), timeout=1) == "sid-live"


def test_session_id_for_dead_pid_returns_none(env):
    assert env["session"].session_id_for_pid(999999, timeout=1) is None


def test_bind_and_inject_full_path(env):
    sock = os.path.join(env["socks"], f"{os.getpid()}.sock")
    srv = FakeSession(sock)
    try:
        write_session(env["sessions"], os.getpid(), sock, "sid-bind", "me",
                      peer_token="feed" * 8)
        env["db"].register("pam", cwd="/tmp/x")
        env["home"].ensure_home("pam", cwd="/tmp/x", initial_prompt="be pam")
        sid = env["cli"]._bind_and_inject("pam", os.getpid(),
                                          is_first_start=True, inject=True)
        assert sid == "sid-bind"
        # durable binding recorded in db + home
        assert env["db"].get("pam")["session_id"] == "sid-bind"
        assert env["home"].read_session_id("pam") == "sid-bind"
        # startup prompt was delivered natively
        assert wait_for(lambda: len(srv.lines) >= 2)
        msg = srv.lines[1]
        assert "be pam" in msg["message"]["content"]
        assert msg["from"] == "agents"
    finally:
        srv.close()


def test_send_resolves_via_db_session(env):
    sock = os.path.join(env["socks"], f"{os.getpid()}.sock")
    srv = FakeSession(sock)
    try:
        write_session(env["sessions"], os.getpid(), sock, "sid-send", "me",
                      peer_token="cafe" * 8)
        env["db"].register("lenny", cwd="/tmp/x")
        env["db"].set_session("lenny", "sid-send")
        rc = env["cli"].cmd_send(type("A", (), {"name": "lenny",
                                                 "message": ["hi lenny"],
                                                 "sender": None})())
        assert rc == 0
        assert wait_for(lambda: len(srv.lines) >= 2)
        assert "hi lenny" in srv.lines[1]["message"]["content"]
    finally:
        srv.close()


def test_register_with_session_id(env):
    # --session-id binds in the DB and writes last-session-id for `up` to resume.
    rc = env["cli"].cmd_register(type("A", (), {
        "name": "ada", "type": "default", "role": "lead", "workspace": "ol-leads",
        "cwd": "/Users/mek/Projects/ada", "identity": None, "prompt": None,
        "session_id": "5662a36c-05c3-44cd-9916-a1e9aaa50b3b"})())
    assert rc == 0
    assert env["db"].get("ada")["session_id"] == "5662a36c-05c3-44cd-9916-a1e9aaa50b3b"
    assert env["home"].read_session_id("ada") == "5662a36c-05c3-44cd-9916-a1e9aaa50b3b"


def test_transcript_exists(env, tmp_path, monkeypatch):
    proj = tmp_path / "projects"
    monkeypatch.setattr(env["session"], "PROJECTS_DIR", str(proj))
    d = proj / "-Users-mek-Projects-ada"
    d.mkdir(parents=True)
    (d / "sid-real.jsonl").write_text("{}")
    assert env["session"].transcript_exists("sid-real", "/Users/mek/Projects/ada")
    assert not env["session"].transcript_exists("sid-missing", "/Users/mek/Projects/ada")


def test_up_detects_already_running(env):
    # Bind ada to a LIVE session (our own pid) -> up must refuse, not launch.
    write_session(env["sessions"], os.getpid(), "/tmp/x.sock", "live-sid", "ada")
    env["db"].register("ada", cwd="/tmp")
    env["db"].set_session("ada", "live-sid")
    env["home"].ensure_home("ada", cwd="/tmp", session_id="live-sid")
    rc = env["cli"].cmd_up(type("A", (), {
        "name": "ada", "group": None, "host": "foreground", "prompt": None,
        "cwd": None, "identity": None, "no_inject": False})())
    assert rc == 1  # refused because already running (no launch attempted)


def test_list_aliases_ls():
    from agents import cli
    args = cli.build_parser().parse_args(["list"])
    assert args.func is cli.cmd_ls
    args = cli.build_parser().parse_args(["ls"])
    assert args.func is cli.cmd_ls


def test_help_subcommand():
    from agents import cli
    args = cli.build_parser().parse_args(["help"])
    assert args.func is cli.cmd_help


def test_unknown_command_errors_not_agent():
    # cmux's footgun: an unknown token became an agent name. argparse must
    # reject it instead.
    from agents import cli
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["frobnicate"])


def test_no_command_is_not_required():
    from agents import cli
    args = cli.build_parser().parse_args([])
    assert getattr(args, "cmd", None) is None  # main() prints help, exits 0


def test_sender_resolution(env, monkeypatch):
    import getpass
    monkeypatch.delenv("AGENTS_AGENT_NAME", raising=False)
    assert env["cli"]._sender("mek") == "mek"             # explicit --from wins
    monkeypatch.setenv("AGENTS_AGENT_NAME", "ada")
    assert env["cli"]._sender(None) == "ada"              # then env
    monkeypatch.delenv("AGENTS_AGENT_NAME", raising=False)
    assert env["cli"]._sender(None) == getpass.getuser()  # never the bare "agents"


def test_send_from_flag_sets_attribution(env):
    sock = os.path.join(env["socks"], f"{os.getpid()}.sock")
    srv = FakeSession(sock)
    try:
        write_session(env["sessions"], os.getpid(), sock, "sid-from", "me",
                      peer_token="feed" * 8)
        env["db"].register("lenny", cwd="/tmp/x")
        env["db"].set_session("lenny", "sid-from")
        env["cli"].cmd_send(type("A", (), {
            "name": "lenny", "message": ["hi"], "sender": "mek"})())
        assert wait_for(lambda: len(srv.lines) >= 2)
        assert srv.lines[1]["from"] == "mek"
    finally:
        srv.close()


class _StubHost:
    name = "stub"
    group = True

    def __init__(self):
        self.started = []

    def start(self, agent, cmd, cwd, env, group=None):
        from agents import hosts
        self.started.append((agent, group))
        return hosts.Launched(pid=None, proc=None)  # detached, no binding


def test_up_group_fans_out(env, monkeypatch):
    env["db"].register("a1", workspace="team")
    env["db"].register("a2", workspace="team")
    env["db"].register("solo", workspace="other")
    stub = _StubHost()
    monkeypatch.setattr(env["cli"].hosts, "get", lambda _n: stub)
    rc = env["cli"].cmd_up(type("A", (), {
        "name": None, "group": "team", "host": "tmux", "prompt": None,
        "cwd": None, "identity": None, "no_inject": True})())
    assert rc == 0
    assert sorted(a for a, _ in stub.started) == ["a1", "a2"]      # not 'solo'
    assert all(g == "team" for _, g in stub.started)               # grouped session


def test_type_stored_and_queryable(env):
    db = env["db"]
    db.register("pam", agent_type="lead", workspace="payments")
    db.register("ada-a", agent_type="ada", workspace="payments")
    db.register("ada-b", agent_type="ada", workspace="payments")
    db.register("solo", agent_type="ada", workspace="other")
    assert [a["name"] for a in db.select(agent_type="lead")] == ["pam"]
    assert sorted(a["name"] for a in db.select(agent_type="ada")) == \
        ["ada-a", "ada-b", "solo"]
    # a team's ADAs = group + type intersection
    assert sorted(a["name"] for a in db.select(group="payments", agent_type="ada")) \
        == ["ada-a", "ada-b"]


def test_up_by_type_fans_out(env, monkeypatch):
    env["db"].register("l1", agent_type="lead")
    env["db"].register("l2", agent_type="lead")
    env["db"].register("a1", agent_type="ada")
    stub = _StubHost()
    monkeypatch.setattr(env["cli"].hosts, "get", lambda _n: stub)
    rc = env["cli"].cmd_up(type("A", (), {
        "name": None, "group": None, "type": "lead", "host": "tmux",
        "prompt": None, "cwd": None, "identity": None, "no_inject": True})())
    assert rc == 0
    assert sorted(a for a, _ in stub.started) == ["l1", "l2"]   # not 'a1'


def test_up_requires_name_or_group(env):
    rc = env["cli"].cmd_up(type("A", (), {
        "name": None, "group": None, "host": "tmux", "prompt": None,
        "cwd": None, "identity": None, "no_inject": True})())
    assert rc == 1  # neither target given


def test_down_group_fans_out(env):
    env["db"].register("d1", workspace="grp")
    env["db"].register("d2", workspace="grp")
    env["home"].ensure_home("d1")
    env["home"].ensure_home("d2")
    # No bound sessions -> each _down_one reports "no bound session" (rc 1), but
    # the dispatch must visit both without raising.
    rc = env["cli"].cmd_down(type("A", (), {"name": None, "group": "grp"})())
    assert rc == 1


def test_rm_keeps_home(env):
    env["db"].register("tmp", cwd="/tmp/x")
    env["home"].ensure_home("tmp", cwd="/tmp/x")
    h = env["home"].home_dir("tmp")
    env["db"].remove("tmp")
    assert env["db"].get("tmp") is None
    assert os.path.isdir(h)  # home preserved


def test_ls_status_filters(env, capsys):
    env["db"].register("pam")
    env["db"].register("ada")
    p = env["cli"].build_parser()
    # no live sessions bound -> every agent reads as "down"
    p.parse_args(["ls", "--down"]).func(p.parse_args(["ls", "--down"]))
    out = capsys.readouterr().out
    assert "pam" in out and "ada" in out
    p.parse_args(["ls", "--running"]).func(p.parse_args(["ls", "--running"]))
    out = capsys.readouterr().out
    assert "pam" not in out and "no agents match" in out


def test_whoami_by_session_id(env, monkeypatch, capsys):
    env["db"].register("ada-1581", agent_type="ada", workspace="br-prs",
                       role="eng", session_id="sess-123")
    env["home"].ensure_home("ada-1581", cwd="/tmp/x", initial_prompt="do the TTS swap")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-123")
    a = env["cli"].build_parser().parse_args(["whoami"])
    assert a.func(a) == 0
    out = capsys.readouterr().out
    assert "ada-1581" in out and "type ada" in out and "br-prs" in out
    assert "do the TTS swap" in out


def test_whoami_json_and_explicit_flag(env, monkeypatch, capsys):
    import json
    env["db"].register("pam", workspace="web", session_id="s-pam")
    env["home"].ensure_home("pam", cwd="/tmp/x")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    # --session-id overrides the env; works without CLAUDE_CODE_SESSION_ID set
    a = env["cli"].build_parser().parse_args(["whoami", "--session-id", "s-pam", "--json"])
    assert a.func(a) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["name"] == "pam" and d["group"] == "web" and d["session_id"] == "s-pam"


def test_whoami_errors(env, monkeypatch):
    p = env["cli"].build_parser()
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    a = p.parse_args(["whoami"])            # no session id at all
    assert a.func(a) == 1
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "not-a-registered-sid")
    a = p.parse_args(["whoami"])            # session isn't a registered agent
    assert a.func(a) == 1


def test_rm_archives_hides_and_restores(env, capsys):
    db, cli = env["db"], env["cli"]
    db.register("pam")
    db.register("ada")
    p = cli.build_parser()

    a = p.parse_args(["rm", "pam"]); assert a.func(a) == 0
    capsys.readouterr()  # drain the rm message

    # default ls hides the removed (archived) agent
    a = p.parse_args(["ls"]); a.func(a)
    out = capsys.readouterr().out
    assert "ada" in out and "down" in out and "pam" not in out

    # -a reveals it as archived; completion excludes it
    a = p.parse_args(["ls", "-a"]); a.func(a)
    assert "pam" in (out2 := capsys.readouterr().out) and "archived" in out2
    a = p.parse_args(["_complete", "names"]); a.func(a)
    assert "pam" not in capsys.readouterr().out.split()

    # re-registering restores it
    a = p.parse_args(["register", "pam"]); assert a.func(a) == 0
    capsys.readouterr()
    a = p.parse_args(["ls"]); a.func(a)
    assert "pam" in capsys.readouterr().out


def test_rm_purge_deletes_row(env):
    db, cli = env["db"], env["cli"]
    db.register("pam")
    a = cli.build_parser().parse_args(["rm", "pam", "--purge"])
    assert a.func(a) == 0
    assert db.get("pam") is None


def test_attach_requires_a_target(env):
    a = env["cli"].build_parser().parse_args(["attach"])
    assert a.func(a) == 1  # neither a name nor -g


def test_attach_and_detach_unsupported_on_foreground(env):
    env["db"].register("solo")
    p = env["cli"].build_parser()
    a = p.parse_args(["attach", "solo", "--host", "foreground"])
    assert a.func(a) == 1   # foreground can't attach
    a = p.parse_args(["detach", "solo", "--host", "foreground"])
    assert a.func(a) == 1


def test_find_command_table_and_json(env, monkeypatch, capsys):
    import json
    from agents import history
    rows = [
        {"session_id": "sid-run", "cwd": "/a/proj", "msgs": 5,
         "title": "Running one", "present": True},
        {"session_id": "sid-idle", "cwd": "/b/proj", "msgs": 2,
         "title": "Idle one", "present": True},
    ]
    monkeypatch.setattr(history, "find_sessions", lambda *a, **k: rows)
    monkeypatch.setattr(env["native"], "list_sessions", lambda: [{"sessionId": "sid-run"}])
    p = env["cli"].build_parser()

    a = p.parse_args(["find"]); assert a.func(a) == 0
    out = capsys.readouterr().out
    assert "running" in out and "idle" in out and "Running one" in out

    a = p.parse_args(["find", "--json"]); assert a.func(a) == 0
    objs = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(objs) == 2 and any(o["running"] for o in objs)


def test_find_command_no_matches(env, monkeypatch, capsys):
    from agents import history
    monkeypatch.setattr(history, "find_sessions", lambda *a, **k: [])
    monkeypatch.setattr(env["native"], "list_sessions", lambda: [])
    a = env["cli"].build_parser().parse_args(["find"])
    assert a.func(a) == 0
    assert "no matching sessions" in capsys.readouterr().out
