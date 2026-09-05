"""Community detection on NetworkX graphs. Uses Leiden (graspologic) if available, falls back to Louvain (networkx). Splits oversized communities. Returns cohesion scores."""
from __future__ import annotations
import contextlib
import inspect
import io
import json
import sys
import networkx as nx


def _suppress_output():
    """Context manager to suppress stdout/stderr during library calls.

    graspologic's leiden() emits ANSI escape sequences (progress bars,
    colored warnings) that corrupt PowerShell 5.1's scroll buffer on
    Windows (see issue #19). Redirecting stdout/stderr to devnull during
    the call prevents this without losing any omnigraph output.
    """
    return contextlib.redirect_stdout(io.StringIO())


def _native_leiden(stable: nx.Graph, resolution: float) -> dict[str, int] | None:
    """Call graspologic_native.leiden() directly, bypassing graspologic's own
    package import.

    graspologic.partition.leiden() is a thin wrapper around exactly this
    native (Rust) call. Importing the *package* — as opposed to the native
    extension module it depends on — pulls in graspologic.layouts, which
    imports umap, which imports pynndescent, which numba-JIT-compiles at
    import time for a layout algorithm this function never calls: measured
    at 7-19s of one-time import cost against a ~1s native call and a ~1.4s
    full round trip (conversion + call + map-back) — see the "third update"
    in OMNIGRAPH_BUILD_PERF.md for the measurements this is based on.

    Returns None (the caller falls through to the graspologic.partition.leiden
    path, then to the networkx Louvain fallback) if graspologic_native isn't
    installed, or if `stable` isn't the plain undirected, non-multigraph
    input leiden actually supports — the same shape check
    graspologic.partition.leiden itself makes before calling the same native
    function.
    """
    try:
        import graspologic_native as gn
    except ImportError:
        return None

    if stable.is_directed() or stable.is_multigraph():
        return None

    # graspologic_native identifies nodes by their string form; two DISTINCT
    # node objects that happen to stringify the same way would silently merge
    # under it (this is exactly what graspologic.partition.leiden's own
    # _IdentityMapper guards against). OmniGraph's own node IDs are already
    # unique strings by construction — extractors/resolution.py's
    # _disambiguate_colliding_node_ids salts any two distinct nodes that would
    # otherwise share a string id before the graph is ever built — so this is
    # a defensive check on an assumption that should never actually trip, not
    # an expected path. One pass over the nodes, cheaper than an
    # _IdentityMapper-style dict-store-per-edge-endpoint.
    id_to_node: dict[str, object] = {}
    for node in stable.nodes():
        key = str(node)
        existing = id_to_node.get(key)
        if existing is not None and existing != node:
            return None  # let graspologic.partition.leiden's own check handle/raise on this
        id_to_node[key] = node

    edges = [
        (str(u), str(v), float(attrs.get("weight", 1.0)))
        for u, v, attrs in stable.edges(data=True)
    ]

    try:
        old_stderr = sys.stderr
        try:
            sys.stderr = io.StringIO()
            with _suppress_output():
                _quality, native_partitions = gn.leiden(
                    edges=edges,
                    starting_communities=None,
                    resolution=resolution,
                    randomness=0.001,
                    iterations=1,
                    use_modularity=True,
                    seed=42,
                    trials=1,
                )
        finally:
            sys.stderr = old_stderr
    except Exception:
        return None

    return {id_to_node[node_id]: community for node_id, community in native_partitions.items()}


