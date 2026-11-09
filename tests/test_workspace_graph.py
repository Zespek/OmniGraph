"""A graph kept in a folder above the project (a workspace of sibling repos)."""
from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

from omnigraph.cli import _default_graph_path, _run_hook_guard
from omnigraph.paths import find_out_dir


def _workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "omnigraph-out").mkdir(parents=True)
    (ws / "omnigraph-out" / "graph.json").write_text("{}", encoding="utf-8")
    backend = ws / "backend"
    backend.mkdir()
    sibling = ws / "app" / "screen.py"
    sibling.parent.mkdir()
    sibling.write_text("x = 1\n", encoding="utf-8")
    past = time.time() - 3600
    os.utime(sibling, (past, past))
    monkeypatch.chdir(backend)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("OMNIGRAPH_HOOK_STRICT", raising=False)
    return ws.resolve(), backend, sibling.resolve()


def _run(kind, payload, monkeypatch, capsys) -> str:
    class _Stdin:
        buffer = io.BytesIO(json.dumps(payload).encode("utf-8"))
    monkeypatch.setattr("sys.stdin", _Stdin())
    _run_hook_guard(kind)
    return capsys.readouterr().out


def test_graph_in_a_folder_above_is_found(tmp_path, monkeypatch):
    ws, _, _ = _workspace(tmp_path, monkeypatch)
    assert find_out_dir() == ws / "omnigraph-out"
    assert Path(_default_graph_path()) == ws / "omnigraph-out" / "graph.json"


def test_a_graph_here_wins_over_the_one_above(tmp_path, monkeypatch):
    _, backend, _ = _workspace(tmp_path, monkeypatch)
    (backend / "omnigraph-out").mkdir()
    (backend / "omnigraph-out" / "graph.json").write_text("{}", encoding="utf-8")
    assert find_out_dir() == Path("omnigraph-out")


def test_the_search_stops_before_the_home_folder(tmp_path, monkeypatch):
    ws, _, _ = _workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: ws))
    assert find_out_dir() == Path("omnigraph-out")


def test_hooks_work_inside_the_workspace(tmp_path, monkeypatch, capsys):
    ws, backend, sibling = _workspace(tmp_path, monkeypatch)
    search = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "grep -rn foo ."}}
    assert "omnigraph query" in _run("search", search, monkeypatch, capsys)
    read = {"session_id": "s1", "tool_name": "Read", "tool_input": {"file_path": str(sibling)}}
    assert "opening files" in _run("read", read, monkeypatch, capsys), "a sibling repo is in the graph's project"
    assert not (backend / "omnigraph-out").exists(), "markers go next to the workspace graph"
    assert (ws / "omnigraph-out" / "cache" / "hook_sessions").is_dir()


def test_a_query_against_the_workspace_graph_silences_nudges(tmp_path, monkeypatch, capsys):
    ws, _, _ = _workspace(tmp_path, monkeypatch)
    stamp = ws / "omnigraph-out" / "cache" / "last_query_stamp"
    stamp.parent.mkdir(parents=True)
    stamp.write_text(str(time.time()), encoding="utf-8")
    search = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "grep -rn foo ."}}
    assert _run("search", search, monkeypatch, capsys) == ""


def _omnigraph(args, cwd):
    import subprocess
    import sys
    env = dict(os.environ)
    env.pop("OMNIGRAPH_OUT", None)
    return subprocess.run([sys.executable, "-m", "omnigraph", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=120)


def test_update_inside_a_workspace_refreshes_the_workspace_graph(tmp_path):
    ws = tmp_path / "ws"
    (ws / "backend").mkdir(parents=True)
    (ws / "app.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    assert _omnigraph(["update", "."], ws).returncode == 0
    (ws / "backend" / "auth.py").write_text("def restore_session():\n    return 2\n", encoding="utf-8")
    result = _omnigraph(["update", "."], ws / "backend")
    assert result.returncode == 0, result.stderr
    assert "updating the workspace graph" in result.stderr
    assert not (ws / "backend" / "omnigraph-out").exists()
    labels = {n.get("label") for n in json.loads((ws / "omnigraph-out" / "graph.json").read_text(encoding="utf-8"))["nodes"]}
    assert "restore_session()" in labels


def test_git_hooks_in_a_repo_under_a_workspace_graph_add_no_gitattributes(tmp_path, monkeypatch):
    import subprocess
    from omnigraph.hooks import install
    ws, backend, _ = _workspace(tmp_path, monkeypatch)
    subprocess.run(["git", "init", "-q", str(backend)], check=True)
    result = install(backend)
    assert "merge driver: skipped" in result
    assert not (backend / ".gitattributes").exists()
