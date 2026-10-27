"""Tests for shell completion: the hidden `_complete` data command and the
`completion` script printer. Hermetic (isolated AGENTS_STATE_DIR)."""

import importlib
import os

import pytest


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_STATE_DIR", str(tmp_path / "state"))
    from agents import cli, db, home
    importlib.reload(db)
    importlib.reload(home)
    importlib.reload(cli)
    return cli, db, home


def _run(cli, argv):
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


def test_complete_names(cli_env, capsys):
    cli, db, _ = cli_env
    db.register("pam", workspace="web")
    db.register("ada", agent_type="ada", workspace="web")
    assert _run(cli, ["_complete", "names"]) == 0
    assert set(capsys.readouterr().out.split()) == {"pam", "ada"}


def test_complete_groups_are_distinct(cli_env, capsys):
    cli, db, _ = cli_env
    db.register("pam", workspace="web")
    db.register("ada", workspace="web")
    db.register("solo")  # no group -> excluded
    assert _run(cli, ["_complete", "groups"]) == 0
    assert capsys.readouterr().out.split() == ["web"]


def test_complete_types(cli_env, capsys):
    cli, _, home = cli_env
    assert _run(cli, ["_complete", "types"]) == 0
    out = capsys.readouterr().out.split()
    assert out == home.available_types() and "default" in out


def test_completion_script_prints(cli_env, capsys):
    cli, _, _ = cli_env
    assert _run(cli, ["completion", "zsh"]) == 0
    assert "compdef" in capsys.readouterr().out
    assert _run(cli, ["completion", "bash"]) == 0
    assert "complete -F _agents" in capsys.readouterr().out


def test_completion_bare_errors(cli_env):
    cli, _, _ = cli_env
    assert _run(cli, ["completion"]) == 1  # neither a shell nor --install


def test_completion_install_is_idempotent(cli_env, tmp_path, monkeypatch, capsys):
    cli, _, _ = cli_env
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = tmp_path / ".zshrc"
    rc.write_text("# my existing config\n")

    assert _run(cli, ["completion", "--install"]) == 0
    body = rc.read_text()
    assert body.count(cli._COMPLETION_MARKER) == 1
    assert "source <(agents completion zsh)" in body
    assert body.startswith("# my existing config")      # preserved
    assert (tmp_path / ".zshrc.pre-agents").exists()     # one-time backup

    assert _run(cli, ["completion", "--install"]) == 0   # again: no duplicate
    assert rc.read_text().count(cli._COMPLETION_MARKER) == 1
    assert "already set up" in capsys.readouterr().out


def _bindir_with_agents(tmp_path, monkeypatch, *extra):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("agents", *extra):
        b = bindir / name
        b.write_text("#!/bin/sh\n")
        b.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    return bindir


def test_ag_shortcut_created_when_free(cli_env, tmp_path, monkeypatch):
    cli, _, _ = cli_env
    bindir = _bindir_with_agents(tmp_path, monkeypatch)
    assert cli._install_ag_shortcut() == 0
    ag = bindir / "ag"
    assert ag.is_symlink()
    assert os.path.realpath(ag) == str(bindir / "agents")


def test_ag_shortcut_skipped_when_name_taken(cli_env, tmp_path, monkeypatch, capsys):
    cli, _, _ = cli_env
    bindir = _bindir_with_agents(tmp_path, monkeypatch, "ag")  # ag already exists
    assert cli._install_ag_shortcut() == 0
    assert "already a command" in capsys.readouterr().out
    assert not (bindir / "ag").is_symlink()  # the real 'ag' is left untouched
