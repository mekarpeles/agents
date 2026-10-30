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
    # isolate history too (ls now reads it for titles) — nonexistent = empty
    monkeypatch.setenv("CLAUDE_HISTORY", str(tmp_path / "history.jsonl"))
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))

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
                                                 "session": None,
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
        "team": None, "cwd": "/Users/mek/Projects/ada", "identity": None,
        "prompt": None, "session_id": "5662a36c-05c3-44cd-9916-a1e9aaa50b3b"})())
    assert rc == 0
    assert env["db"].get("ada")["session_id"] == "5662a36c-05c3-44cd-9916-a1e9aaa50b3b"
    assert env["home"].read_session_id("ada") == "5662a36c-05c3-44cd-9916-a1e9aaa50b3b"


def test_register_by_agent_sets_team_to_caller(env, monkeypatch):
    # An agent (identified by $CLAUDE_CODE_SESSION_ID, like whoami) that registers
    # another agent becomes its lead automatically — no explicit --team needed.
    env["db"].register("ada", agent_type="lead", session_id="sess-ada")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-ada")
    a = env["cli"].build_parser().parse_args(["register", "saul-report", "--role", "Solr"])
    assert a.func(a) == 0
    assert env["db"].get("saul-report")["team"] == "ada"


def test_register_explicit_team_overrides_caller(env, monkeypatch):
    # An explicit --team wins over the caller-as-lead default.
    env["db"].register("ada", agent_type="lead", session_id="sess-ada")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-ada")
    a = env["cli"].build_parser().parse_args(
        ["register", "r", "--team", "someone-else"])
    assert a.func(a) == 0
    assert env["db"].get("r")["team"] == "someone-else"


def test_register_by_human_sets_no_team(env, monkeypatch):
    # A human shell (unregistered session, or none) gets no auto-lead.
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    a = env["cli"].build_parser().parse_args(["register", "standalone"])
    assert a.func(a) == 0
    assert env["db"].get("standalone")["team"] is None


def test_register_self_does_not_self_lead(env, monkeypatch):
    # Re-registering yourself (adopting your own session) must not make you your
    # own lead.
    env["db"].register("ada", agent_type="lead", session_id="sess-ada")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-ada")
    a = env["cli"].build_parser().parse_args(["register", "ada", "--role", "lead"])
    assert a.func(a) == 0
    assert env["db"].get("ada")["team"] is None


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
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("AGENTS_AGENT_NAME", "ada")        # env is NOT an identity source
    assert env["cli"]._sender("mek") == "mek"             # explicit --from wins
    # a human terminal (no agent session) sends as the OS user, not the env var
    assert env["cli"]._sender(None) == getpass.getuser()


def test_send_from_flag_sets_attribution(env):
    sock = os.path.join(env["socks"], f"{os.getpid()}.sock")
    srv = FakeSession(sock)
    try:
        write_session(env["sessions"], os.getpid(), sock, "sid-from", "me",
                      peer_token="feed" * 8)
        env["db"].register("lenny", cwd="/tmp/x")
        env["db"].set_session("lenny", "sid-from")
        env["cli"].cmd_send(type("A", (), {
            "name": "lenny", "message": ["hi"], "session": None, "sender": "mek"})())
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

    def has_window(self, agent, group=None):
        return False


def test_up_group_fans_out(env, monkeypatch):
    env["db"].register("a1", workspace="web")
    env["db"].register("a2", workspace="web")
    env["db"].register("solo", workspace="other")
    stub = _StubHost()
    monkeypatch.setattr(env["cli"].hosts, "get", lambda _n: stub)
    rc = env["cli"].cmd_up(type("A", (), {
        "name": None, "group": "web", "type": None, "team": None, "host": "tmux",
        "prompt": None, "cwd": None, "identity": None, "no_inject": True})())
    assert rc == 0
    assert sorted(a for a, _ in stub.started) == ["a1", "a2"]      # not 'solo'
    # a group is a label, not a session: each member runs in its own session
    # (host key = its team, here None), not one colocated group session.
    assert all(g is None for _, g in stub.started)


def test_up_team_colocates(env, monkeypatch):
    """`up --team <lead>` brings the lead + its reports up in ONE tmux session
    named after the team (the host key is the team for every member)."""
    env["db"].register("lead1", agent_type="lead")
    env["db"].register("ada1", agent_type="ada", team="lead1")
    env["db"].register("ada2", agent_type="ada", team="lead1")
    env["db"].register("other", agent_type="ada", team="lead2")
    stub = _StubHost()
    monkeypatch.setattr(env["cli"].hosts, "get", lambda _n: stub)
    rc = env["cli"].cmd_up(type("A", (), {
        "name": None, "group": None, "type": None, "team": "lead1", "host": "tmux",
        "prompt": None, "cwd": None, "identity": None, "no_inject": True})())
    assert rc == 0
    assert sorted(a for a, _ in stub.started) == ["ada1", "ada2", "lead1"]  # lead + reports
    assert all(g == "lead1" for _, g in stub.started)   # one shared session


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
        "name": None, "group": None, "type": "lead", "team": None, "host": "tmux",
        "prompt": None, "cwd": None, "identity": None, "no_inject": True})())
    assert rc == 0
    assert sorted(a for a, _ in stub.started) == ["l1", "l2"]   # not 'a1'


def test_up_by_meta_fans_out(env, monkeypatch):
    env["db"].register("a1", meta={"project": "ol"})
    env["db"].register("a2", meta={"project": "ol"})
    env["db"].register("b1", meta={"project": "bgp"})
    stub = _StubHost()
    monkeypatch.setattr(env["cli"].hosts, "get", lambda _n: stub)
    rc = env["cli"].cmd_up(type("A", (), {
        "name": None, "group": None, "type": None, "team": None,
        "meta": ["project=ol"], "host": "tmux", "prompt": None, "cwd": None,
        "identity": None, "no_inject": True})())
    assert rc == 0
    assert sorted(a for a, _ in stub.started) == ["a1", "a2"]   # not b1


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
    # No bound sessions -> each registered agent is simply "not running" (a no-op,
    # rc 0), and the dispatch must visit both.
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = env["cli"].cmd_down(type("A", (), {
            "name": None, "group": "grp", "type": None, "team": None})())
    assert rc == 0
    out = buf.getvalue()
    assert "d1" in out and "d2" in out and "not running" in out


def test_rm_keeps_home(env):
    env["db"].register("tmp", cwd="/tmp/x")
    env["home"].ensure_home("tmp", cwd="/tmp/x")
    h = env["home"].home_dir("tmp")
    env["db"].remove("tmp")
    assert env["db"].get("tmp") is None
    assert os.path.isdir(h)  # home preserved


def test_status_glyphs_are_shape_distinct(env):
    # colorblind-safe: up/down/archived differ by SHAPE, not just color
    dot = env["cli"]._dot
    assert dot("up") == "●" and dot("down") == "○" and dot("archived") == "✗"


def test_send_from_defaults_to_calling_agent(env, monkeypatch):
    # an agent's send with no --from attributes to its registered name, not the
    # OS user (which would read as the human speaking)
    env["db"].register("oliver", agent_type="lead", session_id="sid-oliver")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-oliver")
    assert env["cli"]._sender(None) == "oliver"
    assert env["cli"]._sender("github:bot") == "github:bot"   # explicit still wins


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


def test_multi_group_register_and_membership(env):
    db = env["db"]
    db.register("ada", workspace="web, payments ,web")   # dup/space tolerant
    assert db.groups_of(db.get("ada")) == ["web", "payments"]
    assert [a["name"] for a in db.select(group="payments")] == ["ada"]
    assert [a["name"] for a in db.select(group="web")] == ["ada"]
    assert db.select(group="infra") == []
    db.register("pam", workspace="web")
    assert db.all_groups() == ["payments", "web"]


