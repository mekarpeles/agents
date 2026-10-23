"""Hermetic test helpers for agents — isolated state dir + fake Claude session.

Never touches the real ~/.agents or ~/.claude (mirrors cmux's test discipline).
"""

import hashlib
import json
import os
import socket
import threading
import time


def wait_for(cond, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


def write_session(sessions_dir, pid, sock_path, session_id, name,
                  status="idle", peer_token="deadbeefdeadbeefdeadbeefdeadbeef"):
    os.makedirs(sessions_dir, exist_ok=True)
    rec = {
        "pid": pid, "sessionId": session_id, "cwd": "/tmp/x",
        "messagingSocketPath": sock_path, "name": name, "status": status,
        "peerProtocol": 1, "peerFeatures": ["notify_idle"],
        "updatedAt": int(time.time() * 1000),
    }
    with open(os.path.join(sessions_dir, f"{pid}.json"), "w") as f:
        json.dump(rec, f)
    h = hashlib.sha256(sock_path.encode()).hexdigest()
    with open(os.path.join(sessions_dir, f"{pid}.{h}.key"), "w") as f:
        json.dump({"peerToken": peer_token}, f)
    return rec


class FakeSession:
    """Fake messaging socket server; records received NDJSON lines."""

    def __init__(self, sock_path):
        self.sock_path = sock_path
        self.lines = []
        os.makedirs(os.path.dirname(sock_path), exist_ok=True)
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(sock_path)
        self._srv.listen(5)
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._stop:
            try:
                self._srv.settimeout(0.2)
                conn, _ = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            buf = b""
            conn.settimeout(1.0)
            try:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            except (socket.timeout, OSError):
                pass
            for ln in buf.split(b"\n"):
                if ln.strip():
                    self.lines.append(json.loads(ln))
            conn.close()

    def close(self):
        self._stop = True
        try:
            self._srv.close()
        except OSError:
            pass
