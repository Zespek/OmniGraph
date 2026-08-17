"""Markdown extractor. Moved verbatim from omnigraph/extract.py."""
from __future__ import annotations

import re
import os

from pathlib import Path
from omnigraph.extractors.base import _file_stem, _make_id
from omnigraph.security import sanitize_metadata


_MD_INLINE_LINK_RE = re.compile(r'(?<!\!)\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+[^)]*)?\)')

_MD_REF_DEF_RE = re.compile(r'^\s{0,3}\[[^\]]+\]:\s*<?([^\s>]+)>?')

_MD_WIKILINK_RE = re.compile(r'(?<!\!)\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]')

_MD_LINKABLE_EXTS = {".md", ".mdx", ".qmd", ".markdown", ".rst", ".txt"}

_MD_FRONTMATTER_CLOSE = ("---", "...")
_MD_FRONTMATTER_MAX_LINES = 200

_MD_FM_SCALAR_RE = re.compile(r'^([A-Za-z0-9_][A-Za-z0-9_\-. ]*):\s*(.*)$')


def _split_frontmatter(lines: list[str]) -> tuple[list[str], int]:
    """Split leading YAML frontmatter off *lines*.

    Returns ``(frontmatter_lines, body_start_index)``. When the file has no
    frontmatter — the common case — returns ``([], 0)`` and the caller parses
    from line 0 exactly as before.
    """
    if not lines or lines[0].strip() != "---":
        return [], 0
    limit = min(len(lines), _MD_FRONTMATTER_MAX_LINES + 1)
    for i in range(1, limit):
        if lines[i].strip() in _MD_FRONTMATTER_CLOSE:
            return lines[1:i], i + 1
    return [], 0


def _parse_frontmatter(fm_lines: list[str]) -> dict:
    """Parse frontmatter lines into a plain dict.

    Values are passed through ``sanitize_metadata`` by the caller, so nested
    dicts and lists survive (a review workflow's ``coherence_check:`` block,
    Obsidian ``aliases:``) while staying bounded and HTML-safe.
    """
    if not fm_lines:
        return {}
    text = "\n".join(fm_lines)
    try:
        import yaml
    except ImportError:
        return _parse_frontmatter_fallback(fm_lines)
    try:
        data = yaml.safe_load(text)
    except Exception:
        return _parse_frontmatter_fallback(fm_lines)
    return data if isinstance(data, dict) else {}


def _parse_frontmatter_fallback(fm_lines: list[str]) -> dict:
    """Flat `key: value` parser for when PyYAML is not installed.

    Nested blocks and list items are skipped rather than guessed at; the keys
    that matter for graph filtering (``type``, ``review_status``, ``title``)
    are flat scalars in practice.
    """
    out: dict = {}
    for raw in fm_lines:
        if not raw[:1].strip():
            continue
        m = _MD_FM_SCALAR_RE.match(raw.strip())
        if not m:
            continue
        key, value = m.group(1).strip(), m.group(2).strip()
        if not value:
            continue
        out[key] = value.strip('"\'')
    return out

def _resolve_markdown_link(raw: str, source_dir: Path) -> "Path | None":
    """Resolve a markdown link target to the absolute path of a sibling document.

    Returns the resolved (normalized, not necessarily existing) path when the
    target is a *local* relative/absolute file-path link to a document, or None
    when it should be skipped: external URLs (http/https/mailto/protocol-
    relative/data), pure in-page anchors (``#section``), and links to non-doc
    file types (code/assets are handled by their own extractors).

    The anchor fragment (``#section``) and query (``?x=1``) are stripped before
    resolution so ``./repo.md#setup`` resolves to the same node as ``./repo.md``.
    Extension-less targets (typical of wikilinks) are treated as sibling ``.md``.
    """
    target = raw.strip()
    if not target:
        return None
    # Solte a âncora/consulta para que os links #section ainda resolvam para o documento de destino.
    target = target.split("#", 1)[0].split("?", 1)[0].strip()
    if not target:
        return None
    low = target.lower()
    if "://" in target or low.startswith(("mailto:", "tel:", "//", "data:")):
        return None
    suffix = Path(target).suffix.lower()
    if suffix == "":
        target = target + ".md"
        suffix = ".md"
    if suffix not in _MD_LINKABLE_EXTS:
        return None
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = source_dir / candidate
    return Path(os.path.normpath(str(candidate)))

