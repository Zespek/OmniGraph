# montar dictos de nó + aresta em um grafo NetworkX, preservando a direção da aresta
#
# Node deduplication — three layers:
#
# 1. Dentro de um arquivo (AST): cada extrator rastreia um conjunto `seen_ids`. Um ID de nó é
#    emitido no máximo uma vez por arquivo, portanto, duplique as definições de classe/função em
#    o mesmo arquivo de origem é recolhido na primeira ocorrência.
#
# 2. Entre arquivos (construção): NetworkX G.add_node() é idempotente - chamando-o
#    duas vezes com o mesmo ID substitui os atributos pelos da segunda chamada
#    valores. Os nós são adicionados em ordem de extração (primeiro AST, depois semântico),
#    então se a mesma entidade for extraída por ambos passa o nó semântico
#    substitui silenciosamente o nó AST. Isso é intencional: nós semânticos
#    carregam rótulos mais ricos e contexto de arquivo cruzado, enquanto os nós AST têm
#    source_location. Se você precisar alterar a prioridade, reordene as extrações
#    passado para construir().
#
# 3. Mesclagem semântica (habilidade): antes de chamar build(), a habilidade mescla em cache
#    e novos resultados semânticos usando um conjunto `visto` explícito digitado no nó ["id"],
#    portanto, duplicatas em ocorrências de cache e novas extrações são resolvidas lá
#    antes que qualquer construção de grafo aconteça.
#
from __future__ import annotations
import json
import math
import os
import re
import sys
import unicodedata
from pathlib import Path
import networkx as nx
from .ids import make_id, normalize_id as _normalize_id
from .paths import default_graph_json as _default_graph_json
from .validate import validate_extraction


# Famílias de interoperabilidade de idiomas, codificadas por extensão, para a aresta fantasma entre idiomas
# guarda no loop de aresta abaixo. Grupo de famílias por interoperabilidade REAL (JS/TS compartilha um módulo
# grafo; C/C++/ObjC compartilham uma unidade de compilação por meio de cabeçalhos; Idiomas JVM compartilham bytecode),
# portanto, uma chamada legítima de importação TS->JS ou C impl->header sobrevive, enquanto um Python
# ligação `import time` para um `time.ts` ou uma `calls` INFERRED entre idiomas
# aresta é descartada. Mantido local para build.py (não importado de extract.py,
# que importa build.py - um ciclo) e espelha deliberadamente extract._LANG_FAMILY_BY_EXT.
_EDGE_LANG_FAMILY: dict[str, str] = {
    ".py": "py", ".pyi": "py",
    ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js",
    ".ts": "js", ".tsx": "js", ".mts": "js", ".cts": "js",
    ".go": "go", ".rs": "rs",
    ".java": "jvm", ".kt": "jvm", ".scala": "jvm", ".groovy": "jvm",
    ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".hpp": "c",
    ".cxx": "c", ".hh": "c", ".hxx": "c",
    ".cu": "c", ".cuh": "c", ".metal": "c", ".m": "c", ".mm": "c",
    ".rb": "rb", ".rake": "rb", ".php": "php", ".cs": "cs", ".swift": "swift", ".lua": "lua",
}


# Mapeador de sinônimo para valores de tipo de arquivo inválidos conhecidos que os subagentes LLM comumente
# emitir. Mantém a intenção semântica próxima (markdown → documento, ferramenta → código) e cai
# volte ao "conceito" para qualquer outro valor inválido (veja).
_FILE_TYPE_SYNONYMS = {
    "markdown": "document",
    "text": "document",
    "tool": "code",
    "library": "code",
    "pattern": "concept",
    "principle": "concept",
    "constraint": "concept",
    "tech": "concept",
    "technology": "concept",
    "data-source": "concept",
    "data_source": "concept",
    "gotcha": "concept",
    "framework": "concept",
}


# As listas de membros do Hyperedge são `nós` com chave canônica (consulte zspekfy/llm.py
# especificação de extração), mas desvio de LLM/subagente e graph.json fornecido externamente
# às vezes emite `membros` ou `node_ids`. _normalize_hyperedge_members dobras
# esses aliases em `nós` na ingestão, para que cada consumidor downstream leia um
# chave canônica - espelhando a tolerância `from`/`to` edge-endpoint abaixo.
_HE_MEMBER_ALIASES = ("members", "node_ids")


def _normalize_hyperedge_members(he: object) -> None:
    """Canonicalize a hyperedge's member list onto the `nodes` key, in place.

    If `nodes` is already a list it wins (canonical), and only stray alias keys
    are dropped. Otherwise the first alias (`members`, then `node_ids`) that is a
    list is moved to `nodes`, deduped preserving order, with a single stderr
    WARNING naming the hyperedge id and alias used. Leftover alias keys are
    always removed so downstream code never re-reads them.
    """
    if not isinstance(he, dict):
        return
    if not isinstance(he.get("nodes"), list):
        for alias in _HE_MEMBER_ALIASES:
            val = he.get(alias)
            if isinstance(val, list):
                seen: set = set()
                deduped: list = []
                for ref in val:
                    try:
                        is_dupe = ref in seen
                    except TypeError:
                        is_dupe = False  # ref inashable: mantenha-o, o validador sinaliza-o
                    if is_dupe:
                        continue
                    try:
                        seen.add(ref)
                    except TypeError:
                        pass
                    deduped.append(ref)
                he["nodes"] = deduped
                print(
                    f"[omnigraph] WARNING: hyperedge "
                    f"'{he.get('id', '?')}' uses field '{alias}' instead of "
                    f"'nodes'; normalizing.",
                    file=sys.stderr,
                )
                break
    # Elimine quaisquer chaves de alias restantes, independentemente de qual ramificação foi executada acima.
    for alias in _HE_MEMBER_ALIASES:
        he.pop(alias, None)


def _fold_node_aliases(node: dict) -> None:
    """Fold legacy node field aliases onto canonical keys, in place (#2194).

    ``name`` -> ``label`` and ``path`` -> ``source_file``. Uses an empty-check
    (not mere key presence) so a node carrying ``label: ""``/``None`` next to a
    real ``name`` is healed too. When the canonical field already holds a value
    it wins and the alias key is left untouched. Without this fold an alias-only
    node enters the graph with no label/source_file: it fails validation, gets
    ``norm_label == ""`` (invisible to query/explain), and is excluded from every
    label-keyed merge/dedup — a permanent ghost that ``omnigraph update``
    re-feeds through build_from_json forever.
    """
    if not node.get("label") and isinstance(node.get("name"), str) and node["name"]:
        node["label"] = node.pop("name")
    if not node.get("source_file") and isinstance(node.get("path"), str) and node["path"]:
        node["source_file"] = node.pop("path")


def _fold_edge_aliases(edge: dict) -> None:
    """Fold legacy edge field aliases onto canonical keys, in place (#2194).

    ``type`` -> ``relation``. A ``confidence_score`` float with no ``confidence``
    enum backfills ``confidence: "INFERRED"`` — never EXTRACTED (alias recovery
    is not provenance) and never a threshold mapping of the float. The
    ``confidence_score`` key itself is NOT popped: it is a legitimate companion
    field that the edge loop sanitizes and to_json round-trips.
    """
    if not edge.get("relation") and isinstance(edge.get("type"), str) and edge["type"]:
        edge["relation"] = edge.pop("type")
    if not edge.get("confidence") and edge.get("confidence_score") is not None:
        edge["confidence"] = "INFERRED"


def _norm_source_file(p: str | None, root: str | None = None) -> str | None:
    """Normalize path separators and relativize absolute paths.

    Converts backslashes to forward slashes (Windows compatibility) and, when
    root is provided, strips the absolute prefix from paths produced by semantic
    subagents so source_file is always repo-relative (fixes #932).
    """
    if not p:
        return p
    p = p.replace("\\", "/")
    if root and os.path.isabs(p):
        try:
            p = Path(p).relative_to(root).as_posix()
        except ValueError:
            # Relativo léxico_to falhou. Tente novamente com ambos os lados totalmente resolvidos:
            # uma raiz de varredura com link simbólico (macOS /var -> /private/var ou um link simbólico
            # home/worktree) faz com que os prefixos brutos sejam diferentes, embora apontem
            # no mesmo diretório, que de outra forma derrota silenciosamente podar/substituir
            # correspondência. Somente o caminho lento é resolvido, então a correspondência lexical comum
            # stays filesystem-free.
            try:
                p = Path(p).resolve().relative_to(Path(root).resolve()).as_posix()
            except (ValueError, OSError):
                pass
    return p


