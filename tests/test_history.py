"""Tests for session discovery (agents find) — reads a fake ~/.claude.

Hermetic: CLAUDE_HISTORY, CLAUDE_PROJECTS_DIR and AGENTS_STATE_DIR point at tmp.
"""

import json
import os

import pytest


@pytest.fixture
def claude(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    state = tmp_path / "agents-state"
    state.mkdir()
    history_file = tmp_path / "history.jsonl"
    monkeypatch.setenv("CLAUDE_HISTORY", str(history_file))
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("AGENTS_STATE_DIR", str(state))
    return tmp_path, projects, history_file


def _slug(cwd):
    import re
    return re.sub(r"[^A-Za-z0-9]+", "-", cwd)


def _hist(fh, sid, cwd, display, ts):
    fh.write(json.dumps({"sessionId": sid, "project": cwd,
                         "display": display, "timestamp": ts}) + "\n")


def _transcript(projects, cwd, sid, ai_title, last_prompt, turns):
    d = projects / _slug(cwd)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{sid}.jsonl", "w") as f:
        f.write(json.dumps({"type": "ai-title", "aiTitle": ai_title}) + "\n")
        f.write(json.dumps({"type": "last-prompt", "lastPrompt": last_prompt}) + "\n")
        for _ in range(turns):
            f.write(json.dumps({"type": "user"}) + "\n")
            f.write(json.dumps({"type": "assistant"}) + "\n")


def test_find_lists_present_sessions_newest_first(claude):
    _, projects, hf = claude
    with open(hf, "w") as f:
        _hist(f, "aaaa1111", "/Users/x/alpha", "work on alpha", 1000)
        _hist(f, "bbbb2222", "/Users/x/beta", "ping the pam agent", 2000)
    _transcript(projects, "/Users/x/alpha", "aaaa1111", "Alpha title", "last a", 3)
    _transcript(projects, "/Users/x/beta", "bbbb2222", "Beta title", "last b", 5)

    from agents import history
    rows = history.find_sessions()
    assert [r["session_id"] for r in rows] == ["bbbb2222", "aaaa1111"]  # newest first
    beta = rows[0]
    assert beta["title"] == "Beta title"
    assert beta["last_prompt"] == "last b"
    assert beta["msgs"] == 10  # 5 user + 5 assistant
    assert beta["present"] is True


def test_find_query_matches_prompts_and_title(claude):
    _, projects, hf = claude
    with open(hf, "w") as f:
        _hist(f, "aaaa1111", "/Users/x/alpha", "work on alpha", 1000)
        _hist(f, "bbbb2222", "/Users/x/beta", "ping the pam agent", 2000)
    _transcript(projects, "/Users/x/alpha", "aaaa1111", "Alpha", "a", 1)
    _transcript(projects, "/Users/x/beta", "bbbb2222", "Beta", "b", 1)

    from agents import history
    assert [r["session_id"] for r in history.find_sessions("pam")] == ["bbbb2222"]
    assert history.find_sessions("nomatch") == []


def test_gone_transcript_excluded_unless_all(claude):
    _, projects, hf = claude
    with open(hf, "w") as f:
        _hist(f, "cccc3333", "/Users/x/gamma", "a prompt", 1000)  # no transcript

    from agents import history
    assert history.find_sessions() == []
    gone = history.find_sessions(include_gone=True)
    assert len(gone) == 1
    assert gone[0]["present"] is False
    assert gone[0]["msgs"] == 1  # falls back to history prompt count


def test_cache_reused_until_signature_changes(claude):
    _, projects, hf = claude
    with open(hf, "w") as f:
        _hist(f, "aaaa1111", "/Users/x/alpha", "work on alpha", 1000)
    _transcript(projects, "/Users/x/alpha", "aaaa1111", "First", "a", 1)

    from agents import history
    assert history.find_sessions()[0]["title"] == "First"

    # rewrite transcript (changes mtime:size) -> re-parsed
    _transcript(projects, "/Users/x/alpha", "aaaa1111", "Second title longer", "a", 2)
    assert history.find_sessions()[0]["title"] == "Second title longer"
