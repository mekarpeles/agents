"""agents — pty hosts: where an agent's `claude` process runs.

Messaging is native (agents.transport writes to the session's own socket), so a
host has nothing to do with delivery. Its job is to keep an interactive `claude`
alive in a pty and — for the multiplexers — let you attach to it and gather a
group of agents in one place. Three hosts:

    tmux        default; detached. A group is a tmux session, each agent a window.
                Supports attach and detach.
    herdr       detached herdr tab (experimental; needs a running herdr session).
    foreground  claude attached to the current terminal; no group, no attach.

`start` launches claude and returns its pid so the caller can bind the sessionId;
everything after that is host-independent.
"""

import json
import os
import shlex
import shutil
import subprocess
import time
from typing import NamedTuple, Optional

DEFAULT = "tmux"


class Unsupported(RuntimeError):
    """The selected host can't do this (e.g. attach a foreground agent)."""


class Launched(NamedTuple):
    pid: Optional[int]                        # claude's pid, for sessionId binding
    proc: Optional[subprocess.Popen] = None   # set only by inline hosts (foreground)


class Host:
    name = ""
    group = False  # can it gather agents into one group?

    def start(self, agent, cmd, cwd, env, group=None) -> Launched:
        raise NotImplementedError

    def attach(self, agent, group=None):
        raise Unsupported(f"{self.name}: attach not supported")

    def detach(self, agent, group=None):
        raise Unsupported(f"{self.name}: detach not supported")


class Foreground(Host):
    name = "foreground"

    def start(self, agent, cmd, cwd, env, group=None):
        if group:
            raise Unsupported("foreground can't group agents — use --host tmux")
        proc = subprocess.Popen(cmd, cwd=cwd, env=env)
        return Launched(proc.pid, proc)


class Tmux(Host):
    name = "tmux"
    group = True

    @staticmethod
    def _session(agent, group):
        return group or f"agents-{agent}"

    @classmethod
    def _target(cls, agent, group):
        return f"{cls._session(agent, group)}:{agent}"

    def start(self, agent, cmd, cwd, env, group=None):
        if not shutil.which("tmux"):
            raise Unsupported("tmux not found — install tmux or use --host foreground")
        sess = self._session(agent, group)
        # `exec` so the pane's pid *is* claude's (exec preserves the pid): the
        # caller binds the sessionId straight from ~/.claude/sessions/<pid>.json.
        run = f"exec {shlex.join(cmd)}"
        eopt = ["-e", f"AGENTS_AGENT_NAME={env['AGENTS_AGENT_NAME']}"] \
            if env.get("AGENTS_AGENT_NAME") else []
        has = subprocess.run(["tmux", "has-session", "-t", sess],
                             capture_output=True).returncode == 0
        verb = ["new-window", "-t", f"{sess}:"] if has else \
               ["new-session", "-d", "-s", sess]
        subprocess.run(["tmux", *verb, "-n", agent, "-c", cwd, *eopt, run],
                       check=True)
        time.sleep(0.3)
        pid = self._pane_pid(sess, agent)
        if pid is None:
            out = subprocess.run(
                ["tmux", "capture-pane", "-t", self._target(agent, group), "-p"],
                capture_output=True, text=True).stdout.strip()
            raise RuntimeError(f"claude exited immediately in tmux "
                               f"{self._target(agent, group)}"
                               + (f":\n{out}" if out else ""))
        return Launched(pid, None)

    def _pane_pid(self, sess, agent):
        r = subprocess.run(["tmux", "list-panes", "-t", f"{sess}:{agent}",
                            "-F", "#{pane_pid}"], capture_output=True, text=True)
        out = r.stdout.split()
        return int(out[0]) if out else None

    def attach(self, agent, group=None):
        target = group if agent is None else self._target(agent, group)
        os.execvp("tmux", ["tmux", "attach", "-t", target])

    def detach(self, agent, group=None):
        sess = group if agent is None else self._session(agent, group)
        subprocess.run(["tmux", "detach-client", "-s", sess], capture_output=True)


class Herdr(Host):
    name = "herdr"
    group = True  # a herdr workspace groups tabs

    def _require(self):
        if not shutil.which("herdr") or os.environ.get("HERDR_ENV") != "1":
            raise Unsupported("herdr requested but not running inside herdr")

    def _workspace(self, group):
        return group or os.environ.get("HERDR_WORKSPACE_ID")

    def start(self, agent, cmd, cwd, env, group=None):
        self._require()
        r = subprocess.run(
            ["herdr", "tab", "create", "--workspace", self._workspace(group),
             "--cwd", cwd, "--label", agent, "--no-focus"],
            capture_output=True, text=True)
        pane = _json_get(r.stdout, ("result", "root_pane", "pane_id"))
        if not pane:
            raise RuntimeError(f"herdr tab create failed: {r.stdout}{r.stderr}")
        subprocess.run(["herdr", "pane", "run", pane, shlex.join(cmd)],
                       capture_output=True, text=True)
        # Pane pid is the shell; claude is its child, so sessionId binding by pid
        # is best-effort (a resume already knows its id and needs no binding).
        g = subprocess.run(["herdr", "pane", "get", pane],
                           capture_output=True, text=True)
        return Launched(_json_get(g.stdout, ("result", "pane", "pid")), None)

    def attach(self, agent, group=None):
        self._require()
        tab = self._tab_id(agent, group)
        if tab is None:
            raise RuntimeError(f"herdr: no tab labelled '{agent}'")
        subprocess.run(["herdr", "tab", "focus", tab], capture_output=True)

    def _tab_id(self, label, group):
        r = subprocess.run(
            ["herdr", "tab", "list", "--workspace", self._workspace(group)],
            capture_output=True, text=True)
        try:
            tabs = json.loads(r.stdout).get("result", {}).get("tabs", [])
        except (json.JSONDecodeError, AttributeError):
            return None
        for t in tabs:
            if t.get("label") == label:
                return t.get("tab_id") or t.get("id")
        return None

    # detach: inherited (Unsupported) — herdr tabs persist; switch away in herdr.


def _json_get(text, path):
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    for k in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


_HOSTS = {h.name: h for h in (Tmux(), Herdr(), Foreground())}
CHOICES = list(_HOSTS)


def get(name):
    return _HOSTS[name]