def extract_markdown(path: Path) -> dict:
    """Extract structural nodes and edges from a Markdown file.

    Produces nodes for:
    - The file itself, tagged ``node_kind: "page"``, carrying any YAML
      frontmatter under ``frontmatter``
    - Each heading (# / ## / ### etc.), tagged ``node_kind: "heading"``

    ``node_kind`` exists because ``file_type`` cannot carry this distinction:
    it is a closed enum (build.py rewrites anything outside
    ``code|document|paper|image|rationale|concept`` to ``"concept"``) and
    ``"document"`` on both endpoints is load-bearing for the twin-merge pass.
    Without a separate field, headings — typically the large majority of nodes
    in a docs-heavy corpus — cannot be filtered out by a consumer.

    Produces edges for:
    - file --contains--> heading
    - parent heading --contains--> child heading (nesting by level)
    - heading --references--> other node (when backtick `Name` matches a known pattern)
    - file --references--> linked document, for inline ``[text](./other.md)``,
      reference-style ``[label]: ./other.md`` and ``[[wikilink]]`` links, so a
      hub doc (``index.md`` / ``table-of-contents.md``) becomes a real hub node
      instead of an under-connected orphan (#1376). The target node ID is built
      from the resolved target path with the same recipe as the target file's
      own node, so the edge merges into that node (no ghost node). External
      URLs, in-page anchors, images and non-document targets are skipped.

    Fenced code blocks (``` ... ```) are skipped during parsing so their
    contents don't get treated as headings, but no node is emitted for
    them — they were always orphans (only a single contains edge to the
    parent doc) and inflated the disconnected-component count (#1077).

    Leading YAML frontmatter is parsed onto the page node and excluded from
    heading detection (a `#` there is a YAML comment). Links inside it are
    still followed: review workflows keep wikilinks in frontmatter and those
    are genuine references.

    No tree-sitter dependency — pure line-by-line parsing.
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()

    def add_node(nid: str, label: str, line: int, file_type: str = "document",
                 node_kind: str = "heading", extra: "dict | None" = None) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            node = {"id": nid, "label": label, "file_type": file_type,
                    "node_kind": node_kind,
                    "source_file": str_path, "source_location": f"L{line}"}
            if extra:
                node.update(extra)
            nodes.append(node)

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0,
                 target_file: "str | None" = None) -> None:
        edge = {"source": src, "target": tgt, "relation": relation,
                "confidence": confidence, "source_file": str_path,
                "source_location": f"L{line}", "weight": weight}
        if target_file is not None:
            edge["target_file"] = target_file
        edges.append(edge)

    lines = source.splitlines()
    fm_lines, body_start = _split_frontmatter(lines)
    frontmatter = sanitize_metadata(_parse_frontmatter(fm_lines))

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1, node_kind="page",
             extra={"frontmatter": frontmatter} if frontmatter else None)

    source_dir = path.parent
    # Deduplicar arestas do link por nó de destino resolvido para que um documento de hub vinculado ao
    # o mesmo irmão muitas vezes produz uma aresta, não N (mantém os pesos significativos).
    linked_targets: set[str] = set()

    def add_link(raw: str, line: int) -> None:
        resolved = _resolve_markdown_link(raw, source_dir)
        if resolved is None:
            return
        # Crie o ID de destino com a MESMA receita do próprio arquivo de destino
        # nó (_make_id(str(path)) no momento da extração, canonizado para
        # _file_node_id(rel) pelo extract() pós-passagem). Usando o absoluto
        # caminho resolvido significa que ambos os endpoints são remapeados de forma idêntica, então o
        # edge se funde com o nó de documento existente em vez de gerar um fantasma.
        tgt_nid = _make_id(str(resolved))
        if tgt_nid == file_nid or tgt_nid in linked_targets:
            return
        linked_targets.add(tgt_nid)
        target_file = None
        try:
            if resolved.is_file():
                target_file = str(resolved)
        except OSError:
            pass
        add_edge(file_nid, tgt_nid, "references", line, target_file=target_file)

    # Pilha de cabeçalhos de rastreamento para aninhamento: [(nível, nid), ...]
    heading_stack: list[tuple[int, str]] = []
    in_code_block = False

    for line_num_0, line_text in enumerate(lines):
        line_num = line_num_0 + 1

        # Ignore os blocos de código protegidos para que seu conteúdo não seja analisado como
        # títulos, mas não emite nós/arestas para eles.
        stripped = line_text.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue

        if in_code_block:
            continue

        # Links Markdown -> referências de documentos. Digitalizado em cada
        # linha não vedada (incluindo linhas de rumo, que o ramal de rumo
        for m in _MD_INLINE_LINK_RE.finditer(line_text):
            add_link(m.group(1), line_num)
        for m in _MD_WIKILINK_RE.finditer(line_text):
            add_link(m.group(1), line_num)
        ref_def = _MD_REF_DEF_RE.match(line_text)
        if ref_def:
            add_link(ref_def.group(1), line_num)

        if line_num_0 < body_start:
            continue

        heading_match = re.match(r'^(#{1,6})\s+(.+)', line_text)
        if heading_match:
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            h_nid = _make_id(stem, title)
            # Evite IDs de cabeçalho duplicados anexando o número da linha
            if h_nid in seen_ids:
                h_nid = _make_id(stem, title, str(line_num))
            add_node(h_nid, title, line_num)

            # Títulos pop no mesmo nível ou em nível mais profundo
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()

            # Conectar-se ao título ou arquivo pai
            parent = heading_stack[-1][1] if heading_stack else file_nid
            add_edge(parent, h_nid, "contains", line_num)

            heading_stack.append((level, h_nid))
            continue

    return {"nodes": nodes, "edges": edges, "input_tokens": 0, "output_tokens": 0}
