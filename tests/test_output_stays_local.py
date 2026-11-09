"""The generated graph and personal Claude setup must stay out of commits."""
from __future__ import annotations

import json
import subprocess

import networkx as nx
import pytest

from omnigraph.export import to_json
from omnigraph.hooks import install as hook_install
from omnigraph.install import claude_install, claude_uninstall
from omnigraph.paths import ensure_output_git_ignored


def _git(repo, *args) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNIGRAPH_TRACK_OUTPUT", raising=False)
    _git(tmp_path, "init", "-q")
    return tmp_path


def _write_graph(directory):
    out = directory / "omnigraph-out"
    out.mkdir(parents=True, exist_ok=True)
    G = nx.Graph()
    G.add_node("a", label="alpha", source_file="a.py")
    assert to_json(G, {0: ["a"]}, str(out / "graph.json"))
    return out


def test_building_the_graph_adds_it_to_gitignore(repo):
    _write_graph(repo)
    assert (repo / ".gitignore").read_text(encoding="utf-8") == "omnigraph-out/\n"
    assert _git(repo, "status", "--porcelain").split() == ["??", ".gitignore"]


def test_rebuild_does_not_duplicate_the_entry(repo):
    _write_graph(repo)
    _write_graph(repo)
    assert (repo / ".gitignore").read_text(encoding="utf-8").count("omnigraph-out/") == 1


def test_temporary_graph_file_in_output_dir_also_counts(repo):
    # `omnigraph update` and the commit hook write to a temp name first.
    out = repo / "omnigraph-out"
    out.mkdir()
    G = nx.Graph()
    G.add_node("a", label="alpha", source_file="a.py")
    assert to_json(G, {0: ["a"]}, str(out / ".graph.tmp.json"), force=True)
    assert "omnigraph-out/" in (repo / ".gitignore").read_text(encoding="utf-8")


def test_existing_gitignore_is_preserved(repo):
    (repo / ".gitignore").write_bytes(b"node_modules")
    _write_graph(repo)
    assert (repo / ".gitignore").read_text(encoding="utf-8") == "node_modules\nomnigraph-out/\n"


def test_existing_rule_is_respected(repo):
    (repo / ".gitignore").write_text("omnigraph-*/\n", encoding="utf-8")
    _write_graph(repo)
    assert (repo / ".gitignore").read_text(encoding="utf-8") == "omnigraph-*/\n"


def test_build_from_a_monorepo_subfolder_writes_the_root_gitignore(repo):
    app = repo / "apps" / "web"
    app.mkdir(parents=True)
    _write_graph(app)
    assert (repo / ".gitignore").read_text(encoding="utf-8") == "omnigraph-out/\n"
    assert not (app / ".gitignore").exists()


def test_outside_a_repository_nothing_is_created(tmp_path):
    _write_graph(tmp_path)
    assert not (tmp_path / ".gitignore").exists()


def test_other_json_output_paths_are_left_alone(repo):
    G = nx.Graph()
    G.add_node("a", label="alpha", source_file="a.py")
    assert to_json(G, {0: ["a"]}, str(repo / "export.json"))
    assert not (repo / ".gitignore").exists()


def test_already_tracked_graph_gets_the_untrack_command(repo, capsys):
    out = _write_graph(repo)
    (repo / ".gitignore").unlink()
    _git(repo, "add", "-f", str(out / "graph.json"))
    _git(repo, "commit", "-qm", "track graph")
    capsys.readouterr()
    ensure_output_git_ignored(out / "graph.json")
    err = capsys.readouterr().err
    assert "git rm -r --cached omnigraph-out" in err
    assert (repo / ".gitignore").read_text(encoding="utf-8") == "omnigraph-out/\n"
    assert "omnigraph-out/graph.json" in _git(repo, "ls-files")


def test_opt_out_keeps_the_graph_tracked(repo, monkeypatch, capsys):
    monkeypatch.setenv("OMNIGRAPH_TRACK_OUTPUT", "1")
    _write_graph(repo)
    assert not (repo / ".gitignore").exists()
    assert capsys.readouterr().err == ""


def test_hook_install_skips_gitattributes_for_ignored_graph(repo):
    _write_graph(repo)
    result = hook_install(repo)
    assert "merge driver: skipped" in result
    assert not (repo / ".gitattributes").exists()


