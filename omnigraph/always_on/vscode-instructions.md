## omnigraph

To locate code you do not already know the file for, run `omnigraph query "<question>"` when
`omnigraph-out/graph.json` exists. Use `omnigraph path "<A>" "<B>"` for relationship questions and `omnigraph explain "<concept>"`
for focused-concept questions. These return a scoped subgraph, usually much smaller than the full
report or raw grep output. The search matches literal words against the code's own names, not meaning. Put the key words in both the user's language and English in the same query (e.g. `omnigraph query "restaurar sessao restore session"`): code often mixes languages, and a query in only one of them misses every name written in the other. In a monorepo with multiple independent apps/modules, pass `--scope <path-substring>`
(e.g. `--scope backend` or `--scope apps/provider`) so results come only from that app - without it, a
generic term shared across apps can seed the traversal in the wrong one. Results are ordered by
relevance, so the top lines are the answer: go straight to the `file:line` shown instead of querying
again. If they miss, retry once with a specific identifier or `--scope`, then fall back to grep. Raise
`--budget` only for an explicit broad/architecture review.

This applies to you and to every subagent you spawn, including a general-purpose search/explore
agent: check whether omnigraph answers the question in one call BEFORE delegating broad code
exploration, not just before reading/grepping yourself. A subagent starts a fresh context that does
not include this file, so if you still delegate, put the omnigraph instruction explicitly in its
prompt.

Triggers: "how do I…", "where is…", "what does … do", "add/modify a <component>",
"explain the architecture", or anything that depends on how files or classes relate.

If `omnigraph-out/wiki/index.md` exists, use it for broad navigation. Read `omnigraph-out/GRAPH_REPORT.md`
only for broad architecture review or when query/path/explain do not surface enough context. Only read
source files when (a) modifying/debugging specific code, (b) the graph lacks the needed detail, or
(c) the graph is missing or stale.

Type `/omnigraph` in Copilot Chat to build or update the graph.
