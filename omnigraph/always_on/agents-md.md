## omnigraph

This project has a knowledge graph at omnigraph-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/omnigraph`, use the installed omnigraph skill or instructions before doing anything else.

Rules:
- For codebase questions, first run `omnigraph query "<question>"` when omnigraph-out/graph.json exists. Use `omnigraph path "<A>" "<B>"` for relationships and `omnigraph explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- Dirty omnigraph-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip omnigraph. Only skip omnigraph if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If omnigraph-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read omnigraph-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `omnigraph update .` to keep the graph current (AST-only, no API cost).
