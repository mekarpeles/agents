"""Tests for persistent config, permission-mode launch flags, and workspace trust.

Hermetic: AGENTS_STATE_DIR and CLAUDE_CONFIG_PATH are redirected to tmp, and both
modules read those paths per-call, so no reload is needed.
"""

import json
import os

import pytest

from agents import config, trust


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / "agents-state"
    state.mkdir()
    monkeypatch.setenv("AGENTS_STATE_DIR", str(state))
    monkeypatch.setenv("CLAUDE_CONFIG_PATH", str(tmp_path / "claude.json"))
    monkeypatch.delenv("AGENTS_CLAUDE_CMD", raising=False)
    return tmp_path


def test_defaults_when_no_file(env):
    cfg = config.load()
    assert cfg == {"permission_mode": "acceptEdits", "host": "tmux"}


def test_save_roundtrip_and_drops_unknown_keys(env):
    saved = config.save({"permission_mode": "bypassPermissions", "host": "herdr",
                         "junk": "ignored"})
    assert saved == {"permission_mode": "bypassPermissions", "host": "herdr"}
    assert config.load() == saved
    on_disk = json.load(open(config.config_path()))
    assert "junk" not in on_disk


def test_launch_flags_mapping():
    assert config.launch_flags("bypassPermissions") == ["--dangerously-skip-permissions"]
    assert config.launch_flags("acceptEdits") == ["--permission-mode", "acceptEdits"]
    assert config.launch_flags("auto") == ["--permission-mode", "auto"]
    # an unknown mode falls back to the gentle default, never to bypass
    assert config.launch_flags("bogus") == ["--permission-mode", "acceptEdits"]


def test_claude_cmd_uses_configured_mode(env):
    from agents import cli
    assert cli._claude_cmd("x", None) == ["claude", "--permission-mode", "acceptEdits"]
    config.save({"permission_mode": "bypassPermissions", "host": "tmux"})
    assert cli._claude_cmd("x", None) == ["claude", "--dangerously-skip-permissions"]
    assert cli._claude_cmd("x", "sid9")[-2:] == ["--resume", "sid9"]


def test_claude_cmd_env_override_wins(env, monkeypatch):
    from agents import cli
    monkeypatch.setenv("AGENTS_CLAUDE_CMD", "fakeclaude --flag")
    assert cli._claude_cmd("x", None) == ["fakeclaude", "--flag"]


def test_trust_sets_key_and_is_idempotent(env, tmp_path):
    cfgpath = tmp_path / "claude.json"
    cfgpath.write_text(json.dumps({"projects": {}, "other": 1}))
    proj = str(tmp_path / "work")

    assert trust.ensure_trusted(proj) is True          # first time: changed
    d = json.load(open(cfgpath))
    assert d["projects"][os.path.abspath(proj)]["hasTrustDialogAccepted"] is True
    assert d["other"] == 1                              # everything else preserved

    assert trust.ensure_trusted(proj) is False          # already trusted: no-op


def test_trust_missing_config_is_noop(env, tmp_path):
    # no claude.json on disk -> best-effort no-op, never raises
    assert trust.ensure_trusted(str(tmp_path / "work")) is False


def test_configure_noninteractive_writes_file(env):
    from agents import cli
    parser = cli.build_parser()
    args = parser.parse_args(["configure", "--mode", "bypassPermissions",
                              "--host", "foreground", "--no-input"])
    assert args.func(args) == 0
    assert config.load() == {"permission_mode": "bypassPermissions",
                             "host": "foreground"}
