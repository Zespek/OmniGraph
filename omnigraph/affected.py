from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import unicodedata

import networkx as nx


DEFAULT_AFFECTED_RELATIONS = (
    "calls",
    "indirect_call",
    "references",
    "imports",
    "imports_from",
    "dynamic_import",
    "re_exports",
    "inherits",
    "extends",
    "implements",
    "uses",
    "mixes_in",
    "embeds",
    "requires",
)


@dataclass(frozen=True)
class AffectedHit:
    node_id: str
    depth: int
    via_relation: str
    via_file: "str | None" = None
    via_location: "str | None" = None


def _node_label(graph: nx.Graph, node_id: str) -> str:
    data = graph.nodes[node_id]
    return str(data.get("label") or node_id)


def _format_location(data: dict) -> str:
    source_file = data.get("source_file") or "-"
    source_location = data.get("source_location")
    if source_location:
        return f"{source_file}:{source_location}"
    return str(source_file)


def _bare_name(label: str) -> str:
    """Lowercased label with the callable decoration (trailing "()") removed."""
    label = _normalize_label(label)
    return label[:-2] if label.endswith("()") else label


def _normalize_label(label: str) -> str:
    return unicodedata.normalize("NFC", label).casefold()


def _as_repo_relative(query: str, root: Path | None = None) -> str:
    """Repo-relative form of a path query, for matching a stored `source_file`.

    The graph stores repo-relative paths, so `./src/x.py` and
    `/abs/repo/src/x.py` name the same file as `src/x.py` and yet matched
    nothing. `affected` then printed an empty list and exited 0 — a blast-radius
    tool answering "nothing depends on this" about a file with sixteen
    dependents, and indistinguishable from a genuine zero or a typo.

    An absolute path is anchored to `root` when given — the repo root derived
    from the graph's own location — so a seed resolves regardless of the caller's
    working directory (#2706: an absolute-path seed previously only matched when
    cwd happened to be the analysed repo root, which no editor or script can
    guarantee). `root` falls back to the current directory to preserve the prior
    behaviour when a caller has no graph location to derive it from.

    Non-path queries pass through unchanged: `Path("myFunc()").as_posix()` is
    `"myFunc()"`, so label resolution is untouched. An absolute path rooted
    outside `root` is left alone — no basename guessing.
    """
    path = Path(query)
    if path.is_absolute():
        anchor = root if root is not None else Path.cwd()
        try:
            return path.relative_to(anchor).as_posix()
        except ValueError:
            return query
    return path.as_posix()


def _prefer_file_node(
    graph: nx.Graph,
    node_ids: list[str],
    query: str,
) -> str | None:
    """Return the file-level node when a source_file query matches many nodes."""
    query_basename = _normalize_label(Path(query).name)
    exact_file_nodes = [
        node_id
        for node_id in node_ids
        if str(graph.nodes[node_id].get("source_location", "")) == "L1"
        and _normalize_label(str(graph.nodes[node_id].get("label", ""))) == query_basename
    ]
    if len(exact_file_nodes) == 1:
        return exact_file_nodes[0]

    l1_nodes = [
        node_id
        for node_id in node_ids
        if str(graph.nodes[node_id].get("source_location", "")) == "L1"
    ]
    if len(l1_nodes) == 1:
        return l1_nodes[0]

    basename_nodes = [
        node_id
        for node_id in node_ids
        if _normalize_label(str(graph.nodes[node_id].get("label", ""))) == query_basename
    ]
    if len(basename_nodes) == 1:
        return basename_nodes[0]

    return None


def resolve_seed(graph: nx.Graph, query: str, root: Path | None = None) -> str | None:
    # Um separador de caminho final não deve alterar uma correspondência de arquivo de origem - serviço
    # _find_node tokeniza o caminho (que o elimina), então retire-o aqui para obter paridade
    # (caso contrário, `afetado "src/x.ts/"` retornou None enquanto `explain` resolveu).
    query = query.rstrip("/\\") or query
    if query in graph:
        return query
    query_lower = _normalize_label(query)
    exact_label_matches = [
        str(node_id)
        for node_id, data in graph.nodes(data=True)
        if _normalize_label(str(data.get("label", ""))) == query_lower
    ]
    if len(exact_label_matches) == 1:
        return exact_label_matches[0]
    # Os rótulos que podem ser chamados são decorados ("nome()"), portanto, uma consulta simples de "nome" cai
    # por meio de correspondência exata e, em seguida, vincula-se a qualquer irmão "nome *" no
    # contém passe. Combine o nome não decorado antes de desistir.
    query_bare = _bare_name(query_lower)
    bare_name_matches = [
        str(node_id)
        for node_id, data in graph.nodes(data=True)
        if _bare_name(str(data.get("label", ""))) == query_bare
    ]
    if len(bare_name_matches) == 1:
        return bare_name_matches[0]
    query_path = _normalize_label(_as_repo_relative(query, root))
    exact_source_matches = [
        str(node_id)
        for node_id, data in graph.nodes(data=True)
        if _normalize_label(str(data.get("source_file", ""))) in (query_lower, query_path)
    ]
    if len(exact_source_matches) == 1:
        return exact_source_matches[0]
    if exact_source_matches:
        preferred_file_node = _prefer_file_node(
            graph, exact_source_matches, _as_repo_relative(query, root)
        )
        if preferred_file_node is not None:
            return preferred_file_node
    contains_matches = [
        str(node_id)
        for node_id, data in graph.nodes(data=True)
        if query_lower in _normalize_label(str(data.get("label", "")))
    ]
    if len(contains_matches) == 1:
        return contains_matches[0]
    return None


