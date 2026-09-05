# Exportação de Wiki - artigos de redução no estilo Wikipedia do grafo de conhecimento
# Gera um wiki rastreável pelo agente: index.md + um artigo por comunidade + artigos do god node
from __future__ import annotations
from collections import Counter
from pathlib import Path
import re
import networkx as nx

from omnigraph.build import edge_data
from omnigraph.paths import stem_filename_budget

# Room _unique_slug needs for the collision suffix ("_2" … "_9999") it appends
# after _safe_filename has already capped the slug. The suffix is technically
# unbounded, but 5 chars ("_" + 4 digits) covers ~10k identical stems, far past
# anything real; sizing it to 3 digits let a 1000th collision overrun MAX_PATH.
_SLUG_SUFFIX_RESERVE = 5

# Characters a slug may not contain, because the article's LINK and its ON-DISK
# NAME have to be the same string. Anything left here must be legal,
# unescaped, in a CommonMark link destination:
#   < > : " / \ | ? *   Windows-reserved in filenames (pre-existing set)
#   ( )                 parentheses delimit/nest a link destination
#   #                   starts a fragment, so `a#b.md` resolves to the file `a`
#   %                   reads as the start of a percent-escape
#   control chars       forbidden in a link destination, hostile in a filename
#
# Non-ASCII is deliberately NOT stripped. It is legal raw in a link destination
# and resolves fine on every filesystem omnigraph targets; stripping it would
# reduce a CJK, Cyrillic or accented wiki to a wall of underscores.
_UNSAFE_SLUG_CHARS = re.compile(r'[<>:"/\\|?*#%\x00-\x1f\x7f]')


def _safe_filename(name: str, limit: int = 200) -> str:
    """Make a label safe for use as a filename across platforms AND as a
    markdown link destination.

    Substitutes characters that Windows reserves in filenames
    (< > : " / \\ | ? *) plus the ones that would make the emitted link stop
    matching the file on disk, and strips trailing dots/spaces, also reserved.
    Falls back to 'unnamed' for empty results and caps length at ``limit``
    chars (default 200) to stay well under common filesystem limits; ``to_wiki``
    lowers ``limit`` when the wiki directory leaves less than that inside
    Windows' MAX_PATH window (#2655).

    Parentheses are DROPPED rather than substituted: every callable node is
    labelled ``foo()``, and substituting would leave a trailing ``foo__`` on
    each of them — and would mangle Python dunders (``__init__()`` ->
    ``_init_``) if the resulting runs were then collapsed. Dropping keeps
    ``__init__`` intact. Two labels that collapse to one slug are still
    separated by ``_unique_slug``.
    """
    s = name.replace("/", "-").replace(" ", "_").replace(":", "-")
    s = s.replace("(", "").replace(")", "")
    s = _UNSAFE_SLUG_CHARS.sub('_', s)
    s = s.strip('. ')
    return s[:limit] if s else 'unnamed'


def _md_link(label: str, resolver: dict[str, str]) -> str:
    """Render a link to another wiki article as a portable relative markdown link.

    ``resolver`` maps an article's display label to the slug (filename stem) it
    was written under. When the label has an article, emit a standard
    ``[label](slug.md)`` link whose target is the on-disk name VERBATIM.

    The target is deliberately not percent-encoded (#2597). ``quote()`` turned
    ``_make_id().md`` into ``_make_id%28%29.md`` while the file stayed raw, so
    the link pointed at a path that does not exist. Renderers hid it by
    decoding before resolving, but the wiki's whole purpose is to be
    agent-crawlable, and an agent that reads the target off disk verbatim got a
    FileNotFoundError. ``_safe_filename`` now keeps the slug free of everything
    that would need encoding, so raw emission and the filename are the same
    string by construction — one source of truth instead of two spellings that
    happened to agree only for URL-safe labels.

    The old ``[[label]]`` form only resolved inside Obsidian, because the
    on-disk filename differs from the label — _safe_filename turns spaces into
    underscores and substitutes reserved characters — so e.g.
    ``[[Domain Data Models]]`` pointed at a non-existent
    ``Domain Data Models.md`` everywhere else.

    Labels with no article — most node-level links, since only communities and
    god nodes get article files — render as plain text instead of a dead link
    that points nowhere even inside Obsidian.
    """
    text = label.replace("[", r"\[").replace("]", r"\]")
    slug = resolver.get(label)
    if slug is None:
        return text
    return f"[{text}]({slug}.md)"