def _abs_identity(p: str | None, root: str | None = None) -> str | None:
    """Return a form-insensitive absolute identity for a source_file.

    prune/replace matching in build_merge otherwise compares raw strings against
    ``_norm_source_file`` output, so a node whose source_file survived in a THIRD
    form — absolute where prune_sources is relative, or vice versa, or a symlinked
    root — slips past every equality check and its nodes/edges are never pruned
    (silent survival of a deleted file's graph, #2012). Anchoring relative paths
    at ``root`` and resolving both sides to a canonical absolute posix path gives
    a fallback that matches regardless of which form each side happens to hold.
    """
    if not p:
        return None
    q = p.replace("\\", "/")
    pp = Path(q)
    if not pp.is_absolute() and root:
        pp = Path(root) / q
    try:
        return pp.resolve().as_posix()
    except OSError:
        return pp.as_posix()


def _is_file_node_label(label: "str | None", source_file: "str | None") -> bool:
    """Whether *label* is a file node's label for *source_file* — the bare
    basename, OR a directory-qualified suffix produced by the disambiguation pass
    below (#2032). Used both to recognize file nodes when relabeling and by the
    downstream file-node predicates (analyze/tree/serve)."""
    if not label or not source_file:
        return False
    sf = str(source_file).replace("\\", "/")
    lbl = str(label)
    if lbl == sf.rsplit("/", 1)[-1]:
        return True
    return "/" in lbl and (sf == lbl or sf.endswith("/" + lbl))


def _shortest_unique_suffix(sf: str, all_sfs: "set[str]") -> str:
    """Shortest trailing path suffix (basename + k parent dirs) of *sf* that is
    unique among *all_sfs*. `a/b/index.ts` vs `c/b/index.ts` -> `a/b/index.ts`;
    `x/index.ts` vs `y/index.ts` -> `x/index.ts`. Derived from the path (never the
    current label) so relabeling is idempotent across incremental rebuilds."""
    parts = [p for p in sf.replace("\\", "/").split("/") if p]
    others = [
        [p for p in o.replace("\\", "/").split("/") if p]
        for o in all_sfs if o != sf
    ]
    for k in range(1, len(parts) + 1):
        suffix = parts[-k:]
        if all(o[-k:] != suffix for o in others):
            return "/".join(suffix)
    return "/".join(parts)


def _file_label_reassignments(items: "list[tuple]") -> dict:
    """Given (key, label, source_file) triples, return {key: new_label} for file
    nodes whose basename collides with another's — the shortest unique
    directory-qualified suffix (#2032). Keys of non-colliding/basename-unique file
    nodes are omitted (their label stays bare)."""
    from collections import defaultdict
    groups: dict[str, list[tuple]] = defaultdict(list)
    for key, label, sf in items:
        if sf and label and _is_file_node_label(str(label), str(sf)):
            basename = str(sf).replace("\\", "/").rsplit("/", 1)[-1]
            groups[basename].append((key, str(sf)))
    out: dict = {}
    for members in groups.values():
        distinct = {sf for _, sf in members}
        if len(distinct) < 2:
            continue  # no collision — leave the bare basename label
        for key, sf in members:
            out[key] = _shortest_unique_suffix(sf, distinct)
    return out


def _disambiguate_file_node_labels(G: "nx.Graph") -> None:
    """Relabel colliding-basename file nodes on a graph (#2032). Ids/edges are
    never changed — only display labels. Idempotent (labels derive from
    source_file, not the current possibly-qualified label)."""
    items = [(nid, a.get("label"), a.get("source_file")) for nid, a in G.nodes(data=True)]
    for nid, new_label in _file_label_reassignments(items).items():
        G.nodes[nid]["label"] = new_label


def disambiguate_file_labels_in_nodes(nodes: "list") -> None:
    """Relabel colliding-basename file nodes on a raw node-dict list, in place
    (#2032). Used by the extract --no-cluster path, which writes the merged
    extraction directly without going through build_from_json."""
    items = [
        (i, n.get("label"), n.get("source_file"))
        for i, n in enumerate(nodes) if isinstance(n, dict)
    ]
    for i, new_label in _file_label_reassignments(items).items():
        nodes[i]["label"] = new_label


def _infer_merge_root(graph_path: Path) -> str | None:
    """Best-effort scan root for relativizing paths in build_merge when the caller
    passes no ``root`` (#1571).

    Prefers the committed ``omnigraph-out/.omnigraph_root`` marker — the authoritative
    scan root omnigraph records at build/watch time (#686/#1423) — then falls back to
    the directory that contains the output dir (``graph.json``'s grandparent, i.e.
    ``<root>/omnigraph-out/graph.json`` -> ``<root>``). Returns None if neither
    resolves, in which case normalization is a no-op (prior behavior).
    """
    try:
        marker = graph_path.parent / ".omnigraph_root"
        if marker.exists():
            recorded = marker.read_text(encoding="utf-8").strip()
            if recorded:
                return str(Path(recorded).resolve())
    except OSError:
        pass
    try:
        return str(graph_path.parent.parent.resolve())
    except Exception:
        return None


def edge_data(G: nx.Graph, u: str, v: str) -> dict:
    """Return one edge attribute dict for (u, v), tolerating MultiGraph.

    For MultiGraph/MultiDiGraph there can be multiple parallel edges;
    this returns the first one (sufficient for callers that only need
    relation/confidence for rendering). Fixes #796.
    """
    raw = G[u][v]
    if isinstance(G, (nx.MultiGraph, nx.MultiDiGraph)):
        return next(iter(raw.values()), {})
    return raw


def edge_datas(G: nx.Graph, u: str, v: str) -> list[dict]:
    """Return every edge attribute dict for (u, v); always a list."""
    raw = G[u][v]
    if isinstance(G, (nx.MultiGraph, nx.MultiDiGraph)):
        return list(raw.values())
    return [raw]


def dedupe_nodes(nodes: list[dict]) -> list[dict]:
    """Collapse nodes sharing an ``id``, last-writer-wins on attributes.

    Mirrors what ``build_from_json``'s ``G.add_node`` does implicitly (idempotent;
    a later node overwrites an earlier one's attributes). The ``--no-cluster``
    write path dumps the raw node list without building a graph, so same-id nodes
    — e.g. a Swift ``type=module`` anchor emitted once per importing file (#1327)
    — would otherwise appear as duplicates. Insertion order follows each id's
    first appearance; the retained dict is the last one seen.
    """
    by_id: dict = {}
    for n in nodes:
        nid = n.get("id")
        if nid is None:
            continue
        by_id[nid] = n
    return list(by_id.values())


