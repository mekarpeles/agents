"""Tests for cmux -> agents migration. Hermetic: fake ~/.cmux + isolated ~/.agents."""

import importlib
import os
import sqlite3

import pytest


def _make_cmux(cmux_dir, agents):
    """Create a fake cmux state dir: agents.db (cmux schema) + home dirs."""
    os.makedirs(cmux_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(cmux_dir, "agents.db"))
    conn.execute("""
        CREATE TABLE agents (
            name TEXT PRIMARY KEY, role TEXT, workspace TEXT, workflow_path TEXT,
            initial_prompt TEXT, no_inject INTEGER, unblock INTEGER,
            allowed_tools TEXT, identity_path TEXT, registered_at TEXT)
    """)
    for a in agents:
        conn.execute(
            "INSERT INTO agents (name, role, workspace, workflow_path, "
            "initial_prompt) VALUES (?,?,?,?,?)",
            (a["name"], a.get("role"), a.get("workspace"),
             a.get("workflow_path"), a.get("initial_prompt")))
        home = os.path.join(cmux_dir, a["name"])
        os.makedirs(home, exist_ok=True)
        if "cwd" in a:
            open(os.path.join(home, "cwd"), "w").write(a["cwd"])
        if "session_id" in a:
            open(os.path.join(home, "last-session-id"), "w").write(a["session_id"])
        if a.get("identity"):
            open(os.path.join(home, "identity.md"), "w").write(a["identity"])
        # ephemeral junk that must NOT be carried over
        open(os.path.join(home, f"{a['name']}.daemon.log"), "w").write("noise")
    conn.commit()
    conn.close()


@pytest.fixture
def envs(tmp_path, monkeypatch):
    cmux = tmp_path / "cmux"
    agents_state = tmp_path / "agents-state"
    agents_state.mkdir()
    monkeypatch.setenv("AGENTS_STATE_DIR", str(agents_state))
    monkeypatch.setenv("CMUX_STATE_DIR", str(cmux))
    from agents_lib import db, home, migrate
    importlib.reload(db)
    importlib.reload(home)
    importlib.reload(migrate)
    return {"cmux": str(cmux), "agents_state": str(agents_state),
            "db": db, "home": home, "migrate": migrate}


def test_plan_reads_cmux(envs):
    _make_cmux(envs["cmux"], [
        {"name": "pam", "role": "owner", "workspace": "pam",
         "cwd": "/Users/mek/Projects/pam", "session_id": "sid-pam",
         "identity": "# pam"},
    ])
    plan = envs["migrate"].plan()
    assert len(plan) == 1
    p = plan[0]
    assert p["name"] == "pam" and p["cwd"] == "/Users/mek/Projects/pam"
    assert p["session_id"] == "sid-pam" and p["has_identity"]
    assert not p["already_migrated"]


def test_dry_run_changes_nothing(envs):
    _make_cmux(envs["cmux"], [{"name": "ada", "cwd": "/x", "session_id": "s"}])
    envs["migrate"].migrate(apply=False)
    assert envs["db"].get("ada") is None  # dry-run wrote nothing


def test_apply_copies_home_and_binds_session(envs):
    _make_cmux(envs["cmux"], [
        {"name": "ada", "role": "lead", "workspace": "ol-leads",
         "cwd": "/Users/mek/Projects/ada", "session_id": "sid-ada",
         "identity": "# ada", "initial_prompt": "be ada"},
    ])
    envs["migrate"].migrate(apply=True)
    row = envs["db"].get("ada")
    assert row["role"] == "lead" and row["workspace"] == "ol-leads"
    assert row["cwd"] == "/Users/mek/Projects/ada"
    assert row["session_id"] == "sid-ada"
    # durable files copied
    h = envs["home"].home_dir("ada")
    assert os.path.exists(os.path.join(h, "identity.md"))
    assert envs["home"].read_session_id("ada") == "sid-ada"
    # ephemeral junk NOT copied
    assert not os.path.exists(os.path.join(h, "ada.daemon.log"))


def test_apply_is_non_destructive_to_cmux(envs):
    _make_cmux(envs["cmux"], [{"name": "ada", "cwd": "/x", "session_id": "s"}])
    before = sorted(os.listdir(os.path.join(envs["cmux"], "ada")))
    envs["migrate"].migrate(apply=True)
    after = sorted(os.listdir(os.path.join(envs["cmux"], "ada")))
    assert before == after  # cmux home untouched
    assert os.path.exists(os.path.join(envs["cmux"], "agents.db"))


def test_idempotent_skip(envs):
    _make_cmux(envs["cmux"], [{"name": "ada", "cwd": "/x", "session_id": "s"}])
    envs["migrate"].migrate(apply=True)
    second = envs["migrate"].migrate(apply=True)
    assert "skip" in second[0]["action"]


def test_link_mode(envs):
    _make_cmux(envs["cmux"], [{"name": "ada", "cwd": "/x", "session_id": "s",
                               "identity": "# ada"}])
    envs["migrate"].migrate(apply=True, link=True)
    h = envs["home"].home_dir("ada")
    assert os.path.islink(h)
    assert os.path.realpath(h) == os.path.realpath(os.path.join(envs["cmux"], "ada"))


def test_names_filter(envs):
    _make_cmux(envs["cmux"], [
        {"name": "ada", "cwd": "/x", "session_id": "s1"},
        {"name": "pam", "cwd": "/y", "session_id": "s2"},
    ])
    envs["migrate"].migrate(apply=True, names=["pam"])
    assert envs["db"].get("pam") is not None
    assert envs["db"].get("ada") is None


def test_no_cmux_db_returns_empty(envs):
    assert envs["migrate"].read_cmux_agents() == []
    assert envs["migrate"].migrate(apply=True) == []


def test_shared_session_id_warning(envs, capsys):
    import importlib
    from agents_lib import cli
    importlib.reload(cli)
    _make_cmux(envs["cmux"], [
        {"name": "pr-a", "cwd": "/x", "session_id": "dup"},
        {"name": "pr-b", "cwd": "/y", "session_id": "dup"},
        {"name": "solo", "cwd": "/z", "session_id": "uniq"},
    ])
    cli.cmd_migrate(type("A", (), {"apply": False, "link": False, "names": []}))
    out = capsys.readouterr().out
    assert "SHARED session_id x2" in out
    assert "WARNING" in out
