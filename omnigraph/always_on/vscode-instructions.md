## omnigraph

For any question about this repo's architecture, structure, components, or how to add/modify/find
code, your first action should be `omnigraph query "<question>"` when `omnigraph-out/graph.json`
exists. Use `omnigraph path "<A>" "<B>"` for relationship questions and `omnigraph explain "<concept>"`
for focused-concept questions. These return a scoped subgraph, usually much smaller than the full
report or raw grep output. The search matches literal keywords against the code's own identifiers,
not semantic meaning: if the user asks in a language other than the codebase's, phrase the query in
the codebase's language (translate the concept, do not pass the user's own words verbatim) or it will
return no matches.

Triggers: "how do I…", "where is…", "what does … do", "add/modify a <component>",
"explain the architecture", or anything that depends on how files or classes relate.

If `omnigraph-out/wiki/index.md` exists, use it for broad navigation. Read `omnigraph-out/GRAPH_REPORT.md`
only for broad architecture review or when query/path/explain do not surface enough context. Only read
source files when (a) modifying/debugging specific code, (b) the graph lacks the needed detail, or
(c) the graph is missing or stale.

Type `/omnigraph` in Copilot Chat to build or update the graph.
