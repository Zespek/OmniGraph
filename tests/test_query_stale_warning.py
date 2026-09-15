"""`omnigraph query`/`path`/`explain` must warn when a tracked file has
uncommitted changes newer than the graph, so an answer is not silently built
from a snapshot that misses in-progress work (reported by a team running the
tool against a real project: a migration touching several files was modified
but not yet committed, and the graph - and the answer built from it - did not
reflect it)."""
import subprocess
from pathlib import Path

import pytest

from omnigraph.cli import _warn_if_graph_stale_vs_git


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo_with_graph(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git("init", "-q", str(repo), cwd=tmp_path)
    _git("config", "user.email", "t@t.co", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)
    out = repo / "omnigraph-out"
    out.mkdir()
    gp = out / "graph.json"
    gp.write_text("{}", encoding="utf-8")
    return repo, gp


def test_no_warning_on_clean_tree(tmp_path, capsys):
    _, gp = _make_repo_with_graph(tmp_path)
    _warn_if_graph_stale_vs_git(gp)
    assert capsys.readouterr().err == ""


def test_warns_when_tracked_file_is_newer_than_graph(tmp_path, capsys):
    repo, gp = _make_repo_with_graph(tmp_path)
    (repo / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    # Force a visible mtime gap - some filesystems have coarse mtime
    # resolution, and the graph and the edit could otherwise land in the
    # same tick. Push the graph BACKWARD so the edit is unambiguously newer.
    import os
    t = gp.stat().st_mtime - 2
    os.utime(gp, (t, t))
    _warn_if_graph_stale_vs_git(gp)
    err = capsys.readouterr().err
    assert "uncommitted" in err
    assert "src/a.py" in err
    assert "omnigraph update" in err


def test_no_warning_for_untracked_file(tmp_path, capsys):
    """An untracked scratch file must not trigger this - it was never part of
    the graph to begin with, so its own freshness says nothing about the
    graph being stale."""
    repo, gp = _make_repo_with_graph(tmp_path)
    (repo / "scratch.txt").write_text("notes\n", encoding="utf-8")
    _warn_if_graph_stale_vs_git(gp)
    assert capsys.readouterr().err == ""


def test_no_warning_when_change_is_older_than_graph(tmp_path, capsys):
    repo, gp = _make_repo_with_graph(tmp_path)
    (repo / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    import os
    t = gp.stat().st_mtime + 10
    os.utime(gp, (t, t))
    _warn_if_graph_stale_vs_git(gp)
    assert capsys.readouterr().err == ""


def test_silent_outside_a_git_repo(tmp_path, capsys):
    out = tmp_path / "omnigraph-out"
    out.mkdir()
    gp = out / "graph.json"
    gp.write_text("{}", encoding="utf-8")
    _warn_if_graph_stale_vs_git(gp)
    assert capsys.readouterr().err == ""


@pytest.mark.skipif(
    __import__("shutil").which("git") is None, reason="git required"
)
def test_warning_lists_up_to_three_files_then_a_count(tmp_path, capsys):
    repo, gp = _make_repo_with_graph(tmp_path)
    for i in range(2, 6):
        (repo / "src" / f"m{i}.py").write_text(f"y = {i}\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "more files", cwd=repo)
    for i in range(2, 6):
        (repo / "src" / f"m{i}.py").write_text(f"y = {i}00\n", encoding="utf-8")
    import os
    t = gp.stat().st_mtime - 2
    os.utime(gp, (t, t))
    _warn_if_graph_stale_vs_git(gp)
    err = capsys.readouterr().err
    assert "and 1 more" in err