def dedupe_edges(edges: list[dict]) -> list[dict]:
    """Collapse exact parallel edges by ``(source, target, relation)``, keeping the
    first occurrence.

    The clustered build path runs edges through a NetworkX ``DiGraph``, which
    collapses parallel edges automatically. The ``--no-cluster`` and incremental
    ``update`` write paths bypass NetworkX and concatenate edge lists raw, so
    duplicates accumulate and edge counts become non-deterministic across build
    modes / repeated updates (#1317). Deduping on the connectivity identity is
    zero-signal-loss and restores idempotency. Callers that intentionally keep
    parallel edges (multigraph output) must not use this.
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for e in edges:
        key = (e.get("source"), e.get("target"), e.get("relation"))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _old_file_stems(rel: Path) -> list[str]:
    """Pre-migration stem forms a semantic fragment may have used for ``rel``.

    Ordered longest-first so prefix stripping is greedy and unambiguous:
      - one-parent form: ``parent.stem``  (the old _file_stem rule, #550-era)
      - zero-parent form: ``stem``        (the old llm.py prompt rule, #1509)
    """
    forms: list[str] = []
    parent = rel.parent.name
    if parent and parent not in (".", ""):
        forms.append(make_id(f"{parent}.{rel.stem}"))
    forms.append(make_id(rel.stem))
    # Dedupe while preserving order (top-level files collapse both forms).
    seen: set[str] = set()
    return [f for f in forms if f and not (f in seen or seen.add(f))]


def _semantic_id_remap(nodes: list, root: str | None) -> dict:
    """Re-derive non-AST node ids from ``source_file`` using the canonical
    full-path stem, so a cached/LLM fragment carrying a pre-migration short id
    reconciles with the AST node instead of spawning a ghost (#1504/#1509).

    Drift-proof by construction: the new id is computed from ``source_file`` in
    code, never trusted from the fragment's own ``id`` string. AST-origin nodes
    are skipped (they are already canonical via the extract() post-pass)."""
    from omnigraph.extractors.base import _file_stem  # local: evite custos de importação no carregamento do módulo

    remap: dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("_origin") == "ast":
            continue
        nid = node.get("id")
        sf = node.get("source_file")
        if not nid or not isinstance(nid, str) or not sf:
            continue
        sf_norm = _norm_source_file(str(sf), root) or str(sf)
        rel = Path(sf_norm)
        if rel.is_absolute():
            continue  # não é possível relativizar (não/raiz com falha) - deixe o id intocado
        if not rel.name:
            # source_file é igual à raiz da varredura, então _norm_source_file relativizou-o
            # to Path('.') — um nó no nível do projeto sem identidade por arquivo para remapear.
            # Deixe seu id intacto (e evite a falha de nome vazio de _file_stem).
            continue
        new_stem = make_id(_file_stem(rel))
        if not new_stem:
            continue
        norm_nid = _normalize_id(nid)
        # Guarda de idempotência: um id que já carrega seu radical canônico é
        # pronto - não execute novamente o branch legado nele. Quando o radical canônico
        # contém um radical herdado mais curto como prefixo (nome do diretório pai == arquivo
        # stem, e.g. `.claude/CLAUDE.md` -> `claude_claude` over legacy `claude`),
        # um ID já migrado como `claude_claude_x` ainda corresponde ao legado
        # prefixo `claude_` abaixo e ganharia outro segmento radical em cada
        # build, derrotando os curtos-circuitos same_topology/no_change. Espelha o
        # verificação canônica em graph_has_legacy_ids.
        if norm_nid == new_stem or norm_nid.startswith(new_stem + "_"):
            continue
        new_id: str | None = None
        old_forms = _old_file_stems(rel)
        # on Windows, detect() can emit an ABSOLUTE source_file, and a
        # semantic fragment's id derived from that absolute path (e.g.
        # d_projects_myrepo_docs_dataflow) matches neither the canonical
        # relative stem nor the legacy short forms above — so while source_file
        # itself is healed by _norm_source_file, the id would ghost against the
        # existing graph's docs_dataflow. When the raw path was absolute and
        # relativized under root, treat the raw-absolute stem as one more
        # old-stem form — the semantic-side twin of extract.py's absolute-form
        # id registration. It is the longest form, so it goes first (greedy
        # prefix stripping, same ordering rule as _old_file_stems).
        sf_raw = str(sf).replace("\\", "/")
        if sf_raw != sf_norm and os.path.isabs(sf_raw):
            abs_stem = make_id(_file_stem(Path(sf_raw)))
            if abs_stem and abs_stem != new_stem and abs_stem not in old_forms:
                old_forms.insert(0, abs_stem)
        for old_stem in old_forms:
            if old_stem == new_stem:
                continue  # já canônico para este formulário
            if norm_nid == old_stem:
                new_id = new_stem  # o próprio nó do arquivo
                break
            prefix = old_stem + "_"
            if norm_nid.startswith(prefix):
                entity = norm_nid[len(prefix):]
                new_id = make_id(new_stem, entity)
                break
        if new_id and new_id != nid:
            remap[nid] = new_id
    return remap


def graph_has_legacy_ids(nodes: list, root: str | Path | None = None, sample: int = 300) -> bool:
    """Whether a loaded graph still uses pre-#1504 node IDs (parent-dir / filename
    stem) rather than the full repo-relative path. Read-only consumers (query,
    serve) use this to nudge the user to rebuild, since they don't re-extract.

    Heuristic and cheap: only **file-level** nodes (source_location ``L1``) are
    inspected, because their ID is unambiguously the file stem. Symbol nodes are
    skipped — some extractors scope a symbol by package/directory (Go's
    ``_make_id(pkg_dir, name)`` → ``sub_thing``), which can coincide with an old
    file-stem form and would otherwise false-positive. Returns True as soon as one
    file node's ID matches an OLD stem form but not the canonical full-path form."""
    from omnigraph.extractors.base import _file_stem
    _r = str(root) if root is not None else None
    checked = 0
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if str(node.get("source_location") or "") != "L1":
            continue  # apenas nós de nível de arquivo carregam um ID de tronco de arquivo inequívoco
        nid = node.get("id")
        sf = node.get("source_file")
        if not nid or not isinstance(nid, str) or not sf:
            continue
        rel = Path(_norm_source_file(str(sf), _r) or str(sf))
        if rel.is_absolute():
            continue
        if not rel.name:
            continue  # source_file == scan root -> Path('.'), sem arquivo stem
        new_stem = make_id(_file_stem(rel))
        if not new_stem:
            continue
        norm = _normalize_id(nid)
        if norm == new_stem or norm.startswith(new_stem + "_"):
            checked += 1
        else:
            for old in _old_file_stems(rel):
                if old != new_stem and (norm == old or norm.startswith(old + "_")):
                    return True
            checked += 1
        if checked >= sample:
            break
    return False


def _doc_twin_remap(nodes: list) -> dict[str, str]:
    """Map a markdown quick-scan's bare doc node ``<slug>`` to the semantic
    ``<slug>_doc`` node for the SAME file (#1799).

    The markdown quick-scan (``extract_markdown``) mints a file node with the
    bare id ``_make_id(path)`` while the semantic pass mints ``<slug>_doc`` for
    the same document. A ``omnigraph update`` after a semantic build leaves both,
    splitting the file's edges across two disconnected nodes. Canonicalize to the
    semantic ``_doc`` node (it carries the richer references/hyperedges). Gated to
    ``file_type == "document"`` on BOTH twins with an identical ``source_file``,
    so an unrelated code symbol ``foo`` and ``foo_doc`` never merge.
    """
    by_id: dict[str, dict] = {}
    for n in nodes:
        if isinstance(n, dict) and n.get("id"):
            by_id[str(n["id"])] = n
    remap: dict[str, str] = {}
    for nid, node in by_id.items():
        if not nid.endswith("_doc"):
            continue
        bare = by_id.get(nid[:-4])
        if bare is None:
            continue
        sf = node.get("source_file")
        if not sf or bare.get("source_file") != sf:
            continue
        if node.get("file_type") != "document" or bare.get("file_type") != "document":
            continue
        remap[nid[:-4]] = nid
    return remap


def build_from_json(extraction: dict, *, directed: bool = False, root: str | Path | None = None) -> nx.Graph:
    """Build a NetworkX graph from an extraction dict.

    directed=True produces a DiGraph that preserves edge direction (source→target).
    directed=False (default) produces an undirected Graph for backward compatibility.
    root: if given, absolute source_file paths from semantic subagents are made
        relative to root so all nodes share a consistent path key (#932).
    """
    _root = str(Path(root).resolve()) if root else None
    # NetworkX <= 3.1 arestas serializadas como "links"; remapear para "arestas" para compatibilidade.
    if "edges" not in extraction and "links" in extraction:
        extraction = dict(extraction, edges=extraction["links"])

    # Canonize o esquema de nó/aresta herdado antes da validação.
    for node in extraction.get("nodes", []):
        if not isinstance(node, dict):
            continue
        if "source" in node and "source_file" not in node:
            # Contar arestas que fazem referência a este nó para que o aviso seja acionável
            node_id = node.get("id", "?")
            affected_edges = sum(
                1 for e in extraction.get("edges", [])
                if e.get("source") == node_id or e.get("target") == node_id
            )
            print(
                f"[omnigraph] WARNING: node '{node_id}' uses field 'source' instead of "
                f"'source_file' — {affected_edges} edge(s) may be misrouted. "
                f"Rename the field to 'source_file' to silence this warning.",
                file=sys.stderr,
            )
            node["source_file"] = node.pop("source")
        # Fold the remaining legacy node aliases (`name`->`label`,
        # `path`->`source_file`) before validation and before the
        # semantic-rekey / ghost-merge passes below, all of which key on
        # label/source_file and would otherwise skip the node entirely.
        _fold_node_aliases(node)
        # Padrão ausente/Nenhum file_type para "conceito", então legado graph.json
        # entradas (e nós stub preservados por `_rebuild_code` de versões mais antigas
        # versões omnigraph que nem sempre preencheram file_type) não
        # trigger spurious "invalid file_type 'None'" validator warnings.
        if node.get("file_type") in (None, ""):
            node["file_type"] = "concept"
        ft = node.get("file_type", "")
        if ft and ft not in {"code", "document", "paper", "image", "rationale", "concept"}:
            node["file_type"] = _FILE_TYPE_SYNONYMS.get(ft, "concept")

    # Canonizar listas de membros do hyperedge: os produtores às vezes digitam o
    # lista de membros `members`/`node_ids` em vez de `nodes`. Dobre os aliases em
    # `nodes` aqui - ANTES da validação e do loop de rechave semântica abaixo - então
    # cada consumidor downstream (rekey, source_file relativize, to_json) lê
    # uma chave canônica, da mesma forma que os endpoints de aresta alias de/para na construção.
    for he in extraction.get("hyperedges", []) or []:
        _normalize_hyperedge_members(he)

    # Fold legacy edge field aliases (`type`->`relation`,
    # `confidence_score`->`confidence`) BEFORE validation. The existing
    # from/to endpoint fold lives in the edge loop further down, which runs
    # after validate_extraction — too late for fields the validator requires.
    for edge in extraction.get("edges", []):
        if isinstance(edge, dict):
            _fold_edge_aliases(edge)

    errors = validate_extraction(extraction)
    # Arestas pendentes (importações stdlib/externas) são esperadas - apenas avisam sobre erros reais de esquema.
    real_errors = [e for e in errors if "does not match any node id" not in e]
    if real_errors:
        # Break the warning down by cause: a mixed batch used to surface
        # only real_errors[0], hiding every other failure mode. Group on the
        # "missing required field 'X'" suffix and report per-cause counts plus
        # one example each, so the operator sees the full shape of the damage.
        by_cause: dict[str, list[str]] = {}
        for err in real_errors:
            m = re.search(r"missing required field '[^']*'", err)
            by_cause.setdefault(m.group(0) if m else "other schema issue", []).append(err)
        breakdown = "; ".join(
            f"{len(errs)}x {cause} (e.g. {errs[0]})" for cause, errs in by_cause.items()
        )
        print(
            f"[omnigraph] Extraction warning ({len(real_errors)} issues): {breakdown}",
            file=sys.stderr,
        )
    # Rechave semântica determinística: o radical do ID do nó agora é o
    # caminho relativo completo do repositório (docs/v1/api/README.md -> docs_v1_api_readme), mas
    # o cache semântico NÃO É VERSIONADO, portanto, um fragmento armazenado em cache/LLM ainda pode transportar
    # um ID abreviado ANTIGO cujo radical era apenas o diretório pai imediato (api_readme),
    # ou um ID de desvio de prompt com zero diretórios pai (leia-me). Em vez de confiar
    # Prosa LLM para emitir o radical correto, derivamos novamente o ID de cada nó não AST de
    # seu próprio source_file no código, então um fragmento desviado reconcilia fisicamente
    # com o nó AST em vez de gerar um fantasma/uma nova fatura. Nós de origem AST
    # já carregam IDs canônicos (o pós-passagem extract() id-remap garante isso)
    # e ficam intocados.
    _rekey: dict[str, str] = _semantic_id_remap(extraction.get("nodes", []), _root)
    if _rekey:
        for node in extraction.get("nodes", []):
            if isinstance(node, dict) and node.get("id") in _rekey:
                node["id"] = _rekey[node["id"]]
        for edge in extraction.get("edges", []):
            if not isinstance(edge, dict):
                continue
            if edge.get("source") in _rekey:
                edge["source"] = _rekey[edge["source"]]
            if edge.get("target") in _rekey:
                edge["target"] = _rekey[edge["target"]]
        for he in extraction.get("hyperedges", []) or []:
            if isinstance(he, dict) and isinstance(he.get("nodes"), list):
                he["nodes"] = [_rekey.get(n, n) for n in he["nodes"]]

    # Mesclar nós de documentos nus de verificação rápida de marcação em seu gêmeo semântico `_doc`
    # para o mesmo arquivo, portanto, um documento é um nó, independentemente de qual pipeline
    # tocou por último.
    _doc_remap = _doc_twin_remap(extraction.get("nodes", []))
    if _doc_remap:
        extraction["nodes"] = [
            n for n in extraction.get("nodes", [])
            if not (isinstance(n, dict) and n.get("id") in _doc_remap)
        ]
        _new_edges = []
        for edge in extraction.get("edges", []):
            if isinstance(edge, dict):
                s0, t0 = edge.get("source"), edge.get("target")
                if s0 in _doc_remap:
                    edge["source"] = _doc_remap[s0]
                if t0 in _doc_remap:
                    edge["target"] = _doc_remap[t0]
                # Solte apenas os auto-loops, o próprio remapeamento foi recolhido (um bare->_doc
                # link se tornando doc->doc); deixe qualquer auto-loop pré-existente em paz.
                if edge.get("source") == edge.get("target") and (s0 in _doc_remap or t0 in _doc_remap):
                    continue
            _new_edges.append(edge)
        extraction["edges"] = _new_edges
        for he in extraction.get("hyperedges", []) or []:
            if isinstance(he, dict) and isinstance(he.get("nodes"), list):
                he["nodes"] = [_doc_remap.get(n, n) for n in he["nodes"]]

    G: nx.Graph = nx.DiGraph() if directed else nx.Graph()
    for node in extraction.get("nodes", []):
        # Ignore nós de dict com um ID ausente ou não hashável (por exemplo, uma lista emitida
        # por uma extração LLM com erros) para que NetworkX add_node nunca aumente
        # TypeError: tipo não lavável. Nós não-dict são deliberadamente deixados para
        # raise como antes, então os chamadores que sondam a construção em busca de erros de forma (por exemplo
        # diagnóstico multigráfico) ainda observam o formato malformado.
        if isinstance(node, dict):
            if "id" not in node:
                continue
            try:
                hash(node["id"])
            except TypeError:
                print(
                    f"[omnigraph] WARNING: skipping node with non-hashable id "
                    f"{node['id']!r} (must be a string).",
                    file=sys.stderr,
                )
                continue
            if "source_file" in node:
                node["source_file"] = _norm_source_file(node["source_file"], _root)
        G.add_node(node["id"], **{k: v for k, v in node.items() if k != "id"})
    node_set = set(G.nodes())

    # (extended): merge LLM ghost-duplicate nodes into AST canonical nodes.
    # Bug original: AST usa IDs qualificados pelos pais (mingpt_bpe_get_pairs) enquanto LLM
    # usa IDs de haste simples (bpe_get_pairs) — IDs diferentes, mesmo símbolo.
    # A correção original capturou apenas nós LLM com source_location=None; LLM agora
    # preenche source_location, então esses fantasmas sobreviveram. Correção estendida: use
    # _origin=="ast" como o sinal canônico. Os nós AST sempre vencem; qualquer não AST
    # o compartilhamento de nó (nome base, rótulo) com um nó AST é um fantasma.
    _loc_nodes: dict[tuple[str, str], str] = {}   # (source_file, label) -> canonical node id
    _loc_collisions: set[tuple[str, str]] = set()  # chaves compartilhadas por mais de 2 nós AST
    _noloc_nodes: dict[tuple[str, str], str] = {}  # (source_file, label) -> ghost node id

    # Pass 1: collect canonical nodes — AST-origin nodes take precedence over LLM nodes.
    # Quando mais de 2 nós AST compartilham uma chave (símbolos com o mesmo nome em arquivos com o mesmo nome em
    # diretórios, por ex. renderizar em dois index.ts), a chave é ambígua: mesclar um
    # o fantasma escolheria um vencedor arbitrário por meio da ordem de iteração definida. Acompanhar
    # essas chaves, então a passagem 2 as ignora - o mesmo conservadorismo de
    # _rewire_unique_stub_nodes, que só mescla quando existe exatamente um def real.
    # Itere em uma ordem determinística (classificada), não em uma ordem de iteração definida, de modo que o
    # o vencedor canônico e as decisões de ambigüidade abaixo não mudam de corrida para corrida
    # com a semente de hash de string por processo do CPython - o mesmo motivo pelo qual
    # loop de iteração de aresta mais abaixo classifica propositalmente.
    for nid in sorted(node_set):
        attrs = G.nodes[nid]
        label = str(attrs.get("label", "")).strip()
        sf = str(attrs.get("source_file", ""))
        if not label or not sf:
            continue
        is_ast = attrs.get("_origin") == "ast"
        if attrs.get("source_location") or is_ast:
            # Key on the FULL normalized source_file, not the bare basename
            #: the AST/LLM ghost twins of always share the same
            # source_file (different ids, same file), so full-path keying still
            # collapses them, while unrelated same-basename nodes in DIFFERENT
            # directories (docs/a/index.md vs docs/b/index.md) now get distinct
            # keys and are never falsely merged. This subsumes the
            # cross-file ambiguity guard, which is why the non-AST branch below
            # no longer needs it.
            key = (sf, label)
            if is_ast:
                # Two AST nodes on the same key (same file, same label) is an
                # ambiguous collision.
                if key in _loc_nodes and G.nodes[_loc_nodes[key]].get("_origin") == "ast":
                    _loc_collisions.add(key)
                # Os nós de origem AST sempre substituem uma entrada anterior não AST.
                _loc_nodes[key] = nid
            else:
                # First non-AST node for this (file, label) wins as canonical; a
                # later same-key node is a genuine same-file duplicate and still
                # collapses in Pass 2.
                _loc_nodes.setdefault(key, nid)

    # Passo 2: encontre fantasmas - nós não AST que possuem um gêmeo canônico AST.
    for nid in sorted(node_set):
        attrs = G.nodes[nid]
        if attrs.get("_origin") == "ast":
            continue  # Os nós AST nunca são fantasmas
        label = str(attrs.get("label", "")).strip()
        sf = str(attrs.get("source_file", ""))
        if not label or not sf:
            continue
        key = (sf, label)
        if key in _loc_collisions:
            continue  # chave ambígua: nenhum vencedor canônico seguro, deixe o fantasma intacto
        if key in _loc_nodes and _loc_nodes[key] != nid:
            _noloc_nodes[key] = nid
    # Para cada fantasma que possui uma contraparte AST, registre um remapeamento.
    _ghost_remap: dict[str, str] = {}  # ghost_id -> canonical_id
    for key, sem_id in _noloc_nodes.items():
        ast_id = _loc_nodes.get(key)
        if ast_id is not None:
            _ghost_remap[sem_id] = ast_id
    # Remova nós fantasmas do grafo; as arestas serão apontadas novamente via norm_to_id.
    for ghost_id in _ghost_remap:
        G.remove_node(ghost_id)
        node_set.discard(ghost_id)

    # Mapa de ID normalizado: permite que as arestas sobrevivam quando o LLM gera IDs com
    # caixa ou pontuação ligeiramente diferente do extrator AST.
    # por exemplo "Session_ValidateToken" mapeia para "session_validatetoken".
    norm_to_id: dict[str, str] = {_normalize_id(nid): nid for nid in node_set}
    # Mapeie também IDs fantasmas para suas substituições canônicas de AST.
    for ghost_id, canonical_id in _ghost_remap.items():
        norm_to_id[_normalize_id(ghost_id)] = canonical_id
        norm_to_id[ghost_id] = canonical_id
    # Índice de alias de pré-migração: registre o ID de tronco ANTIGO de cada nó canônico
    # formulários como aliases, de modo que um ponto de extremidade de ID obsoleto vindo de um não redigitado
    # fragmento (por exemplo, uma atualização incremental cujo fragmento faz referência a um símbolo em um
    # arquivo que NÃO foi extraído novamente) ainda é resolvido para o nó migrado
    # de pendurado. Apenas preenche lacunas – nunca substitui um ID de nó real.
    #
    # A forma radical antiga descarta a extensão e (para o próprio nó do arquivo) cada
    # diretório, mas o pai imediato, então ele entra em colapso facilmente: "ping.h" e
    # "ping.php" em diretórios diferentes, ambos alias para "ping". Coletando
    # cada candidato a um pseudônimo ANTES de cometer qualquer um deles - e somente
    # comprometer-se quando exatamente um candidato o reivindica - mantém isso preciso
    # ajuda de redigitação em vez de uma mesclagem silenciosa de arquivos cruzados (e idiomas cruzados).
    # Sem isso, uma vantagem pendente para um ID substituto vazio e deliberadamente sem escopo
    # (por exemplo, o destino de último recurso do extrator C/C++ para um #include que não foi possível
    # resolver para um caminho real) poderia usar esse alias em qualquer coisa não relacionada
    # mesmo arquivo-stem foi inserido primeiro em ``node_set`` — um arquivo Python
    # definido, então "primeiro" é a ordem de hash, não é nada significativo.
    #
    # O ID PRÓPRIO de um nó de arquivo nem sempre é um prefixo ``new_stem`` limpo: quando um
    # O par ``.h``/``.cpp`` do mesmo diretório colide em sua pré-extensão compartilhada
    # id, _disambiguate_colliding_node_ids salts both apart into ids like
    # ``tools_aolserver_utility_h_tools_aolserver_utility`` — que não é mais
    # string-prefixos limpos para a matemática do sufixo abaixo. Detectando "este É o
    # nó de arquivo" por rótulo (o rótulo de cada nó de arquivo é seu próprio nome de base,
    # independentemente da manipulação de id) em vez de por formato de id mantém um nó de arquivo salgado
    # na competição de alias, então uma colisão genuína (um cabeçalho C E um
    # script PHP não relacionado com o mesmo nome) ainda é considerado ambíguo em vez de
    # o cabeçalho sai silenciosamente da corrida e deixa o arquivo PHP como
    # o único (errado) vencedor "inequívoco".
    from omnigraph.extractors.base import _file_stem as _fs
    _alias_candidates: dict[str, set[str]] = {}
    for nid in node_set:
        attrs = G.nodes[nid]
        sf = attrs.get("source_file")
        if not sf:
            continue
        rel = Path(str(sf))
        if rel.is_absolute():
            continue
        new_stem = make_id(_fs(rel))
        if str(attrs.get("label", "")) == rel.name:
            suffix = ""  # este nó É o arquivo, qualquer que seja seu ID (possivelmente salgado)
        else:
            suffix = ""
            if _normalize_id(nid).startswith(new_stem):
                suffix = _normalize_id(nid)[len(new_stem):]  # liderando "_entity" ou ""
        for old_stem in _old_file_stems(rel):
            if old_stem == new_stem:
                continue
            alias = old_stem + suffix
            _alias_candidates.setdefault(_normalize_id(alias), set()).add(nid)
            _alias_candidates.setdefault(alias, set()).add(nid)
    for alias_key, candidates in _alias_candidates.items():
        if len(candidates) == 1:
            norm_to_id.setdefault(alias_key, next(iter(candidates)))
    # Itere arestas em uma ordem determinística. O grafo não é direcionado e armazena
    # direção em _src/_tgt; quando duas arestas colapsam no mesmo par de nós, o
    # a última gravação vence, portanto, uma ordem de iteração instável muda _src/_tgt de execução para execução
    # e faz com que o grafo serializado se altere. A classificação corrige o resultado da última gravação.
    for edge in sorted(
        extraction.get("edges", []),
        key=lambda e: (
            str(e.get("source", e.get("from", ""))),
            str(e.get("target", e.get("to", ""))),
            str(e.get("relation", "")),
        ),
    ):
        if "source" not in edge and "from" in edge:
            edge["source"] = edge["from"]
        if "target" not in edge and "to" in edge:
            edge["target"] = edge["to"]
        if "source" not in edge or "target" not in edge:
            continue
        src, tgt = edge["source"], edge["target"]
        # Ignorar arestas com endpoints não hasháveis ​​(por exemplo, uma lista emitida por um buggy
        # Extração LLM) para que o teste de associação `not in node_set` abaixo nunca
        # gera TypeError: tipo não lavável. O validador já relatou isso.
        try:
            hash(src)
            hash(tgt)
        except TypeError:
            print(
                f"[omnigraph] WARNING: skipping edge with non-hashable endpoint "
                f"(source={src!r}, target={tgt!r}).",
                file=sys.stderr,
            )
            continue
        # Remapeie IDs incompatíveis por meio de normalização antes de eliminar a aresta.
        if src not in node_set:
            src = norm_to_id.get(_normalize_id(src), src)
        if tgt not in node_set:
            tgt = norm_to_id.get(_normalize_id(tgt), tgt)
        if src not in node_set or tgt not in node_set:
            continue  # pular arestas para nós externos/stdlib - esperado, não é um erro
        # `target_file` is a transient import-disambiguation salt hint
        # with no downstream reader; it holds an absolute path, so it must never
        # be persisted. Disambiguation already pops it off fresh extractions —
        # dropping it here as well keeps a pre-fix graph's stale absolute hint
        # from surviving an incremental build_merge, which re-serializes base
        # edges through here without re-running disambiguation.
        # `local_alias` is the same shape of transient hint: it exists only
        # for the module arm of _resolve_python_member_calls to match an aliased
        # import receiver, and extract() already drops it once that pass has run.
        # Dropping it here too covers a stale pre-fix graph re-serialized through
        # an incremental build_merge, same rationale as target_file above.
        # Sanitize numeric edge fields: an explicit ``"weight": null`` in
        # the extraction JSON survives ``.get("weight", 1.0)`` (the key is present,
        # so the default never applies) and reaches Louvain/Leiden as None,
        # crashing modularity arithmetic with a TypeError (graspologic's Leiden
        # even panics on NaN). Coerce to float and fall back to the schema default
        # of 1.0 for anything the clustering backends reject — None, non-numeric
        # strings, NaN/inf, negatives — while numeric strings coerce cleanly.
        # Repair (not drop) the key so graph.json round-trips a clean value and a
        # cluster-only/--update reload never re-ingests the null.
        attrs = {k: v for k, v in edge.items() if k not in ("source", "target", "target_file", "local_alias")}
        for _num_key in ("weight", "confidence_score"):
            if _num_key in attrs:
                try:
                    _num_val = float(attrs[_num_key])
                except (TypeError, ValueError):
                    _num_val = 1.0
                if not math.isfinite(_num_val) or _num_val < 0:
                    _num_val = 1.0
                attrs[_num_key] = _num_val
        # Preencha source_file dos nós do terminal (cada nó carrega um).
        # As arestas semânticas/LLM ocasionalmente o omitem, o que a validação downstream
        # sinaliza e deixa os resultados da consulta sem referência de arquivo.
        if not attrs.get("source_file"):
            attrs["source_file"] = (
                G.nodes[src].get("source_file")
                or G.nodes[tgt].get("source_file")
                or ""
            )
        if "source_file" in attrs:
            attrs["source_file"] = _norm_source_file(attrs["source_file"], _root)
        # Eliminar arestas fantasmas entre idiomas — os mesmos nomes curtos (renderizar, analisar,
        # tempo, ...) ocorrem através das fronteiras linguísticas, de modo que um alvo não resolvido pode
        # vincular-se a um nó com o mesmo nome em outro idioma. A especificação de extração proíbe
        # isto para `chamadas`; é igualmente inválido para `importações`/`referências` (um
        # O `import time` do Python não deve ser vinculado a um `time.ts`).
        _edge_rel = attrs.get("relation")
        if _edge_rel in ("calls", "imports", "imports_from", "references"):
            src_ext = Path(G.nodes[src].get("source_file") or "").suffix.lower()
            tgt_ext = Path(G.nodes[tgt].get("source_file") or "").suffix.lower()
            src_fam = _EDGE_LANG_FAMILY.get(src_ext)
            tgt_fam = _EDGE_LANG_FAMILY.get(tgt_ext)
            if _edge_rel == "calls":
                # Comportamento inalterado: apenas chamadas INFERRED e descartadas como
                # assim que uma das famílias for diferente (um ramal desconhecido conta como diferente).
                if (
                    attrs.get("confidence") == "INFERRED"
                    and src_ext and tgt_ext and src_fam != tgt_fam
                ):
                    continue
            else:
                # importações/referências: descartadas apenas quando AMBOS os endpoints são códigos conhecidos
                # linguagens de famílias diferentes, então uma referência de config->código
                # (ramal desconhecido, por exemplo, um manifesto) nunca é confundido com um fantasma.
                if src_fam is not None and tgt_fam is not None and src_fam != tgt_fam:
                    continue
        # A file-level import or re-export cannot carry useful connectivity when
        # both endpoints resolve to the same node.  This most often happens when
        # the target is an unresolved bare module name (``builtins``, ``poseidon``)
        # that the legacy-ID alias index above mistakes for the importing file's
        # own old stem.  It also covers a nested module importing its parent file:
        # at file-node granularity that relationship necessarily collapses.  Keep
        # other self-edges, notably recursive ``calls``, because those are real
        # program structure rather than import-resolution artifacts.
        if src == tgt and _edge_rel in ("imports", "imports_from", "re_exports"):
            continue
        # Preservar a direção original da aresta - caso contrário, os grafos não direcionados a perderão,
        # fazendo com que as funções de exibição mostrem as arestas para trás.
        attrs["_src"] = src
        attrs["_tgt"] = tgt
        # Quando o grafo não é direcionado e o mesmo par de nós aparece duas vezes com
        # a mesma relação, mas direções opostas (por exemplo, a `chama` b e b `chama` a),
        # nx.Graph os recolhe em uma aresta. A classificação determinística acima significa
        # a direção lexicograficamente posterior substituiria sistematicamente o
        # o _src/_tgt anterior, invertendo silenciosamente o chamador da aresta sobrevivente
        # e chamado. Em vez disso, a direção vista pela primeira vez vence - abandone o redundante
        # duplique a direção reversa para que a direção original seja preservada.
        if not G.is_directed() and G.has_edge(src, tgt):
            existing = edge_data(G, src, tgt)
            if existing.get("relation") == attrs.get("relation") and (
                existing.get("_src") == tgt and existing.get("_tgt") == src
            ):
                continue
        G.add_edge(src, tgt, **attrs)
    hyperedges = extraction.get("hyperedges", [])
    if hyperedges:
        # Relativize o hyperedge source_file da mesma forma que os nós e as arestas são
        # (acima), então to_json — que não tem raiz e escreve G.graph["hyperedges"]
        # literalmente — nunca vaza um caminho absoluto de um subagente semântico.
        kept_hyperedges = []
        for he in hyperedges:
            if isinstance(he, dict) and he.get("source_file"):
                he["source_file"] = _norm_source_file(he["source_file"], _root)
            # Valide membros em relação ao conjunto de nós construídos: uma hiperarestas
            # membro ausente do grafo usado para ser copiado para
            # G.graph["hyperedges"] literalmente e alcance graph.json pendente,
            # mesmo de uma extração ao vivo (sem cache). Espelhe a aresta dos pares
            # handling above: remap mismatched ids via normalization first,
            # então descarte os membros que ainda não resolveram; abandone a hiperarestas
            # em si quando nenhum membro válido permanece (hiperarestas de membro único
            # são legais nesta base de código, por ex. um fluxo por arquivo, então podamos
            # em vez de exigir dois sobreviventes).
            if isinstance(he, dict) and isinstance(he.get("nodes"), list):
                valid_members = []
                for m in he["nodes"]:
                    try:
                        hash(m)
                    except TypeError:
                        continue
                    if m not in node_set and isinstance(m, str):
                        m = norm_to_id.get(_normalize_id(m), m)
                    if m in node_set:
                        valid_members.append(m)
                if not valid_members:
                    print(
                        f"[omnigraph] WARNING: dropping hyperedge "
                        f"{he.get('id', '?')!r} — none of its members "
                        f"{he.get('nodes')!r} match built nodes.",
                        file=sys.stderr,
                    )
                    continue
                if valid_members != he["nodes"]:
                    he["nodes"] = valid_members
            kept_hyperedges.append(he)
        if kept_hyperedges:
            G.graph["hyperedges"] = kept_hyperedges
    # Runs LAST, after the alias-competition above (which relies on file-node
    # labels still being bare basenames): give colliding-basename file nodes a
    # directory-qualified display label so lookup/discovery can disambiguate
    # them. Labels only — ids and edges are untouched.
    _disambiguate_file_node_labels(G)
    return G


def build(
    extractions: list[dict],
    *,
    directed: bool = False,
    dedup: bool = True,
    dedup_llm_backend: str | None = None,
    root: str | Path | None = None,
) -> nx.Graph:
    """Merge multiple extraction results into one graph.

    directed=True produces a DiGraph that preserves edge direction (source→target).
    directed=False (default) produces an undirected Graph for backward compatibility.
    dedup=True (default) runs entity deduplication before building the graph.
    dedup_llm_backend: if set (e.g. "gemini", "claude", or "kimi"), uses LLM to resolve
        ambiguous pairs in the 75–92 Jaro-Winkler score zone.
    root: if given, absolute source_file paths are made relative to root (#932).

    With dedup disabled, extractions are merged in order and the last node's
    attributes win (NetworkX add_node overwrites). With dedup enabled, nodes
    sharing an ID use a deterministic survivor and retain missing attributes
    from duplicate records of the same source entity. Genuine cross-file ID
    collisions remain isolated and are reported.
    """
    from omnigraph.dedup import deduplicate_entities
    combined: dict = {"nodes": [], "edges": [], "hyperedges": [], "input_tokens": 0, "output_tokens": 0}
    for ext in extractions:
        combined["nodes"].extend(ext.get("nodes", []))
        combined["edges"].extend(ext.get("edges", []))
        combined["hyperedges"].extend(ext.get("hyperedges", []))
        combined["input_tokens"] += ext.get("input_tokens", 0)
        combined["output_tokens"] += ext.get("output_tokens", 0)
    if dedup and combined["nodes"]:
        # Fold legacy node field aliases before dedup: dedup runs BEFORE
        # build_from_json and keys on `label`, so a `name`/`path` alias node
        # would be invisible to it and only label-dedup one build later, after
        # build_from_json's own fold has healed the persisted graph.json.
        for n in combined["nodes"]:
            if isinstance(n, dict):
                _fold_node_aliases(n)
        combined["nodes"], combined["edges"] = deduplicate_entities(
            combined["nodes"], combined["edges"], communities={},
            dedup_llm_backend=dedup_llm_backend,
        )
    return build_from_json(combined, directed=directed, root=root)


def _norm_label(label: str | None) -> str:
    """Canonical dedup key — Unicode-aware, preserves CJK/word characters."""
    if not isinstance(label, str):
        label = "" if label is None else str(label)
    label = unicodedata.normalize("NFKC", label)
    return re.sub(r"[\W_ ]+", " ", label.casefold(), flags=re.UNICODE).strip()


def deduplicate_by_label(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict]]:
    """Merge nodes that share a normalised label, rewriting edge references.

    Prefers IDs without chunk suffixes (_c\\d+) and shorter IDs when tied.
    Drops self-loops created by the merge.

    Dormant: this is NOT wired into ``build()`` — the active dedup path is
    ``deduplicate_entities`` (imported and called in ``build``), which supersedes
    it. The previous "Called in build() automatically" note was never true. It
    also merges by label alone with no ``file_type`` guard, so it must not be
    enabled for code nodes: same-label symbols from different files/packages
    (e.g. two ``Account`` types) would collapse into one — the cross-file
    conflation ``deduplicate_entities`` deliberately avoids for code (#1205).
    """
    _CHUNK_SUFFIX = re.compile(r"_c\d+$")
    canonical: dict[str, dict] = {}  # norm_label -> surviving node
    remap: dict[str, str] = {}       # old_id -> surviving_id

    for node in nodes:
        key = _norm_label(node.get("label", node.get("id", "")))
        if not key:
            continue
        existing = canonical.get(key)
        if existing is None:
            canonical[key] = node
        else:
            has_suffix = bool(_CHUNK_SUFFIX.search(node["id"]))
            existing_has_suffix = bool(_CHUNK_SUFFIX.search(existing["id"]))
            if has_suffix and not existing_has_suffix:
                remap[node["id"]] = existing["id"]
            elif existing_has_suffix and not has_suffix:
                remap[existing["id"]] = node["id"]
                canonical[key] = node
            elif len(node["id"]) < len(existing["id"]):
                remap[existing["id"]] = node["id"]
                canonical[key] = node
            else:
                remap[node["id"]] = existing["id"]

    if not remap:
        return nodes, edges

    print(f"[omnigraph] Deduplicated {len(remap)} duplicate node(s) by label.", file=sys.stderr)
    deduped_nodes = list(canonical.values())
    deduped_edges = []
    for edge in edges:
        e = dict(edge)
        e["source"] = remap.get(e["source"], e["source"])
        e["target"] = remap.get(e["target"], e["target"])
        if e["source"] != e["target"]:
            deduped_edges.append(e)
    return deduped_nodes, deduped_edges


def _load_existing_graph(graph_path: Path) -> "tuple[list, list, list] | None":
    """Load (nodes, edges, hyperedges) from an existing graph.json for an
    incremental merge, accepting both the ``links`` and ``edges`` spellings.

    Reads the JSON directly instead of going through node_link_graph().
    The latter rebuilds an undirected nx.Graph and then enumerating
    edges() yields endpoints based on node insertion order, which
    silently flips directional edges (e.g. `calls`) when the callee
    was inserted before the caller. The _src/_tgt direction-preserving
    attrs are popped before saving in export.py, so going through the
    NetworkX round-trip loses direction permanently (#760).

    Returns None when the file does not exist. Raises RuntimeError when it
    exists but cannot be parsed — callers must refuse to overwrite rather
    than silently replace a possibly-recoverable graph.
    """
    if not graph_path.exists():
        return None
    from omnigraph.security import check_graph_file_size_cap
    check_graph_file_size_cap(graph_path)
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            f"Cannot read {graph_path} for incremental merge: {exc}. "
            "Delete the file and run a full rebuild."
        ) from exc
    links_key = "links" if "links" in data else "edges"
    return (
        list(data.get("nodes", [])),
        list(data.get(links_key, [])),
        list(data.get("hyperedges", [])),
    )


