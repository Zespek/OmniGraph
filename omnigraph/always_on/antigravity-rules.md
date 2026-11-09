---
trigger: always_on
description: Consult the omnigraph knowledge graph at omnigraph-out/ for codebase and architecture questions.
---

## omnigraph

This project has a omnigraph knowledge graph at omnigraph-out/.

Rules:
- For codebase or architecture questions, when `omnigraph-out/graph.json` exists, first run `omnigraph query "<question>"` (CLI) or `query_graph` (MCP). Use `omnigraph path "<A>" "<B>"` / `shortest_path` for relationships and `omnigraph explain "<concept>"` / `get_node` for focused concepts. These return a scoped subgraph, usually much smaller than `GRAPH_REPORT.md` or raw grep output. The search matches literal words against the code's own names, not meaning. Put the key words in both the user's language and English in the same query (e.g. `omnigraph query "restaurar sessao restore session"`): code often mixes languages, and a query in only one of them misses every name written in the other. In a monorepo with multiple independent apps/modules, pass `--scope <path-substring>` (e.g. `--scope backend` or `--scope apps/provider`) so results come only from that app - without it, a generic term shared across apps can seed the traversal in the wrong one. Results are ordered by relevance, so the top lines are the answer: go straight to the `file:line` shown instead of querying again. If they miss, retry once with a specific identifier or `--scope`, then fall back to grep. Raise `--budget` only for an explicit broad/architecture review.
- This applies to you and to every subagent you spawn, including a general-purpose search/explore agent: check whether omnigraph answers the question in one call BEFORE delegating broad code exploration, not just before reading/grepping yourself. A subagent starts a fresh context that does not include this file, so if you still delegate, put the omnigraph instruction explicitly in its prompt.
- If omnigraph-out/wiki/index.md exists, navigate it instead of reading raw files
- Read omnigraph-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context
- After modifying code files in this session, run `omnigraph update .` to keep the graph current (AST-only, no API cost)
