# omnigraph reference: GitHub clone and cross-repo merge

Load this when the user passed one or more `https://github.com/...` URLs, or named several local subfolders to merge into one graph.

### Step 0 - Clone GitHub repo(s) (only if a GitHub URL was given)

**Single repo:**
```bash
LOCAL_PATH=$(omnigraph clone <github-url> [--branch <branch>])
# Use LOCAL_PATH as the target for all subsequent steps
```

**Multiple repos (cross-repo graph):**
```bash
# Clone each repo, run the full pipeline on each, then merge
omnigraph clone <url1>   # → ~/.omnigraph/repos/<owner1>/<repo1>
omnigraph clone <url2>   # → ~/.omnigraph/repos/<owner2>/<repo2>
# Run /omnigraph on each local path to produce their graph.json files
# Then merge:
omnigraph merge-graphs \
  ~/.omnigraph/repos/<owner1>/<repo1>/omnigraph-out/graph.json \
  ~/.omnigraph/repos/<owner2>/<repo2>/omnigraph-out/graph.json \
  --out omnigraph-out/cross-repo-graph.json
```

OmniGraph clones into `~/.omnigraph/repos/<owner>/<repo>` and reuses existing clones on repeat runs. Each node in the merged graph carries a `repo` attribute so you can filter by origin.

**Multiple local subfolders (monorepo or multi-service layout):**

The skill pipeline writes all intermediate and final outputs to `omnigraph-out/` in the current working directory. Running the skill on each subfolder separately will clobber the same output dir. Instead, use the CLI directly for each subfolder — it places `omnigraph-out/` *inside* the scanned path:

```bash
omnigraph extract ./core/     # → ./core/omnigraph-out/graph.json
omnigraph extract ./service/  # → ./service/omnigraph-out/graph.json
omnigraph extract ./platform/ # → ./platform/omnigraph-out/graph.json
# Add --backend gemini|kimi|openai|deepseek|claude-cli depending on which API key you have set

# Then merge at the project root:
omnigraph merge-graphs \
  ./core/omnigraph-out/graph.json \
  ./service/omnigraph-out/graph.json \
  ./platform/omnigraph-out/graph.json \
  --out omnigraph-out/graph.json
```

Once `omnigraph-out/graph.json` exists, the fast path above takes over: any codebase question runs `omnigraph query` directly on the merged graph — no re-extraction, no size gate.