def test_group_subcommand_add_and_remove(env):
    env["db"].register("ada", workspace="web")
    p = env["cli"].build_parser()
    p.parse_args(["group", "add", "ada", "payments", "infra"]).func(
        p.parse_args(["group", "add", "ada", "payments", "infra"]))
    assert env["db"].groups_of(env["db"].get("ada")) == ["web", "payments", "infra"]
    a = p.parse_args(["group", "rm", "ada", "web"])
    assert a.func(a) == 0
    assert env["db"].groups_of(env["db"].get("ada")) == ["payments", "infra"]


def test_team_subcommand_set_and_clear(env):
    env["db"].register("lead1", agent_type="lead")
    env["db"].register("ada1", agent_type="ada")
    p = env["cli"].build_parser()
    a = p.parse_args(["team", "add", "ada1", "lead1"])
    assert a.func(a) == 0
    assert env["db"].get("ada1")["team"] == "lead1"
    # team selection returns the lead + its reports
    assert sorted(x["name"] for x in env["db"].select(team="lead1")) == ["ada1", "lead1"]
    a = p.parse_args(["team", "rm", "ada1"])
    assert a.func(a) == 0
    assert env["db"].get("ada1")["team"] is None


def test_ls_team_tree_view(env, capsys):
    db = env["db"]
    db.register("lead1", agent_type="lead")
    db.register("ada1", agent_type="ada", team="lead1")
    db.register("ada2", agent_type="ada", team="lead1")
    db.register("loner", agent_type="ada")
    p = env["cli"].build_parser()
    a = p.parse_args(["ls", "-t"])
    assert a.func(a) == 0
    out = capsys.readouterr().out
    assert "NAME" in out and "DESC" in out                   # header columns
    # reports nest under their lead with tree branches; loner is its own root
    assert "lead1" in out and "─ ada1" in out and "─ ada2" in out
    lead_line = next(l for l in out.splitlines() if "lead1" in l)
    ada1_line = next(l for l in out.splitlines() if "ada1" in l)
    assert out.index(lead_line) < out.index(ada1_line)      # lead above its report
    assert "─" not in next(l for l in out.splitlines() if "loner" in l)  # root, no branch


def test_complete_teams(env, capsys):
    env["db"].register("lead1", agent_type="lead")
    env["db"].register("ada1", agent_type="ada", team="lead1")
    a = env["cli"].build_parser().parse_args(["_complete", "teams"])
    assert a.func(a) == 0
    assert capsys.readouterr().out.split() == ["lead1"]


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
    assert d["name"] == "pam" and d["groups"] == ["web"] and d["session_id"] == "s-pam"


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
    assert "ada" in out and "pam" not in out

    # -a reveals it; archived shows as the ✗ glyph (no STATUS text column anymore)
    a = p.parse_args(["ls", "-a"]); a.func(a)
    assert "pam" in (out2 := capsys.readouterr().out) and "✗" in out2
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


def test_rm_downs_a_live_agent_before_archiving(env, monkeypatch, capsys):
    """Archiving a running agent stops it first, so `rm` never leaves a
    hidden-but-live orphan. `down` is lossless — the session resumes on `up`."""
    db, cli = env["db"], env["cli"]
    db.register("pam", session_id="live-sid")
    monkeypatch.setattr(env["native"], "resolve", lambda sid: {"pid": 4242})
    killed = {}
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.update(pid=pid, sig=sig))

    a = cli.build_parser().parse_args(["rm", "pam"])
    assert a.func(a) == 0
    assert killed == {"pid": 4242, "sig": 15}      # SIGTERM'd before archiving
    out = capsys.readouterr().out
    assert "stopped 'pam'" in out and "removed 'pam'" in out
    assert db.get("pam")["archived"]               # row kept, archived


def test_rm_skips_down_when_agent_not_running(env, monkeypatch, capsys):
    """An already-stopped agent archives without a kill or a spurious message."""
    db, cli = env["db"], env["cli"]
    db.register("pam", session_id="dead-sid")
    monkeypatch.setattr(env["native"], "resolve",
                        lambda sid: (_ for _ in ()).throw(env["native"].SessionNotFound(sid)))
    monkeypatch.setattr(cli.os, "kill",
                        lambda *a, **k: pytest.fail("rm must not SIGTERM a stopped agent"))
    a = cli.build_parser().parse_args(["rm", "pam"])
    assert a.func(a) == 0
    out = capsys.readouterr().out
    assert "stopped" not in out and "removed 'pam'" in out
    assert db.get("pam")["archived"]


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


def test_attach_promotes_live_nontmux_session(env, monkeypatch):
    """An agent that is up but not in tmux (foreground/adopted) is moved into a
    tmux window so it can be attached — SIGTERM the process, resume in tmux."""
    cli, native = env["cli"], env["native"]
    env["db"].register("ada", cwd="/w/ada", session_id="sid-live")
    env["home"].ensure_home("ada", cwd="/w/ada")
    env["home"].write_session_id("ada", "sid-live")

    calls = {"n": 0}
    def fake_resolve(sid):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"pid": 999}          # first look: live, outside tmux
        raise native.SessionNotFound(sid)  # after SIGTERM: gone
    monkeypatch.setattr(native, "resolve", fake_resolve)
    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    host = _StubHost()                   # name == "stub"; start() records
    rc = cli._promote_to_host("ada", host, None)
    assert rc == 0
    assert killed == [(999, 15)]         # SIGTERM'd the foreground process
    assert host.started and host.started[0][0] == "ada"  # resumed under the host


def test_attach_promotes_down_agent_into_herdr(env, monkeypatch):
    """`attach <name> --host herdr` on a down agent resumes it on that host (the
    generalized promote) and then focuses it — the move-into-herdr path, symmetric
    with the tmux promote but for a non-tmux pty host."""
    cli, native = env["cli"], env["native"]
    env["db"].register("wally", cwd="/w", session_id="sid-w", team="oliver")
    env["home"].ensure_home("wally", cwd="/w")
    env["home"].write_session_id("wally", "sid-w")
    monkeypatch.setattr(native, "resolve",
                        lambda sid: (_ for _ in ()).throw(native.SessionNotFound(sid)))
    monkeypatch.setattr(env["session"], "transcript_exists", lambda sid, cwd: True)

    class _Herdrish:
        name = "herdr"; group = True
        def __init__(self): self.started, self.attached = [], []
        def has_window(self, agent, group=None): return False      # not a tab yet
        def start(self, agent, cmd, cwd, e, group=None):
            from agents import hosts
            self.started.append((agent, group)); return hosts.Launched(pid=None, proc=None)
        def attach(self, agent, group=None): self.attached.append((agent, group))
    h = _Herdrish()
    monkeypatch.setattr(cli.hosts, "get", lambda _n: h)

    a = cli.build_parser().parse_args(["attach", "wally", "--host", "herdr"])
    assert a.func(a) == 0
    assert h.started == [("wally", "oliver")]   # resumed on herdr, team-keyed workspace
    assert h.attached == [("wally", "oliver")]  # then focused the new tab


def test_attach_will_not_kill_live_agent_without_consent(env, monkeypatch):
    """A live, out-of-tmux agent is not restarted on a bare `attach`: it refuses
    (same refuse-unless---force gate `rename` uses) rather than interrupting
    possible mid-task work. `attach -f` is the way to move it."""
    cli, native = env["cli"], env["native"]
    env["db"].register("busy", cwd="/w/busy", session_id="sid-x")
    env["home"].ensure_home("busy", cwd="/w/busy")
    env["home"].write_session_id("busy", "sid-x")
    monkeypatch.setattr(native, "resolve", lambda sid: {"pid": 123})  # always live
    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda *a: killed.append(a))

    a = cli.build_parser().parse_args(["attach", "busy"])
    assert a.func(a) == 1          # refused — needs --force
    assert killed == []            # the process was left alone


