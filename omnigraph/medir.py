"""`omnigraph medir`: how much this project's Claude Code sessions spend finding code.

Reads the local Claude Code transcripts (``~/.claude/projects``, or
``$CLAUDE_CONFIG_DIR/projects``) of the sessions run in this project and compares
the sessions before and after OmniGraph was set up here. Only sizes are used:
no transcript content is printed or sent anywhere.

"Finding code" is what the hooks watch: Read/Grep/Glob, shell searches (grep,
rg, find...) and shell reads (cat, head, sed -n...), code-navigation tools of
MCP servers such as Serena, plus the graph queries meant to replace them.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import statistics
import sys
from pathlib import Path

_CHARS_PER_TOKEN = 4
_SEARCH_TOOLS = frozenset({"Read", "Grep", "Glob"})
_MAP_SUBCOMMANDS = frozenset({"query", "path", "explain"})
_MAP_MCP_TOOLS = ("query_graph", "get_node", "get_neighbors", "shortest_path")
# Code-navigation tools of other MCP servers (Serena and the like): they find and
# read code too, so leaving them out would hide part of the cost being compared.
_MCP_CODE_TOOLS = frozenset({
    "find_symbol", "find_referencing_symbols", "get_symbols_overview",
    "search_for_pattern", "read_file", "find_file", "list_dir",
})
_MIN_SESSIONS = 3


def _transcripts_root() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def _project_roots() -> set[str]:
    """This project's root as Claude Code may have recorded it."""
    from omnigraph.paths import _git
    here = Path.cwd()
    found = _git(here, "rev-parse", "--show-toplevel")
    root = Path(found.stdout.strip()) if found is not None and found.returncode == 0 else here
    return {str(root), str(root.resolve()), os.getcwd()}


def _setup_time(root: Path) -> float | None:
    """When OmniGraph was set up here: when omnigraph-ide wrote CLAUDE.local.md,
    in this folder or one above it (a workspace that holds several repos)."""
    home = Path.home()
    for folder in (root, *root.parents):
        if folder == home or folder == folder.parent:
            break
        marker = folder / "CLAUDE.local.md"
        try:
            if "omnigraph" not in marker.read_text(encoding="utf-8", errors="replace"):
                continue
            st = marker.stat()
        except OSError:
            continue
        return getattr(st, "st_birthtime", st.st_mtime)
    return None


def _classify(name: str, tool_input: dict) -> str:
    """'busca' for code search/read, 'mapa' for graph queries, 'outros' for the rest."""
    if name in _SEARCH_TOOLS:
        return "busca"
    if "omnigraph" in name.lower() and name.endswith(_MAP_MCP_TOOLS):
        return "mapa"
    if name.startswith("mcp__") and name.rsplit("__", 1)[-1] in _MCP_CODE_TOOLS:
        return "busca"
    if name == "Bash":
        from omnigraph.cli import _bash_commands, _bash_invokes_search, _bash_read_targets
        cmd = str(tool_input.get("command") or "")
        for exe, args in _bash_commands(cmd):
            if exe == "omnigraph" and args[:1] and args[0] in _MAP_SUBCOMMANDS:
                return "mapa"
        if _bash_invokes_search(cmd) or _bash_read_targets(cmd):
            return "busca"
    return "outros"


def _text_size(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(item.get("text", "") or "") if isinstance(item, dict) else len(str(item))
            for item in content
        )
    return 0


