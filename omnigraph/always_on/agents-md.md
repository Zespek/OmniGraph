## omnigraph

This project has a knowledge graph at omnigraph-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/omnigraph`, use the installed omnigraph skill or instructions before doing anything else.

Rules:
- For codebase questions, first run `omnigraph query "<question>"` when omnigraph-out/graph.json exists. Use `omnigraph path "<A>" "<B>"` for relationships and `omnigraph explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output. The search matches literal keywords against the code's own identifiers, not semantic meaning: if the user asks in a language other than the codebase's, phrase the query in the codebase's language (translate the concept, do not pass the user's own words verbatim) or it will return no matches. In a monorepo with multiple independent apps/modules, pass `--scope <path-substring>` (e.g. `--scope backend` or `--scope apps/provider`) so results come only from that app - without it, a generic term shared across apps can seed the traversal in the wrong one. If the result says `[!] TRUNCATED`, that means nodes are missing, not that the answer is complete: rerun with a higher `--budget` (e.g. `--budget 8000`) for a broad/architecture question before falling back to grep - the default budget can cut before reaching the relevant node.
- This applies to you and to every subagent you spawn, including a general-purpose search/explore agent: check whether omnigraph answers the question in one call BEFORE delegating broad code exploration, not just before reading/grepping yourself. A subagent starts a fresh context that does not include this file, so if you still delegate, put the omnigraph instruction explicitly in its prompt.
- Dirty omnigraph-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip omnigraph. Only skip omnigraph if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If omnigraph-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read omnigraph-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `omnigraph update .` to keep the graph current (AST-only, no API cost).