def merge_raw_extraction(
    new: dict,
    graph_path: str | Path,
    prune_sources: "list[str] | None" = None,
    root: "str | Path | None" = None,
) -> dict:
    """Merge the existing raw graph.json forward into a fresh raw extraction
    (the ``extract --no-cluster`` incremental path, #2169).

    Replace/prune semantics mirror :func:`build_merge` exactly, so the raw and
    clustered incremental paths can't drift:

    - sources re-extracted this run REPLACE their prior contribution — existing
      nodes/edges/hyperedges owned by them are dropped, matched in both raw and
      :func:`_norm_source_file` form (#1007);
    - ``prune_sources`` (deleted / excluded / graph-stale files) are dropped,
      with the ``_abs_identity`` third-form fallback (#2012), and "replace" wins
      over a contradictory "delete" of a re-extracted source (#1796);
    - everything else — nodes/edges/hyperedges owned by unchanged files — is
      carried forward unchanged.

    Survivors are PREPENDED to ``new``'s lists (existing-first), so the caller's
    ``dedupe_nodes`` last-writer-wins keeps fresh attributes for re-extracted
    nodes while ``dedupe_edges`` first-wins never resurrects a replaced edge
    (replaced sources' edges were already dropped above). Token counters and
    every other key of ``new`` are left untouched. Returns ``new``, mutated in
    place. Raises RuntimeError (via :func:`_load_existing_graph`) when the
    existing graph is present but unparseable — the caller must refuse to
    overwrite it. No-op when ``graph_path`` does not exist.
    """
    graph_path = Path(graph_path)
    loaded = _load_existing_graph(graph_path)
    if loaded is None:
        return new
    existing_nodes, existing_edges, existing_hyperedges = loaded

    _eff_root = (
        str(Path(root).resolve()) if root is not None
        else _infer_merge_root(graph_path)
    )

    new_sources: set[str] = set()
    for n in new.get("nodes", []):
        if not isinstance(n, dict):
            continue
        sf = n.get("source_file")
        if not sf:
            continue
        new_sources.add(sf)
        norm = _norm_source_file(sf, _eff_root)
        if norm:
            new_sources.add(norm)

    prune_set: set[str] = set()
    prune_abs: set[str] = set()
    for p in (prune_sources or []):
        if not p:
            continue
        prune_set.add(p)
        norm = _norm_source_file(p, _eff_root)
        if norm:
            prune_set.add(norm)
        a = _abs_identity(p, _eff_root)
        if a:
            prune_abs.add(a)
    # "Replace" wins over a contradictory "delete" of the same source,
    # in both string and absolute-identity space — as in build_merge.
    prune_set -= new_sources
    new_abs = {_abs_identity(s, _eff_root) for s in new_sources}
    new_abs.discard(None)
    prune_abs -= new_abs

    def _dropped(item: dict) -> bool:
        if not isinstance(item, dict):
            return True
        sf = item.get("source_file")
        if sf in new_sources or _norm_source_file(sf, _eff_root) in new_sources:
            return True  # re-extracted this run — replaced by the new chunk
        if not sf:
            return False  # unowned — carry forward
        if sf in prune_set:
            return True
        norm = _norm_source_file(sf, _eff_root)
        if norm and norm in prune_set:
            return True
        a = _abs_identity(sf, _eff_root)
        return bool(a) and a in prune_abs

    new["nodes"] = [n for n in existing_nodes if not _dropped(n)] + list(new.get("nodes", []))
    new["edges"] = [e for e in existing_edges if not _dropped(e)] + list(new.get("edges", []))
    carried_hyper = [he for he in existing_hyperedges if not _dropped(he)]
    if carried_hyper or new.get("hyperedges"):
        new["hyperedges"] = carried_hyper + list(new.get("hyperedges", []))
    return new


