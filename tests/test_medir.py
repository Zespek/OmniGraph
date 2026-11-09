"""`omnigraph medir` compares code-finding tokens before and after setup."""
from __future__ import annotations

import datetime as dt
import json
import time

from omnigraph.medir import _classify, main


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _session(folder, sid, cwd, when, calls):
    lines = []
    for i, (name, tool_input, size) in enumerate(calls):
        tid = f"{sid}-{i}"
        base = {"sessionId": sid, "cwd": cwd, "timestamp": _iso(when)}
        lines.append({**base, "type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": name, "input": tool_input}]}})
        lines.append({**base, "type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": "x" * size}]}})
    (folder / f"{sid}.jsonl").write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")


def _setup(tmp_path, monkeypatch):
    project = (tmp_path / "proj").resolve()
    project.mkdir()
    config = tmp_path / "cfg"
    folder = config / "projects" / "proj"
    folder.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.chdir(project)
    return project, folder


def test_classify_matches_what_the_hooks_watch():
    assert _classify("Grep", {"pattern": "x"}) == "busca"
    assert _classify("Read", {"file_path": "a.py"}) == "busca"
    assert _classify("Bash", {"command": "cat src/a.py"}) == "busca"
    assert _classify("Bash", {"command": "grep -rn foo ."}) == "busca"
    assert _classify("Bash", {"command": 'omnigraph query "restore session"'}) == "mapa"
    assert _classify("mcp__omnigraph__query_graph", {}) == "mapa"
    assert _classify("Bash", {"command": "npm test"}) == "outros"
    assert _classify("Edit", {"file_path": "a.py"}) == "outros"
    assert _classify("mcp__serena__find_symbol", {"name_path": "restore_session"}) == "busca"
    assert _classify("mcp__serena__search_for_pattern", {}) == "busca"
    assert _classify("mcp__serena__replace_symbol_body", {}) == "outros"


def test_compares_sessions_before_and_after(tmp_path, monkeypatch, capsys):
    project, folder = _setup(tmp_path, monkeypatch)
    now = time.time()
    for i in range(3):
        _session(folder, f"old{i}", str(project), now - 30 * 86400, [
            ("Grep", {"pattern": "x"}, 4000),
            ("Read", {"file_path": "a.py"}, 8000),
            ("Bash", {"command": "npm test"}, 4000),
        ])
        _session(folder, f"new{i}", str(project), now - 86400, [
            ("Bash", {"command": 'omnigraph query "x"'}, 2000),
            ("Read", {"file_path": "a.py"}, 2000),
            ("Bash", {"command": "npm test"}, 4000),
        ])
    _session(folder, "elsewhere", str(tmp_path / "other"), now - 86400, [("Grep", {}, 99999)])
    since = dt.datetime.fromtimestamp(now - 10 * 86400).strftime("%Y-%m-%d")
    assert main(["--desde", since]) == 0
    out = capsys.readouterr().out
    assert "caiu 67%" in out
    assert "sessões que usaram o mapa" in out
    assert "x" * 50 not in out


def test_too_few_sessions_says_so(tmp_path, monkeypatch, capsys):
    project, folder = _setup(tmp_path, monkeypatch)
    now = time.time()
    _session(folder, "old", str(project), now - 30 * 86400, [("Grep", {}, 400)])
    _session(folder, "new", str(project), now - 86400, [("Grep", {}, 400)])
    since = dt.datetime.fromtimestamp(now - 10 * 86400).strftime("%Y-%m-%d")
    assert main(["--desde", since]) == 0
    assert "Poucas sessões" in capsys.readouterr().out


def test_setup_date_comes_from_the_local_rules_file(tmp_path, monkeypatch, capsys):
    project, folder = _setup(tmp_path, monkeypatch)
    _session(folder, "s", str(project), time.time() - 86400, [("Grep", {}, 400)])
    assert main([]) == 1
    assert "--desde" in capsys.readouterr().err
    (project / "CLAUDE.local.md").write_text("## omnigraph\n", encoding="utf-8")
    assert main([]) == 0
    assert "antes" in capsys.readouterr().out


def test_setup_date_is_found_in_a_workspace_folder_above(tmp_path, monkeypatch, capsys):
    project, folder = _setup(tmp_path, monkeypatch)
    _session(folder, "s", str(project), time.time() - 86400, [("Grep", {}, 400)])
    (project.parent / "CLAUDE.local.md").write_text("## omnigraph\n", encoding="utf-8")
    assert main([]) == 0
    assert "antes" in capsys.readouterr().out