def _partition(G: nx.Graph, resolution: float = 1.0) -> dict[str, int]:
    """Run community detection. Returns {node_id: community_id}.

    Tries Leiden (graspologic_native directly, then graspologic) first — best
    quality. Falls back to Louvain (built into networkx) if neither is
    installed.

    resolution > 1.0 → more, smaller communities.
    resolution < 1.0 → fewer, larger communities.

    Output from graspologic is suppressed to prevent ANSI escape codes
    from corrupting terminal scroll buffers on Windows PowerShell 5.1.
    """
    stable = nx.Graph()
    stable.add_nodes_from(sorted(G.nodes(), key=str))
    # Canonicalise the endpoint pair before sorting. On an undirected graph the
    # (u, v) orientation each edge is yielded with comes from adjacency
    # iteration, which follows CPython's per-process string-hash order - so the
    # SAME edge appears as (A, B) in one run and (B, A) in the next. Sorting on
    # the raw pair therefore does not canonicalise anything: the edge lands in a
    # different position, `stable` is built in a different insertion order, and
    # Louvain - order-sensitive even with a fixed seed - can return a different
    # grouping. Measured on a 914-node graph: identical input, identical
    # first-pass partition, but the cohesion-split pass produced 70 communities
    # under PYTHONHASHSEED=1 and 69 under =2. Sorting the pair itself removes
    # the dependency; for nx.Graph the orientation carries no meaning anyway.
    edge_rows = sorted(
        G.edges(data=True),
        key=lambda row: (
            *sorted((str(row[0]), str(row[1]))),
            json.dumps(row[2], sort_keys=True, ensure_ascii=False, default=str),
        ),
    )
    for src, tgt, attrs in edge_rows:
        stable.add_edge(src, tgt, **attrs)

    native_result = _native_leiden(stable, resolution)
    if native_result is not None:
        return native_result

    try:
        from graspologic.partition import leiden
        lsig = inspect.signature(leiden).parameters
        kwargs: dict = {}
        if "random_seed" in lsig:
            kwargs["random_seed"] = 42
        if "trials" in lsig:
            kwargs["trials"] = 1
        if "resolution" in lsig:
            kwargs["resolution"] = resolution
        # Suprima a saída grampológica para evitar que códigos de escape ANSI
        # corrupting PowerShell 5.1 scroll buffer (issue #19)
        old_stderr = sys.stderr
        try:
            sys.stderr = io.StringIO()
            with _suppress_output():
                result = leiden(stable, **kwargs)
        finally:
            sys.stderr = old_stderr
        return result
    except ImportError:
        pass

    # Fallback: networkx louvain (disponível desde networkx 2.7).
    # Inspecione kwargs para permanecer compatível com as versões NetworkX – max_level
    # foi adicionado em uma versão posterior e evita travamentos em grafos grandes e esparsos.
    kwargs: dict = {"seed": 42, "threshold": 1e-4, "resolution": resolution}
    if "max_level" in inspect.signature(nx.community.louvain_communities).parameters:
        kwargs["max_level"] = 10
    communities = nx.community.louvain_communities(stable, **kwargs)
    return {node: cid for cid, nodes in enumerate(communities) for node in nodes}


_MAX_COMMUNITY_FRACTION = 0.25   # comunidades maiores que 25% do grafo são divididas
_MIN_SPLIT_SIZE = 10             # só será dividido se a comunidade tiver pelo menos esse número de nós
_COHESION_SPLIT_THRESHOLD = 0.05 # dividir novamente as comunidades com coesão abaixo deste
_COHESION_SPLIT_MIN_SIZE = 50    # apenas divisão por coesão se a comunidade tiver pelo menos este número de nós


def label_communities_by_hub(
    G: nx.Graph, communities: dict[int, list[str]]
) -> dict[int, str]:
    """Deterministic, LLM-free community labels: name each community after its
    highest-degree member — the structural hub — so a report reads ``auth`` /
    ``log_action`` instead of ``Community 70``. Degree is measured on the full graph
    ``G``; ties break by node id for run-to-run stability. A community whose members
    are all absent from ``G`` falls back to ``Community {cid}``.

    Used as the default (no-backend) labeler; an LLM naming pass, when configured,
    overrides these with richer names.
    """
    labels: dict[int, str] = {}
    for cid, members in communities.items():
        present = [n for n in members if n in G]
        if not present:
            labels[cid] = f"Community {cid}"
            continue
        # vitórias de maior grau; laços quebrados pelo ID do nó (ascendente) para determinismo
        hub = min(present, key=lambda n: (-G.degree(n), str(n)))
        name = str(G.nodes[hub].get("label") or hub).strip()
        if name.endswith("()"):
            name = name[:-2]
        labels[cid] = name or f"Community {cid}"
    return labels


def community_member_sigs(communities: dict[int, list[str]]) -> dict[int, str]:
    """Per-community membership fingerprints: ``{cid: sha256(sorted member ids)}``.

    Persisted next to ``.omnigraph_labels.json`` so a later ``cluster-only`` can tell
    which communities actually changed since labeling. A cid whose members no longer
    hash the same is a different community — reusing its old (LLM) label there is the
    "stale label after re-scoping" bug this guards against. Deterministic; independent
    of cid index, node order, and machine.
    """
    import hashlib

    sigs: dict[int, str] = {}
    for cid, members in communities.items():
        h = hashlib.sha256()
        for nid in sorted(str(n) for n in members):
            h.update(nid.encode("utf-8", "replace"))
            h.update(b"\x00")
        sigs[cid] = h.hexdigest()[:16]
    return sigs