def build_merge(
    new_chunks: list[dict],
    graph_path: str | Path | None = None,
    prune_sources: list[str] | None = None,
    *,
    directed: bool = False,
    dedup: bool = True,
    dedup_llm_backend: str | None = None,
    root: str | Path | None = None,
) -> nx.Graph:
    """Load existing graph.json, merge new chunks into it, and save back.

    Re-extracted files REPLACE their prior contribution: any source_file present
    in new_chunks is dropped from the loaded graph before merging, so a changed
    file's stale nodes/edges don't accumulate. Files absent from new_chunks are
    preserved unchanged; deleted files are removed via prune_sources.
    Safe to call repeatedly.
    root: if given, absolute source_file paths in new_chunks are made relative (#932).
    """
    graph_path = Path(graph_path if graph_path is not None else _default_graph_json())
    _loaded = _load_existing_graph(graph_path)
    if _loaded is not None:
        existing_nodes, existing_edges, existing_hyperedges = _loaded
        had_graph = True
    else:
        existing_nodes = []
        existing_edges = []
        existing_hyperedges = []
        had_graph = False

    # Raiz efetiva para relativizar caminhos absolutos de source_file / prune de volta ao
    # chaves relativas source_file armazenadas. Quando o chamador passa pelo root, nós o usamos;
    # caso contrário, retorne à raiz de varredura registrada do grafo, tão absoluta
    # prune_sources e caminhos de novos pedaços ainda correspondem mesmo quando um chamador omite root
    # (— o runbook --update da habilidade chama build_merge sem root, então
    # caminhos absolutos de arquivos excluídos nunca corresponderam às chaves de nó relativas e seus
    # nós sobreviveram como fantasmas).
    _eff_root = (
        str(Path(root).resolve()) if root is not None
        else _infer_merge_root(graph_path)
    )

    # Arquivos reextraídos SUBSTITUEM sua contribuição anterior. Cada source_file
    # presente em new_chunks é eliminado da base carregada antes da fusão, portanto, um
    # CHANGED file's stale nodes/edges don't accumulate across incremental
    # atualizações. Sem isso, build() mescla antigo+novo para o mesmo arquivo e apenas
    # colapso de arestas duplicadas exatas — arestas/nós que desapareceram do novo
    # versão sobreviverá para sempre. Arquivos novos não estão na base, então este é um ambiente autônomo
    # para eles; arquivos genuinamente excluídos ainda são tratados via prune_sources.
    # Correspondido na forma bruta e _norm_source_file porque new_chunks pode carregar
    # caminhos win32 absolutos enquanto o grafo armazenado mantém posix relativo.
    _replace_root = _eff_root
    new_sources: set[str] = set()
    for ch in new_chunks:
        for n in ch.get("nodes", []):
            sf = n.get("source_file")
            if not sf:
                continue
            new_sources.add(sf)
            norm = _norm_source_file(sf, _replace_root)
            if norm:
                new_sources.add(norm)
    if new_sources:
        def _kept(item: dict) -> bool:
            sf = item.get("source_file")
            return sf not in new_sources and _norm_source_file(sf, _replace_root) not in new_sources
        existing_nodes = [n for n in existing_nodes if _kept(n)]
        existing_edges = [e for e in existing_edges if _kept(e)]

    base = [{"nodes": existing_nodes, "edges": existing_edges}] if had_graph else []

    all_chunks = base + list(new_chunks)
    G = build(all_chunks, directed=directed, dedup=dedup, dedup_llm_backend=dedup_llm_backend, root=root)

    # Conjunto de remoção para arquivos de origem excluídos - tanto na forma bruta (corresponde aos nós que
    # mantido absoluto source_file) e a forma relativa normalizada (corresponde aos nós
    # relativizado por _norm_source_file em tempo de construção). .resolve() (via _eff_root)
    # lida com raízes simbólicas e segmentos ".." / "./" então Path.relative_to()
    # é bem-sucedido mesmo quando a raiz da varredura é um link simbólico.
    prune_set: set[str] = set()
    prune_abs: set[str] = set()
    for p in (prune_sources or []):
        if not p:
            continue
        prune_set.add(p)
        norm = _norm_source_file(p, _eff_root)
        if norm:
            prune_set.add(norm)
        a = _abs_identity(p, _eff_root)
        if a:
            prune_abs.add(a)
    # Um arquivo que acabou de ser extraído novamente (presente em new_chunks) está sendo SUBSTITUÍDO,
    # nunca excluído - portanto, nunca remova-o, mesmo que o chamador também o liste
    # prune_sources. Caso contrário, seus nós novos e recém-construídos serão removidos silenciosamente
    # (perda de dados): comum quando uma edição mantém o rótulo de um nó e o chamador segue
    # o antigo fluxo de trabalho de edição de passar o arquivo alterado em prune_sources.
    # "replace" wins over a contradictory "delete" of the same source. Applied in
    # both string and absolute-identity space so the third-form fallback below
    # can't resurrect the delete for a re-extracted file.
    prune_set -= new_sources
    new_abs = {_abs_identity(s, _eff_root) for s in new_sources}
    new_abs.discard(None)
    prune_abs -= new_abs

    def _prune_match(sf: "str | None") -> bool:
        # Match a node/edge/hyperedge source_file against the prune set in a
        # form-insensitive way: exact string, normalised-relative, then the
        # absolute-identity fallback for the third-form case.
        if not sf:
            return False
        if sf in prune_set:
            return True
        norm = _norm_source_file(sf, _eff_root)
        if norm and norm in prune_set:
            return True
        a = _abs_identity(sf, _eff_root)
        return bool(a) and a in prune_abs

    # Transportar hiperarestas de arquivos que não foram reextraídos nem
    # excluído. build() vê apenas as hiperarestas dos novos pedaços, portanto, sem
    # isso every --update recolhe a hiperarestas do grafo definida apenas para o
    # arquivos alterados'. As hiperarestas anteriores dos arquivos reextraídos são eliminadas (suas novas
    # a versão já está em G — substituição por fonte, como nós/arestas); excluído
    # arquivos' são descartados via prune_set. id-dedup (attach_hyperedges) então é transportado
    # o hyperedge nunca duplica um dos novos pedaços reemitidos. Espelhos watch.py,
    # que já preserva as hiperarestas existentes em uma reconstrução.
    if existing_hyperedges:
        carried = []
        for he in existing_hyperedges:
            if not isinstance(he, dict):
                continue
            sf = he.get("source_file")
            norm = _norm_source_file(sf, _eff_root)
            if sf in new_sources or norm in new_sources:
                continue  # reextraído - substituído pela versão do novo pedaço
            if _prune_match(sf):
                continue  # deleted — pruned
            carried.append(he)
        if carried:
            from omnigraph.export import attach_hyperedges
            attach_hyperedges(G, carried)

    # Remover nós e arestas de arquivos de origem excluídos
    if prune_sources:
        to_remove = [
            n for n, d in G.nodes(data=True)
            if _prune_match(d.get("source_file"))
        ]
        G.remove_nodes_from(to_remove)
        n_files = len(prune_sources)
        n_nodes = len(to_remove)
        if n_nodes:
            print(
                f"[omnigraph] Pruned {n_nodes} node(s) from {n_files} deleted or "
                f"excluded source file(s).",
                file=sys.stderr,
            )

        edges_to_remove = [
            (u, v) for u, v, d in G.edges(data=True)
            if _prune_match(d.get("source_file"))
        ]
        if edges_to_remove:
            G.remove_edges_from(edges_to_remove)
            print(
                f"[omnigraph] Pruned {len(edges_to_remove)} edge(s) from deleted or "
                f"excluded source file(s).",
                file=sys.stderr,
            )

        if not n_nodes and not edges_to_remove:
            print(
                f"[omnigraph] {n_files} source file(s) deleted or excluded since "
                f"last run — no matching nodes or edges in graph, already clean.",
                file=sys.stderr,
            )

    # Verificação de segurança: recuse-se a reduzir o grafo silenciosamente
    # Ignore quando dedup ou prune_sources estiver ativo – a redução é intencional aí.
    if graph_path.exists() and not dedup and not prune_sources:
        existing_n = len(existing_nodes)
        new_n = G.number_of_nodes()
        if new_n < existing_n:
            raise ValueError(
                f"omnigraph: build_merge would shrink graph from {existing_n} → {new_n} nodes. "
                f"Pass prune_sources explicitly if you intend to remove nodes."
            )

    return G


