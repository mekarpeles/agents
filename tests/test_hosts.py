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