def cluster(
    G: nx.Graph,
    resolution: float = 1.0,
    exclude_hubs_percentile: float | None = None,
) -> dict[int, list[str]]:
    """Run Leiden community detection. Returns {community_id: [node_ids]}.

    Community IDs are stable across runs: 0 = largest community after splitting.
    Oversized communities (> 25% of graph nodes, min 10) are split by running
    a second Leiden pass on the subgraph.

    Accepts directed or undirected graphs. DiGraphs are converted to undirected
    internally since Louvain/Leiden require undirected input.

    resolution: passed to Leiden/Louvain. >1.0 = more smaller communities,
        <1.0 = fewer larger communities. Default 1.0.
    exclude_hubs_percentile: if set (0-100), nodes whose degree exceeds this
        percentile are excluded from partitioning and reattached to their
        majority-vote neighbour community afterwards. Useful for staging/utility
        super-hubs that inflate god-node rankings (#919).
    """
    if G.number_of_nodes() == 0:
        return {}
    if G.is_directed():
        G = G.to_undirected()
    if G.number_of_edges() == 0:
        return {i: [n] for i, n in enumerate(sorted(G.nodes))}

    # Exclusão do hub de cálculo definida antes de remover qualquer coisa, de modo que o grau seja baseado no grafo completo
    hub_nodes: set[str] = set()
    if exclude_hubs_percentile is not None:
        degrees = sorted(d for _, d in G.degree())
        if degrees:
            idx = max(0, int(len(degrees) * exclude_hubs_percentile / 100) - 1)
            threshold = degrees[idx]
            hub_nodes = {n for n, d in G.degree() if d > threshold}

    # Leiden avisa e descarta isolados – trate-os separadamente
    # Exclua também os nós do hub do particionamento para que eles não extraiam
    # subsistemas na mesma comunidade
    excluded = hub_nodes
    isolates = [n for n in G.nodes() if G.degree(n) == 0 and n not in excluded]
    connected_nodes = [n for n in G.nodes() if G.degree(n) > 0 and n not in excluded]
    connected = G.subgraph(connected_nodes)

    raw: dict[int, list[str]] = {}
    if connected.number_of_nodes() > 0:
        partition = _partition(connected, resolution=resolution)
        for node, cid in partition.items():
            raw.setdefault(cid, []).append(node)

    # Cada isolado se torna sua própria comunidade de nó único
    next_cid = max(raw.keys(), default=-1) + 1
    for node in isolates:
        raw[next_cid] = [node]
        next_cid += 1

    # Reconecte hubs excluídos por comunidade vizinha com maioria de votos
    if hub_nodes:
        node_community: dict[str, int] = {n: cid for cid, nodes in raw.items() for n in nodes}
        for hub in sorted(hub_nodes):
            votes: dict[int, int] = {}
            for nb in G.neighbors(hub):
                cid = node_community.get(nb)
                if cid is not None:
                    votes[cid] = votes.get(cid, 0) + 1
            if votes:
                best = min(votes, key=lambda c: (-votes[c], c))
                raw.setdefault(best, []).append(hub)
                node_community[hub] = best
            else:
                raw[next_cid] = [hub]
                node_community[hub] = next_cid
                next_cid += 1

    # Split oversized communities
    max_size = max(_MIN_SPLIT_SIZE, int(G.number_of_nodes() * _MAX_COMMUNITY_FRACTION))
    final_communities: list[list[str]] = []
    for nodes in raw.values():
        if len(nodes) > max_size:
            final_communities.extend(_split_community(G, nodes))
        else:
            final_communities.append(nodes)

    # Segunda passagem: re-dividir comunidades de baixa coesão causadas por nós doc-hub
    # que conectam subsistemas não relacionados (por exemplo, CLAUDE.md conectado a tudo).
    second_pass: list[list[str]] = []
    for nodes in final_communities:
        if len(nodes) >= _COHESION_SPLIT_MIN_SIZE and cohesion_score(G, nodes) < _COHESION_SPLIT_THRESHOLD:
            splits = _split_community(G, nodes)
            second_pass.extend(splits if len(splits) > 1 else [nodes])
        else:
            second_pass.append(nodes)
    final_communities = second_pass

    # Reindexar por tamanho decrescente. O desempate tuple(sorted(nodes)) torna isso um
    # Ordem TOTAL, portanto, um agrupamento idêntico sempre obtém IDs de comunidade idênticos.
    # Sem ela, as centenas de pequenas comunidades de tamanhos iguais são ordenadas pelo
    # ordem de enumeração do particionador (não estável em sementes), portanto, seus IDs inteiros
    # permutar execução a execução - o que parece uma "rotatividade da comunidade" massiva em um nó por nó
    # cid diff mesmo que o agrupamento real seja reproduzível (follow-up).
    final_communities.sort(key=lambda nodes: (-len(nodes), tuple(sorted(map(str, nodes)))))
    return {i: sorted(nodes) for i, nodes in enumerate(final_communities)}


