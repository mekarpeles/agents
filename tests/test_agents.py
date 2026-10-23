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

    from agents_lib import db, home, session
    from claudio import native
    importlib.reload(db)
    importlib.reload(home)
    monkeypatch.setattr(session, "SESSIONS_DIR", str(sessions))
    monkeypatch.setattr(native, "SESSIONS_DIR", str(sessions))
    from agents_lib import cli
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


def test_home_layout_matches_cmux(env, tmp_path):
    env["home"].ensure_home("ada", cwd=str(tmp_path),
                            initial_prompt="be ada")
    h = env["home"].home_dir("ada")
    assert os.path.exists(os.path.join(h, "initial-prompt.md"))
    assert os.path.exists(os.path.join(h, "cwd"))
    env["home"].write_session_id("ada", "sid-xyz")
    assert env["home"].read_session_id("ada") == "sid-xyz"


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
                                                 "message": ["hi lenny"]}))
        assert rc == 0
        assert wait_for(lambda: len(srv.lines) >= 2)
        assert "hi lenny" in srv.lines[1]["message"]["content"]
    finally:
        srv.close()


def test_register_with_session_id(env):
    # --session-id binds in the DB and writes last-session-id for `up` to resume.
    rc = env["cli"].cmd_register(type("A", (), {
        "name": "ada", "role": "lead", "workspace": "ol-leads",
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
        "name": "ada", "host": "foreground", "prompt": None,
        "cwd": None, "identity": None, "no_inject": False})())
    assert rc == 1  # refused because already running (no launch attempted)


def test_list_aliases_ls():
    from agents_lib import cli
    args = cli.build_parser().parse_args(["list"])
    assert args.func is cli.cmd_ls
    args = cli.build_parser().parse_args(["ls"])
    assert args.func is cli.cmd_ls


def test_help_subcommand():
    from agents_lib import cli
    args = cli.build_parser().parse_args(["help"])
    assert args.func is cli.cmd_help


def test_unknown_command_errors_not_agent():
    # cmux's footgun: an unknown token became an agent name. argparse must
    # reject it instead.
    from agents_lib import cli
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["frobnicate"])


def test_no_command_is_not_required():
    from agents_lib import cli
    args = cli.build_parser().parse_args([])
    assert getattr(args, "cmd", None) is None  # main() prints help, exits 0


def test_rm_keeps_home(env):
    env["db"].register("tmp", cwd="/tmp/x")
    env["home"].ensure_home("tmp", cwd="/tmp/x")
    h = env["home"].home_dir("tmp")
    env["db"].remove("tmp")
    assert env["db"].get("tmp") is None
    assert os.path.isdir(h)  # home preserved