def affected_nodes(
    graph: nx.Graph,
    seed: str,
    *,
    relations: Iterable[str] = DEFAULT_AFFECTED_RELATIONS,
    depth: int = 2,
) -> list[AffectedHit]:
    relation_set = set(relations)
    seen = {seed}
    queue: deque[tuple[str, int]] = deque([(seed, 0)])
    hits: list[AffectedHit] = []

    # semeia o passeio reverso com os próprios nós membros da raiz (um para fora
    # `método`/`contém` hop). Um chamador pode vincular-se ao nó do método de uma classe em vez
    # do que o próprio nó da classe (por exemplo, `Service.call` resolve para o `def
    # nó self.call`), então esses chamadores ficam inacessíveis na classe
    # de outra forma. Os nós membros são apenas sementes (não relatados como ocorrências) e
    # `método`/`contém` ficam fora da caminhada geral filtrada por relação, então isso
    # não adiciona ruído direto em nenhum outro lugar.
    if hasattr(graph, "out_edges"):
        member_edges = graph.out_edges(seed, data=True)
    else:
        member_edges = (
            (s, t, d) for s, t, d in graph.edges(data=True) if s == seed
        )
    for _s, member, data in member_edges:
        if str(data.get("relation", "")) not in ("method", "contains"):
            continue
        member = str(member)
        if member not in seen:
            seen.add(member)
            queue.append((member, 0))

    while queue:
        current, current_depth = queue.popleft()
        if current_depth >= depth:
            continue
        if hasattr(graph, "in_edges"):
            incoming = graph.in_edges(current, data=True)
        else:
            incoming = (
                (source, target, data)
                for source, target, data in graph.edges(data=True)
                if target == current
            )
        for source, _target, data in incoming:
            relation = str(data.get("relation", ""))
            if relation not in relation_set:
                continue
            source = str(source)
            if source in seen:
                continue
            seen.add(source)
            hit = AffectedHit(
                source, current_depth + 1, relation,
                via_file=str(data.get("source_file") or "") or None,
                via_location=str(data.get("source_location") or "") or None,
            )
            hits.append(hit)
            queue.append((source, current_depth + 1))

    return hits


def format_affected(
    graph: nx.Graph,
    query: str,
    *,
    relations: Iterable[str] = DEFAULT_AFFECTED_RELATIONS,
    depth: int = 2,
    root: Path | None = None,
) -> str:
    relation_list = tuple(relations)
    seed = resolve_seed(graph, query, root)
    if seed is None:
        return f"No unique node match for {query}"

    hits = affected_nodes(graph, seed, relations=relation_list, depth=depth)
    lines = [
        f"Affected nodes for {_node_label(graph, seed)}",
        f"Relations: {', '.join(relation_list)}",
        f"Depth: {depth}",
    ]
    if not hits:
        lines.append("No affected nodes found.")
        return "\n".join(lines)

    for hit in hits:
        data = graph.nodes[hit.node_id]
        if hit.via_location:
            location = f"{hit.via_file or data.get('source_file') or '-'}:{hit.via_location}"
        else:
            location = _format_location(data)
        lines.append(
            f"- {_node_label(graph, hit.node_id)} [{hit.via_relation}] {location}"
        )
    return "\n".join(lines)


def load_graph(path: Path) -> nx.Graph:
    import json
    from networkx.readwrite import json_graph

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            f"Cannot read graph file {path}: {exc}. "
            "Re-run 'omnigraph extract' to regenerate it."
        ) from exc
    # A força direcionada para que a direção do chamador → chamador armazenado sobreviva à viagem de ida e volta;
    raw = {**raw, "directed": True}
    # Normalize a chave de aresta: a saída `extract` do omnigraph usa "arestas" enquanto
    # O padrão node_link_data do networkx é "links". Sem isso, uma chave de arestas
    # graph.json gera um KeyError não detectado: 'links' aqui - todos os outros carregadores
    # (__main__.py) já normaliza isso (; mesma classe que).
    if "links" not in raw and "edges" in raw:
        raw = dict(raw, links=raw["edges"])
    try:
        return json_graph.node_link_graph(raw, edges="links")
    except TypeError:
        return json_graph.node_link_graph(raw)
