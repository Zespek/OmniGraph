"""Soft hook nudges stay in the conversation, so they must not repeat on every call."""
from __future__ import annotations

import io
import json
import time

from omnigraph.cli import _run_hook_guard


def _graph(tmp_path, monkeypatch):
    out = tmp_path / "omnigraph-out"
    out.mkdir()
    (out / "graph.json").write_text("{}", encoding="utf-8")
    src = tmp_path / "mod.py"
    src.write_text("x = 1\n", encoding="utf-8")
    past = time.time() - 3600
    import os
    os.utime(src, (past, past))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("OMNIGRAPH_HOOK_STRICT", raising=False)
    return src


def _run(kind, payload, monkeypatch, capsys) -> str:
    class _Stdin:
        buffer = io.BytesIO(json.dumps(payload).encode("utf-8"))
    monkeypatch.setattr("sys.stdin", _Stdin())
    _run_hook_guard(kind)
    return capsys.readouterr().out


def _search(session="s1"):
    payload = {"tool_name": "Bash", "tool_input": {"command": "grep -rn foo ."}}
    if session:
        payload["session_id"] = session
    return payload


def test_search_nudge_fires_once_per_session(tmp_path, monkeypatch, capsys):
    _graph(tmp_path, monkeypatch)
    assert "omnigraph query" in _run("search", _search(), monkeypatch, capsys)
    assert _run("search", _search(), monkeypatch, capsys) == ""


def test_new_session_is_nudged_again(tmp_path, monkeypatch, capsys):
    _graph(tmp_path, monkeypatch)
    assert _run("search", _search("s1"), monkeypatch, capsys)
    assert _run("search", _search("s2"), monkeypatch, capsys)


def test_read_and_search_nudges_are_independent(tmp_path, monkeypatch, capsys):
    src = _graph(tmp_path, monkeypatch)
    read = {"session_id": "s1", "tool_name": "Read", "tool_input": {"file_path": str(src)}}
    assert _run("search", _search(), monkeypatch, capsys)
    assert _run("read", read, monkeypatch, capsys)
    assert _run("read", read, monkeypatch, capsys) == ""


def test_recent_query_silences_nudges(tmp_path, monkeypatch, capsys):
    _graph(tmp_path, monkeypatch)
    stamp = tmp_path / "omnigraph-out" / "cache" / "last_query_stamp"
    stamp.parent.mkdir(parents=True)
    stamp.write_text(str(time.time()), encoding="utf-8")
    assert _run("search", _search(), monkeypatch, capsys) == ""


def test_payload_without_session_keeps_nudging(tmp_path, monkeypatch, capsys):
    _graph(tmp_path, monkeypatch)
    assert _run("search", _search(session=None), monkeypatch, capsys)
    assert _run("search", _search(session=None), monkeypatch, capsys)


def test_gemini_nudge_stops_after_a_recent_query(tmp_path, monkeypatch, capsys):
    _graph(tmp_path, monkeypatch)
    _run_hook_guard("gemini")
    assert "additionalContext" in json.loads(capsys.readouterr().out)
    stamp = tmp_path / "omnigraph-out" / "cache" / "last_query_stamp"
    stamp.parent.mkdir(parents=True)
    stamp.write_text(str(time.time()), encoding="utf-8")
    _run_hook_guard("gemini")
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"decision": "allow"}