def _cross_community_links(G: nx.Graph, nodes: list[str], own_cid: int, labels: dict[int, str], node_community: dict[str, int]) -> list[tuple[str, int]]:
    """Return (community_label, edge_count) pairs for cross-community connections, sorted descending."""
    counts: dict[str, int] = Counter()
    for nid in nodes:
        for neighbor in G.neighbors(nid):
            ncid = node_community.get(neighbor)
            if ncid is not None and ncid != own_cid:
                counts[labels.get(ncid, f"Community {ncid}")] += 1
    return sorted(counts.items(), key=lambda x: -x[1])


def _community_article(
    G: nx.Graph,
    cid: int,
    nodes: list[str],
    label: str,
    labels: dict[int, str],
    cohesion: float | None,
    node_community: dict[str, int] | None = None,
    resolver: dict[str, str] | None = None,
) -> str:
    resolver = resolver or {}
    top_nodes = sorted(nodes, key=lambda n: G.degree(n), reverse=True)[:25]
    cross = _cross_community_links(G, nodes, cid, labels, node_community or {})

    # Edge confidence breakdown, over every edge INCIDENT to the community (the
    # cross-community ones included — those are disproportionately the uncertain
    # edges, and AMBIGUOUS is what ARCHITECTURE.md flags for human review).
    #
    # ``G.edges(nbunch)`` reports each incident edge exactly once. Walking
    # ``nodes x G.neighbors`` instead visited an edge once per endpoint inside
    # the community, so an intra-community edge was counted TWICE while a
    # crossing one was counted once. Intra-community edges are overwhelmingly
    # the high-confidence EXTRACTED ones — that is what makes a community — so
    # the split was biased towards confidence and understated the review
    # burden. On a MultiGraph this also counts parallel edges individually
    # rather than collapsing them to the first (``edge_data``), which is the
    # same understatement: an AMBIGUOUS edge parallel to an EXTRACTED one used
    # to be invisible here.
    conf_counts: Counter = Counter(
        d.get("confidence", "EXTRACTED") for *_, d in G.edges(nodes, data=True)
    )
    total_edges = sum(conf_counts.values()) or 1

    sources = sorted({G.nodes[n].get("source_file") or "" for n in nodes} - {""})

    lines: list[str] = []
    lines += [f"# {label}", ""]

    meta_parts = [f"{len(nodes)} nodes"]
    if cohesion is not None:
        meta_parts.append(f"cohesion {cohesion:.2f}")
    lines += [f"> {' · '.join(meta_parts)}", ""]

    lines += ["## Key Concepts", ""]
    for nid in top_nodes:
        d = G.nodes[nid]
        node_label = d.get("label", nid)
        src = d.get("source_file", "")
        degree = G.degree(nid)
        src_str = f" — `{src}`" if src else ""
        lines.append(f"- **{node_label}** ({degree} connections){src_str}")
    remaining = len(nodes) - len(top_nodes)
    if remaining > 0:
        lines.append(f"- *... and {remaining} more nodes in this community*")
    lines.append("")

    lines += ["## Relationships", ""]
    if cross:
        for other_label, count in cross[:12]:
            lines.append(f"- {_md_link(other_label, resolver)} ({count} shared connections)")
    else:
        lines.append("- No strong cross-community connections detected")
    lines.append("")

    if sources:
        lines += ["## Source Files", ""]
        for src in sources[:20]:
            lines.append(f"- `{src}`")
        lines.append("")

    lines += ["## Audit Trail", ""]
    for conf in ("EXTRACTED", "INFERRED", "AMBIGUOUS"):
        n = conf_counts.get(conf, 0)
        pct = round(n / total_edges * 100)
        lines.append(f"- {conf}: {n} ({pct}%)")
    lines.append("")

    lines += ["---", "", f"*Part of the omnigraph knowledge wiki. See {_md_link('index', resolver)} to navigate.*"]
    return "\n".join(lines)