def test_attach_targets_real_window_by_pid(env, monkeypatch):
    """attach finds the agent's actual tmux window by pid — even when its registry
    team names a different session that happens to exist (the modsec-daily bug:
    team 'ada' session existed with another agent's window, so attach leaked
    'can't find window'). It must attach to where the pid actually runs."""
    cli, native = env["cli"], env["native"]
    env["db"].register("modsec-daily", agent_type="ada", team="ada", session_id="sid-m")
    env["home"].ensure_home("modsec-daily")
    env["home"].write_session_id("modsec-daily", "sid-m")
    monkeypatch.setattr(native, "resolve", lambda sid: {"pid": 66300})      # live
    host = cli._host(type("A", (), {"host": None})())
    monkeypatch.setattr(type(host), "locate_pid",
                        staticmethod(lambda pid: "agents-modsec-daily:0" if pid == 66300 else None))
    class _Attached(Exception): pass
    monkeypatch.setattr(type(host), "attach_window",
                        lambda self, target: (_ for _ in ()).throw(_Attached(target)))
    a = cli.build_parser().parse_args(["attach", "modsec-daily"])
    with pytest.raises(_Attached) as e:
        a.func(a)
    assert str(e.value) == "agents-modsec-daily:0"   # its real window, not team 'ada'


def test_ensure_stopped_for_restart_gate(env, monkeypatch):
    """The shared gate behind attach-into-tmux and rename: refuse a live agent
    without force; stop it with force; pass through when it isn't running."""
    cli, native = env["cli"], env["native"]
    env["db"].register("busy", session_id="sid-x")
    env["home"].ensure_home("busy")
    env["home"].write_session_id("busy", "sid-x")

    # live + no force -> 'needs_force', nothing killed
    monkeypatch.setattr(native, "resolve", lambda sid: {"pid": 5})
    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda *a: killed.append(a))
    assert cli._ensure_stopped_for_restart("busy", False)[0] == "needs_force"
    assert killed == []

    # live + force -> stops it -> 'stopped'
    state = {"alive": True}
    def resolve(sid):
        if state["alive"]:
            return {"pid": 5}
        raise native.SessionNotFound(sid)
    monkeypatch.setattr(native, "resolve", resolve)
    monkeypatch.setattr(cli.os, "kill", lambda p, s: state.update(alive=False))
    assert cli._ensure_stopped_for_restart("busy", True)[0] == "stopped"

    # not running -> 'ok'
    monkeypatch.setattr(native, "resolve",
                        lambda sid: (_ for _ in ()).throw(native.SessionNotFound(sid)))
    assert cli._ensure_stopped_for_restart("busy", False)[0] == "ok"


def _not_running(env, monkeypatch):
    native = env["native"]
    monkeypatch.setattr(native, "resolve",
                        lambda sid: (_ for _ in ()).throw(native.SessionNotFound(sid)))


def test_project_dir_slug(env, monkeypatch):
    """Claude's project dir replaces EVERY non-alphanumeric char with '-' (the dot
    in `.agents` becomes a dash too), not just slashes. Renaming relied on this —
    a bare slash substitution strands the transcript."""
    session = env["session"]
    monkeypatch.setattr(session, "PROJECTS_DIR", "/P")
    assert session.project_dir("/Users/mek/.agents/ada-1581") == \
        "/P/-Users-mek--agents-ada-1581"
    assert session.project_dir("/Users/mek/Projects/ada") == "/P/-Users-mek-Projects-ada"


def test_rename_external_cwd_leaves_transcript(env, tmp_path, monkeypatch):
    """Renaming an agent whose cwd is a real project checkout moves its home and
    repoints the row, but leaves cwd (and so the Claude transcript) untouched —
    resume keeps working because the cwd slug didn't change."""
    cli, db, home, session = env["cli"], env["db"], env["home"], env["session"]
    monkeypatch.setattr(session, "PROJECTS_DIR", str(tmp_path / "projects"))
    db.register("saul", role="Solr", cwd="/Projects/ada", session_id="sid-1")
    home.ensure_home("saul", cwd="/Projects/ada")
    home.write_session_id("saul", "sid-1")
    slug = session.project_dir("/Projects/ada")
    os.makedirs(slug)
    open(os.path.join(slug, "sid-1.jsonl"), "w").write("{}")
    _not_running(env, monkeypatch)

    a = cli.build_parser().parse_args(["rename", "saul", "sol"])
    assert a.func(a) == 0
    assert db.get("saul") is None
    assert db.get("sol")["session_id"] == "sid-1"
    assert db.get("sol")["cwd"] == "/Projects/ada"          # cwd unchanged
    assert os.path.isdir(home.home_dir("sol"))
    assert not os.path.isdir(home.home_dir("saul"))
    assert os.path.exists(os.path.join(slug, "sid-1.jsonl"))  # transcript stayed put


def test_rename_home_cwd_moves_transcript(env, tmp_path, monkeypatch):
    """Renaming a default-cwd agent (cwd == home dir) moves the home AND the Claude
    transcript to the new cwd slug, and rewrites cwd — otherwise `claude --resume`
    would look in the wrong, empty project dir. The home dir path has a dot
    (`.agents`-style) via a dotted state dir, exercising the real slug rule."""
    cli, db, home, session = env["cli"], env["db"], env["home"], env["session"]
    monkeypatch.setattr(session, "PROJECTS_DIR", str(tmp_path / "projects"))
    db.register("ada", session_id="sid-9")      # no cwd -> defaults to home dir
    home.ensure_home("ada")
    home.write_session_id("ada", "sid-9")
    old_home = home.home_dir("ada")
    old_slug = session.project_dir(old_home)
    os.makedirs(old_slug)
    open(os.path.join(old_slug, "sid-9.jsonl"), "w").write("{}")
    _not_running(env, monkeypatch)

    a = cli.build_parser().parse_args(["rename", "ada", "ada-lead"])
    assert a.func(a) == 0
    new_home = home.home_dir("ada-lead")
    assert db.get("ada-lead")["cwd"] == new_home            # cwd follows the home
    assert home.agent_cwd("ada-lead") == new_home           # home/cwd file too
    new_slug = session.project_dir(new_home)
    assert os.path.exists(os.path.join(new_slug, "sid-9.jsonl"))  # transcript moved
    assert not os.path.isdir(old_slug)                     # old slug cleaned up


def test_rename_repoints_reports(env, monkeypatch):
    """A rename updates every report whose team pointed at the old name, so the
    reporting tree survives."""
    cli, db, home = env["cli"], env["db"], env["home"]
    db.register("ada", agent_type="lead")
    db.register("r1", team="ada")
    db.register("r2", team="ada")
    for n in ("ada", "r1", "r2"):
        home.ensure_home(n)
    _not_running(env, monkeypatch)

    a = cli.build_parser().parse_args(["rename", "ada", "program-lead"])
    assert a.func(a) == 0
    assert db.get("r1")["team"] == "program-lead"
    assert db.get("r2")["team"] == "program-lead"


def test_rename_refuses_live_without_force(env, monkeypatch):
    cli, db, home, native = env["cli"], env["db"], env["home"], env["native"]
    db.register("busy", session_id="sid-x")
    home.ensure_home("busy")
    home.write_session_id("busy", "sid-x")
    monkeypatch.setattr(native, "resolve", lambda sid: {"pid": 7})   # always live
    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda *a: killed.append(a))

    a = cli.build_parser().parse_args(["rename", "busy", "calm"])
    assert a.func(a) == 1
    assert db.get("busy") is not None and db.get("calm") is None     # untouched
    assert killed == []
    assert os.path.isdir(home.home_dir("busy"))


