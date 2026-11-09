## omnigraph

This project has a knowledge graph at omnigraph-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/omnigraph`, use the installed omnigraph skill or instructions before doing anything else.

Rules:
- To locate code you do not already know the file for, run `omnigraph query "<question>"` when omnigraph-out/graph.json exists. Use `omnigraph path "<A>" "<B>"` for relationships and `omnigraph explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output. The search matches literal words against the code's own names, not meaning. Put the key words in both the user's language and English in the same query (e.g. `omnigraph query "restaurar sessao restore session"`): code often mixes languages, and a query in only one of them misses every name written in the other. In a monorepo with multiple independent apps/modules, pass `--scope <path-substring>` (e.g. `--scope backend` or `--scope apps/provider`) so results come only from that app - without it, a generic term shared across apps can seed the traversal in the wrong one. Results are ordered by relevance, so the top lines are the answer: go straight to the `file:line` shown instead of querying again. Then read only that part, not the whole file: a symbol tool such as Serena's `find_symbol` if you have one, otherwise Read with offset/limit around that line (about 80 lines). If they miss, retry once with a specific identifier or `--scope`, then fall back to grep. Raise `--budget` only for an explicit broad/architecture review.
- This applies to you and to every subagent you spawn, including a general-purpose search/explore agent: check whether omnigraph answers the question in one call BEFORE delegating broad code exploration, not just before reading/grepping yourself. A subagent starts a fresh context that does not include this file, so if you still delegate, put the omnigraph instruction explicitly in its prompt.
- Dirty omnigraph-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip omnigraph. Only skip omnigraph if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If omnigraph-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read omnigraph-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `omnigraph update .` to keep the graph current (AST-only, no API cost).