def _god_node_article(G: nx.Graph, nid: str, labels: dict[int, str], node_community: dict[str, int] | None = None, resolver: dict[str, str] | None = None) -> str:
    resolver = resolver or {}
    d = G.nodes[nid]
    node_label = d.get("label", nid)
    src = d.get("source_file", "")
    cid = (node_community or {}).get(nid)
    community_name = labels.get(cid, f"Community {cid}") if cid is not None else None

    lines: list[str] = []
    lines += [f"# {node_label}", ""]
    lines += [f"> God node · {G.degree(nid)} connections · `{src}`", ""]

    if community_name:
        lines += [f"**Community:** {_md_link(community_name, resolver)}", ""]

    # Agrupar vizinhos por tipo de relação
    by_relation: dict[str, list[str]] = {}
    for neighbor in sorted(G.neighbors(nid), key=lambda n: G.degree(n), reverse=True):
        nd = G.nodes[neighbor]
        ed = edge_data(G, nid, neighbor)
        rel = ed.get("relation", "related")
        neighbor_label = nd.get("label", neighbor)
        conf = ed.get("confidence", "")
        conf_str = f" `{conf}`" if conf else ""
        by_relation.setdefault(rel, []).append(f"{_md_link(neighbor_label, resolver)}{conf_str}")

    lines += ["## Connections by Relation", ""]
    for rel, targets in sorted(by_relation.items()):
        lines.append(f"### {rel}")
        for t in targets[:20]:
            lines.append(f"- {t}")
        # The cap keeps god-node articles readable, but silently dropping the
        # tail made the body disagree with the header's degree count with
        # nothing telling the reader anything was cut. Entries are
        # degree-sorted, so what is hidden is the low-degree tail.
        if len(targets) > 20:
            lines.append(
                f"- *…and {len(targets) - 20} more `{rel}` "
                f"connection(s) not listed (lowest-degree first to go)*"
            )
        lines.append("")

    lines += ["---", "", f"*Part of the omnigraph knowledge wiki. See {_md_link('index', resolver)} to navigate.*"]
    return "\n".join(lines)


def _index_md(
    communities: dict[int, list[str]],
    labels: dict[int, str],
    god_nodes_data: list[dict],
    total_nodes: int,
    total_edges: int,
    resolver: dict[str, str] | None = None,
) -> str:
    resolver = resolver or {}
    lines: list[str] = [
        "# Knowledge Graph Index",
        "",
        "> Auto-generated by omnigraph. Start here — read community articles for context, then drill into god nodes for detail.",
        "",
        f"**{total_nodes} nodes · {total_edges} edges · {len(communities)} communities**",
        "",
        "---",
        "",
        "## Communities",
        "(sorted by size, largest first)",
        "",
    ]

    for cid, nodes in sorted(communities.items(), key=lambda x: -len(x[1])):
        label = labels.get(cid, f"Community {cid}")
        lines.append(f"- {_md_link(label, resolver)} — {len(nodes)} nodes")
    lines.append("")

    if god_nodes_data:
        lines += ["## God Nodes", "(most connected concepts — the load-bearing abstractions)", ""]
        for node in god_nodes_data:
            lines.append(f"- {_md_link(node['label'], resolver)} — {node['degree']} connections")
        lines.append("")

    lines += [
        "---",
        "",
        "*Generated by [omnigraph](https://github.com/Zespek/OmniGraph)*",
    ]
    return "\n".join(lines)


