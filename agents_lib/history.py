"""agents — discover past Claude Code sessions to resume or adopt.

~/.claude/history.jsonl records every user input (sessionId, project cwd,
timestamp, display text) and is never pruned, so it stays the full catalog of
sessions even after a transcript is cleaned up. Each session's transcript
(~/.claude/projects/<slug>/<sessionId>.jsonl) adds its AI title, last prompt and
turn count; it is re-parsed only when its mtime:size signature changes, cached
under the agents state dir.
"""

import json
import os
import re
from datetime import datetime, timezone

CACHE_VERSION = 1


def _history_path():
    return os.path.expanduser(os.environ.get("CLAUDE_HISTORY", "~/.claude/history.jsonl"))


def _projects_dir():
    return os.path.expanduser(
        os.environ.get("CLAUDE_PROJECTS_DIR", "~/.claude/projects"))


def _cache_path():
    state = os.environ.get("AGENTS_STATE_DIR", os.path.expanduser("~/.agents"))
    return os.path.join(state, ".sessions-index.json")


def _slug(cwd):
    return re.sub(r"[^A-Za-z0-9]+", "-", cwd)


def transcript_path(cwd, session_id):
    return os.path.join(_projects_dir(), _slug(cwd), f"{session_id}.jsonl")


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M")


def _read_history():
    """sessionId -> dict(cwd, first_prompt, last_prompt, first_ts, last_ts, prompts)."""
    index = {}
    try:
        fh = open(_history_path(), encoding="utf-8")
    except FileNotFoundError:
        return index
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = r.get("sessionId")
            if not sid:
                continue
            ts = r.get("timestamp", 0)
            disp = (r.get("display") or "").strip()
            h = index.get(sid)
            if h is None:
                h = index[sid] = {
                    "cwd": r.get("project", ""), "first_prompt": "",
                    "last_prompt": "", "first_ts": ts, "last_ts": ts, "prompts": [],
                }
            h["cwd"] = r.get("project", "") or h["cwd"]
            h["first_ts"] = min(h["first_ts"], ts) if h["first_ts"] else ts
            h["last_ts"] = max(h["last_ts"], ts)
            if disp:
                h["prompts"].append(disp)
                if not h["first_prompt"]:
                    h["first_prompt"] = disp
                h["last_prompt"] = disp
    return index


def _parse_transcript(path):
    ai_title = last_prompt = ""
    msgs = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = r.get("type")
            if t == "ai-title":
                ai_title = r.get("aiTitle", "") or ai_title
            elif t == "last-prompt":
                last_prompt = r.get("lastPrompt", "") or last_prompt
            elif t in ("user", "assistant"):
                msgs += 1
    return {"ai_title": ai_title, "last_prompt": last_prompt, "msgs": msgs}


def _load_cache():
    try:
        with open(_cache_path(), encoding="utf-8") as fh:
            c = json.load(fh)
        if c.get("v") == CACHE_VERSION:
            return c.get("sessions", {})
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def _save_cache(sessions):
    path = _cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"v": CACHE_VERSION, "sessions": sessions}, fh)
    os.replace(tmp, path)


def _rows():
    hist = _read_history()
    cache = _load_cache()
    fresh = {}
    rows = []
    for sid, h in hist.items():
        path = transcript_path(h["cwd"], sid)
        try:
            st = os.stat(path)
            sig = f"{st.st_mtime_ns}:{st.st_size}"
            present = True
        except FileNotFoundError:
            sig = None
            present = False

        info = None
        if present:
            cached = cache.get(sid)
            info = cached if cached and cached.get("sig") == sig else {
                "sig": sig, **_parse_transcript(path)}
            fresh[sid] = info

        ai_title = (info or {}).get("ai_title") or ""
        rows.append({
            "session_id": sid,
            "cwd": h["cwd"],
            "present": present,
            "path": path if present else "",
            "title": ai_title or h["first_prompt"],
            "recap": h["first_prompt"],
            "last_prompt": (info or {}).get("last_prompt") or h["last_prompt"],
            "msgs": (info or {}).get("msgs") or len(h["prompts"]),
            "started": _iso(h["first_ts"]),
            "ended": _iso(h["last_ts"]),
            "ended_ts": h["last_ts"],
            "_haystack": " ".join(
                [ai_title, h["cwd"], h["first_prompt"], h["last_prompt"]]
                + h["prompts"]).lower(),
        })
    # keep cached entries for sessions whose transcript is now gone
    for sid, cached in cache.items():
        fresh.setdefault(sid, cached)
    _save_cache(fresh)
    return rows


def find_sessions(query=None, include_gone=False, limit=None):
    """Past Claude sessions, newest first. Resumable (transcript present) only
    unless include_gone. `query` is a case-insensitive substring over the title,
    cwd and every prompt."""
    rows = _rows()
    if not include_gone:
        rows = [r for r in rows if r["present"]]
    if query:
        q = query.lower()
        rows = [r for r in rows if q in r["_haystack"]]
    rows.sort(key=lambda r: r["ended_ts"], reverse=True)
    if limit:
        rows = rows[:limit]
    for r in rows:
        r.pop("_haystack", None)
    return rows
