# omnigraph reference: commit hook and native AGENTS.md integration

Load this when the user asked to install the post-commit hook or wire omnigraph into a project's AGENTS.md.

## For git commit hook

Install a post-commit hook that auto-rebuilds the graph after every commit. No background process needed - triggers once per commit, works with any editor.

```bash
omnigraph hook install    # install
omnigraph hook uninstall  # remove
omnigraph hook status     # check
```

After every `git commit`, the hook detects which code files changed (via `git diff HEAD~1`), re-runs AST extraction on those files, and rebuilds `graph.json` and `GRAPH_REPORT.md`. Doc/image changes are ignored by the hook - run `/omnigraph --update` manually for those.

If a post-commit hook already exists, omnigraph appends to it rather than replacing it.

---

## For native AGENTS.md integration (Trae)

Run once per project to make omnigraph always-on in Trae sessions:

```bash
omnigraph trae install       # or: omnigraph trae-cn install
```

This writes a `## omnigraph` section to the local `AGENTS.md` that instructs Trae to check the graph before answering codebase questions and rebuild it after code changes. No manual `/omnigraph` needed in future sessions.

> **Note:** Unlike Claude Code, Trae does NOT support PreToolUse hooks. The AGENTS.md rules are the always-on mechanism — there is no automatic graph rebuild on tool use. Run `/omnigraph --update` manually after code changes if the graph needs refreshing.

```bash
omnigraph trae uninstall     # or: omnigraph trae-cn uninstall   # remove the section
```