def to_wiki(
    G: nx.Graph,
    communities: dict[int, list[str]],
    output_dir: str | Path,
    community_labels: dict[int, str] | None = None,
    cohesion: dict[int, float] | None = None,
    god_nodes_data: list[dict] | None = None,
) -> int:
    """Generate a Wikipedia-style wiki from the graph.

    Writes:
      - index.md            — agent entry point, catalog of all articles
      - <CommunityName>.md  — one article per community
      - <GodNodeLabel>.md   — one article per god node

    Returns the number of articles written (excluding index.md).
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    if not communities:
        raise ValueError(
            "communities dict is empty — refusing to clear wiki/. "
            "Run `omnigraph extract .` or `omnigraph cluster-only .` first."
        )

    # Filtre IDs de nós obsoletos que existem em comunidades, mas não em G.
    # O JSON de análise pode desviar do grafo após desduplicação/reextração/atualização.
    # NetworkX 3.x retorna DegreeView({}) para nós ausentes em vez de aumentar,
    # que trava sorted() com TypeError; G.neighbors()/G.nodes[] também aumenta.
    import sys as _sys
    _g_nodes = set(G.nodes)
    _orig_total = sum(len(ns) for ns in communities.values())
    communities = {cid: [n for n in nodes if n in _g_nodes] for cid, nodes in communities.items()}
    communities = {cid: nodes for cid, nodes in communities.items() if nodes}
    _kept_total = sum(len(ns) for ns in communities.values())
    if _kept_total < _orig_total:
        print(
            f"wiki: dropped {_orig_total - _kept_total} stale node ID(s) not in graph "
            f"({len(communities)} communities remaining)",
            file=_sys.stderr,
        )

    if not communities:
        raise ValueError(
            "all community node IDs are stale — none exist in the graph. "
            "Re-run `omnigraph extract .` to regenerate .omnigraph_analysis.json."
        )

    # Limpe arquivos .md obsoletos de execuções anteriores para evitar o acúmulo de órfãos.
    # Os rótulos da comunidade são gerados por LLM (por habilidade.md Etapa 5) e não determinísticos
    # entre execuções - a mesma comunidade conceitual pode receber nomes diferentes a cada vez
    # (por exemplo, "AutoAgent Skills" → "AutoAgent Methodology"), deixando o arquivo anterior
    # como órfão. Como to_wiki() possui wiki/ inteiramente (sempre escreve o conjunto completo),
    # ele pode limpar arquivos .md com segurança no início de cada chamada.
    for old_article in out.glob("*.md"):
        old_article.unlink()

    labels = community_labels or {cid: f"Community {cid}" for cid in communities}
    cohesion = cohesion or {}
    god_nodes_data = god_nodes_data or []

    # Construir node->community lookup uma vez; os atributos do nó nunca carregam comunidade (ele vive em
    # as comunidades determinam), então _cross_community_links e _god_node_article precisam disso.
    node_community: dict[str, int] = {n: cid for cid, nodes in communities.items() for n in nodes}

    count = 0
    used_slugs: set[str] = set()

    # Articles are capped against THIS wiki directory, not just NAME_MAX: on
    # Windows a 200-char slug under an ordinary omnigraph-out/wiki/ overruns
    # MAX_PATH and write_text raises FileNotFoundError partway through the
    # export. No-op on POSIX.
    _slug_limit = stem_filename_budget(out, reserve=_SLUG_SUFFIX_RESERVE)

    def _unique_slug(base: str) -> str:
        # Dobrar caso na verificação de colisão: dois rótulos diferindo apenas por caso
        # (por exemplo, "Analisador" vs "analisador") resolve para um caminho sem distinção entre maiúsculas e minúsculas
        # sistemas de arquivos (macOS/APFS, Windows/NTFS), portanto, eles devem ser desduplicados em cada um
        # other enquanto ainda emite o nome do arquivo do caso original.
        slug = base
        n = 2
        while slug.lower() in used_slugs:
            slug = f"{base}_{n}"
            n += 1
        used_slugs.add(slug.lower())
        return slug

    # Primeira passagem: atribua a cada artigo seu slug antes de renderizar qualquer corpo, para que o
    # os corpos podem se ligar uns aos outros. O destino de um link é o nome do arquivo no disco (o
    # slug), que difere do rótulo — _safe_filename transforma espaços em
    # sublinhados e substitutos de caracteres reservados, e um slug pode pegar um número
    # sufixo de desduplicação de colisão - portanto, o slug final deve ser conhecido antecipadamente.
    # rótulo de exibição de mapas do resolvedor -> slug; rótulos sem artigo estão ausentes, então
    # _md_link os renderiza como texto simples. Comunidades são atacadas diante de god node
    # (e setdefault mantém o primeiro), preservando a ordem de atribuição do nome do arquivo
    # a desduplicação de caso-colisão depende.
    resolver: dict[str, str] = {"index": "index"}

    community_slugs: dict[int, str] = {}
    for cid in communities:
        label = labels.get(cid, f"Community {cid}")
        slug = _unique_slug(_safe_filename(label, _slug_limit))
        community_slugs[cid] = slug
        resolver.setdefault(label, slug)

    god_articles: list[tuple[str, str]] = []  # (node_id, slug)
    for node_data in god_nodes_data:
        nid = node_data.get("id")
        if nid and nid in G:
            slug = _unique_slug(_safe_filename(node_data['label'], _slug_limit))
            god_articles.append((nid, slug))
            resolver.setdefault(node_data['label'], slug)

    # Segunda passagem: renderize e escreva cada artigo com o resolvedor completo em mãos.
    for cid, nodes in communities.items():
        label = labels.get(cid, f"Community {cid}")
        article = _community_article(G, cid, nodes, label, labels, cohesion.get(cid), node_community, resolver)
        (out / f"{community_slugs[cid]}.md").write_text(article, encoding="utf-8")
        count += 1

    for nid, slug in god_articles:
        article = _god_node_article(G, nid, labels, node_community, resolver)
        (out / f"{slug}.md").write_text(article, encoding="utf-8")
        count += 1

    # Index
    (out / "index.md").write_text(
        _index_md(communities, labels, god_nodes_data, G.number_of_nodes(), G.number_of_edges(), resolver),
        encoding="utf-8",
    )

    return count
