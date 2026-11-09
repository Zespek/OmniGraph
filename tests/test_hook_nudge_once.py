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


def _sub(agent="agent-7", session="s1"):
    return dict(_search(session), agent_id=agent)


def test_each_subagent_gets_its_own_nudge(tmp_path, monkeypatch, capsys):
    _graph(tmp_path, monkeypatch)
    assert _run("search", _search(), monkeypatch, capsys)
    assert _run("search", _sub(), monkeypatch, capsys)
    assert _run("search", _sub(), monkeypatch, capsys) == ""
    assert _run("search", _sub("agent-8"), monkeypatch, capsys)


def test_subagent_is_nudged_even_right_after_a_query(tmp_path, monkeypatch, capsys):
    _graph(tmp_path, monkeypatch)
    stamp = tmp_path / "omnigraph-out" / "cache" / "last_query_stamp"
    stamp.parent.mkdir(parents=True)
    stamp.write_text(str(time.time()), encoding="utf-8")
    assert _run("search", _search(), monkeypatch, capsys) == ""
    assert _run("search", _sub(), monkeypatch, capsys)


def test_compaction_gives_the_session_its_budget_back(tmp_path, monkeypatch, capsys):
    _graph(tmp_path, monkeypatch)
    assert _run("search", _search(), monkeypatch, capsys)
    assert _run("search", _sub(), monkeypatch, capsys)
    assert _run("search", _search("s2"), monkeypatch, capsys)
    compact = {"session_id": "s1", "hook_event_name": "SessionStart", "source": "compact"}
    assert _run("compact", compact, monkeypatch, capsys) == ""
    assert _run("search", _search(), monkeypatch, capsys)
    assert _run("search", _sub(), monkeypatch, capsys)
    assert _run("search", _search("s2"), monkeypatch, capsys) == ""


def test_other_session_starts_do_not_reset(tmp_path, monkeypatch, capsys):
    _graph(tmp_path, monkeypatch)
    assert _run("search", _search(), monkeypatch, capsys)
    for source in ("startup", "resume", "clear"):
        _run("compact", {"session_id": "s1", "source": source}, monkeypatch, capsys)
    assert _run("search", _search(), monkeypatch, capsys) == ""


def test_install_registers_and_uninstall_removes_the_compaction_hook(tmp_path):
    from omnigraph.install import claude_install, claude_uninstall
    claude_install(tmp_path, local=True)
    claude_install(tmp_path, local=True)
    settings_path = tmp_path / ".claude" / "settings.local.json"
    starts = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    assert len(starts) == 1 and starts[0]["matcher"] == "compact"
    assert starts[0]["hooks"][0]["command"].endswith("hook-guard compact")
    claude_uninstall(tmp_path)
    assert "hook-guard" not in settings_path.read_text(encoding="utf-8")
