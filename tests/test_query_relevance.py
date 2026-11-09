"""`query` must answer with relevant nodes instead of filling the token budget."""
from __future__ import annotations

import networkx as nx

from omnigraph.serve import _query_graph_text, _term_stem


def _node_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("NODE ")]


def _noisy_graph() -> nx.Graph:
    """A hub whose traversal reaches far more unrelated nodes than the budget needs."""
    G = nx.Graph()
    G.add_node("target", label="_install_claude_hook()", source_file="pkg/install.py",
               source_location="L10", file_type="code")
    G.add_node("doc", label="Write the settings entry for the editor", source_file="pkg/install.py",
               source_location="L11", file_type="rationale")
    G.add_edge("doc", "target", relation="rationale_for", confidence="EXTRACTED")
    G.add_node("helper", label="test_install_claude_hook()", source_file="tests/test_install.py",
               source_location="L5", file_type="code")
    G.add_edge("helper", "target", relation="calls", confidence="EXTRACTED")
    G.add_node("hub", label="install.py", source_file="pkg/install.py", source_location="L1", file_type="code")
    G.add_edge("hub", "target", relation="contains", confidence="EXTRACTED")
    for i in range(30):
        mid = f"mid{i}"
        G.add_node(mid, label=f"neighbor_{i}()", source_file=f"pkg/mid{i}.py",
                   source_location="L1", file_type="code")
        G.add_edge("target", mid, relation="calls", confidence="EXTRACTED")
        for j in range(5):
            far = f"far{i}_{j}"
            G.add_node(far, label=f"unrelated_{i}_{j}()", source_file=f"pkg/far{i}.py",
                       source_location="L1", file_type="code")
            G.add_edge(mid, far, relation="calls", confidence="EXTRACTED")
    return G


def test_plural_query_term_finds_singular_identifier():
    text = _query_graph_text(_noisy_graph(), "install claude hooks")
    assert "NODE _install_claude_hook()" in _node_lines(text)[0]


def test_budget_is_a_ceiling_not_a_target():
    text = _query_graph_text(_noisy_graph(), "install claude hooks", token_budget=2000)
    assert len(_node_lines(text)) <= 12
    assert "TRUNCATED" not in text
    assert "unrelated_" not in text


def test_test_nodes_rank_below_production_code():
    lines = _node_lines(_query_graph_text(_noisy_graph(), "install claude hooks"))
    assert lines[0].startswith("NODE _install_claude_hook()")
    assert any("test_install_claude_hook" in line for line in lines[1:])


def test_asking_about_tests_ranks_them_first():
    lines = _node_lines(_query_graph_text(_noisy_graph(), "test install claude hook"))
    assert lines[0].startswith("NODE test_install_claude_hook()")


def test_docstring_hit_is_credited_to_its_symbol():
    text = _query_graph_text(_noisy_graph(), "write settings entry editor")
    lines = _node_lines(text)
    assert lines[0].startswith("NODE _install_claude_hook()")
    assert not any("Write the settings entry" in line for line in lines)


def test_node_line_is_compact_file_and_line():
    line = _node_lines(_query_graph_text(_noisy_graph(), "install claude hooks"))[0]
    assert "[pkg/install.py:L10]" in line
    assert "community=" not in line


def test_small_traversal_is_not_pruned():
    G = nx.Graph()
    G.add_node("a", label="alpha()", source_file="a.py", source_location="L1")
    G.add_node("b", label="beta()", source_file="b.py", source_location="L1")
    G.add_node("c", label="gamma()", source_file="c.py", source_location="L1")
    G.add_edge("a", "b", relation="calls", confidence="EXTRACTED")
    G.add_edge("b", "c", relation="calls", confidence="EXTRACTED")
    text = _query_graph_text(G, "alpha", depth=2)
    assert {"NODE alpha()", "NODE beta()", "NODE gamma()"} <= {
        line.split(" [")[0] for line in _node_lines(text)
    }


def test_term_stem_keeps_words_that_only_look_plural():
    assert _term_stem("hooks") == "hook"
    assert _term_stem("queries") == "query"
    assert _term_stem("clustering") == "cluster"
    assert _term_stem("truncated") == "truncat"
    assert _term_stem("status") == "status"
    assert _term_stem("analysis") == "analysis"
    assert _term_stem("class") == "class"


def test_portuguese_filler_words_are_not_search_terms():
    from omnigraph.serve import _query_terms
    assert _query_terms("onde fica o login?") == ["login"]
    assert _query_terms("onde esta a validacao com o backend") == ["validacao", "backend"]


def test_rules_ask_for_both_languages():
    import pathlib
    import omnigraph
    from omnigraph.install import _CURSOR_RULE, _DEVIN_RULES
    texts = {f.name: f.read_text(encoding="utf-8") for f in (pathlib.Path(omnigraph.__file__).parent / "always_on").glob("*.md")}
    texts.update({"_CURSOR_RULE": _CURSOR_RULE, "_DEVIN_RULES": _DEVIN_RULES})
    for name, text in texts.items():
        assert "both the user's language and English" in " ".join(text.split()), name


def _bilingual_graph() -> nx.Graph:
    """A hop-2 neighbor that matches one of the two words the code uses."""
    G = nx.Graph()
    G.add_node("t", label="restore_session()", source_file="app/auth.py", source_location="L10", file_type="code")
    G.add_node("mid", label="bootstrap_app()", source_file="app/main.py", source_location="L1", file_type="code")
    G.add_node("far", label="refresh_session_token()", source_file="app/tokens.py", source_location="L7", file_type="code")
    G.add_edge("mid", "t", relation="calls", confidence="EXTRACTED")
    G.add_edge("mid", "far", relation="calls", confidence="EXTRACTED")
    for i in range(40):
        G.add_node(f"m{i}", label=f"other_{i}()", source_file=f"app/o{i}.py", source_location="L1", file_type="code")
        G.add_edge("mid", f"m{i}", relation="calls", confidence="EXTRACTED")
    for i in range(400):
        G.add_node(f"z{i}", label=f"unrelated_thing_{i}()", source_file=f"lib/z{i}.py", source_location="L1", file_type="code")
    return G


def test_words_missing_from_the_graph_do_not_change_the_answer():
    def answer(q):
        return _node_lines(_query_graph_text(_bilingual_graph(), q, depth=2))
    one_language = answer("restore session")
    assert answer("restaurar sessao restore session") == one_language
    assert any("refresh_session_token()" in line for line in one_language)


def test_rationale_attribute_counts_toward_coverage():
    from omnigraph.serve import _term_coverage
    data = {"label": "FAB visibility rule", "rationale": "hide the button while the map moves"}
    idf = {"button": 2.0, "map": 2.0}
    assert _term_coverage(data, ["button", "map"], idf, 4.0) == 0.5
