---
description: Build or query a omnigraph knowledge graph
---

Invoke the `omnigraph` skill immediately.

Pass the full `/omnigraph` argument string through unchanged.
If no arguments were supplied, treat the target path as `.`.

Examples:
- `/omnigraph`
- `/omnigraph src --update`
- `/omnigraph query "what connects auth to billing?"`

Do not answer from raw files before handing off to the `omnigraph` skill.