def _split_community(G: nx.Graph, nodes: list[str]) -> list[list[str]]:
    """Run a second Leiden pass on a community subgraph to split it further."""
    subgraph = G.subgraph(nodes)
    if subgraph.number_of_edges() == 0:
        # Sem arestas - dividido em nós individuais
        return [[n] for n in sorted(nodes)]
    try:
        sub_partition = _partition(subgraph)
        sub_communities: dict[int, list[str]] = {}
        for node, cid in sub_partition.items():
            sub_communities.setdefault(cid, []).append(node)
        if len(sub_communities) <= 1:
            return [sorted(nodes)]
        return [sorted(v) for v in sub_communities.values()]
    except Exception:
        return [sorted(nodes)]


def cohesion_score(G: nx.Graph, community_nodes: list[str]) -> float:
    """Ratio of actual intra-community edges to maximum possible."""
    n = len(community_nodes)
    if n <= 1:
        return 1.0
    subgraph = G.subgraph(community_nodes)
    actual = subgraph.number_of_edges()
    possible = n * (n - 1) / 2
    return actual / possible if possible > 0 else 0.0


def score_all(G: nx.Graph, communities: dict[int, list[str]]) -> dict[int, float]:
    return {cid: cohesion_score(G, nodes) for cid, nodes in communities.items()}


def remap_communities_to_previous(
    communities: dict[int, list[str]],
    previous_node_community: dict[str, int],
) -> dict[int, list[str]]:
    """Remap community IDs to maximize overlap with a previous assignment.

    Uses greedy one-to-one matching by intersection size, then assigns fresh IDs
    to unmatched communities in deterministic order (size desc, lexical tie-break).
    """
    if not communities:
        return {}

    new_sets = {cid: set(nodes) for cid, nodes in communities.items()}
    old_sets: dict[int, set[str]] = {}
    for node, old_cid in previous_node_community.items():
        old_sets.setdefault(old_cid, set()).add(node)

    overlaps: list[tuple[int, int, int]] = []
    for old_cid, old_nodes in old_sets.items():
        for new_cid, new_nodes in new_sets.items():
            overlap = len(old_nodes & new_nodes)
            if overlap > 0:
                overlaps.append((overlap, old_cid, new_cid))
    overlaps.sort(key=lambda x: (-x[0], x[1], x[2]))

    new_to_final: dict[int, int] = {}
    used_old_ids: set[int] = set()
    matched_new_ids: set[int] = set()
    for _overlap, old_cid, new_cid in overlaps:
        if old_cid in used_old_ids or new_cid in matched_new_ids:
            continue
        new_to_final[new_cid] = old_cid
        used_old_ids.add(old_cid)
        matched_new_ids.add(new_cid)

    unmatched = [cid for cid in communities if cid not in matched_new_ids]
    unmatched.sort(key=lambda cid: (-len(communities[cid]), tuple(sorted(communities[cid]))))
    next_id = 0
    for new_cid in unmatched:
        while next_id in used_old_ids:
            next_id += 1
        new_to_final[new_cid] = next_id
        used_old_ids.add(next_id)
        next_id += 1

    remapped: dict[int, list[str]] = {}
    for new_cid, nodes in communities.items():
        remapped[new_to_final[new_cid]] = sorted(nodes)
    return dict(sorted(remapped.items(), key=lambda kv: kv[0]))
