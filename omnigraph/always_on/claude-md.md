## omnigraph

This project has a knowledge graph at omnigraph-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `omnigraph query "<question>"` when omnigraph-out/graph.json exists. Use `omnigraph path "<A>" "<B>"` for relationships and `omnigraph explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output. The search matches literal keywords against the code's own identifiers, not semantic meaning: if the user asks in a language other than the codebase's, phrase the query in the codebase's language (translate the concept, do not pass the user's own words verbatim) or it will return no matches.
- If omnigraph-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read omnigraph-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `omnigraph update .` to keep the graph current (AST-only, no API cost).
