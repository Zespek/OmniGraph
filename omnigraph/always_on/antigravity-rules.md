---
trigger: always_on
description: Consult the omnigraph knowledge graph at omnigraph-out/ for codebase and architecture questions.
---

## omnigraph

This project has a omnigraph knowledge graph at omnigraph-out/.

Rules:
- For codebase or architecture questions, when `omnigraph-out/graph.json` exists, first run `omnigraph query "<question>"` (CLI) or `query_graph` (MCP). Use `omnigraph path "<A>" "<B>"` / `shortest_path` for relationships and `omnigraph explain "<concept>"` / `get_node` for focused concepts. These return a scoped subgraph, usually much smaller than `GRAPH_REPORT.md` or raw grep output. The search matches literal keywords against the code's own identifiers, not semantic meaning: if the user asks in a language other than the codebase's, phrase the query in the codebase's language (translate the concept, do not pass the user's own words verbatim) or it will return no matches.
- If omnigraph-out/wiki/index.md exists, navigate it instead of reading raw files
- Read omnigraph-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context
- After modifying code files in this session, run `omnigraph update .` to keep the graph current (AST-only, no API cost)
