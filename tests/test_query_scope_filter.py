"""`omnigraph query`/`explain` must support `--scope <path-substring>` to
restrict results to one app/module in a monorepo.

Reported independently across several real audit sessions on the same
monorepo (5+ apps sharing generic vocabulary like "service"/"controller"):
asking about "App Prestador" by name still returned nodes from unrelated
apps (affiliate, admin-web), because nothing in the query pipeline filtered
by path - seed selection is pure keyword matching against the whole graph,
so a same-named symbol in another app can win the seed and pull in more of
that app's nodes. `--scope` filters the graph BEFORE seeding, not just the
printed traversal, so the wrong app is never even a candidate.
"""
from omnigraph.build import build_from_json
from omnigraph.serve import _filter_graph_by_scope, _query_graph_text


def _monorepo_graph():
    """Three apps, each with an identically-named ServiceController.timeline(),
    mirroring the real shared-vocabulary collision across a monorepo's apps."""
    nodes = []
    edges = []
    for app in ("backend", "prestador", "afiliado"):
        src = f"{app}/src/service/service_request.py"
        nodes.append({"id": f"{app}::ServiceController", "label": "ServiceController",
                       "file_type": "code", "source_file": src})
        nodes.append({"id": f"{app}::timeline", "label": ".timeline()",
                       "file_type": "code", "source_file": src})
        edges.append({"source": f"{app}::ServiceController", "target": f"{app}::timeline",
                       "relation": "method", "confidence": "EXTRACTED", "source_file": src})
    return build_from_json({"nodes": nodes, "edges": edges, "hyperedges": []})


def test_no_scope_returns_graph_unchanged():
    G = _monorepo_graph()
    assert _filter_graph_by_scope(G, None) is G
    assert _filter_graph_by_scope(G, "") is G


def test_scope_keeps_only_matching_app():
    G = _monorepo_graph()
    H = _filter_graph_by_scope(G, "prestador")
    assert H.number_of_nodes() == 2
    assert all("prestador" in (d.get("source_file") or "") for _, d in H.nodes(data=True))


def test_scope_is_case_insensitive():
    G = _monorepo_graph()
    H = _filter_graph_by_scope(G, "PRESTADOR")
    assert H.number_of_nodes() == 2


def test_scope_drops_edges_to_excluded_nodes():
    G = _monorepo_graph()
    H = _filter_graph_by_scope(G, "prestador")
    assert H.number_of_edges() == 1
    u, v = list(H.edges())[0]
    assert "prestador" in G.nodes[u]["source_file"]
    assert "prestador" in G.nodes[v]["source_file"]


def test_scope_matching_nothing_returns_empty_graph():
    G = _monorepo_graph()
    H = _filter_graph_by_scope(G, "no-such-app")
    assert H.number_of_nodes() == 0


def test_query_with_scope_answers_only_from_that_app():
    """The real bug: an unscoped query on a shared term seeds from whichever
    app the traversal happens to land on first. Scoped, it must answer only
    from the requested app - proving the filter runs before seeding, not just
    on the printed result."""
    G = _monorepo_graph()
    result = _query_graph_text(G, "service timeline", scope="prestador")
    assert "prestador" in result
    assert "afiliado" not in result
    assert "backend/src" not in result


def test_query_scope_matching_nothing_is_a_clean_message_not_a_crash():
    G = _monorepo_graph()
    result = _query_graph_text(G, "service timeline", scope="no-such-app")
    assert "no-such-app" in result.lower() or "no nodes found" in result.lower()
