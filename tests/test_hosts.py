"""Tests for the pty hosts — naming, capabilities, and a real tmux round-trip.

The tmux test runs against an isolated tmux server (TMUX_TMPDIR) and never
touches the user's own server or any real agent.
"""

import shutil
import subprocess

import pytest

from agents import hosts


def test_registry_and_default():
    assert hosts.DEFAULT == "tmux"
    assert set(hosts.CHOICES) == {"tmux", "herdr", "foreground"}
    assert isinstance(hosts.get("tmux"), hosts.Tmux)


def test_tmux_names():
    t = hosts.Tmux()
    assert t._session("pam", None) == "agents-pam"      # standalone
    assert t._session("pam", "web") == "web"            # grouped
    assert t._target("pam", "web") == "web:pam"


def test_foreground_cannot_group():
    with pytest.raises(hosts.Unsupported):
        hosts.Foreground().start("pam", ["true"], "/tmp", {}, group="web")


def test_foreground_has_no_attach():
    with pytest.raises(hosts.Unsupported):
        hosts.Foreground().attach("pam")


def test_herdr_requires_herdr(monkeypatch):
    monkeypatch.delenv("HERDR_ENV", raising=False)
    with pytest.raises(hosts.Unsupported):
        hosts.Herdr().start("pam", ["true"], "/tmp", {})


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_tmux_start_creates_window(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))  # isolated tmux server
    t = hosts.Tmux()
    try:
        launched = t.start("t1", ["sleep", "30"], str(tmp_path), {}, group="grp")
        assert launched.pid and launched.proc is None
        # the pane's pid is the sleep (exec preserves the pid) and is alive
        import os
        os.kill(launched.pid, 0)
        # a second agent joins the same group as another window
        t.start("t2", ["sleep", "30"], str(tmp_path), {}, group="grp")
        windows = subprocess.run(
            ["tmux", "list-windows", "-t", "grp", "-F", "#{window_name}"],
            capture_output=True, text=True).stdout.split()
        assert {"t1", "t2"} <= set(windows)
        t.detach("t1", group="grp")  # no client attached -> no-op, must not raise
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_tmux_has_window_checks_window_and_locate_pid(tmp_path, monkeypatch):
    # has_window must check the specific WINDOW, not just the session: a team
    # session can exist holding *other* agents' windows (the modsec-daily bug,
    # where attach found session 'ada' and leaked "can't find window"). locate_pid
    # finds an agent's real window by pid regardless of session naming.
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    t = hosts.Tmux()
    try:
        launched = t.start("a1", ["sleep", "30"], str(tmp_path), {})        # agents-a1
        t.start("other", ["sleep", "30"], str(tmp_path), {}, group="team")  # session 'team'
        assert t.has_window("a1", group=None) is True        # a1's own session
        assert t.has_window("a1", group="team") is False     # 'team' exists, no a1 window
        assert t.has_window(None, group="team") is True      # whole-session check
        assert t.locate_pid(launched.pid) == "agents-a1:0"   # real window, by pid
        assert t.locate_pid(2 ** 31) is None
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_tmux_attach_missing_session_is_clear(tmp_path, monkeypatch):
    # No tmux window for this agent -> a clear error, never execvp into tmux's
    # raw "can't find session" leak.
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="isn't running under tmux"):
        hosts.Tmux().attach("ghost")