def prefix_graph_for_global(G: nx.Graph, repo_tag: str) -> nx.Graph:
    """Return a copy of G with all node IDs prefixed with repo_tag::.

    Labels are preserved unchanged (for display). A 'local_id' attribute
    is added to each node so the original ID can be recovered. Edges and
    their directional attributes (_src/_tgt) are rewritten to match the new
    prefixed IDs. The 'repo' attribute is set on every node.
    """
    relabel = {n: f"{repo_tag}::{n}" for n in G.nodes}
    H = nx.relabel_nodes(G, relabel, copy=True)
    for node, data in H.nodes(data=True):
        data["repo"] = repo_tag
        data.setdefault("local_id", node.split("::", 1)[1])
    for u, v, data in H.edges(data=True):
        if "_src" in data and data["_src"] in relabel:
            data["_src"] = relabel[data["_src"]]
        if "_tgt" in data and data["_tgt"] in relabel:
            data["_tgt"] = relabel[data["_tgt"]]
    return H


def distinct_repo_tags(graph_paths: "list[Path]") -> "list[str]":
    """Return a unique, human-meaningful repo tag per input graph for merge-graphs.

    The naive tag (the ``omnigraph-out`` parent dir name) is NOT unique across
    inputs: ``src/omnigraph-out`` and ``frontend/src/omnigraph-out`` both yield
    ``src``. Prefixing both node sets with ``src::`` then makes same-stem nodes
    (a backend ``src/app.js`` and a frontend ``App.jsx``, both bare ``app``)
    collide, so ``nx.compose`` silently merges two unrelated entities and invents
    cross-runtime edges (#1729). Colliding tags are widened with their own parent
    dir (``frontend_src``), then an index suffix guarantees uniqueness so no two
    graphs ever share a prefix.
    """
    repo_dirs = [p.parent.parent for p in graph_paths]  # omnigraph-out/.. → repo dir
    tags = [d.name or "repo" for d in repo_dirs]
    if len(set(tags)) != len(tags):
        widened: list[str] = []
        for d in repo_dirs:
            parent = d.parent.name
            widened.append(f"{parent}_{d.name}" if parent and d.name else (d.name or "repo"))
        tags = widened
    seen: dict[str, int] = {}
    unique: list[str] = []
    for t in tags:
        seen[t] = seen.get(t, 0) + 1
        unique.append(t if seen[t] == 1 else f"{t}-{seen[t]}")
    return unique


def prune_repo_from_graph(G: nx.Graph, repo_tag: str) -> int:
    """Remove all nodes tagged with repo_tag from G in-place. Returns count removed."""
    to_remove = [n for n, d in G.nodes(data=True) if d.get("repo") == repo_tag]
    G.remove_nodes_from(to_remove)
    return len(to_remove)