def _timestamp(value) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def collect_sessions(roots: set[str]) -> dict[str, dict]:
    """Per-session sizes for the sessions whose working directory is this project."""
    prefixes = tuple(r.rstrip(os.sep) + os.sep for r in roots)
    sessions: dict[str, dict] = {}
    pattern = str(_transcripts_root() / "**" / "*.jsonl")
    for path in glob.glob(pattern, recursive=True):
        pending: dict[str, str] = {}
        belongs: bool | None = None
        try:
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                cwd = rec.get("cwd")
                if belongs is None and cwd:
                    belongs = cwd in roots or cwd.startswith(prefixes)
                    if not belongs:
                        break
                if not belongs:
                    continue
                sid = str(rec.get("sessionId") or path)
                session = sessions.setdefault(sid, {
                    "start": None, "busca": 0, "mapa": 0, "outros": 0,
                    "busca_calls": 0, "mapa_calls": 0,
                })
                when = _timestamp(rec.get("timestamp"))
                if when is not None and (session["start"] is None or when < session["start"]):
                    session["start"] = when
                content = (rec.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        kind = _classify(str(block.get("name") or ""), block.get("input") or {})
                        pending[str(block.get("id"))] = kind
                        if kind != "outros":
                            session[f"{kind}_calls"] += 1
                    elif block.get("type") == "tool_result":
                        kind = pending.pop(str(block.get("tool_use_id")), None)
                        if kind:
                            session[kind] += _text_size(block.get("content"))
    return sessions


def _summary(group: list[dict]) -> dict | None:
    if not group:
        return None
    finding = [(s["busca"] + s["mapa"]) / _CHARS_PER_TOKEN for s in group]
    tool_total = sum(s["busca"] + s["mapa"] + s["outros"] for s in group)
    return {
        "sessions": len(group),
        "finding": statistics.median(finding),
        "map_tokens": statistics.median(s["mapa"] / _CHARS_PER_TOKEN for s in group),
        "search_calls": statistics.mean(s["busca_calls"] for s in group),
        "map_calls": statistics.mean(s["mapa_calls"] for s in group),
        "used_map": sum(1 for s in group if s["mapa_calls"]) / len(group),
        "share": sum(s["busca"] + s["mapa"] for s in group) / tool_total if tool_total else 0.0,
    }


def render(sessions: dict[str, dict], since: float) -> str:
    before = _summary([s for s in sessions.values() if s["start"] and s["start"] < since])
    after = _summary([s for s in sessions.values() if s["start"] and s["start"] >= since])
    day = dt.datetime.fromtimestamp(since).strftime("%d/%m/%Y")
    rows = [
        ("sessões", "sessions", "{:.0f}"),
        ("tokens para achar código (mediana por sessão)", "finding", "{:,.0f}"),
        ("  dos quais em consultas ao mapa", "map_tokens", "{:,.0f}"),
        ("buscas e leituras de código por sessão", "search_calls", "{:.1f}"),
        ("consultas ao mapa por sessão", "map_calls", "{:.1f}"),
        ("sessões que usaram o mapa", "used_map", "{:.0%}"),
        ("parte da saída das ferramentas gasta achando código", "share", "{:.0%}"),
    ]
    lines = [f"OmniGraph: gasto para achar código neste projeto, antes e depois de {day}", ""]
    lines.append(f"{'':52}{'antes':>10}{'depois':>10}")
    for label, key, fmt in rows:
        cells = [fmt.format(group[key]).replace(",", ".") if group else "-" for group in (before, after)]
        lines.append(f"{label:52}{cells[0]:>10}{cells[1]:>10}")
    lines.append("")
    if not before or not after or before["sessions"] < _MIN_SESSIONS or after["sessions"] < _MIN_SESSIONS:
        lines.append(f"Poucas sessões para comparar: são precisas pelo menos {_MIN_SESSIONS} de cada lado.")
    elif before["finding"]:
        change = (after["finding"] - before["finding"]) / before["finding"]
        verb = "caiu" if change < 0 else "subiu"
        lines.append(f"O gasto para achar código por sessão {verb} {abs(change):.0%}.")
    lines.append("Estimativa: 4 caracteres ≈ 1 token, só a saída das ferramentas, e as sessões variam de tamanho.")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    since: float | None = None
    if argv[:1] == ["--desde"] and len(argv) > 1:
        try:
            since = dt.datetime.strptime(argv[1], "%Y-%m-%d").timestamp()
        except ValueError:
            print("error: use --desde AAAA-MM-DD", file=sys.stderr)
            return 1
    elif argv:
        print("Usage: omnigraph medir [--desde AAAA-MM-DD]", file=sys.stderr)
        return 1
    roots = _project_roots()
    if since is None:
        since = min((t for t in (_setup_time(Path(r)) for r in roots) if t), default=None)
    if since is None:
        print("Não achei a data de instalação (CLAUDE.local.md do omnigraph-ide). "
              "Rode com --desde AAAA-MM-DD.", file=sys.stderr)
        return 1
    sessions = collect_sessions(roots)
    if not sessions:
        print("Nenhuma sessão do Claude Code encontrada para este projeto nesta máquina.")
        return 0
    print(render(sessions, since))
    return 0
