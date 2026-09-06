---
inclusion: always
---

omnigraph: A knowledge graph of this project lives in `omnigraph-out/`. For codebase, architecture, or dependency questions, when `omnigraph-out/graph.json` exists, first run `omnigraph query "<question>"` (or `omnigraph path "<A>" "<B>"` / `omnigraph explain "<concept>"`). These return a scoped subgraph, usually much smaller than `GRAPH_REPORT.md` or raw grep output. The search matches literal keywords against the code's own identifiers, not semantic meaning: if the user asks in a language other than the codebase's, phrase the query in the codebase's language (translate the concept, do not pass the user's own words verbatim) or it will return no matches. Read `GRAPH_REPORT.md` only for broad architecture review or when those commands do not surface enough context.