def test_rename_force_stops_then_renames(env, monkeypatch):
    cli, db, home, native = env["cli"], env["db"], env["home"], env["native"]
    db.register("busy", session_id="sid-x")
    home.ensure_home("busy")
    home.write_session_id("busy", "sid-x")
    state = {"alive": True}
    def resolve(sid):
        if state["alive"]:
            return {"pid": 999}
        raise native.SessionNotFound(sid)
    monkeypatch.setattr(native, "resolve", resolve)
    killed = []
    monkeypatch.setattr(cli.os, "kill",
                        lambda p, s: (killed.append((p, s)), state.update(alive=False)))

    a = cli.build_parser().parse_args(["rename", "-f", "busy", "calm"])
    assert a.func(a) == 0
    assert db.get("busy") is None and db.get("calm") is not None
    assert killed == [(999, 15)]


def test_rename_rejects_bad_targets(env, monkeypatch):
    cli, db, home = env["cli"], env["db"], env["home"]
    db.register("a"); home.ensure_home("a")
    db.register("b"); home.ensure_home("b")
    _not_running(env, monkeypatch)
    p = cli.build_parser()
    assert p.parse_args(["rename", "a", "b"]).func(p.parse_args(["rename", "a", "b"])) == 1   # collision
    assert p.parse_args(["rename", "a", "a"]).func(p.parse_args(["rename", "a", "a"])) == 1   # same name
    assert p.parse_args(["rename", "a", "x/y"]).func(p.parse_args(["rename", "a", "x/y"])) == 1  # bad name
    assert p.parse_args(["rename", "nope", "z"]).func(p.parse_args(["rename", "nope", "z"])) == 1  # missing
    assert db.get("a") is not None and db.get("b") is not None    # all no-ops


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


def test_find_deep_searches_transcript_body(env, tmp_path, capsys):
    """Default find matches session metadata only (title/cwd/typed prompts); deep
    (-a) greps the transcript body, so a session is findable by what the agent did,
    not just what the user said."""
    import json
    from agents import history

    cwd, sid = "/home/mek/Projects/alpha", "sid-deep-1"
    ts = 1_700_000_000_000
    with open(tmp_path / "history.jsonl", "w") as fh:
        fh.write(json.dumps({"sessionId": sid, "project": cwd,
                             "timestamp": ts, "display": "please build the thing"}) + "\n")
    path = history.transcript_path(cwd, sid)   # uses the real cwd->slug mapping
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(json.dumps({"type": "user",
                             "message": {"content": "please build the thing"}}) + "\n")
        fh.write(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "I adjusted the gearbox torque in waf.conf"}]}}) + "\n")

    # "gearbox" lives only in the body: metadata search misses it, deep finds it.
    assert history.find_sessions("gearbox") == []
    deep = history.find_sessions("gearbox", deep=True)
    assert [r["session_id"] for r in deep] == [sid]
    # a metadata term (the cwd) matches without deep
    assert [r["session_id"] for r in history.find_sessions("alpha")] == [sid]

    # the cli wires -a to deep: plain `find gearbox` finds nothing, `find -a gearbox` hits
    p = env["cli"].build_parser()
    a = p.parse_args(["find", "gearbox"]); a.func(a)
    assert "no matching sessions" in capsys.readouterr().out
    a = p.parse_args(["find", "-a", "gearbox"]); a.func(a)
    assert sid in capsys.readouterr().out


def test_ignore_session_roundtrip(env):
    db = env["db"]
    assert db.ignored_sessions() == set()
    db.ignore_session("s1", reason="noise")
    db.ignore_session("s1")                       # idempotent, keeps reason
    assert "s1" in db.ignored_sessions()
    assert db.unignore_session("s1") is True
    assert db.unignore_session("s1") is False     # already gone
    assert db.ignored_sessions() == set()


def test_rm_archives_restores_and_purges_session(env, tmp_path, capsys):
    """rm on a bare session id/prefix tombstones it (hidden from find, shown with
    --archived), --restore lifts it, --purge deletes the transcript."""
    import json
    from agents import history
    db = env["db"]

    cwd, sid = "/home/mek/Projects/beta", "5d14bfa6-aaaa-bbbb-cccc-000000000001"
    with open(tmp_path / "history.jsonl", "w") as fh:
        fh.write(json.dumps({"sessionId": sid, "project": cwd,
                             "timestamp": 1_700_000_000_000, "display": "beta work"}) + "\n")
    path = history.transcript_path(cwd, sid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(json.dumps({"type": "user", "message": {"content": "beta work"}}) + "\n")

    p = env["cli"].build_parser()
    visible = lambda **k: [r["session_id"] for r in
                           history.find_sessions(ignored=db.ignored_sessions(), **k)]
    assert sid in visible()                         # present before archiving

    # archive by id-prefix
    a = p.parse_args(["rm", "5d14bfa6"]); assert a.func(a) == 0
    assert "archived session" in capsys.readouterr().out
    assert sid in db.ignored_sessions()
    assert sid not in visible()                     # hidden from default find
    shown = [r for r in history.find_sessions(ignored=db.ignored_sessions(),
                                              show_archived=True) if r["session_id"] == sid]
    assert shown and shown[0]["archived"] is True

    # cli find hides it; find --archived reveals it as archived
    a = p.parse_args(["find"]); a.func(a)
    assert sid not in capsys.readouterr().out
    a = p.parse_args(["find", "--archived"]); a.func(a)
    out = capsys.readouterr().out
    assert sid in out and "archived" in out

    # restore lifts the tombstone
    a = p.parse_args(["rm", sid, "--restore"]); assert a.func(a) == 0
    assert sid not in db.ignored_sessions() and sid in visible()

    # purge deletes the transcript (and tombstones it)
    a = p.parse_args(["rm", sid, "--purge"]); assert a.func(a) == 0
    assert "deleted its transcript" in capsys.readouterr().out
    assert not os.path.exists(path) and sid in db.ignored_sessions()


def test_upgrade_clone_and_pipx(monkeypatch, capsys):
    from agents import cli
    calls = []
    class R:
        def __init__(self): self.returncode = 0; self.stderr = ""; self.stdout = "0.1.0 abc1234"
    monkeypatch.setattr("tempfile.mkdtemp", lambda: "/tmp/agents-up-test")
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **k: calls.append(cmd) or R())
    monkeypatch.setattr(cli.shutil, "rmtree", lambda *a, **k: None)
    a = cli.build_parser().parse_args(["upgrade"])
    assert a.func(a) == 0
    clone = next(c for c in calls if c[:2] == ["git", "clone"])
    assert "--branch" not in clone  # plain upgrade = main
    assert any(c[0] == "pipx" and "install" in c and "--force" in c for c in calls)
    # consolidated message names the version + the venv, replacing pipx's chatter
    out = capsys.readouterr().out
    assert "upgrading via pipx into venv 'agents'" in out
    assert "pulling" in out and "upgraded to" in out


def test_upgrade_testing_branch(monkeypatch, capsys):
    from agents import cli
    calls = []
    class R:
        def __init__(self): self.returncode = 0; self.stderr = ""; self.stdout = "abc1234"
    monkeypatch.setattr("tempfile.mkdtemp", lambda: "/tmp/agents-up-test2")
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **k: calls.append(cmd) or R())
    monkeypatch.setattr(cli.shutil, "rmtree", lambda *a, **k: None)
    a = cli.build_parser().parse_args(["upgrade", "--testing", "fix-1"])
    assert a.func(a) == 0
    clone = next(c for c in calls if c[:2] == ["git", "clone"])
    assert "--branch" in clone and "fix-1" in clone
    out = capsys.readouterr().out
    assert "fix-1" in out and "not the stable release" in out   # branch warning + label