def test_local_claude_install_touches_no_shared_file(repo):
    claude_install(repo, local=True)
    assert (repo / "CLAUDE.local.md").exists()
    assert not (repo / "CLAUDE.md").exists()
    assert not (repo / ".claude" / "settings.json").exists()
    settings = json.loads((repo / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert len(settings["hooks"]["PreToolUse"]) == 3
    assert _git(repo, "status", "--porcelain").split() == ["??", ".gitignore"]


def test_local_claude_install_can_be_uninstalled(repo):
    claude_install(repo, local=True)
    claude_uninstall(repo)
    assert "omnigraph" not in (repo / ".claude" / "settings.local.json").read_text(encoding="utf-8")
    local_md = repo / "CLAUDE.local.md"
    assert not local_md.exists() or "omnigraph" not in local_md.read_text(encoding="utf-8")


def test_default_claude_install_is_unchanged(repo):
    claude_install(repo)
    assert (repo / "CLAUDE.md").exists()
    assert (repo / ".claude" / "settings.json").exists()
    assert not (repo / ".gitignore").exists()


def test_local_cursor_install_is_ignored(repo):
    from omnigraph.install import _cursor_install
    _cursor_install(repo, local=True)
    assert (repo / ".cursor" / "rules" / "omnigraph.mdc").exists()
    assert ".cursor/rules/omnigraph.mdc" in (repo / ".gitignore").read_text(encoding="utf-8")
    assert _git(repo, "status", "--porcelain").split() == ["??", ".gitignore"]


def test_local_vscode_install_leaves_team_instructions_alone(repo, monkeypatch, tmp_path_factory):
    from omnigraph.install import vscode_install, vscode_uninstall
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    team = repo / ".github" / "copilot-instructions.md"
    team.parent.mkdir()
    team.write_text("Team rules.\n", encoding="utf-8")
    _git(repo, "add", ".github")
    _git(repo, "commit", "-qm", "team instructions")
    vscode_install(repo, local=True)
    own = repo / ".github" / "instructions" / "omnigraph.instructions.md"
    assert own.read_text(encoding="utf-8").startswith('---\napplyTo: "**"\n---\n\n## omnigraph')
    assert team.read_text(encoding="utf-8") == "Team rules.\n"
    assert _git(repo, "status", "--porcelain").split() == ["??", ".gitignore"]
    vscode_uninstall(repo)
    assert not own.exists()
    assert team.read_text(encoding="utf-8") == "Team rules.\n"


def test_committed_personal_file_gets_the_untrack_command(repo, capsys):
    from omnigraph.install import _cursor_install
    _cursor_install(repo)
    _git(repo, "add", ".cursor")
    _git(repo, "commit", "-qm", "teammate committed the rule")
    capsys.readouterr()
    _cursor_install(repo, local=True)
    assert "git rm --cached .cursor/rules/omnigraph.mdc" in capsys.readouterr().err


def test_graph_opt_out_does_not_block_local_config(repo, monkeypatch):
    monkeypatch.setenv("OMNIGRAPH_TRACK_OUTPUT", "1")
    claude_install(repo, local=True)
    assert "CLAUDE.local.md" in (repo / ".gitignore").read_text(encoding="utf-8")


def test_local_antigravity_install_is_ignored(repo, monkeypatch):
    from omnigraph.install import _antigravity_install
    monkeypatch.setattr("pathlib.Path.home", lambda: repo.parent / (repo.name + "-home"))
    _antigravity_install(repo, local=True)
    assert (repo / ".agents" / "rules" / "omnigraph.md").exists()
    assert (repo / ".agents" / "workflows" / "omnigraph.md").exists()
    ignored = (repo / ".gitignore").read_text(encoding="utf-8")
    assert ".agents/rules/omnigraph.md" in ignored
    assert ".agents/workflows/omnigraph.md" in ignored
    assert _git(repo, "status", "--porcelain").split() == ["??", ".gitignore"]


def test_local_install_takes_omnigraph_out_of_the_shared_files(repo, capsys):
    (repo / "CLAUDE.md").write_text("# Team\n\nKeep tests green.\n\n## omnigraph\n\nold rules\n", encoding="utf-8")
    shared = repo / ".claude" / "settings.json"
    shared.parent.mkdir()
    shared.write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls)"]},
        "hooks": {"PreToolUse": [{"matcher": "Bash|Grep", "hooks": [
            {"type": "command", "command": "/Users/someone/.local/bin/omnigraph hook-guard search"}]}]},
    }), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "old shared setup")
    capsys.readouterr()
    claude_install(repo, local=True)
    md = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Keep tests green." in md and "## omnigraph" not in md
    settings = json.loads(shared.read_text(encoding="utf-8"))
    assert "omnigraph" not in json.dumps(settings.get("hooks", {}))
    assert settings["permissions"]["allow"] == ["Bash(ls)"]
    assert "Commit this change once" in capsys.readouterr().err
    changed = _git(repo, "status", "--porcelain")
    assert " M CLAUDE.md" in changed and " M .claude/settings.json" in changed


def test_local_install_leaves_files_without_omnigraph_untouched(repo, capsys):
    (repo / "CLAUDE.md").write_text("# Team\n", encoding="utf-8")
    claude_install(repo, local=True)
    assert (repo / "CLAUDE.md").read_text(encoding="utf-8") == "# Team\n"
    assert "Commit this change once" not in capsys.readouterr().err