def test_invalid_command_prints_help_not_choices(capsys):
    from agents import cli
    with pytest.raises(SystemExit) as e:
        cli.build_parser().parse_args(["upgradex"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "usage: agents" in err and "_complete" not in err


def test_ls_shows_unregistered_live_sessions(env, monkeypatch, capsys):
    from agents import history
    env["db"].register("pam")  # registered, no live session -> down
    monkeypatch.setattr(env["native"], "list_sessions",
                        lambda *a, **k: [{"sessionId": "live-unreg-1", "cwd": "/x/proj",
                                          "name": "stray"}])
    monkeypatch.setattr(history, "find_sessions",
                        lambda *a, **k: [{"session_id": "live-unreg-1", "title": "a stray task"}])
    a = env["cli"].build_parser().parse_args(["ls"])
    assert a.func(a) == 0
    out = capsys.readouterr().out
    assert "pam" in out                                # registered agent still shown
    assert "stray" in out and "live-unr" in out and "a stray task" in out  # unregistered up


def test_send_by_session_id(env, monkeypatch, capsys):
    sent = {}
    monkeypatch.setattr(env["native"], "send",
                        lambda sid, msg, sender=None: sent.update(sid=sid, msg=msg))
    a = env["cli"].build_parser().parse_args(["send", "-s", "sess-xyz", "hello", "world"])
    assert a.func(a) == 0
    assert sent["sid"] == "sess-xyz" and sent["msg"] == "hello world"
    assert "delivered to 'sess-xyz'" in capsys.readouterr().out


def test_send_requires_a_target(env):
    a = env["cli"].build_parser().parse_args(["send"])  # no name, no -s
    assert a.func(a) == 1


def test_resolve_session_id_prefix_full_name_pid_ambiguity(env, monkeypatch):
    cli, native = env["cli"], env["native"]
    recs = [{"sessionId": "48ba4ae5-aaaa-1111", "name": "foo-1", "pid": 11},
            {"sessionId": "48ba4ae5-bbbb-2222", "name": "foo-2", "pid": 22},
            {"sessionId": "9c0d0e0f-cccc-3333", "name": "bar-3", "pid": 33}]
    monkeypatch.setattr(native, "list_sessions", lambda *a, **k: recs)
    # unique id prefix -> full id
    assert cli._resolve_session_id("9c0d0e")[0] == "9c0d0e0f-cccc-3333"
    # full id -> itself
    assert cli._resolve_session_id("48ba4ae5-aaaa-1111")[0] == "48ba4ae5-aaaa-1111"
    # exact live name / pid
    assert cli._resolve_session_id("foo-2")[0] == "48ba4ae5-bbbb-2222"
    assert cli._resolve_session_id("33")[0] == "9c0d0e0f-cccc-3333"
    # ambiguous prefix -> err, no sid
    sid, _, err = cli._resolve_session_id("48ba4ae5")
    assert sid is None and "matches 2 live sessions" in err
    # nothing live matches -> pass through unchanged (transport resolves/fails)
    assert cli._resolve_session_id("deadbeef-0000")[0] == "deadbeef-0000"


def test_send_dash_s_accepts_ls_prefix(env, monkeypatch, capsys):
    """`send -s <8-char prefix>` (exactly what ls prints) resolves to the full id
    and delivers — the gap that used to need the full sessionId."""
    cli, native = env["cli"], env["native"]
    monkeypatch.setattr(native, "list_sessions",
                        lambda *a, **k: [{"sessionId": "48ba4ae5-84da-4c6d", "name": "x-1"}])
    sent = {}
    monkeypatch.setattr(native, "send",
                        lambda sid, msg, sender=None: sent.update(sid=sid, msg=msg))
    a = cli.build_parser().parse_args(["send", "-s", "48ba4ae5", "ping"])
    assert a.func(a) == 0
    assert sent["sid"] == "48ba4ae5-84da-4c6d"      # expanded to the full id
    assert sent["msg"] == "ping"


def test_send_dash_s_ambiguous_prefix_refuses(env, monkeypatch):
    cli, native = env["cli"], env["native"]
    monkeypatch.setattr(native, "list_sessions", lambda *a, **k: [
        {"sessionId": "48ba4ae5-aaaa", "name": "a"},
        {"sessionId": "48ba4ae5-bbbb", "name": "b"}])
    sent = {}
    monkeypatch.setattr(native, "send", lambda *a, **k: sent.update(called=True))
    a = cli.build_parser().parse_args(["send", "-s", "48ba4ae5", "hi"])
    assert a.func(a) == 1                 # refused as ambiguous
    assert not sent                       # nothing delivered


def test_resolve_target_registered_and_live(env, monkeypatch):
    cli = env["cli"]
    monkeypatch.setattr(env["native"], "list_sessions",
                        lambda *a, **k: [{"sessionId": "dc33a929-aaaa-bbbb", "name": "projects-70"}])
    assert cli._resolve_target("projects-70")[0] == "dc33a929-aaaa-bbbb"   # by display name
    assert cli._resolve_target("dc33a929")[0] == "dc33a929-aaaa-bbbb"      # by id prefix
    env["db"].register("pam"); env["db"].set_session("pam", "sid-pam")
    assert cli._resolve_target("pam") == ("sid-pam", "pam")                # registered wins
    assert cli._resolve_target("nope")[0] is None


def test_down_stops_a_live_session(env, monkeypatch, capsys):
    cli = env["cli"]
    monkeypatch.setattr(env["native"], "list_sessions",
                        lambda *a, **k: [{"sessionId": "live-sid", "name": "projects-70"}])
    monkeypatch.setattr(env["native"], "resolve", lambda sid: {"pid": 999999})
    killed = {}
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.update(pid=pid, sig=sig))
    a = cli.build_parser().parse_args(["down", "projects-70"])
    assert a.func(a) == 0
    assert killed == {"pid": 999999, "sig": 15}
    assert "stopped 'projects-70'" in capsys.readouterr().out


def test_send_to_live_session_by_name(env, monkeypatch, capsys):
    cli = env["cli"]
    monkeypatch.setattr(env["native"], "list_sessions",
                        lambda *a, **k: [{"sessionId": "live-sid", "name": "projects-70"}])
    sent = {}
    monkeypatch.setattr(env["native"], "send",
                        lambda sid, msg, sender=None: sent.update(sid=sid, msg=msg))
    a = cli.build_parser().parse_args(["send", "projects-70", "ping"])
    assert a.func(a) == 0
    assert sent["sid"] == "live-sid" and sent["msg"] == "ping"


def test_complete_targets_includes_live_sessions(env, monkeypatch, capsys):
    cli = env["cli"]
    env["db"].register("pam")
    monkeypatch.setattr(env["native"], "list_sessions",
                        lambda *a, **k: [{"sessionId": "x", "name": "projects-70"}])
    a = cli.build_parser().parse_args(["_complete", "targets"])
    assert a.func(a) == 0
    out = capsys.readouterr().out.split()
    assert "pam" in out and "projects-70" in out


def test_existing_session_matches_live_and_history(env, monkeypatch):
    from agents import history
    cli = env["cli"]
    monkeypatch.setattr(env["native"], "list_sessions",
                        lambda *a, **k: [{"sessionId": "live-aaaa-bbbb", "cwd": "/w/a",
                                          "name": "projects-70"}])
    monkeypatch.setattr(history, "find_sessions",
                        lambda *a, **k: [{"session_id": "idle-cccc-dddd", "cwd": "/w/b",
                                          "title": "t"}])
    assert cli._existing_session("projects-70")[:2] == ("live-aaaa-bbbb", "/w/a")  # live by name
    assert cli._existing_session("live-aaaa")[0] == "live-aaaa-bbbb"               # live by id prefix
    assert cli._existing_session("idle-c")[:2] == ("idle-cccc-dddd", "/w/b")       # idle via history
    assert cli._existing_session("brand-new")[0] is None                           # a new agent


def test_up_adopts_existing_live_session(env, monkeypatch):
    cli = env["cli"]
    monkeypatch.setattr(env["native"], "list_sessions",
                        lambda *a, **k: [{"sessionId": "live-sid-123", "cwd": "/w/proj",
                                          "name": "projects-70"}])
    monkeypatch.setattr(env["native"], "resolve", lambda sid: {"pid": 4242})
    host = _StubHost()
    rc = cli._up_one("projects-70", host)      # unregistered, but names a live session
    assert rc == 1                             # already running (resolved), not spawned
    assert host.started == []
    row = env["db"].get("projects-70")         # adopted into the registry with its binding
    assert row["session_id"] == "live-sid-123" and row["cwd"] == "/w/proj"


# --- whois: registry name -> live Claude-land SendMessage address -------------

def test_whois_live_resolves_claude_land_address(env, monkeypatch, capsys):
    # registry name 'oliver' but Claude advertises a different, cwd-derived name.
    env["db"].register("oliver", session_id="sid-oliver")
    env["home"].ensure_home("oliver", cwd="/tmp/x")
    monkeypatch.setattr(env["native"], "resolve", lambda sid: {
        "sessionId": "sid-oliver", "name": "ada-8d", "formerNames": ["ada-3f"],
        "messagingSocketPath": "/tmp/cc-socks/999.sock", "pid": 999, "status": "idle"})
    a = env["cli"].build_parser().parse_args(["whois", "oliver"])
    assert a.func(a) == 0
    out = capsys.readouterr().out
    assert "uds:/tmp/cc-socks/999.sock" in out   # the fool-proof SendMessage target
    assert "ada-8d" in out                        # claude name != registry name


def test_whois_json_is_machine_readable(env, monkeypatch, capsys):
    import json
    env["db"].register("locke", session_id="sid-locke")
    env["home"].ensure_home("locke", cwd="/tmp/x")
    monkeypatch.setattr(env["native"], "resolve", lambda sid: {
        "sessionId": "sid-locke", "name": "locke-28",
        "messagingSocketPath": "/tmp/cc-socks/77.sock", "pid": 77, "status": "busy"})
    a = env["cli"].build_parser().parse_args(["whois", "locke", "--json"])
    assert a.func(a) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["sendmessage_to"] == "uds:/tmp/cc-socks/77.sock"
    assert d["claude_name"] == "locke-28" and d["live"] is True


def test_whois_down_registered_agent_has_no_target(env, capsys):
    # registered but never bound (no session) -> clear 'not live', exit 1, not an error
    env["db"].register("saul")
    env["home"].ensure_home("saul", cwd="/tmp/x")
    a = env["cli"].build_parser().parse_args(["whois", "saul"])
    assert a.func(a) == 1
    assert "not live" in capsys.readouterr().out


def test_whois_unknown_name_errors(env):
    a = env["cli"].build_parser().parse_args(["whois", "ghost"])
    assert a.func(a) != 0


# --- structured metadata (pr/issue/project) --------------------------------

def test_meta_set_at_register_and_read_back(env):
    db = env["db"]
    db.register("alpha", agent_type="ada",
                meta={"project": "ol", "issue": "14200", "pr": "14212"})
    assert db.meta_of(db.get("alpha")) == {
        "project": "ol", "issue": "14200", "pr": "14212"}
    # an agent with no metadata reads back as an empty dict, never None
    db.register("plain", agent_type="ada")
    assert db.meta_of(db.get("plain")) == {}


def test_meta_query_existence_and_equality(env):
    db = env["db"]
    db.register("alpha", meta={"project": "ol", "issue": "14200", "pr": "14212"})
    db.register("beta", meta={"project": "ol", "issue": "14300"})
    db.register("gamma", meta={"project": "bgp"})
    db.register("delta")
    has = lambda *f: sorted(a["name"] for a in db.select(meta=f))
    # Mek's three existence queries: "agents that have a metadata.<key>"
    assert has(("pr", None)) == ["alpha"]
    assert has(("issue", None)) == ["alpha", "beta"]
    assert has(("project", None)) == ["alpha", "beta", "gamma"]
    # equality, incl. the forge resolver "the agent for PR N"
    assert has(("project", "ol")) == ["alpha", "beta"]
    assert has(("pr", "14212")) == ["alpha"]


def test_meta_equality_coerces_both_sides_to_string(env):
    # stored as an int (via a {json} blob), queried as a string -> still matches
    db = env["db"]
    db.register("eps", meta={"pr": 99})
    assert [a["name"] for a in db.select(meta=[("pr", "99")])] == ["eps"]


def test_meta_set_merges_and_rm_drops(env, capsys):
    db = env["db"]
    db.register("beta", meta={"project": "ol", "issue": "14300"})
    P = env["cli"].build_parser()
    a = P.parse_args(["meta", "set", "beta", "pr=14305"]); assert a.func(a) == 0
    assert db.meta_of(db.get("beta")) == {
        "project": "ol", "issue": "14300", "pr": "14305"}   # merge, not replace
    a = P.parse_args(["meta", "rm", "beta", "issue"]); assert a.func(a) == 0
    assert db.meta_of(db.get("beta")) == {"project": "ol", "pr": "14305"}


def test_meta_survives_reregister_without_meta(env):
    # re-registering (e.g. on `up`) without --meta must not wipe metadata (coalesce)
    db = env["db"]
    db.register("alpha", meta={"pr": "14212"})
    db.register("alpha", agent_type="ada")  # no meta passed
    assert db.meta_of(db.get("alpha")) == {"pr": "14212"}


def test_ls_json_emits_meta_and_filters(env, capsys):
    import json
    db = env["db"]
    db.register("alpha", agent_type="ada", meta={"project": "ol", "pr": "14212"})
    db.register("beta", agent_type="ada", meta={"project": "ol"})
    a = env["cli"].build_parser().parse_args(["ls", "--json", "--meta", "pr"])
    assert a.func(a) == 0
    rows = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert [r["name"] for r in rows] == ["alpha"]
    assert rows[0]["meta"] == {"project": "ol", "pr": "14212"}


def test_whois_json_includes_meta(env, monkeypatch, capsys):
    import json
    env["db"].register("alpha", session_id="sid-a", meta={"pr": "14212"})
    env["home"].ensure_home("alpha", cwd="/tmp/x")
    monkeypatch.setattr(env["native"], "resolve", lambda sid: {
        "sessionId": "sid-a", "name": "ada-9",
        "messagingSocketPath": "/tmp/cc-socks/9.sock", "pid": 9, "status": "idle"})
    a = env["cli"].build_parser().parse_args(["whois", "alpha", "--json"])
    assert a.func(a) == 0
    assert json.loads(capsys.readouterr().out)["meta"] == {"pr": "14212"}


def test_meta_register_rejects_bare_key(env):
    # register --meta must be key=value; a bare key is a usage error, not silent
    a = env["cli"].build_parser().parse_args(["register", "x", "--meta", "pr"])
    assert a.func(a) == 1


# --- recap / checkin -------------------------------------------------------

def test_checkin_sets_recap_timestamp_and_journal(env):
    db, cli = env["db"], env["cli"]
    db.register("dewey", agent_type="lead")
    a = cli.build_parser().parse_args(
        ["checkin", "--agent", "dewey", "ada", "staffed;", "commit", "drafted"])
    assert a.func(a) == 0
    row = db.get("dewey")
    assert row["recap"] == "ada staffed; commit drafted"
    assert row["recap_at"]                              # timestamped
    assert env["home"].recap_log("dewey")[-1][1] == "ada staffed; commit drafted"


def test_checkin_defaults_to_calling_agent(env, monkeypatch):
    db, cli = env["db"], env["cli"]
    db.register("saul", agent_type="lead", session_id="sid-saul")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-saul")
    a = cli.build_parser().parse_args(["checkin", "solr", "reindex", "running"])
    assert a.func(a) == 0
    assert db.get("saul")["recap"] == "solr reindex running"


def test_checkin_without_agent_or_caller_errors(env, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    a = env["cli"].build_parser().parse_args(["checkin", "hello"])
    assert a.func(a) == 1


def test_detail_lines_stack_meta_and_groups_with_cap(env):
    f = env["cli"]._detail_lines
    assert f([], {}) == []
    # common keys lead; lists bracketed; groups folded in as its own line
    assert f(["solr", "tags"], {"pr": "123", "project": "ol", "epics": [1, 2]}) == \
        ["project: ol", "pr: 123", "epics: [1, 2]", "groups: [solr, tags]"]
    # with a cap, extras collapse into a '… +N more' line
    capped = f(["solr"], {"project": "ol", "issue": "1", "pr": "2"}, limit=3)
    assert capped == ["project: ol", "issue: 1", "pr: 2", "… +1 more"]


def test_ls_default_shows_meta_count_not_fields(env, capsys):
    db, cli = env["db"], env["cli"]
    db.register("w", agent_type="lead", meta={"project": "ol", "pr": "123"},
                workspace="solr")
    a = cli.build_parser().parse_args(["ls", "--flat"])
    assert a.func(a) == 0
    out = capsys.readouterr().out
    assert "META" in out                                 # the count column header
    assert "3" in out                                    # project + pr + groups = 3
    assert "project: ol" not in out                      # not expanded by default


def test_ls_bare_meta_expands_stacked(env, capsys):
    db, cli = env["db"], env["cli"]
    db.register("w", agent_type="lead", meta={"project": "ol", "pr": "123"},
                workspace="solr")
    a = cli.build_parser().parse_args(["ls", "--flat", "--meta"])
    assert a.func(a) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(l.strip() == "project: ol" for l in lines)      # stacked beneath
    assert any(l.strip() == "pr: 123" for l in lines)
    assert any(l.strip() == "groups: [solr]" for l in lines)
    assert "{" not in "\n".join(lines)                          # not JSON


def test_ls_meta_key_shows_inline_and_filters(env, capsys):
    db, cli = env["db"], env["cli"]
    db.register("w", agent_type="lead", meta={"project": "ol", "pr": "123"})
    db.register("x", agent_type="ada", meta={"project": "ol"})   # no pr
    a = cli.build_parser().parse_args(["ls", "--flat", "--meta", "pr"])
    assert a.func(a) == 0
    out = capsys.readouterr().out
    assert "pr: 123" in out                              # the key, inline
    assert "project: ol" not in out                      # only the asked-for key
    assert "x " not in out and "\nx" not in out          # filtered to pr-havers


def test_ls_meta_list_key_stacks_full(env, capsys):
    # a list-valued key (epics) is too long to inline, so --meta epics stacks the
    # full list beneath the agent instead of clipping it in the column
    db, cli = env["db"], env["cli"]
    db.register("dewey", agent_type="lead", meta={"epics": [13923, 13924, 13925]})
    db.register("saul", agent_type="lead", meta={"project": "ol"})   # no epics
    a = cli.build_parser().parse_args(["ls", "--flat", "--meta", "epics"])
    assert a.func(a) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(l.strip() == "epics: [13923, 13924, 13925]" for l in lines)  # full, stacked
    assert not any("saul" in l for l in lines)           # filtered to epics-havers


def test_recap_shows_checkin_history_newest_first(env, capsys):
    db, cli = env["db"], env["cli"]
    db.register("dewey", agent_type="lead")
    for text in ("stepA", "stepB", "stepC"):
        a = cli.build_parser().parse_args(["checkin", "--agent", "dewey", text])
        a.func(a)
    capsys.readouterr()  # drain the checkin confirmations
    a = cli.build_parser().parse_args(["recap", "dewey"])
    assert a.func(a) == 0
    out = capsys.readouterr().out
    assert "check-ins" in out and "stepA" in out and "stepC" in out
    assert out.index("stepC") < out.index("stepA")       # newest first
    a = cli.build_parser().parse_args(["recap", "dewey", "-n", "1"])
    a.func(a)
    out2 = capsys.readouterr().out
    assert "stepC" in out2 and "stepA" not in out2        # -n caps


def test_recap_no_checkins(env, capsys):
    env["db"].register("x", agent_type="ada")
    a = env["cli"].build_parser().parse_args(["recap", "x"])
    assert a.func(a) == 0
    assert "no check-ins" in capsys.readouterr().out


def test_ls_desc_prefers_recap_over_role(env, capsys):
    db, cli = env["db"], env["cli"]
    db.register("dewey", agent_type="lead", role="Tags Lead")
    db.set_recap("dewey", "mapping registry.json")
    a = cli.build_parser().parse_args(["ls", "--flat"])
    assert a.func(a) == 0
    out = capsys.readouterr().out
    assert "mapping registry.json" in out      # the fresh recap, not the role


# --- more (full record) ----------------------------------------------------

def test_more_shows_full_record(env, capsys):
    db, cli = env["db"], env["cli"]
    db.register("dewey", agent_type="lead", role="Tags Lead",
                meta={"project": "ol"}, cwd="/tmp/x")
    db.set_recap("dewey", "ada up")
    a = cli.build_parser().parse_args(["more", "dewey"])
    assert a.func(a) == 0
    out = capsys.readouterr().out
    assert "Tags Lead" in out and "ada up" in out and "project" in out


def test_more_json_is_machine_readable(env, capsys):
    import json
    db, cli = env["db"], env["cli"]
    db.register("dewey", agent_type="lead", role="Tags Lead", meta={"pr": "1"})
    db.set_recap("dewey", "done")
    a = cli.build_parser().parse_args(["more", "dewey", "--json"])
    assert a.func(a) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["desc"] == "Tags Lead" and d["recap"] == "done" and d["meta"] == {"pr": "1"}


def test_register_desc_is_alias_for_role(env):
    a = env["cli"].build_parser().parse_args(
        ["register", "x", "--desc", "does a thing"])
    assert a.func(a) == 0
    assert env["db"].get("x")["role"] == "does a thing"


# --- snapshot / reindex ----------------------------------------------------

def test_snapshot_reindex_rebuilds_registry(env):
    db = env["db"]
    db.register("alpha", agent_type="ada", role="A", meta={"pr": "1"})
    db.register("beta", agent_type="lead", team=None)
    db.set_recap("alpha", "halfway")
    db.ignore_session("ghost-sid", reason="noise")
    db.snapshot()
    # simulate loss: delete the rows, then rebuild purely from the JSON snapshot
    db.remove("alpha")
    db.remove("beta")
    assert db.get("alpha") is None
    n_agents, n_ignored = db.reindex_from_snapshot()
    assert n_agents == 2 and n_ignored == 1
    a = db.get("alpha")
    assert a["role"] == "A" and db.meta_of(a) == {"pr": "1"} and a["recap"] == "halfway"
    assert "ghost-sid" in db.ignored_sessions()


def test_maybe_snapshot_is_lazy(env):
    db = env["db"]
    db.register("alpha")
    assert db.maybe_snapshot() is True       # missing -> writes
    assert db.maybe_snapshot() is False      # fresh -> skips
    assert db.maybe_snapshot(max_age=0) is True   # forced stale -> rewrites


def test_snapshot_is_atomic_json(env, capsys):
    import json
    db, cli = env["db"], env["cli"]
    db.register("alpha", meta={"project": "ol"})
    a = cli.build_parser().parse_args(["snapshot"])
    assert a.func(a) == 0
    with open(db.SNAPSHOT_PATH) as f:
        data = json.load(f)
    assert data["agents"][0]["name"] == "alpha" and "written_at" in data


# --- session (transcript tail/head) ----------------------------------------

def _write_transcript(env, name, sid, cwd, lines):
    from agents import history
    path = history.transcript_path(cwd, sid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    import json
    with open(path, "w") as f:
        for r in lines:
            f.write(json.dumps(r) + "\n")
    return path


def test_session_renders_transcript(env, capsys, tmp_path):
    db, cli = env["db"], env["cli"]
    cwd = str(tmp_path / "proj")
    db.register("t1", agent_type="ada", session_id="sid-t1", cwd=cwd)
    _write_transcript(env, "t1", "sid-t1", cwd, [
        {"type": "user", "message": {"role": "user", "content": "add warnings"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "on it"}, {"type": "tool_use", "name": "Edit"}]}},
        {"type": "mode"},
    ])
    a = cli.build_parser().parse_args(["session", "t1"])
    assert a.func(a) == 0
    out = capsys.readouterr().out
    assert "you: add warnings" in out and "asst: on it" in out and "⚙ Edit" in out
    assert "mode" not in out                  # non-message events are skipped


def test_session_head_and_raw(env, capsys, tmp_path):
    db, cli = env["db"], env["cli"]
    cwd = str(tmp_path / "proj")
    db.register("t1", agent_type="ada", session_id="sid-t1", cwd=cwd)
    _write_transcript(env, "t1", "sid-t1", cwd, [
        {"type": "user", "message": {"role": "user", "content": "first"}},
        {"type": "user", "message": {"role": "user", "content": "second"}},
    ])
    a = cli.build_parser().parse_args(["session", "t1", "--head", "-n", "1"])
    assert a.func(a) == 0
    assert "first" in capsys.readouterr().out
    a = cli.build_parser().parse_args(["session", "t1", "--raw", "-n", "1"])
    assert a.func(a) == 0
    assert '"second"' in capsys.readouterr().out   # exact JSONL, last line


def test_session_no_transcript_errors(env):
    env["db"].register("t2", agent_type="ada")   # never ran, no session
    a = env["cli"].build_parser().parse_args(["session", "t2"])
    assert a.func(a) == 1


# --- man (manuals) ---------------------------------------------------------

def test_man_renders_live_flags_and_authored_examples(env, capsys):
    a = env["cli"].build_parser().parse_args(["man", "register"])
    assert a.func(a) == 0
    out = capsys.readouterr().out
    assert "--meta" in out and "--session-id" in out        # flags, live from parser
    assert "BEST PRACTICES" in out and "EXAMPLES" in out
    assert "agents register saul" in out                    # an authored example
    assert "gh issue create" in out                         # the open-an-issue nudge


def test_man_overview_recommends_man_and_lists_concepts(env, capsys):
    a = env["cli"].build_parser().parse_args(["man"])
    assert a.func(a) == 0
    out = capsys.readouterr().out
    assert "agents man <command>" in out
    assert "herdr" in out and "conventions" in out


def test_man_concept_topic(env, capsys):
    a = env["cli"].build_parser().parse_args(["man", "herdr"])
    assert a.func(a) == 0
    out = capsys.readouterr().out
    assert "workspace" in out and "tab" in out              # the lead-vs-ada rule


def test_man_unknown_topic_errors(env):
    a = env["cli"].build_parser().parse_args(["man", "nope"])
    assert a.func(a) != 0


def test_manuals_stay_in_sync_with_commands(env):
    # Anti-drift: every CORE manual names a real command, and no manual is stale.
    from agents import manuals
    sub = env["cli"]._subparser_map(env["cli"].build_parser())
    for name in manuals.CORE:
        assert name in sub, f"manual for '{name}' but no such command"
    for name in manuals.MANUALS:
        assert name in sub, f"stale manual: '{name}' is not a command"


def test_help_leads_with_man_recommendation(env):
    text = env["cli"].build_parser().format_help()
    assert "agents man <command>" in text                   # before the command list


# --- meta.epics (list value) -----------------------------------------------

def test_meta_epics_list_set_and_membership_query(env):
    db, cli = env["db"], env["cli"]
    db.register("dewey", agent_type="lead")
    db.register("saul", agent_type="lead")
    # a list value is set via the {json} blob form
    a = cli.build_parser().parse_args(
        ["meta", "set", "dewey", '{"epics":[14200,14300]}'])
    assert a.func(a) == 0
    assert db.meta_of(db.get("dewey"))["epics"] == [14200, 14300]
    # existence: leads that have epics; membership: the lead owning epic 14200
    assert sorted(r["name"] for r in db.select(meta=[("epics", None)])) == ["dewey"]
    assert [r["name"] for r in db.select(meta=[("epics", "14200")])] == ["dewey"]
    assert db.select(meta=[("epics", "99999")]) == []   # not owned by anyone


# --- library facade (import agents; agents.*) ------------------------------

def test_api_register_ls_meta_checkin(env):
    import agents
    row = agents.register("tess", type="ada", team="saul", groups="project:ol",
                          desc="Solr helper", meta={"pr": "14212"})
    assert row["role"] == "Solr helper" and row["meta"] == {"pr": "14212"}
    # ls returns plain dicts with status + parsed groups/meta
    rows = agents.ls(group="project:ol", meta=[("pr", "14212")])
    assert [r["name"] for r in rows] == ["tess"]
    assert rows[0]["status"] == "down" and rows[0]["groups"] == ["project:ol"]
    # checkin + meta_set are data operations, no printing
    ts = agents.checkin("tess", "builder patched")
    assert agents.get("tess")["recap"] == "builder patched" and ts
    assert agents.meta_set("tess", {"issue": "99"})["issue"] == "99"
    assert env["db"].get("tess")["meta"]            # written through to the same db


def test_api_whoami_and_whois_are_data(env, monkeypatch):
    import agents
    agents.register("saul", type="lead", session_id="sid-saul")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-saul")
    assert agents.whoami()["name"] == "saul"
    # not live -> whois is None (no resolver record), never raises
    assert agents.whois("saul") is None


def test_api_send_delivers(env):
    import agents
    sock = os.path.join(env["socks"], f"{os.getpid()}.sock")
    srv = FakeSession(sock)
    try:
        write_session(env["sessions"], os.getpid(), sock, "sid-api", "me",
                      peer_token="cafe" * 8)
        agents.register("lenny", cwd="/tmp/x", session_id="sid-api")
        agents.send("lenny", "hi via api")
        assert wait_for(lambda: any("hi via api" in l.get("message", {}).get("content", "")
                                    for l in srv.lines))
    finally:
        srv.close()


def test_api_send_unbound_raises(env):
    import agents
    agents.register("ghosty", type="ada")   # no session
    import pytest
    with pytest.raises(LookupError):
        agents.send("ghosty", "anyone there?")


# --- versioned schema migrations -------------------------------------------

def test_schema_baseline_version(env):
    db = env["db"]
    db.init()
    assert db.schema_version() == db.BASELINE_VERSION == 1


def test_migration_runs_once_then_is_idempotent(env, monkeypatch):
    db = env["db"]
    db.init()                                  # baseline -> v1
    calls = {"n": 0}

    def migrate_v2(c):
        calls["n"] += 1
        c.execute("ALTER TABLE agents ADD COLUMN extra TEXT")

    monkeypatch.setattr(db, "MIGRATIONS", {2: migrate_v2})
    old, new = db.migrate_now()
    assert (old, new) == (1, 2) and calls["n"] == 1
    # a second pass must not re-run it (version already at 2)
    old2, new2 = db.migrate_now()
    assert (old2, new2) == (2, 2) and calls["n"] == 1


def test_schema_command_reports(env, capsys):
    P = env["cli"].build_parser()
    a = P.parse_args(["schema"])
    assert a.func(a) == 0
    assert "migrated v0 → v1" in capsys.readouterr().out    # fresh db adopts baseline
    a = P.parse_args(["schema"])
    assert a.func(a) == 0
    assert "schema at v1 (up to date)" in capsys.readouterr().out


def test_upgrade_snapshots_before_touching_anything(env, monkeypatch):
    db, cli = env["db"], env["cli"]
    db.register("alpha", meta={"pr": "1"})
    # make the clone fail immediately: the upgrade aborts, but the snapshot must
    # already have been written first.
    import subprocess
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 1, "stderr": "no net", "stdout": ""})())
    a = cli.build_parser().parse_args(["upgrade"])
    assert a.func(a) == 1                       # clone failed
    assert os.path.exists(db.SNAPSHOT_PATH)     # but we backed up first
    import json
    assert json.load(open(db.SNAPSHOT_PATH))["agents"][0]["name"] == "alpha"
