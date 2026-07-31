"""omnigraph command dispatch — every non-install subcommand.

Extracted verbatim from __main__.main(); __main__ now calls dispatch_command(cmd)
after the install/platform dispatch. Kept out of __main__ to shrink the CLI entry
module. The path-redirect (`omnigraph <path>` -> extract) re-enters via a lazy
import of main to avoid a cli<->__main__ import cycle.
"""
from __future__ import annotations
import json
import os
import re
import sys
import time
from omnigraph.paths import OMNIGRAPH_OUT as _OMNIGRAPH_OUT
from pathlib import Path


_SEARCH_NUDGE = json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": (
            'MANDATORY: omnigraph-out/graph.json exists. You MUST run '
            '`omnigraph query "<question>"` before grepping raw files. Only grep '
            'after omnigraph has oriented you, or to modify/debug specific lines.'
        ),
    }
}, ensure_ascii=False, separators=(",", ":")) + "\n"
_READ_NUDGE = json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": (
            'MANDATORY: omnigraph-out/graph.json exists. You MUST run omnigraph '
            'before reading source files. Use: `omnigraph query "<question>"` '
            '(scoped subgraph), `omnigraph explain "<concept>"`, or '
            '`omnigraph path "<A>" "<B>"`. Only read raw files after omnigraph has '
            'oriented you, or to modify/debug specific lines. This rule applies to '
            'subagents too — include it in every subagent prompt involving code '
            'exploration.'
        ),
    }
}, ensure_ascii=False, separators=(",", ":")) + "\n"
_READ_NUDGE_STALE = json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": (
            'omnigraph-out/graph.json exists but may be STALE for this file (the file '
            'changed after the last build). Prefer `omnigraph query "<question>"` for '
            'orientation, and run `omnigraph update` to refresh the graph. Reading the '
            'file directly is fine.'
        ),
    }
}, ensure_ascii=False, separators=(",", ":")) + "\n"
# Strict-mode block (opt-in). Claude Code PreToolUse honors
# hookSpecificOutput.permissionDecision == "deny" and shows permissionDecisionReason
# to the model. Fires at most once per session (see _mark_session_denied) so it can
# never strand an agent: the very next read proceeds with the soft nudge.
_READ_DENY = json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            'omnigraph strict mode: this project has a fresh knowledge graph that covers '
            'this file. Run `omnigraph query "<your question>"` (or `omnigraph explain` / '
            '`omnigraph path`) FIRST to orient yourself, then re-issue this Read — it '
            'will be allowed. This block fires at most once per session; reading raw '
            'files to modify or debug specific lines is fine after one query. Apply the '
            'same rule in any subagent prompt that explores code.'
        ),
    }
}, ensure_ascii=False, separators=(",", ":")) + "\n"
_HOOK_SOURCE_EXTS = (
    '.py', '.js', '.cjs', '.ts', '.tsx', '.jsx', '.astro', '.vue', '.svelte', '.go',
    '.rs', '.java', '.rb', '.c', '.h', '.cpp', '.hpp', '.cc', '.cs', '.kt',
    '.swift', '.php', '.scala', '.lua', '.sh', '.md', '.rst', '.txt', '.mdx',
)
_GEMINI_NUDGE_TEXT = (
    'omnigraph: knowledge graph at omnigraph-out/. For focused questions, run '
    '`omnigraph query "<question>"` (scoped subgraph, usually much smaller than '
    'GRAPH_REPORT.md) instead of grepping raw files. Read GRAPH_REPORT.md only '
    'for broad architecture context.'
)


def _default_graph_path() -> str:
    return str(Path(_OMNIGRAPH_OUT) / "graph.json")


def _stamped_manifest_files(
    files_by_type: dict[str, list[str]],
    sem_result: dict,
    root: Path,
    partial_source_files: "set[str] | None" = None,
) -> dict[str, list[str]]:
    """Manifest-safe files dict: only stamp semantic files that actually
    produced output (cache hit or fresh extraction). Files whose chunk failed
    have no source_file entry in sem_result — leaving their semantic_hash
    empty so detect_incremental re-queues them (#933).

    A file in ``partial_source_files`` DID produce output this run, but only a
    truncated fragment of it, so it is excluded from stamping too — otherwise
    detect_incremental would see it "done" and never re-dispatch it, leaving the
    incomplete node set live forever on the warm-incremental path. Same #933
    mechanism: leave it unstamped and it is re-queued next run.

    Both sides of the membership test are resolved against the scan ``root``
    before comparing (#1897): node/edge/hyperedge ``source_file`` values are
    root-relative on a fresh extraction while ``files_by_type`` entries are
    absolute (from detect()), so a raw string comparison never matched and
    every freshly-extracted semantic doc was dropped from the manifest.
    Mirrors the #1890 path normalization in omnigraph.llm.

    Hyperedges are counted as output (#1920): a chunk whose only result for a
    document is a hyperedge (3+ nodes sharing a concept) is valid output that
    the semantic cache persists per-``source_file`` — omitting it here left the
    doc unstamped, so detect_incremental re-queued it on every run. The stamping
    condition mirrors the cache-write keying (a hyperedge carries its own
    ``source_file``); do not derive it from member nodes.
    """
    root = Path(root)

    def _resolve(value: str) -> Path:
        p = Path(value)
        if not p.is_absolute():
            p = root / p
        try:
            return p.resolve()
        except (OSError, RuntimeError):
            return p

    sem_extracted: set[Path] = set()
    for coll in ("nodes", "edges", "hyperedges"):
        for item in sem_result.get(coll, []):
            sf = item.get("source_file", "")
            if sf:
                sem_extracted.add(_resolve(sf))
    partial_resolved = {_resolve(p) for p in (partial_source_files or set())}
    sem_types = {"document", "paper", "image"}
    return {
        ftype: [
            f for f in flist
            if ftype not in sem_types
            or (_resolve(f) in sem_extracted and _resolve(f) not in partial_resolved)
        ]
        for ftype, flist in files_by_type.items()
    }


def _stale_graph_sources(
    graph_path: Path,
    scan_root: Path,
    seen_files: set[str],
    detection: dict | None = None,
) -> list[str]:
    """Source files graph.json still references but the current scan no longer
    contains (#1909).

    Incremental extract's prune set was historically derived from the manifest
    alone (``manifest - corpus``), so a file that became EXCLUDED
    (.omnigraphignore/.gitignore/--exclude changed) without being listed in the
    manifest kept its stale nodes in graph.json forever. Derive prune
    candidates from the graph's own node ``source_file``s instead: anything
    the graph references that the post-exclude detect corpus no longer
    contains is stale, whether the file was deleted or newly excluded.

    Only IN-ROOT paths are candidates: out-of-root/absolute entries
    (--include sources, symlinked external corpora) are never walked by
    detect, so their absence from the corpus is not staleness evidence.
    Relative entries are re-anchored against both the scan root and the
    graph's own output root; only anchors that land inside the scan root
    count. Since #1941 extracts always store source_file relative to the SCAN
    root, so the scan-root anchor is the live one; the out-root anchor stays
    for graphs written by <=0.9.16, which stored them relative to the OUT root
    (e.g. ``../project/x.py``, #555/#1899).
    ``seen_files`` must be the FULL detect output including unclassified
    files, so nodes from walked-but-unsupported sources (e.g. introspected
    Cargo.toml manifests) are not misread as stale.

    Paths are compared NFC-normalized on both sides: macOS reports NFD
    filenames while graph ``source_file`` entries are typically NFC, and a
    raw-string membership test misread every accented live file as stale
    (#2210; same class as the manifest-layer #2221/#2224).

    Fail-closed liveness guard (#2210, mirrors watch.py's excluded-vs-deleted
    distinction): a source missing from the scan corpus is only pruned when
    the file is gone from disk, or when its exclusion is PROVABLE from the
    same scan that produced ``seen_files`` — ``detection``'s ``ignored`` /
    ``pruned_noise_dirs`` / ``skipped_sensitive`` output, or detect's
    sensitivity predicate. An alive file that merely failed the membership
    test (path-spelling drift the normalization didn't cover, walk errors,
    …) is KEPT and reported, never mass-evicted.
    """
    from omnigraph.paths import nfc
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    try:
        root_res = scan_root.resolve()
    except (OSError, RuntimeError):
        root_res = scan_root
    # <out>/omnigraph-out/graph.json — source_files relativos podem ser ancorados aqui.
    out_base = graph_path.parent.parent
    try:
        out_base = out_base.resolve()
    except (OSError, RuntimeError):
        pass

    def _within_root(p: Path) -> bool:
        try:
            p.relative_to(root_res)
            return True
        except ValueError:
            pass
        try:
            p.resolve().relative_to(root_res)
            return True
        except (ValueError, OSError, RuntimeError):
            return False

    seen_nfc = {nfc(s) for s in seen_files}
    seen_basenames = {nfc(os.path.basename(s)) for s in seen_files}

    def _in_seen(p: Path) -> bool:
        if nfc(str(p)) in seen_nfc:
            return True
        try:
            return nfc(str(p.resolve())) in seen_nfc
        except (OSError, RuntimeError):
            return False

    # Provable-exclusion evidence from the scan that produced seen_files:
    # individually ignored files are exact entries; ignored/noise-pruned
    # directories are recorded once with a trailing separator and cover
    # their whole subtree. skipped_sensitive entries may carry a
    # " [reason]" suffix.
    excluded_exact: set[str] = set()
    excluded_prefixes: list[str] = []
    if detection:
        for entry in list(detection.get("ignored", [])) + list(
            detection.get("pruned_noise_dirs", [])
        ):
            e = nfc(str(entry))
            if e.endswith(os.sep) or e.endswith("/"):
                excluded_prefixes.append(e)
            else:
                excluded_exact.add(e)
        for entry in detection.get("skipped_sensitive", []):
            excluded_exact.add(nfc(str(entry).split(" [", 1)[0]))

    def _provably_excluded(c: Path) -> bool:
        spellings = [nfc(str(c))]
        try:
            spellings.append(nfc(str(c.resolve())))
        except (OSError, RuntimeError):
            pass
        for s in spellings:
            if s in excluded_exact:
                return True
            if any(s.startswith(pref) for pref in excluded_prefixes):
                return True
        try:
            from omnigraph.detect import _is_sensitive as _det_sensitive
            if _det_sensitive(c):
                return True
        except Exception:
            pass
        return False

    stale: list[str] = []
    kept_alive: list[str] = []
    checked: set[str] = set()
    for n in data.get("nodes", []):
        if not isinstance(n, dict):
            continue
        sf = n.get("source_file")
        if not sf or not isinstance(sf, str) or sf in checked:
            continue
        checked.add(sf)
        if "://" in sf:
            continue  # fonte remota/virtual (por exemplo, Google Workspace), não um caminho verificado
        p = Path(sf)
        if p.is_absolute():
            candidates = [p]
        else:
            rel = sf.replace("\\", "/")
            bases = [root_res]
            if out_base != root_res:
                bases.append(out_base)
            candidates = [
                Path(os.path.normpath(str(base / rel))) for base in bases
            ]
        in_root = [c for c in candidates if _within_root(c)]
        if not in_root:
            continue  # fora da raiz sob cada âncora: nunca podar
        if any(_in_seen(c) for c in in_root):
            continue  # ainda faz parte do corpus da varredura
        # Fail-closed liveness guard: absence from the corpus is
        # only deletion evidence when the file is actually gone from disk.
        alive = []
        for c in in_root:
            try:
                if c.exists():
                    alive.append(c)
            except OSError:
                pass
        if alive:
            if all(_provably_excluded(c) for c in alive):
                stale.append(sf)  # alive but excluded under current rules
            else:
                kept_alive.append(sf)
            continue
        # No anchored candidate exists, but a legacy bare-basename spelling
        # can't be anchored reliably — a live corpus file with the same name
        # means deletion is unproven; keep.
        rel_sf = sf.replace("\\", "/")
        if "/" not in rel_sf and nfc(rel_sf) in seen_basenames:
            kept_alive.append(sf)
            continue
        stale.append(sf)
    if kept_alive:
        print(
            f"[omnigraph] fail-closed: kept node(s) from {len(kept_alive)} "
            "source file(s) that left the scan corpus but still exist on disk "
            "(ignore rules or filters changed?). Run a full re-extraction to "
            "purge them if the exclusion is intentional.",
            file=sys.stderr,
        )
    return stale


def _prune_graph_json_sources(graph_path: Path, stale_sources: list[str]) -> int:
    """Drop nodes/edges/hyperedges owned by ``stale_sources`` from graph.json
    in place. Returns the number of nodes removed.

    Used by the ``--no-cluster`` incremental early-exit: that path never runs
    ``build_merge`` (it would raw-dump only the new chunks), so an
    exclusion-only change must prune the existing raw graph directly or the
    newly-excluded file's nodes survive forever (#1909).
    ``stale_sources`` comes from :func:`_stale_graph_sources`, i.e. the
    graph's own ``source_file`` spellings, so exact string matching is enough.
    """
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    stale = set(stale_sources)
    links_key = "links" if "links" in data else "edges"
    nodes = [n for n in data.get("nodes", []) if isinstance(n, dict)]
    kept_nodes = [n for n in nodes if n.get("source_file") not in stale]
    removed_ids = {
        n.get("id") for n in nodes if n.get("source_file") in stale
    }
    n_removed = len(nodes) - len(kept_nodes)
    kept_edges = [
        e for e in data.get(links_key, [])
        if isinstance(e, dict)
        and e.get("source_file") not in stale
        and e.get("source") not in removed_ids
        and e.get("target") not in removed_ids
    ]
    kept_hyper = [
        h for h in data.get("hyperedges", [])
        if isinstance(h, dict) and h.get("source_file") not in stale
    ]
    if n_removed == 0 and len(kept_edges) == len(data.get(links_key, [])) and (
        len(kept_hyper) == len(data.get("hyperedges", []))
    ):
        return 0
    data["nodes"] = kept_nodes
    data[links_key] = kept_edges
    if "hyperedges" in data:
        data["hyperedges"] = kept_hyper
    from omnigraph.export import backup_if_protected as _backup
    _backup(graph_path.parent)
    from omnigraph.paths import write_json_atomic
    write_json_atomic(graph_path, data, indent=2)
    return n_removed


class _StageTimer:
    """Print per-stage wall-clock timings to stderr when --timing is set (#1490).

    Monotonic (perf_counter), diagnostic-only: emits ``[omnigraph timing] <stage>:
    N.Ns`` after each stage and a final total. Off by default, so normal output is
    byte-identical and machine-read stdout is untouched.
    """

    def __init__(self, enabled: bool) -> None:
        import time as _time
        self._now = _time.perf_counter
        self.enabled = enabled
        self.start = self._now()
        self._last = self.start

    def mark(self, stage: str) -> None:
        now = self._now()
        if self.enabled:
            print(f"[omnigraph timing] {stage}: {now - self._last:.1f}s", file=sys.stderr)
        self._last = now

    def total(self) -> None:
        if self.enabled:
            print(f"[omnigraph timing] total: {self._now() - self.start:.1f}s", file=sys.stderr)
def _enforce_graph_size_cap_or_exit(gp: Path) -> None:
    """Reject oversized graph files before parsing (CLI exit-on-fail flavor).

    Delegates to ``omnigraph.security.check_graph_file_size_cap`` and turns the
    raised ``ValueError`` into a CLI-style ``error: ...`` message + exit 1.
    Use this from ``__main__.py`` subcommands that already use the ``print +
    sys.exit(1)`` idiom. Library/MCP/loader callers (``serve._load_graph``,
    ``build``, ``benchmark``, ``tree_html``, ``callflow_html``, ``prs``,
    ``global_graph``, ``watch``, ``export``) call the security helper directly
    and let the ``ValueError`` propagate.
    """
    from omnigraph.security import check_graph_file_size_cap
    try:
        check_graph_file_size_cap(gp)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
def _hook_strict_enabled(flag: bool) -> bool:
    """Resolve strict mode: OMNIGRAPH_HOOK_STRICT env overrides the baked-in flag
    (truthy forces on without a reinstall, falsy is the kill switch); unset defers
    to the flag the installed hook command carried."""
    v = os.environ.get("OMNIGRAPH_HOOK_STRICT", "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return flag


def _touch_query_stamp(graph_path: "Path") -> None:
    """Record that omnigraph oriented the agent recently, next to the queried graph.
    The strict guard suppresses its block while this stamp is fresh. Fail-silent."""
    try:
        from omnigraph.paths import write_text_atomic
        stamp = Path(graph_path).parent / "cache" / "last_query_stamp"
        stamp.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(stamp, str(time.time()))
    except Exception:
        pass


def _query_stamp_fresh() -> bool:
    """True if a query/explain/path ran within OMNIGRAPH_HOOK_STRICT_TTL (default
    1800s) — recent orientation, so strict mode does not block this read."""
    from omnigraph.paths import out_path
    try:
        ttl = float(os.environ.get("OMNIGRAPH_HOOK_STRICT_TTL", "1800"))
        return (time.time() - out_path("cache", "last_query_stamp").stat().st_mtime) < ttl
    except Exception:
        return False


def _mark_session_denied(session_id: str) -> bool:
    """Atomically claim a one-time strict block for this session. Returns True only
    on the FIRST call for a given session id (O_EXCL create wins once); every later
    call — or any error — returns False, so a session is blocked at most once and an
    agent can never be stranded. Best-effort GC of markers older than 24h."""
    from omnigraph.paths import out_path
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", str(session_id))[:64]
    if not sid:
        return False
    try:
        d = out_path("cache", "hook_sessions")
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(d / f"{sid}.denied"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
        try:
            cutoff = time.time() - 86400
            for entry in os.scandir(d):
                try:
                    if entry.stat().st_mtime < cutoff:
                        os.unlink(entry.path)
                except OSError:
                    pass
        except OSError:
            pass
        return True
    except FileExistsError:
        return False
    except Exception:
        return False


def _run_hook_guard(kind: str, strict: bool = False) -> None:
    """Shell-agnostic PreToolUse guard (#522).

    Reads the tool-call JSON from stdin and, when a fresh in-project knowledge graph
    exists, nudges the agent to use omnigraph instead of grepping/reading raw files.
    Replaces the old inline bash hooks that failed to parse on Windows.

    Fails open everywhere: any error, or a non-matching tool call, prints nothing
    and the caller exits 0, so a legitimate tool call is never blocked by a bug.

    In strict mode (opt-in, Claude Code Read only) the FIRST raw read of indexed,
    in-project, fresh code per session is DENIED with a redirect to `omnigraph query`
    (permissionDecision), then downgrades to the soft nudge — it fires at most once
    per session and can never strand the agent. Search (Bash) and Glob stay
    nudge-only: a compound shell command has no single parseable target and blocking
    file listing would strand navigation. #1840: reads of out-of-project files are
    ignored, and a graph that is stale for the target file softens to a non-mandatory
    nudge instead of blocking or demanding.
    """
    from omnigraph.paths import out_path, OMNIGRAPH_OUT_NAME
    # O gancho BeforeTool do Gemini não aceita stdin e SEMPRE deve retornar uma decisão, então
    # a ferramenta nunca é bloqueada; o deslocamento do grafo é anexado somente quando um grafo
    # existe. Manipulado antes do stdin ler abaixo (que os guardas de pesquisa/leitura precisam).
    if kind == "gemini":
        payload = {"decision": "allow"}
        try:
            if out_path("graph.json").is_file():
                payload["additionalContext"] = _GEMINI_NUDGE_TEXT
        except Exception:
            pass
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return
    try:
        d = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    except Exception:
        return
    if not isinstance(d, dict):
        return
    t = d.get("tool_input", d)
    if not isinstance(t, dict):
        return
    try:
        if kind == "search":
            cmd_str = str(t.get("command", "") or "")
            # Two input shapes reach this guard (matcher "Bash|Grep"):
            # the Bash tool carries `command`, while Claude Code's dedicated
            # Grep tool carries `pattern` (plus optional path/glob) and no
            # command — a Grep call IS a content search by definition, so it
            # nudges whenever a graph exists. For Bash, keep matching the same
            # set the old `case` matched: *grep*, *ripgrep*, and rg/find/fd/
            # ack/ag as a token (name followed by a space). Nudge-only, even in
            # strict mode — see the docstring.
            is_grep_tool = not cmd_str and bool(t.get("pattern"))
            is_bash_search = any(tok in cmd_str for tok in (
                "grep", "ripgrep", "rg ", "find ", "fd ", "ack ", "ag "))
            if (is_grep_tool or is_bash_search) and out_path("graph.json").is_file():
                sys.stdout.write(_SEARCH_NUDGE)
        elif kind == "read":
            vals = [str(t.get("file_path") or ""), str(t.get("pattern") or ""), str(t.get("path") or "")]
            j = " ".join(vals).lower().replace("\\", "/")
            tails = [
                "." + seg.rsplit(".", 1)[-1]
                for v in vals if v
                for seg in [v.lower().replace("\\", "/").rsplit("/", 1)[-1]]
                if "." in seg
            ]
            under_out = "omnigraph-out/" in j or (OMNIGRAPH_OUT_NAME.lower() + "/") in j
            if under_out or not any(tl in _HOOK_SOURCE_EXTS for tl in tails):
                return
            # (a): skip files outside the graph's project. cwd (or
            # CLAUDE_PROJECT_DIR, which Claude Code sets) is the project root, since
            # the guard only triggers when graph.json exists relative to cwd. A path
            # candidate that resolves outside that root is out-of-project.
            root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
            try:
                root = root.resolve()
            except (OSError, RuntimeError):
                pass
            path_vals = [str(t.get("file_path") or ""), str(t.get("path") or "")]
            explicit = [v for v in path_vals if v]
            if explicit:
                in_project = False
                for v in explicit:
                    p = Path(v)
                    if not p.is_absolute():
                        in_project = True  # relative -> anchored at cwd == in project
                        break
                    try:
                        p.resolve().relative_to(root)
                        in_project = True
                        break
                    except (ValueError, OSError, RuntimeError):
                        continue
                if not in_project:
                    return
            # One stat for existence + mtime of the graph.
            try:
                gmtime = os.stat(str(out_path("graph.json"))).st_mtime
            except OSError:
                return
            # (b): stale-for-target -> soften, never block. The target file
            # changed after the last build, or watch flagged the tree.
            stale = False
            fp = str(t.get("file_path") or "")
            if fp:
                try:
                    stale = os.stat(fp).st_mtime > gmtime
                except OSError:
                    stale = False
            try:
                if out_path("needs_update").exists():
                    stale = True
            except Exception:
                pass
            if stale:
                sys.stdout.write(_READ_NUDGE_STALE)
                return
            # Strict block: Read tool only, first time per session, not recently
            # oriented, and the file is demonstrably indexed.
            tool_name = d.get("tool_name")
            if _hook_strict_enabled(strict) and tool_name in (None, "Read") \
                    and not _query_stamp_fresh() \
                    and _target_is_indexed(fp, root) \
                    and _mark_session_denied(str(d.get("session_id") or "")):
                sys.stdout.write(_READ_DENY)
                return
            sys.stdout.write(_READ_NUDGE)
    except Exception:
        pass


def _target_is_indexed(file_path: str, root: "Path") -> bool:
    """Guard the strict deny: only block a read of a file the graph actually indexes.
    Reads manifest.json (cheap, capped); on any doubt (missing/corrupt/oversized
    manifest, unresolvable path) returns True so the once-per-session deny still
    applies — that block is self-limiting, so erring toward it is safe."""
    from omnigraph.paths import out_path
    if not file_path:
        return True
    try:
        mp = out_path("manifest.json")
        st = mp.stat()
        if st.st_size > 2_000_000:
            return True
        manifest = json.loads(mp.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not manifest:
            return True
        p = Path(file_path)
        rels = set()
        try:
            rels.add(p.resolve().relative_to(root).as_posix())
        except (ValueError, OSError, RuntimeError):
            pass
        rels.add(p.name)
        keys = {str(k).replace("\\", "/") for k in manifest}
        abskey = str(p).replace("\\", "/")
        return abskey in keys or any(r and (r in keys or any(k.endswith("/" + r) or k == r for k in keys)) for r in rels)
    except Exception:
        return True
def _clone_repo(
    url: str, branch: str | None = None, out_dir: Path | None = None
) -> Path:
    """Clone a GitHub repo to a local cache dir and return the path.

    Clones into ~/.omnigraph/repos/<owner>/<repo> by default so repeated
    runs on the same URL reuse the existing clone (git pull instead of clone).
    """
    import subprocess as _sp
    import re as _re

    # Normalizar URL – remover o .git final, se presente
    url = url.rstrip("/")
    if not url.endswith(".git"):
        git_url = url + ".git"
    else:
        git_url = url
        url = url[:-4]

    # Extraia o proprietário/repositório do URL
    m = _re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
    if not m:
        print(f"error: not a recognised GitHub URL: {url}", file=sys.stderr)
        sys.exit(1)
    owner, repo = m.group(1), m.group(2)

    if out_dir:
        dest = out_dir
    else:
        dest = Path.home() / ".omnigraph" / "repos" / owner / repo

    if branch and branch.startswith("-"):
        print(f"error: invalid branch name: {branch!r}", file=sys.stderr)
        sys.exit(1)

    if dest.exists():
        print(f"Repo already cloned at {dest} - pulling latest...", flush=True)
        cmd = ["git", "-C", str(dest), "pull"]
        if branch:
            cmd += ["origin", "--", branch]
        result = _sp.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"warning: git pull failed:\n{result.stderr}", file=sys.stderr)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"Cloning {url} -> {dest} ...", flush=True)
        cmd = ["git", "clone", "--depth", "1"]
        if branch:
            cmd += ["--branch", branch]
        cmd += ["--", git_url, str(dest)]
        result = _sp.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"error: git clone failed:\n{result.stderr}", file=sys.stderr)
            sys.exit(1)

    print(f"Ready at: {dest}", flush=True)
    return dest


def _reenter_main() -> None:
    from omnigraph.__main__ import main
    main()


def dispatch_command(cmd: str) -> None:
    if cmd == "provider":
        from omnigraph.llm import _custom_providers_path, BACKENDS
        import json as _json
        subcmd = sys.argv[2] if len(sys.argv) > 2 else ""
        global_path = _custom_providers_path(global_=True)

        if subcmd == "list":
            global_path.parent.mkdir(parents=True, exist_ok=True)
            existing: dict = {}
            if global_path.is_file():
                try:
                    existing = _json.loads(global_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            if not existing:
                print("No custom providers registered.")
            else:
                for name in existing:
                    print(f"  {name}  ({existing[name].get('base_url', '')})")

        elif subcmd == "show":
            name = sys.argv[3] if len(sys.argv) > 3 else ""
            if not name:
                print("Usage: omnigraph provider show <name>", file=sys.stderr)
                sys.exit(1)
            existing = {}
            if global_path.is_file():
                try:
                    existing = _json.loads(global_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            if name not in existing:
                print(f"Provider '{name}' not found.", file=sys.stderr)
                sys.exit(1)
            print(_json.dumps({name: existing[name]}, indent=2))

        elif subcmd == "add":
            args = sys.argv[3:]
            name = args[0] if args and not args[0].startswith("-") else ""
            if not name:
                print("Usage: omnigraph provider add <name> --base-url URL --default-model MODEL --env-key KEY", file=sys.stderr)
                sys.exit(1)
            if name in BACKENDS:
                print(f"Error: '{name}' is a built-in provider and cannot be overridden.", file=sys.stderr)
                sys.exit(1)
            base_url = ""
            default_model = ""
            env_key = ""
            pricing_input = 0.0
            pricing_output = 0.0
            i = 1
            while i < len(args):
                a = args[i]
                if a == "--base-url" and i + 1 < len(args):
                    base_url = args[i + 1]; i += 2
                elif a.startswith("--base-url="):
                    base_url = a.split("=", 1)[1]; i += 1
                elif a == "--default-model" and i + 1 < len(args):
                    default_model = args[i + 1]; i += 2
                elif a.startswith("--default-model="):
                    default_model = a.split("=", 1)[1]; i += 1
                elif a == "--env-key" and i + 1 < len(args):
                    env_key = args[i + 1]; i += 2
                elif a.startswith("--env-key="):
                    env_key = a.split("=", 1)[1]; i += 1
                elif a == "--pricing-input" and i + 1 < len(args):
                    pricing_input = float(args[i + 1]); i += 2
                elif a == "--pricing-output" and i + 1 < len(args):
                    pricing_output = float(args[i + 1]); i += 2
                else:
                    i += 1
            if not base_url or not default_model or not env_key:
                print("Error: --base-url, --default-model, and --env-key are required.", file=sys.stderr)
                sys.exit(1)
            from omnigraph.llm import provider_base_url_ok
            if not provider_base_url_ok(base_url, name):
                print(f"Error: refusing to add provider with unsafe base_url {base_url!r}.", file=sys.stderr)
                sys.exit(1)
            global_path.parent.mkdir(parents=True, exist_ok=True)
            existing = {}
            if global_path.is_file():
                try:
                    existing = _json.loads(global_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            existing[name] = {
                "base_url": base_url,
                "default_model": default_model,
                "env_key": env_key,
                "pricing": {"input": pricing_input, "output": pricing_output},
                "temperature": 0,
            }
            global_path.write_text(_json.dumps(existing, indent=2) + "\n", encoding="utf-8")
            print(f"Provider '{name}' added. Use with: omnigraph extract . --backend {name}")

        elif subcmd == "remove":
            name = sys.argv[3] if len(sys.argv) > 3 else ""
            if not name:
                print("Usage: omnigraph provider remove <name>", file=sys.stderr)
                sys.exit(1)
            existing = {}
            if global_path.is_file():
                try:
                    existing = _json.loads(global_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            if name not in existing:
                print(f"Provider '{name}' not found.", file=sys.stderr)
                sys.exit(1)
            del existing[name]
            global_path.write_text(_json.dumps(existing, indent=2) + "\n", encoding="utf-8")
            print(f"Provider '{name}' removed.")

        else:
            print("Usage: omnigraph provider [add|list|show|remove]", file=sys.stderr)
            if subcmd:
                sys.exit(1)
    elif cmd == "prs":
        from omnigraph.prs import cmd_prs
        cmd_prs(sys.argv[2:])
    elif cmd == "hook":
        from omnigraph.hooks import (
            install as hook_install,
            uninstall as hook_uninstall,
            status as hook_status,
        )

        subcmd = sys.argv[2] if len(sys.argv) > 2 else ""
        if subcmd == "install":
            print(hook_install(Path(".")))
        elif subcmd == "uninstall":
            print(hook_uninstall(Path(".")))
        elif subcmd == "status":
            print(hook_status(Path(".")))
        else:
            print("Usage: omnigraph hook [install|uninstall|status]", file=sys.stderr)
            sys.exit(1)
    elif cmd == "query":
        if len(sys.argv) < 3:
            print("Usage: omnigraph query \"<question>\" [--dfs] [--context C] [--budget N] [--graph path]", file=sys.stderr)
            sys.exit(1)
        from omnigraph.serve import _query_graph_text
        from omnigraph.security import sanitize_label
        from networkx.readwrite import json_graph
        from omnigraph import querylog

        question = sys.argv[2]
        use_dfs = "--dfs" in sys.argv
        budget = 2000
        graph_path = _default_graph_path()
        context_filters: list[str] = []
        args = sys.argv[3:]
        i = 0
        while i < len(args):
            if args[i] == "--budget" and i + 1 < len(args):
                try:
                    budget = int(args[i + 1])
                except ValueError:
                    print(f"error: --budget must be an integer", file=sys.stderr)
                    sys.exit(1)
                i += 2
            elif args[i].startswith("--budget="):
                try:
                    budget = int(args[i].split("=", 1)[1])
                except ValueError:
                    print(f"error: --budget must be an integer", file=sys.stderr)
                    sys.exit(1)
                i += 1
            elif args[i] == "--context" and i + 1 < len(args):
                context_filters.append(args[i + 1])
                i += 2
            elif args[i].startswith("--context="):
                context_filters.append(args[i].split("=", 1)[1])
                i += 1
            elif args[i] == "--graph" and i + 1 < len(args):
                graph_path = args[i + 1]
                i += 2
            else:
                i += 1
        gp = Path(graph_path).resolve()
        if not gp.exists():
            print(f"error: graph file not found: {gp}", file=sys.stderr)
            sys.exit(1)
        if not gp.suffix == ".json":
            print(f"error: graph file must be a .json file", file=sys.stderr)
            sys.exit(1)
        _enforce_graph_size_cap_or_exit(gp)
        try:
            import json as _json
            import networkx as _nx

            _raw = _json.loads(gp.read_text(encoding="utf-8"))
            if "links" not in _raw and "edges" in _raw:
                _raw = dict(_raw, links=_raw["edges"])
            # `query` deliberately keeps the graph undirected (unlike `path` /
            # `explain`, which force directed=True): BFS/DFS here must explore
            # both callers and callees of the seed node to build useful
            # context, and forcing a DiGraph would make G.neighbors() return
            # successors only, silently dropping every caller-side result for
            # a seed with no outgoing edges. Direction is instead preserved
            # per-edge below (mirrors omnigraph/build.py's _src/_tgt pattern)
            # so the *rendering* stays correct without narrowing traversal.
            # Keep in-file markers when present: unconditionally
            # overwriting them with source/target would clobber the true
            # direction of a link persisted in flipped endpoint order.
            _raw = dict(
                _raw,
                links=[
                    {
                        **link,
                        "_src": link.get("_src", link.get("source")),
                        "_tgt": link.get("_tgt", link.get("target")),
                    }
                    for link in _raw.get("links", [])
                ],
            )
            try:
                G = json_graph.node_link_graph(_raw, edges="links")
            except TypeError:
                G = json_graph.node_link_graph(_raw)
            try:
                from omnigraph.build import graph_has_legacy_ids as _legacy
                if _legacy(_raw.get("nodes", [])):
                    print(
                        "[omnigraph] note: this graph uses the pre-#1504 node-ID scheme; "
                        "rebuild with `omnigraph extract --force` to get path-qualified IDs "
                        "(fixes same-name-file collisions).",
                        file=sys.stderr,
                    )
            except Exception:
                pass
        except Exception as exc:
            print(f"error: could not load graph: {exc}", file=sys.stderr)
            sys.exit(1)
        import time as _time
        _t0 = _time.perf_counter()
        _mode = "dfs" if use_dfs else "bfs"
        _result = _query_graph_text(
            G,
            question,
            mode=_mode,
            depth=2,
            token_budget=budget,
            context_filters=context_filters,
        )
        querylog.log_query(
            kind="query",
            question=question,
            corpus=str(gp),
            result=_result,
            mode=_mode,
            depth=2,
            token_budget=budget,
            duration_ms=(_time.perf_counter() - _t0) * 1000,
        )
        _touch_query_stamp(gp)
        print(_result)
    elif cmd == "affected":
        if len(sys.argv) < 3:
            print("Usage: omnigraph affected \"<node-or-label>\" [--relation R] [--depth N] [--graph path]", file=sys.stderr)
            sys.exit(1)
        from omnigraph.affected import DEFAULT_AFFECTED_RELATIONS, format_affected, load_graph
        query = sys.argv[2]
        graph_path = _default_graph_path()
        depth = 2
        relations: list[str] = []
        args = sys.argv[3:]
        i = 0
        while i < len(args):
            if args[i] == "--graph" and i + 1 < len(args):
                graph_path = args[i + 1]
                i += 2
            elif args[i].startswith("--graph="):
                graph_path = args[i].split("=", 1)[1]
                i += 1
            elif args[i] == "--depth" and i + 1 < len(args):
                try:
                    depth = int(args[i + 1])
                except ValueError:
                    print("error: --depth must be an integer", file=sys.stderr)
                    sys.exit(1)
                i += 2
            elif args[i].startswith("--depth="):
                try:
                    depth = int(args[i].split("=", 1)[1])
                except ValueError:
                    print("error: --depth must be an integer", file=sys.stderr)
                    sys.exit(1)
                i += 1
            elif args[i] == "--relation" and i + 1 < len(args):
                relations.append(args[i + 1])
                i += 2
            elif args[i].startswith("--relation="):
                relations.append(args[i].split("=", 1)[1])
                i += 1
            else:
                i += 1
        gp = Path(graph_path).resolve()
        if not gp.exists():
            print(f"error: graph file not found: {gp}", file=sys.stderr)
            sys.exit(1)
        if not gp.suffix == ".json":
            print("error: graph file must be a .json file", file=sys.stderr)
            sys.exit(1)
        try:
            graph = load_graph(gp)
        except Exception as exc:
            print(f"error: could not load graph: {exc}", file=sys.stderr)
            sys.exit(1)
        print(
            format_affected(
                graph,
                query,
                relations=relations or DEFAULT_AFFECTED_RELATIONS,
                depth=depth,
            )
        )
    elif cmd in ("god-nodes", "god_nodes"):
        # god_nodes has long been an analyzer (analyze.py), an MCP tool, and a
        # README-advertised capability, but never a CLI subcommand — `omnigraph
        # god_nodes` fell through to "unknown command". Wire it as a
        # read-only graph query, mirroring `affected`.
        from omnigraph.affected import load_graph
        from omnigraph.analyze import god_nodes as _god_nodes
        from omnigraph.security import sanitize_label as _sanitize_label
        graph_path = _default_graph_path()
        top_n = 10
        as_json = "--json" in sys.argv
        args = sys.argv[2:]
        i = 0
        while i < len(args):
            if args[i] == "--graph" and i + 1 < len(args):
                graph_path = args[i + 1]
                i += 2
            elif args[i].startswith("--graph="):
                graph_path = args[i].split("=", 1)[1]
                i += 1
            elif args[i] == "--top" and i + 1 < len(args):
                try:
                    top_n = int(args[i + 1])
                except ValueError:
                    print("error: --top must be an integer", file=sys.stderr)
                    sys.exit(1)
                i += 2
            elif args[i].startswith("--top="):
                try:
                    top_n = int(args[i].split("=", 1)[1])
                except ValueError:
                    print("error: --top must be an integer", file=sys.stderr)
                    sys.exit(1)
                i += 1
            else:
                i += 1
        gp = Path(graph_path).resolve()
        if not gp.exists():
            print(f"error: graph file not found: {gp}", file=sys.stderr)
            sys.exit(1)
        if not gp.suffix == ".json":
            print("error: graph file must be a .json file", file=sys.stderr)
            sys.exit(1)
        try:
            G = load_graph(gp)
        except Exception as exc:
            print(f"error: could not load graph: {exc}", file=sys.stderr)
            sys.exit(1)
        gods = _god_nodes(G, top_n=top_n)
        if as_json:
            print(json.dumps(gods, indent=2))
        else:
            print("God nodes (most connected):")
            for rank, n in enumerate(gods, 1):
                print(f"  {rank}. {_sanitize_label(str(n['label']))} - {n['degree']} edges")
    elif cmd == "save-result":
        # omnigraph salvar resultado --question Q --resposta A [--type T] [-nodes N1 N2 ...]
        #                      [--outcome useful|dead_end|corrected] [--correction TEXT]
        import argparse as _ap

        p = _ap.ArgumentParser(prog="omnigraph save-result")
        p.add_argument("--question", required=True)
        p.add_argument("--answer", default=None)
        p.add_argument("--answer-file", dest="answer_file", default=None)
        p.add_argument("--type", dest="query_type", default="query")
        p.add_argument("--nodes", nargs="*", default=[])
        p.add_argument("--outcome", choices=("useful", "dead_end", "corrected"), default=None)
        p.add_argument("--correction", default=None)
        p.add_argument("--memory-dir", default=str(Path(_OMNIGRAPH_OUT) / "memory"))
        opts = p.parse_args(sys.argv[2:])
        if opts.answer_file:
            opts.answer = Path(opts.answer_file).read_text(encoding="utf-8").strip()
        elif not opts.answer:
            p.error("--answer or --answer-file is required")
        from omnigraph.ingest import save_query_result as _sqr

        out = _sqr(
            question=opts.question,
            answer=opts.answer,
            memory_dir=Path(opts.memory_dir),
            query_type=opts.query_type,
            source_nodes=opts.nodes or None,
            outcome=opts.outcome,
            correction=opts.correction,
        )
        print(f"Saved to {out}")
    elif cmd == "reflect":
        import argparse as _ap

        p = _ap.ArgumentParser(prog="omnigraph reflect")
        p.add_argument("--memory-dir", default=str(Path(_OMNIGRAPH_OUT) / "memory"))
        p.add_argument(
            "--out",
            default=str(Path(_OMNIGRAPH_OUT) / "reflections" / "LESSONS.md"),
        )
        p.add_argument("--graph", default=None)
        p.add_argument("--analysis", default=None)
        p.add_argument("--labels", default=None)
        p.add_argument("--half-life-days", type=float, default=30.0,
                       help="signal weight halves every N days (default 30)")
        p.add_argument("--min-corroboration", type=int, default=2,
                       help="distinct useful results to promote a node to preferred (default 2)")
        p.add_argument("--if-stale", action="store_true",
                       help="skip when LESSONS.md is already newer than every input "
                            "(e.g. the git hook just refreshed it)")
        opts = p.parse_args(sys.argv[2:])
        from omnigraph.reflect import reflect as _reflect, lessons_fresh as _lessons_fresh

        graph_arg = opts.graph
        if graph_arg is None:
            default_graph = Path(_OMNIGRAPH_OUT) / "graph.json"
            if default_graph.exists():
                graph_arg = str(default_graph)

        _gp = Path(graph_arg) if graph_arg else None
        _analysis_path = None
        _labels_path = None
        if _gp is not None:
            _analysis_path = Path(opts.analysis) if opts.analysis else (
                _gp.parent / ".omnigraph_analysis.json")
            _labels_path = Path(opts.labels) if opts.labels else (
                _gp.parent / ".omnigraph_labels.json")

        if opts.if_stale and _lessons_fresh(
            Path(opts.out), Path(opts.memory_dir), _gp, _analysis_path, _labels_path
        ):
            print(f"Lessons already up to date -> {opts.out} (skipped; omit --if-stale to force)")
        else:
            out_path, agg = _reflect(
                memory_dir=Path(opts.memory_dir),
                out_path=Path(opts.out),
                graph_path=_gp,
                analysis_path=_analysis_path,
                labels_path=_labels_path,
                half_life_days=opts.half_life_days,
                min_corroboration=opts.min_corroboration,
            )
            c = agg["counts"]
            print(
                f"Reflected {agg['total']} memories "
                f"({c['useful']} useful, {c['dead_end']} dead ends, "
                f"{c['corrected']} corrected) -> {out_path}"
            )
    elif cmd == "path":
        if len(sys.argv) < 4:
            print(
                'Usage: omnigraph path "<source>" "<target>" [--graph path]',
                file=sys.stderr,
            )
            sys.exit(1)
        from omnigraph.serve import _pick_scored_endpoint, _score_nodes
        from networkx.readwrite import json_graph
        import networkx as _nx

        source_label = sys.argv[2]
        target_label = sys.argv[3]
        graph_path = _default_graph_path()
        args = sys.argv[4:]
        for i, a in enumerate(args):
            if a == "--graph" and i + 1 < len(args):
                graph_path = args[i + 1]
        gp = Path(graph_path).resolve()
        if not gp.exists():
            print(f"error: graph file not found: {gp}", file=sys.stderr)
            sys.exit(1)
        _enforce_graph_size_cap_or_exit(gp)
        _raw = json.loads(gp.read_text(encoding="utf-8"))
        if "links" not in _raw and "edges" in _raw:
            _raw = dict(_raw, links=_raw["edges"])
        # Force directed so the renderer can recover stored caller→callee
        # direction, and multigraph so exact-pair parallel links (e.g. a
        # `references` and a `calls` edge between the same two nodes) survive load
        # instead of being silently collapsed last-writer-wins — otherwise the
        # printed relation could be one the traversed pair doesn't actually
        # carry. Local to this read; serve's shared graph is untouched.
        _raw = {**_raw, "directed": True, "multigraph": True}
        try:
            G = json_graph.node_link_graph(_raw, edges="links")
        except TypeError:
            G = json_graph.node_link_graph(_raw)
        src_scored = _score_nodes(G, [t.lower() for t in source_label.split()])
        tgt_scored = _score_nodes(G, [t.lower() for t in target_label.split()])
        if not src_scored:
            print(f"No node matching '{source_label}' found.", file=sys.stderr)
            sys.exit(1)
        if not tgt_scored:
            print(f"No node matching '{target_label}' found.", file=sys.stderr)
            sys.exit(1)
        src_nid = _pick_scored_endpoint(G, src_scored, source_label)
        tgt_nid = _pick_scored_endpoint(G, tgt_scored, target_label)
        # Proteção de ambigüidade: quando ambas as consultas são resolvidas para o mesmo nó, o
        # o caminho mais curto é trivialmente zero saltos, o que quase nunca é o que o
        # caller wanted (see bug).
        if src_nid == tgt_nid:
            print(
                f"'{source_label}' and '{target_label}' both resolved to the same "
                f"node '{src_nid}'. Use a more specific label or the exact node ID.",
                file=sys.stderr,
            )
            sys.exit(1)
        for _name, _scored, _nid in (
            ("source", src_scored, src_nid),
            ("target", tgt_scored, tgt_nid),
        ):
            # Um segundo colocado só tornou a resolução ambígua quando o assunto bruto
            # cabeça de pontuação é o que foi escolhido; uma substituição de token completo foi escolhida em
            # cobertura simbólica, não pontuação, portanto a margem da cabeça é irrelevante.
            if len(_scored) >= 2 and _nid == _scored[0][1]:
                _top, _runner = _scored[0][0], _scored[1][0]
                if _top > 0 and (_top - _runner) / _top < 0.10:
                    print(
                        f"warning: {_name} match was ambiguous "
                        f"(top score {_top:g}, runner-up {_runner:g})",
                        file=sys.stderr,
                    )
        # Deterministic shortest path: to_undirected(as_view=True)
        # iterates neighbors via a hash-seeded set union, so among equal-length
        # paths BFS returned an arbitrary route that varied per process. Build a
        # sorted, materialized undirected graph so neighbor order — and thus the
        # chosen path — is canonical for a given graph.json.
        _und = _nx.Graph()
        _und.add_nodes_from(sorted(G.nodes))
        _und.add_edges_from(sorted((min(u, v), max(u, v)) for u, v in G.edges()))
        try:
            path_nodes = _nx.shortest_path(_und, src_nid, tgt_nid)
        except (_nx.NetworkXNoPath, _nx.NodeNotFound):
            print(f"No path found between '{source_label}' and '{target_label}'.")
            sys.exit(0)
        hops = len(path_nodes) - 1
        segments = []
        from omnigraph.build import edge_datas
        for i in range(len(path_nodes) - 1):
            u, v = path_nodes[i], path_nodes[i + 1]
            # Report the ACTUAL stored relation(s) of the traversed pair and
            # direction — never a fabricated `calls`. A pair may carry
            # several parallel relations; show all, and fall back to an honest
            # "related" when the stored edge has no relation.
            # Direction truth lives in the per-link _src/_tgt markers:
            # undirected NetworkX storage canonicalizes endpoint order, so the
            # persisted source/target arc can be flipped relative to the real
            # caller→callee direction. Recover it from _src when present, else
            # fall back to the loaded arc tail (markerless canonical files keep
            # today's behavior).
            fwd, bwd = [], []
            for a, b in ((u, v), (v, u)):
                if G.has_edge(a, b):
                    for d in edge_datas(G, a, b):
                        (fwd if d.get("_src", a) == u else bwd).append(d)
            datas = fwd or bwd
            forward = bool(fwd)
            rels = sorted({d.get("relation") for d in datas if d.get("relation")})
            rel = "/".join(rels) if rels else "related"
            confs = sorted({d.get("confidence") for d in datas if d.get("confidence")})
            conf_str = f" [{'/'.join(confs)}]" if confs else ""
            if i == 0:
                segments.append(G.nodes[u].get("label", u))
            if forward:
                segments.append(f"--{rel}{conf_str}--> {G.nodes[v].get('label', v)}")
            else:
                segments.append(f"<--{rel}{conf_str}-- {G.nodes[v].get('label', v)}")
        print(f"Shortest path ({hops} hops):\n  " + " ".join(segments))
        from omnigraph import querylog
        querylog.log_query(
            kind="path",
            question=f"{sys.argv[2]} -> {sys.argv[3]}",
            corpus=str(gp),
            nodes_returned=hops,
        )
        _touch_query_stamp(gp)

    elif cmd == "explain":
        if len(sys.argv) < 3:
            print('Usage: omnigraph explain "<node>" [--graph path]', file=sys.stderr)
            sys.exit(1)
        from omnigraph.serve import _find_node, find_node_ambiguity
        from networkx.readwrite import json_graph

        label = sys.argv[2]
        graph_path = _default_graph_path()
        args = sys.argv[3:]
        for i, a in enumerate(args):
            if a == "--graph" and i + 1 < len(args):
                graph_path = args[i + 1]
        gp = Path(graph_path).resolve()
        if not gp.exists():
            print(f"error: graph file not found: {gp}", file=sys.stderr)
            sys.exit(1)
        _enforce_graph_size_cap_or_exit(gp)
        _raw = json.loads(gp.read_text(encoding="utf-8"))
        if "links" not in _raw and "edges" in _raw:
            _raw = dict(_raw, links=_raw["edges"])
        # Força direcionada para que o renderizador possa recuperar a direção armazenada do chamador → receptor.
        _raw = {**_raw, "directed": True}
        try:
            G = json_graph.node_link_graph(_raw, edges="links")
        except TypeError:
            G = json_graph.node_link_graph(_raw)
        matches = _find_node(G, label)
        if not matches:
            print(f"No node matching '{label}' found.")
            sys.exit(0)
        rivals = find_node_ambiguity(G, label)
        if rivals:
            print(f"Ambiguous: '{label}' matches {len(rivals)} nodes in different files.")
            for rival in rivals:
                print(f"  {G.nodes[rival].get('source_file') or rival}")
                print(f"    id: {rival}")
            print("Retry with the repo-relative path or the full node id.")
            sys.exit(1)
        nid = matches[0]
        d = G.nodes[nid]
        print(f"Node: {d.get('label', nid)}")
        print(f"  ID:        {nid}")
        print(
            f"  Source:    {d.get('source_file', '')} {d.get('source_location', '')}".rstrip()
        )
        print(f"  Type:      {d.get('file_type', '')}")
        print(f"  Community: {d.get('community_name') or d.get('community', '')}")
        # Sobreposição de memória de trabalho: uma dica experiencial derivada de `omnigraph reflect`,
        # mesclado somente para exibição do arquivo secundário .omnigraph_learning.json próximo a
        # grafo.json. Nenhuma linha quando o nó não possui entrada de sobreposição.
        try:
            from omnigraph.reflect import load_learning_overlay as _llo
            from omnigraph.security import sanitize_label as _sl
            _overlay = _llo(gp)
            _entry = _overlay.get(str(nid))
            if _entry:
                _status = _sl(str(_entry.get("status", "")))
                if _status == "contested":
                    _line = (f"  Lesson: contested (useful {_entry.get('uses', 0)} / "
                             f"dead-end {_entry.get('neg', 0)})")
                elif _status == "preferred":
                    _line = (f"  Lesson: preferred source (start here) — "
                             f"{_entry.get('uses', 0)} useful, score={_entry.get('score', 0)}")
                else:
                    _line = (f"  Lesson: {_status or 'tentative'} — "
                             f"{_entry.get('uses', 0)} useful, score={_entry.get('score', 0)}")
                if _entry.get("stale"):
                    _line += " [code changed since — re-verify]"
                print(_line)
        except Exception:
            pass
        print(f"  Degree:    {G.degree(nid)}")
        from omnigraph.build import edge_data
        connections: list[tuple[str, str, dict]] = []  # (direction, neighbor_id, edge_data)
        # Classify by the edge's TRUE direction, not the loaded arc order:
        # a link persisted in flipped endpoint order carries its truth in the
        # per-edge _src marker. Markerless edges fall back to the arc
        # tail (today's behavior).
        for nb in G.successors(nid):
            _ed = edge_data(G, nid, nb)
            connections.append(
                ("out" if _ed.get("_src", nid) == nid else "in", nb, _ed)
            )
        for nb in G.predecessors(nid):
            _ed = edge_data(G, nb, nid)
            connections.append(
                ("in" if _ed.get("_src", nb) == nb else "out", nb, _ed)
            )
        if connections:
            print(f"\nConnections ({len(connections)}):")
            connections.sort(key=lambda c: G.degree(c[1]), reverse=True)
            for direction, nb, edata in connections[:20]:
                rel = edata.get("relation", "")
                conf = edata.get("confidence", "")
                arrow = "-->" if direction == "out" else "<--"
                # Append the edge's location — the actual call/import/reference
                # SITE (in the caller's file for an incoming call), not a def
                # line (#BUG1). Labeled by [rel] so the meaning is unambiguous.
                loc = edata.get("source_location") or ""
                sfile = edata.get("source_file") or ""
                at = f" {sfile}:{loc}" if loc else ""
                print(f"  {arrow} {G.nodes[nb].get('label', nb)} [{rel}] [{conf}]{at}")
            if len(connections) > 20:
                remainder = connections[20:]
                print(f"  ... and {len(remainder)} more")
                # a bare count silently hides the answer on high-degree
                # nodes ("who calls this, what's the impact?"). Group the cut
                # connections by direction + file so their shape is visible
                # without falling back to a repo-wide grep.
                by_file: dict[tuple[str, str], int] = {}
                for direction, _nb, edata in remainder:
                    sfile = edata.get("source_file") or "(unknown file)"
                    key = (direction, sfile)
                    by_file[key] = by_file.get(key, 0) + 1
                # Count desc, then (direction, file) so equal-count groups have a
                # byte-stable order (not the degree-derived insertion order).
                grouped = sorted(by_file.items(), key=lambda kv: (-kv[1], kv[0]))
                print("  Grouped by file:")
                for (direction, sfile), count in grouped[:20]:
                    arrow = "-->" if direction == "out" else "<--"
                    noun = "connection" if count == 1 else "connections"
                    print(f"    {arrow} {sfile}: {count} {noun}")
                if len(grouped) > 20:
                    print(f"    ... and {len(grouped) - 20} more files")
        from omnigraph import querylog
        querylog.log_query(
            kind="explain",
            question=sys.argv[2],
            corpus=str(gp),
            nodes_returned=len(connections),
        )
        _touch_query_stamp(gp)

    elif cmd == "diagnose":
        subcmd = sys.argv[2] if len(sys.argv) > 2 else ""
        if subcmd != "multigraph":
            print(
                "Usage: omnigraph diagnose multigraph "
                "[--graph path] [--json] [--max-examples N] "
                "[--directed] [--undirected] [--extract-path path]",
                file=sys.stderr,
            )
            sys.exit(1)

        graph_path = Path(_default_graph_path())
        max_examples = 5
        directed: bool | None = None
        direction_flag: str | None = None
        json_output = False
        extract_path: Path | None = None

        i = 3
        while i < len(sys.argv):
            arg = sys.argv[i]
            if arg == "--graph":
                i += 1
                if i >= len(sys.argv):
                    print("error: --graph requires a path", file=sys.stderr)
                    sys.exit(1)
                graph_path = Path(sys.argv[i])
            elif arg == "--json":
                json_output = True
            elif arg == "--max-examples":
                i += 1
                if i >= len(sys.argv):
                    print("error: --max-examples requires an integer", file=sys.stderr)
                    sys.exit(1)
                try:
                    max_examples = int(sys.argv[i])
                except ValueError:
                    print("error: --max-examples requires an integer", file=sys.stderr)
                    sys.exit(1)
                if max_examples < 0:
                    print("error: --max-examples must be >= 0", file=sys.stderr)
                    sys.exit(1)
            elif arg == "--directed":
                if direction_flag == "undirected":
                    print(
                        "error: --directed and --undirected are mutually exclusive",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                direction_flag = "directed"
                directed = True
            elif arg == "--undirected":
                if direction_flag == "directed":
                    print(
                        "error: --directed and --undirected are mutually exclusive",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                direction_flag = "undirected"
                directed = False
            elif arg == "--extract-path":
                i += 1
                if i >= len(sys.argv):
                    print("error: --extract-path requires a path", file=sys.stderr)
                    sys.exit(1)
                extract_path = Path(sys.argv[i])
            else:
                print(f"error: unknown diagnose option {arg}", file=sys.stderr)
                sys.exit(1)
            i += 1

        from omnigraph.diagnostics import (
            diagnose_file,
            format_diagnostic_json,
            format_diagnostic_report,
        )

        try:
            summary = diagnose_file(
                graph_path,
                directed=directed,
                root=Path(".").resolve(),
                max_examples=max_examples,
                extract_path=extract_path,
            )
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)

        if json_output:
            print(json.dumps(format_diagnostic_json(summary), indent=2))
        else:
            print(format_diagnostic_report(summary))

    elif cmd == "add":
        if len(sys.argv) < 3:
            print(
                "Usage: omnigraph add <url> [--author Name] [--contributor Name] [--dir ./raw]",
                file=sys.stderr,
            )
            sys.exit(1)
        from omnigraph.ingest import ingest as _ingest

        url = sys.argv[2]
        author: str | None = None
        contributor: str | None = None
        target_dir = Path("raw")
        args = sys.argv[3:]
        i = 0
        while i < len(args):
            if args[i] == "--author" and i + 1 < len(args):
                author = args[i + 1]
                i += 2
            elif args[i] == "--contributor" and i + 1 < len(args):
                contributor = args[i + 1]
                i += 2
            elif args[i] == "--dir" and i + 1 < len(args):
                target_dir = Path(args[i + 1])
                i += 2
            else:
                i += 1
        try:
            saved = _ingest(url, target_dir, author=author, contributor=contributor)
            print(f"Saved to {saved}")
            print("Run /omnigraph --update in your AI assistant to update the graph.")
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)

    elif cmd == "watch":
        watch_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".")
        if not watch_path.exists():
            print(f"error: path not found: {watch_path}", file=sys.stderr)
            sys.exit(1)
        from omnigraph.watch import watch as _watch

        try:
            _watch(watch_path)
        except ImportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)

    elif cmd in ("cluster-only", "label"):
        # `label` é `cluster-only` que sempre (re)gera nomes de comunidades com
        # o back-end configurado, mesmo quando já existe um .omnigraph_labels.json.
        force_relabel = cmd == "label"
        # Espelhe o padrão de análise de arg da árvore/exportação: ande argv so flags e
        # o caminho posicional opcional pode aparecer em qualquer ordem.
        no_viz = "--no-viz" in sys.argv
        no_label = "--no-label" in sys.argv
        missing_only = "--missing-only" in sys.argv
        co_timing = "--timing" in sys.argv
        _backend_arg = next((a for a in sys.argv if a.startswith("--backend=")), None)
        label_backend = _backend_arg.split("=", 1)[1] if _backend_arg else None
        _model_arg = next((a for a in sys.argv if a.startswith("--model=")), None)
        label_model = _model_arg.split("=", 1)[1] if _model_arg else None
        _min_cs_arg = next((a for a in sys.argv if a.startswith("--min-community-size=")), None)
        min_community_size = int(_min_cs_arg.split("=")[1]) if _min_cs_arg else 3
        args = sys.argv[2:]
        watch_path: Path | None = None
        graph_override: Path | None = None
        co_resolution: float = 1.0
        co_exclude_hubs: float | None = None
        label_max_concurrency: int = 4
        label_batch_size: int = 100
        i_arg = 0
        while i_arg < len(args):
            a = args[i_arg]
            if a == "--graph" and i_arg + 1 < len(args):
                graph_override = Path(args[i_arg + 1]); i_arg += 2
            elif a == "--backend" and i_arg + 1 < len(args):
                label_backend = args[i_arg + 1]; i_arg += 2
            elif a.startswith("--backend="):
                label_backend = a.split("=", 1)[1]; i_arg += 1
            elif a == "--model" and i_arg + 1 < len(args):
                label_model = args[i_arg + 1]; i_arg += 2
            elif a.startswith("--model="):
                label_model = a.split("=", 1)[1]; i_arg += 1
            elif a == "--resolution" and i_arg + 1 < len(args):
                co_resolution = float(args[i_arg + 1]); i_arg += 2
            elif a.startswith("--resolution="):
                co_resolution = float(a.split("=", 1)[1]); i_arg += 1
            elif a == "--exclude-hubs" and i_arg + 1 < len(args):
                co_exclude_hubs = float(args[i_arg + 1]); i_arg += 2
            elif a.startswith("--exclude-hubs="):
                co_exclude_hubs = float(a.split("=", 1)[1]); i_arg += 1
            elif a == "--max-concurrency" and i_arg + 1 < len(args):
                label_max_concurrency = int(args[i_arg + 1]); i_arg += 2
            elif a.startswith("--max-concurrency="):
                label_max_concurrency = int(a.split("=", 1)[1]); i_arg += 1
            elif a == "--batch-size" and i_arg + 1 < len(args):
                label_batch_size = int(args[i_arg + 1]); i_arg += 2
            elif a.startswith("--batch-size="):
                label_batch_size = int(a.split("=", 1)[1]); i_arg += 1
            elif a in ("--no-viz", "--missing-only") or a.startswith("--min-community-size="):
                i_arg += 1
            elif a.startswith("--"):
                i_arg += 1
            elif watch_path is None:
                watch_path = Path(a); i_arg += 1
            else:
                i_arg += 1
        if watch_path is None:
            watch_path = Path(".")
        graph_json = graph_override if graph_override is not None else watch_path / _OMNIGRAPH_OUT / "graph.json"
        if not graph_json.exists():
            print(
                f"error: no graph found at {graph_json} — run /omnigraph first",
                file=sys.stderr,
            )
            sys.exit(1)
        from networkx.readwrite import json_graph as _jg
        from omnigraph.build import build_from_json
        from omnigraph.cluster import cluster, score_all, remap_communities_to_previous
        from omnigraph.analyze import (
            god_nodes,
            surprising_connections,
            suggest_questions,
        )
        from omnigraph.report import generate
        from omnigraph.export import to_json, to_html

        stages = _StageTimer(co_timing)
        print("Loading existing graph...")
        # Solução 3: não faça hard-exit em um graph.json superdimensionado aqui.
        # As saídas principais (graph.json + GRAPH_REPORT.md) ainda são gravadas; o
        # A renderização graph.html abaixo volta para a visualização de agregação da comunidade
        # (node_limit=5000) quando estiver acima do limite.
        from omnigraph.security import check_graph_file_size_cap as _check_cap
        _over_cap = False
        try:
            _check_cap(graph_json)
        except ValueError:
            _over_cap = True
            try:
                _over_cap_bytes = graph_json.stat().st_size
            except OSError:
                _over_cap_bytes = -1
            print(
                f"warning: graph.json exceeds cap ({_over_cap_bytes} bytes); "
                f"falling back to community-aggregation view (node_limit=5000)",
                file=sys.stderr,
            )
        _raw = json.loads(graph_json.read_text(encoding="utf-8"))
        _directed = bool(_raw.get("directed", False))
        G = build_from_json(_raw, directed=_directed)
        print(f"Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
        stages.mark("load")
        print("Re-clustering...")
        communities = cluster(G, resolution=co_resolution, exclude_hubs_percentile=co_exclude_hubs)
        # Espelhe o caminho de observação/atualização: mapeie novos cids para os anteriores
        # node-overlap para que o .omnigraph_labels.json existente continue anexando
        # para a mesma comunidade conceitual após o reagrupamento. Sem isso,
        # os rótulos seguem o índice cid bruto e ficam desalinhados sempre que o
        # o grafo mudou entre rotulagem e somente cluster.
        previous_node_community = {
            n["id"]: n["community"]
            for n in _raw.get("nodes", [])
            if n.get("community") is not None and n.get("id") is not None
        }
        if previous_node_community:
            communities = remap_communities_to_previous(communities, previous_node_community)
        stages.mark("cluster")
        cohesion = score_all(G, communities)
        gods = god_nodes(G)
        surprises = surprising_connections(G, communities)
        stages.mark("analyze")
        # Onde as saídas (GRAPH_REPORT.md, graph.json reagrupado, rótulos,
        # análise, html) terreno. Quando `--graph` aponta para um grafo DENTRO de um
        # omnigraph-out/ dir (saída de outro projeto/inquilino), escreva ao lado dele,
        # não em um omnigraph-out/ no CWD. Mas quando `--graph`
        # aponta para um caminho arbitrário - por ex. um `backup/graph.json` arquivado
        # antes do reagrupamento — volte para o omnigraph-out/ do CWD,
        # que é o fluxo de trabalho de restauração no local que testa os pinos. O padrão
        # (sem --graph) o caso já possui graph_json em watch_path/omnigraph-out.
        _out_name = Path(_OMNIGRAPH_OUT).name
        if graph_override is not None and graph_json.parent.name == _out_name:
            out = graph_json.parent
        else:
            out = watch_path / _OMNIGRAPH_OUT
        out.mkdir(parents=True, exist_ok=True)
        labels_path = out / ".omnigraph_labels.json"
        existing_labels: dict[int, str] = {}
        if labels_path.exists():
            try:
                existing_labels = {
                    int(k): v
                    for k, v in json.loads(labels_path.read_text(encoding="utf-8")).items()
                    if isinstance(v, str)
                }
            except Exception:
                existing_labels = {}
        # Acumule o uso de token das chamadas LLM de rotulagem para modo somente cluster
        # relata o custo real em vez de um zero codificado. Permanece {0, 0} ativado
        # os caminhos reutilizados/sem rótulo, que não fazem chamadas LLM.
        label_token_usage = {"input": 0, "output": 0}
        # a --no-label run produces only "Community N" placeholders.
        # Persisting them (plus a matching .sig) made the reuse branch treat them
        # as fresh forever, permanently blocking real labeling on later runs.
        placeholder_only = False
        if labels_path.exists() and not force_relabel:
            # Reutilize rótulos salvos, mas não confie cegamente neles: o grafo pode ter
            # foi redefinido/reagrupado desde a rotulagem, caso em que um cid agora
            # cobre uma comunidade DIFERENTE e seu nome antigo (LLM) está errado (#label-stale).
            # Valide cada comunidade com base na assinatura de membro salva ao lado do
            # rótulos; qualquer comunidade que mudou (ou não tem rótulo salvo) é renomeada por
            # seu hub atual - determinístico e correto por construção - e o usuário
            # é instruído a `omnigraph label` para novos nomes de LLM. Comunidades inalteradas mantêm
            # seu rótulo salvo. Quando não existe nenhum sidecar de assinatura (os rótulos são anteriores a isso),
            # volte ao preenchimento central apenas das comunidades que faltam um rótulo.
            from omnigraph.cluster import community_member_sigs, label_communities_by_hub
            sig_path = labels_path.parent / (labels_path.name + ".sig")
            saved_sigs: dict[int, str] = {}
            if sig_path.exists():
                try:
                    saved_sigs = {
                        int(k): v for k, v in
                        json.loads(sig_path.read_text(encoding="utf-8")).items()
                        if isinstance(v, str)
                    }
                except Exception:
                    saved_sigs = {}
            cur_sigs = community_member_sigs(communities)
            count_mismatch = len(existing_labels) != len(communities)
            labels = {}
            hub_labels: dict[int, str] | None = None
            changed = 0
            for cid in communities:
                # A persisted "Community {cid}" is a placeholder, not an earned
                # label — treat it as absent so the hub labeler replaces it and an
                # already-polluted sidecar (e.g. from a prior --no-label run) heals
                # instead of suppressing real labels forever.
                have_label = (
                    cid in existing_labels
                    and existing_labels[cid] != f"Community {cid}"
                )
                if saved_sigs:
                    # Preciso: a assinatura de membro nos diz se isso é exato
                    # comunidade mudou desde que foi rotulada.
                    fresh = have_label and saved_sigs.get(cid) == cur_sigs.get(cid)
                else:
                    # Nenhum sidecar de assinatura (os rótulos são anteriores). Uma comunidade diferente
                    # COUNT significa que os rótulos descrevem um agrupamento diferente, então um cid
                    # rótulo antigo não é confiável; contagem igual é o melhor sinal "mesmo".
                    fresh = have_label and not count_mismatch
                if fresh:
                    labels[cid] = existing_labels[cid]
                else:
                    if hub_labels is None:
                        hub_labels = label_communities_by_hub(G, communities)
                    labels[cid] = hub_labels[cid]
                    if have_label:
                        changed += 1
            if changed:
                print(
                    f"[omnigraph] community set changed since labeling "
                    f"({len(existing_labels)} saved labels, {len(communities)} communities now; "
                    f"renamed {changed} community(ies) by their hub). "
                    f"Run `omnigraph label` to refresh names with the LLM.",
                    file=sys.stderr,
                )
        elif no_label and not force_relabel:
            labels = {cid: f"Community {cid}" for cid in communities}
            placeholder_only = True
        else:
            # Nenhum arquivo de rótulos ainda (ou `omnigraph label` forçou uma atualização). Quando executado
            # autônomo, não há agente de orquestração para executar o skill.md Etapa 5, então
            # nomear automaticamente as comunidades em vez de sair de "Comunidade N".
            from omnigraph.cluster import label_communities_by_hub
            from omnigraph.llm import generate_community_labels
            print("Labeling communities...")
            # Rótulos básicos determinísticos e livres de LLM: nomeie cada comunidade com base em seu
            # hub de mais alto nível, para que o relatório seja legível mesmo sem back-end
            # (anteriormente denominado "Comunidade N"). Um back-end LLM configurado substitui esses
            # com nomes mais ricos abaixo; seu substituto de espaço reservado sem backend NÃO.
            hub_labels = label_communities_by_hub(G, communities)
            label_communities_input = communities
            labels = dict(hub_labels)
            if missing_only:
                labels = {
                    cid: existing_labels.get(cid, hub_labels[cid])
                    for cid in communities
                }
                label_communities_input = {
                    cid: members
                    for cid, members in communities.items()
                    if cid not in existing_labels or existing_labels.get(cid) == f"Community {cid}"
                }
            generated_labels, _ = generate_community_labels(
                G, label_communities_input, backend=label_backend, model=label_model, gods=gods,
                max_concurrency=label_max_concurrency, batch_size=label_batch_size,
                usage_out=label_token_usage,
            )
            # Deixe o LLM OVERRIDE apenas onde produziu um nome real - sem back-end
            # fallback retorna espaços reservados "Community {cid}", que não devem ser prejudicados
            # os rótulos de hub determinísticos.
            labels.update({
                cid: v for cid, v in generated_labels.items()
                if v and v != f"Community {cid}"
            })
        stages.mark("label")
        questions = suggest_questions(G, communities, labels)
        tokens = label_token_usage
        from omnigraph.export import _git_head as _gh
        _commit = _gh()
        from omnigraph.report import load_learning_for_report as _llfr
        report = generate(G, communities, cohesion, labels, gods, surprises,
                          {"warning": "cluster-only mode — file stats not available"},
                          tokens, str(watch_path), suggested_questions=questions,
                          min_community_size=min_community_size, built_at_commit=_commit,
                          learning=_llfr(out / "graph.json"))
        (out / "GRAPH_REPORT.md").write_text(report, encoding="utf-8")
        stages.mark("report")
        from omnigraph.export import backup_if_protected as _backup
        _backup(out)
        analysis = {
            "communities": {str(k): v for k, v in communities.items()},
            "cohesion": {str(k): v for k, v in cohesion.items()},
            "gods": gods,
            "surprises": surprises,
            "questions": questions,
        }
        (out / ".omnigraph_analysis.json").write_text(
            json.dumps(analysis, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        to_json(G, communities, str(out / "graph.json"), community_labels=labels)
        # Don't persist placeholder-only labels (or their .sig): leaving the
        # sidecar absent lets a later run generate real labels instead of reading
        # back "Community N" as authoritative.
        if not placeholder_only:
            from omnigraph.paths import write_json_atomic as _wja
            _wja(labels_path, {str(k): v for k, v in labels.items()}, ensure_ascii=False)
            # Membership signatures beside the labels so a later cluster-only can
            # detect which communities changed and avoid reusing a stale label
            # (see reuse above).
            from omnigraph.cluster import community_member_sigs as _cms
            (labels_path.parent / (labels_path.name + ".sig")).write_text(
                json.dumps({str(k): v for k, v in _cms(communities).items()}), encoding="utf-8")

        # Espelhe o padrão watch.py: gate to_html para saídas principais (graph.json +
        # GRAPH_REPORT.md) sempre pousa. Honra --no-viz explicitamente; de outra forma
        # volte ao tratamento de ValueError para que um grafo superdimensionado não trave
        # a CLI está no meio da gravação e deixa um graph.html obsoleto no disco.
        html_target = out / "graph.html"
        if no_viz:
            if html_target.exists():
                html_target.unlink()
            stages.mark("export"); stages.total()
            print(f"Done - {len(communities)} communities. GRAPH_REPORT.md and graph.json updated (--no-viz; graph.html removed).")
        else:
            try:
                # Fallback over-cap: forçar a agregação da comunidade
                # path para que um grafo superdimensionado ainda renderize um graph.html utilizável.
                _node_limit = 5000 if _over_cap else None
                to_html(G, communities, str(html_target), community_labels=labels or None,
                        node_limit=_node_limit)
                stages.mark("export"); stages.total()
                print(f"Done - {len(communities)} communities. GRAPH_REPORT.md, graph.json and graph.html updated.")
            except ValueError as viz_err:
                if html_target.exists():
                    html_target.unlink()
                print(f"Skipped graph.html: {viz_err}")
                stages.mark("export"); stages.total()
                print(f"Done - {len(communities)} communities. GRAPH_REPORT.md and graph.json updated.")

    elif cmd == "update":
        force = os.environ.get("OMNIGRAPH_FORCE", "").lower() in ("1", "true", "yes")
        no_cluster = False
        args = sys.argv[2:]
        watch_arg: str | None = None
        for a in args:
            if a == "--force":
                force = True
                continue
            if a == "--no-cluster":
                no_cluster = True
                continue
            if a.startswith("-"):
                print(f"error: unknown update option: {a}", file=sys.stderr)
                sys.exit(2)
            if watch_arg is not None:
                print("error: update accepts at most one path argument", file=sys.stderr)
                sys.exit(2)
            watch_arg = a

        if watch_arg is not None:
            watch_path = Path(watch_arg)
        else:
            # Tente recuperar a raiz do scan salva pela última compilação completa
            saved = Path(_OMNIGRAPH_OUT) / ".omnigraph_root"
            if saved.exists():
                watch_path = Path(saved.read_text(encoding="utf-8").strip())
            else:
                watch_path = Path(".")
        if not watch_path.exists():
            print(f"error: path not found: {watch_path}", file=sys.stderr)
            sys.exit(1)
        from omnigraph.watch import _rebuild_code

        print(f"Re-extracting code files in {watch_path} (no LLM needed)...")
        # CLI interativo: bloqueie o bloqueio por repositório em vez de pular, então o
        # o usuário vê sua `omnigraph update` explícita concluída em vez de
        # saindo silenciosamente quando uma reconstrução acionada por gancho estiver em execução.
        ok = _rebuild_code(watch_path, force=force, no_cluster=no_cluster, block_on_lock=True)
        if ok:
            print("Code graph updated. For doc/paper/image changes run /omnigraph --update in your AI assistant.")
            if not (
                os.environ.get("GEMINI_API_KEY")
                or os.environ.get("GOOGLE_API_KEY")
                or os.environ.get("MOONSHOT_API_KEY")
                or os.environ.get("DEEPSEEK_API_KEY")
                or os.environ.get("OMNIGRAPH_NO_TIPS")
            ):
                print("Tip: set GEMINI_API_KEY or GOOGLE_API_KEY to use Gemini for semantic extraction.")
        else:
            print(
                "Nothing to update or rebuild failed — check output above.",
                file=sys.stderr,
            )
            sys.exit(1)

    elif cmd == "hook-check":
        # Codex Desktop rejeita hookSpecificOutput.additionalContext em PreToolUse.
        # Mantenha isso como um ambiente independente de plataforma cruzada para que os ganchos instalados nunca quebrem o Bash
        # chamadas de ferramenta. A orientação sobre o grafo chega ao agente por AGENTS.md / skill.
        sys.exit(0)
    elif cmd == "hook-guard":
        # Guarda Claude/Codebuddy PreToolUse independente de shell. Substitui o antigo
        # ganchos inline-bash que falharam no Windows. Imprime um empurrão adicional de Contexto
        # toward omnigraph when a fresh in-project graph exists; always exits 0. In
        # strict mode (opt-in, `hook-guard read --strict`) it blocks the first raw
        # read per session via the JSON permissionDecision payload — never via exit
        # code — and downgrades to the nudge thereafter.
        _run_hook_guard(
            sys.argv[2] if len(sys.argv) > 2 else "",
            strict="--strict" in sys.argv[3:],
        )
        sys.exit(0)
    elif cmd == "check-update":
        if len(sys.argv) < 3:
            print("Usage: omnigraph check-update <path>", file=sys.stderr)
            sys.exit(1)
        from omnigraph.watch import check_update

        check_update(Path(sys.argv[2]).resolve())
        sys.exit(0)
    elif cmd == "tree":
        # Emita uma visualização HTML de árvore dobrável D3 v7 de graph.json:
        # botões expandir tudo / recolher tudo / redefinir visualização, multilinha
        # Etiquetas wrapText com nome + contagem em cores separadas,
        # paleta baseada em profundidade, subárvore clique para alternar, inspetor flutuante
        # showing top-K outbound edges per symbol.
        from typing import Optional as _Opt
        from omnigraph.tree_html import write_tree_html, DEFAULT_MAX_CHILDREN
        graph_path = Path(_OMNIGRAPH_OUT) / "graph.json"
        output_path: "_Opt[Path]" = None
        root: "_Opt[str]" = None
        max_children = DEFAULT_MAX_CHILDREN
        top_k_edges = 0
        project_label: "_Opt[str]" = None
        args = sys.argv[2:]
        i_arg = 0
        while i_arg < len(args):
            a = args[i_arg]
            if a == "--graph" and i_arg + 1 < len(args):
                graph_path = Path(args[i_arg + 1]); i_arg += 2
            elif a == "--output" and i_arg + 1 < len(args):
                output_path = Path(args[i_arg + 1]); i_arg += 2
            elif a == "--root" and i_arg + 1 < len(args):
                root = args[i_arg + 1]; i_arg += 2
            elif a == "--max-children" and i_arg + 1 < len(args):
                max_children = int(args[i_arg + 1]); i_arg += 2
            elif a == "--top-k-edges" and i_arg + 1 < len(args):
                top_k_edges = int(args[i_arg + 1]); i_arg += 2
            elif a == "--label" and i_arg + 1 < len(args):
                project_label = args[i_arg + 1]; i_arg += 2
            elif a in ("-h", "--help"):
                print("Usage: omnigraph tree [--graph PATH] [--output HTML]")
                print("  --graph PATH         path to graph.json (default omnigraph-out/graph.json)")
                print("  --output HTML        output path (default omnigraph-out/GRAPH_TREE.html)")
                print("  --root PATH          filesystem root (default: longest common dir of all source_files)")
                print("  --max-children N     cap visible children per node (default 200)")
                print("  --top-k-edges N      pre-compute top-K outbound edges per symbol (default 12)")
                print("  --label NAME         project label shown in the page header")
                return
            else:
                i_arg += 1
        if not graph_path.is_file():
            print(f"error: graph.json not found at {graph_path}", file=sys.stderr)
            sys.exit(1)
        _enforce_graph_size_cap_or_exit(graph_path)
        if output_path is None:
            output_path = graph_path.parent / "GRAPH_TREE.html"
        out = write_tree_html(
            graph_path=graph_path, output_path=output_path,
            root=root, max_children=max_children,
            top_k_edges=top_k_edges, project_label=project_label,
        )
        size_kb = out.stat().st_size / 1024
        print(f"wrote {out} ({size_kb:.1f} KB)")
        print(f"open with: xdg-open {out}  (or file://{out.resolve()})")
        sys.exit(0)

    elif cmd == "merge-driver":
        # driver git merge para graph.json - pega (base, atual, outro) e grava
        # a união de atuais + outros nós/arestas de volta ao atual. Sai 1 em
        # entrada corrompida, então o git revela o conflito em vez de silenciosamente
        # aceitar uma fusão envenenada (ver F-005).
        # Uso: omnigraph merge-driver %O %A %B (definido no driver de mesclagem .git/config)
        if len(sys.argv) < 5:
            print("Usage: omnigraph merge-driver <base> <current> <other>", file=sys.stderr)
            sys.exit(1)
        _base_path, _current_path, _other_path = sys.argv[2], sys.argv[3], sys.argv[4]
        # Limites rígidos para que um graph.json malicioso ou corrompido não possa esgotar a memória
        # no momento da análise. Nós de 50 MB/100k estão bem acima de qualquer grafo realista
        # (grafos típicos são nós <5 MB/<50k); qualquer coisa maior deve falhar
        # a fusão para que um humano possa investigar.
        _MERGE_MAX_BYTES = 50 * 1024 * 1024
        _MERGE_MAX_NODES = 100_000
        import networkx as _nx
        from networkx.readwrite import json_graph as _jg
        def _load_graph(p: str):
            path_obj = Path(p)
            try:
                size = path_obj.stat().st_size
            except OSError as exc:
                raise RuntimeError(f"cannot stat {p}: {exc}") from exc
            if size > _MERGE_MAX_BYTES:
                raise RuntimeError(
                    f"graph.json {p} is {size} bytes, exceeds {_MERGE_MAX_BYTES}-byte cap"
                )
            data = json.loads(path_obj.read_text(encoding="utf-8"))
            # A committed raw (--no-cluster) graph stores edges under "edges";
            # parse via the shared links/edges-normalizing loader.
            from omnigraph.paths import load_node_link_graph as _lnlg
            return _lnlg(data), data
        try:
            G_cur, _ = _load_graph(_current_path)
            G_oth, _ = _load_graph(_other_path)
        except Exception as exc:
            print(f"[omnigraph merge-driver] error loading graphs: {exc}", file=sys.stderr)
            sys.exit(1)  # trazer à tona o conflito para que o git não aceite uma mesclagem corrompida
        merged = _nx.compose(G_cur, G_oth)
        if merged.number_of_nodes() > _MERGE_MAX_NODES:
            print(
                f"[omnigraph merge-driver] merged graph has {merged.number_of_nodes()} nodes, "
                f"exceeds {_MERGE_MAX_NODES}-node cap; aborting merge.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            out_data = _jg.node_link_data(merged, edges="links")
        except TypeError:
            out_data = _jg.node_link_data(merged)
        from omnigraph.paths import write_json_atomic
        write_json_atomic(_current_path, out_data, indent=2)
        sys.exit(0)

    elif cmd == "merge-graphs":
        # omnigraph merge-graphs graph1.json graph2.json ... --out merged.json
        args = sys.argv[2:]
        graph_paths: list[Path] = []
        out_path = Path(_OMNIGRAPH_OUT) / "merged-graph.json"
        i = 0
        while i < len(args):
            if args[i] == "--out" and i + 1 < len(args):
                out_path = Path(args[i + 1])
                i += 2
            else:
                graph_paths.append(Path(args[i]))
                i += 1
        if len(graph_paths) < 2:
            print(
                "Usage: omnigraph merge-graphs <graph1.json> <graph2.json> [...] [--out merged.json]",
                file=sys.stderr,
            )
            sys.exit(1)
        import networkx as _nx
        from networkx.readwrite import json_graph as _jg
        from omnigraph.build import prefix_graph_for_global as _prefix, distinct_repo_tags as _repo_tags
        graphs = []
        for gp in graph_paths:
            if not gp.exists():
                print(f"error: not found: {gp}", file=sys.stderr)
                sys.exit(1)
            _enforce_graph_size_cap_or_exit(gp)
            data = json.loads(gp.read_text(encoding="utf-8"))
            # Normalize a chave de arestas/links antes de carregar - zspekfy escreve "links"
            # via node_link_data mas execuções mais antigas podem ter usado "arestas".
            if "links" not in data and "edges" in data:
                data = dict(data, links=data["edges"])
            # Preserve stored edge direction across undirected node_link_graph.
            # Mirrors cli.py's query pattern and export.py's _src/_tgt restoration.
            # Keep in-file markers when present: unconditionally
            # overwriting them with source/target would clobber the true
            # direction of a link persisted in flipped endpoint order.
            data = dict(
                data,
                links=[
                    {
                        **link,
                        "_src": link.get("_src", link.get("source")),
                        "_tgt": link.get("_tgt", link.get("target")),
                    }
                    for link in data.get("links", [])
                ],
            )
            try:
                G = _jg.node_link_graph(data, edges="links")
            except TypeError:
                G = _jg.node_link_graph(data)
            graphs.append(G)
        # nx.compose exige que todos os grafos sejam do mesmo tipo.  Quando inserir grafos
        # vêm de fontes diferentes (por exemplo, uma execução apenas AST versus uma execução LLM completa) uma
        # pode ser um MultiGraph e outro um Graph.  Normalize tudo para grafo
        # (o padrão zspekfy) convertendo MultiGraphs com nx.Graph().
        def _to_simple(g: "_nx.Graph") -> "_nx.Graph":
            # nx.compose exige que todos os grafos sejam do mesmo tipo. As entradas podem
            # discordo em AMBOS os eixos - direcionado versus não direcionado e multi versus simples
            # - porque os arquivos graph.json por repositório são escritos por extratos diferentes
            # caminhos em momentos diferentes. Normalize tudo para um plano simples e não direcionado
            # Grafo (a visão de repositório cruzado mesclada não é direcionada de qualquer maneira), que cobre
            # DiGraph / MultiGraph / MultiDiGraph. Sem isso, uma entrada direcionada
            # composição travada com "Todos os grafos devem ser direcionados ou não direcionados".
            if type(g) is not _nx.Graph:
                return _nx.Graph(g)
            return g
        # Tag de repositório exclusiva por grafo. O nome do diretório `omnigraph-out/..` não é
        # exclusivo entre entradas (src/omnigraph-out e frontend/src/omnigraph-out ambos
        # → "src"), que colide IDs de nós do mesmo tronco e mescla silenciosamente
        # entidades. distinct_repo_tags garante um prefixo distinto por grafo.
        repo_tags = _repo_tags(graph_paths)
        naive_tags = [gp.parent.parent.name for gp in graph_paths]
        if len(set(naive_tags)) != len(naive_tags):
            print(f"  note: repo dir names collide; using distinct tags: {', '.join(repo_tags)}")
        merged = _nx.Graph()
        for G, repo_tag in zip(graphs, repo_tags):
            prefixed = _to_simple(_prefix(G, repo_tag))
            merged = _nx.compose(merged, prefixed)
        try:
            out_data = _jg.node_link_data(merged, edges="links")
        except TypeError:
            out_data = _jg.node_link_data(merged)
        # Restore original edge direction from _src/_tgt markers (same pattern as export.py/)
        for link in out_data.get("links", []):
            tsrc = link.pop("_src", None)
            ttgt = link.pop("_tgt", None)
            if tsrc is not None and ttgt is not None:
                link["source"] = tsrc
                link["target"] = ttgt
        out_path.parent.mkdir(parents=True, exist_ok=True)
        from omnigraph.paths import write_json_atomic as _wja
        _wja(out_path, out_data, indent=2)
        print(f"Merged {len(graphs)} graphs -> {merged.number_of_nodes()} nodes, {merged.number_of_edges()} edges")
        print(f"Written to: {out_path}")

    elif cmd == "clone":
        if len(sys.argv) < 3:
            print(
                "Usage: omnigraph clone <github-url> [--branch <branch>] [--out <dir>]",
                file=sys.stderr,
            )
            sys.exit(1)
        url = sys.argv[2]
        branch: str | None = None
        out_dir: Path | None = None
        args = sys.argv[3:]
        i = 0
        while i < len(args):
            if args[i] == "--branch" and i + 1 < len(args):
                branch = args[i + 1]
                i += 2
            elif args[i] == "--out" and i + 1 < len(args):
                out_dir = Path(args[i + 1])
                i += 2
            else:
                i += 1
        local_path = _clone_repo(url, branch=branch, out_dir=out_dir)
        print(local_path)

    elif cmd == "export":
        subcmd = sys.argv[2] if len(sys.argv) > 2 else ""
        if subcmd not in ("html", "callflow-html", "obsidian", "wiki", "svg", "graphml", "neo4j", "falkordb"):
            print("Usage: omnigraph export <format>", file=sys.stderr)
            print("  html      [--graph PATH] [--labels PATH] [--node-limit N] [--no-viz]", file=sys.stderr)
            print("  callflow-html [GRAPH|DIR] [--graph PATH] [--labels PATH] [--report PATH] [--sections PATH] [--output HTML]", file=sys.stderr)
            print("            [--lang auto|zh-CN|en] [--max-sections N] [--diagram-scale N]", file=sys.stderr)
            print("  obsidian  [--graph PATH] [--labels PATH] [--dir PATH]", file=sys.stderr)
            print("  wiki      [--graph PATH] [--labels PATH]", file=sys.stderr)
            print("  svg       [--graph PATH] [--labels PATH]", file=sys.stderr)
            print("  graphml   [--graph PATH]", file=sys.stderr)
            print("  neo4j     [--graph PATH] [--push URI] [--user U] [--password P]", file=sys.stderr)
            print("            (or set NEO4J_PASSWORD instead of --password to keep it off argv)", file=sys.stderr)
            print("  falkordb  [--graph PATH] [--push URI] [--user U] [--password P]", file=sys.stderr)
            print("            (or set FALKORDB_PASSWORD instead of --password to keep it off argv)", file=sys.stderr)
            sys.exit(1)

        # Parse shared args
        args = sys.argv[3:]
        graph_path = Path(_OMNIGRAPH_OUT) / "graph.json"
        graph_path_explicit = False
        labels_path = Path(_OMNIGRAPH_OUT) / ".omnigraph_labels.json"
        labels_path_explicit = False
        report_path = Path(_OMNIGRAPH_OUT) / "GRAPH_REPORT.md"
        report_path_explicit = False
        sections_path: Path | None = None
        callflow_output: Path | None = None
        callflow_lang = "auto"
        callflow_max_sections = 15
        callflow_diagram_scale = 1.0
        callflow_max_diagram_nodes = 18
        callflow_max_diagram_edges = 24
        analysis_path = Path(_OMNIGRAPH_OUT) / ".omnigraph_analysis.json"
        node_limit = 5000
        no_viz = False
        obsidian_dir = Path(_OMNIGRAPH_OUT) / "obsidian"
        # Configurações de conexão push compartilhadas para coletores de banco de dados grafo (neo4j,
        # falkordb), analisado a partir dos sinalizadores genéricos --push/--user/--password abaixo.
        push_uri: str | None = None
        push_user = "neo4j"  # Usuário padrão do Neo4j; A autenticação FalkorDB é opcional e a ignora
        # F-031: prefira um env var para que a senha nunca apareça no argv (visível
        # na saída `ps`/histórico do shell). O sinalizador --password explícito ainda
        # substitui-lo. Cada coletor lê sua própria var: FALKORDB_PASSWORD para falkordb,
        # NEO4J_PASSWORD caso contrário.
        push_password: str | None = (
            os.environ.get("FALKORDB_PASSWORD") if subcmd == "falkordb"
            else os.environ.get("NEO4J_PASSWORD")
        ) or None
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--graph" and i + 1 < len(args):
                graph_path = Path(args[i + 1])
                graph_path_explicit = True
                i += 2
            elif a == "--labels" and i + 1 < len(args):
                labels_path = Path(args[i + 1])
                labels_path_explicit = True
                i += 2
            elif a == "--report" and i + 1 < len(args):
                report_path = Path(args[i + 1])
                report_path_explicit = True
                i += 2
            elif a == "--sections" and i + 1 < len(args):
                sections_path = Path(args[i + 1]); i += 2
            elif a == "--output" and i + 1 < len(args):
                callflow_output = Path(args[i + 1]).expanduser()
                if not callflow_output.is_absolute():
                    callflow_output = Path.cwd() / callflow_output
                i += 2
            elif a == "--lang" and i + 1 < len(args):
                callflow_lang = args[i + 1]; i += 2
            elif a == "--max-sections" and i + 1 < len(args):
                callflow_max_sections = int(args[i + 1]); i += 2
            elif a == "--diagram-scale" and i + 1 < len(args):
                callflow_diagram_scale = float(args[i + 1]); i += 2
            elif a == "--max-diagram-nodes" and i + 1 < len(args):
                callflow_max_diagram_nodes = int(args[i + 1]); i += 2
            elif a == "--max-diagram-edges" and i + 1 < len(args):
                callflow_max_diagram_edges = int(args[i + 1]); i += 2
            elif a in ("-h", "--help") and subcmd == "callflow-html":
                print("Usage: omnigraph export callflow-html [GRAPH|DIR] [--graph PATH] [--labels PATH]")
                print("  --report PATH          path to GRAPH_REPORT.md")
                print("  --sections PATH        JSON section definitions")
                print("  --output HTML          output path (default omnigraph-out/<project>-callflow.html)")
                print("  --lang LANG            auto, zh-CN, en, etc. (default auto)")
                print("  --max-sections N       maximum auto-derived sections (default 15)")
                print("  --diagram-scale N      Mermaid diagram scale (default 1.0)")
                print("  --max-diagram-nodes N  representative nodes per section (default 18)")
                print("  --max-diagram-edges N  representative edges per section (default 24)")
                sys.exit(0)
            elif a == "--node-limit" and i + 1 < len(args):
                node_limit = int(args[i + 1]); i += 2
            elif a == "--no-viz":
                no_viz = True; i += 1
            elif a == "--dir" and i + 1 < len(args):
                obsidian_dir = Path(args[i + 1]); i += 2
            elif a == "--push" and i + 1 < len(args):
                push_uri = args[i + 1]; i += 2
            elif a == "--user" and i + 1 < len(args):
                push_user = args[i + 1]; i += 2
            elif a == "--password" and i + 1 < len(args):
                push_password = args[i + 1]; i += 2
            elif subcmd == "callflow-html" and not a.startswith("-") and not graph_path_explicit:
                candidate = Path(a)
                if candidate.name == "graph.json" or candidate.suffix.lower() == ".json":
                    graph_path = candidate
                elif (candidate / "graph.json").exists():
                    graph_path = candidate / "graph.json"
                else:
                    graph_path = candidate / _OMNIGRAPH_OUT / "graph.json"
                graph_path_explicit = True
                i += 1
            else:
                i += 1

        graph_path = graph_path.expanduser()
        if graph_path_explicit:
            graph_out_dir = graph_path.parent
            if not labels_path_explicit:
                labels_path = graph_out_dir / ".omnigraph_labels.json"
            if not report_path_explicit:
                report_path = graph_out_dir / "GRAPH_REPORT.md"
        labels_path = labels_path.expanduser()
        report_path = report_path.expanduser()

        if not graph_path.exists():
            print(f"error: graph not found: {graph_path}. Run /omnigraph <path> first.", file=sys.stderr)
            sys.exit(1)

        if subcmd == "callflow-html":
            from omnigraph.callflow_html import write_callflow_html as _write_callflow_html
            out = _write_callflow_html(
                graph=graph_path,
                report=report_path,
                labels=labels_path,
                sections=sections_path,
                output=callflow_output,
                lang=callflow_lang,
                max_sections=callflow_max_sections,
                diagram_scale=callflow_diagram_scale,
                max_diagram_nodes=callflow_max_diagram_nodes,
                max_diagram_edges=callflow_max_diagram_edges,
                verbose=True,
            )
            print(f"callflow HTML written - open in any browser: {out}")
            sys.exit(0)

        from networkx.readwrite import json_graph as _jg
        from omnigraph.build import build_from_json as _bfj
        from omnigraph.security import check_graph_file_size_cap as _check_cap

        # Solução 3: para a visualização HTML, um graph.json superdimensionado deve
        # não seja um erro grave. Detecte a condição de excesso de limite aqui e recue
        # para a visualização de agregação da comunidade (node_limit=5000) abaixo, em vez de
        # saindo 1. Todos os outros subcomandos mantêm o hard cap.
        _over_cap = False
        try:
            _check_cap(graph_path)
        except ValueError as _cap_err:
            if subcmd == "html":
                _over_cap = True
                try:
                    _over_cap_bytes = graph_path.stat().st_size
                except OSError:
                    _over_cap_bytes = -1
                print(
                    f"warning: graph.json exceeds cap ({_over_cap_bytes} bytes); "
                    f"falling back to community-aggregation view (node_limit=5000)",
                    file=sys.stderr,
                )
            else:
                print(f"error: {_cap_err}", file=sys.stderr)
                sys.exit(1)
        _raw = json.loads(graph_path.read_text(encoding="utf-8"))
        if "links" not in _raw and "edges" in _raw:
            _raw = dict(_raw, links=_raw["edges"])
        try:
            G = _jg.node_link_graph(_raw, edges="links")
        except TypeError:
            G = _jg.node_link_graph(_raw)

        # Load optional analysis/labels
        communities: dict[int, list[str]] = {}
        if analysis_path.exists():
            _an = json.loads(analysis_path.read_text(encoding="utf-8"))
            communities = {int(k): v for k, v in _an.get("communities", {}).items()}
            cohesion: dict[int, float] = {int(k): v for k, v in _an.get("cohesion", {}).items()}
            gods_data = _an.get("gods", [])
        else:
            cohesion = {}
            gods_data = []

        # Fallback: graph.json carrega a comunidade por nó como um atributo do nó
        # (`to_json` escreve em cada nó). O sidecar de análise é o
        # fonte canônica - mas o caminho de reconstrução pós-commit/watch não
        # regenere-o e `extract` poderá ter seus arquivos temporários limpos. Quando
        # isso acontece, `omnigraph export html` anteriormente resgatado com
        # "Comunidade única - visualização agregada não é útil." mesmo que o
        # o atributo por nó tinha os dados corretos o tempo todo. Reconstruir de
        # o próprio grafo, então subcomandos downstream (html, obsidian, wiki,
        # svg, graphml, neo4j) não produzem silenciosamente um artefato degradado.
        if not communities:
            reconstructed: dict[int, list[str]] = {}
            for node_id, data in G.nodes(data=True):
                cid_raw = data.get("community")
                if cid_raw is None:
                    continue
                try:
                    cid = int(cid_raw)
                except (TypeError, ValueError):
                    continue
                reconstructed.setdefault(cid, []).append(str(node_id))
            if reconstructed:
                communities = reconstructed

        labels: dict[int, str] = {}
        if labels_path.exists():
            labels = {int(k): v for k, v in json.loads(labels_path.read_text(encoding="utf-8")).items()}

        out_dir = graph_path.parent

        if subcmd == "html":
            from omnigraph.export import to_html as _to_html
            if no_viz:
                html_target = out_dir / "graph.html"
                if html_target.exists():
                    html_target.unlink()
                print("--no-viz: skipped graph.html")
            else:
                # Fallback over-cap: forçar a agregação da comunidade
                # caminho para que o grafo superdimensionado ainda renderize um artefato utilizável.
                _effective_node_limit = 5000 if _over_cap else node_limit
                _to_html(G, communities, str(out_dir / "graph.html"),
                         community_labels=labels or None, node_limit=_effective_node_limit)
                if G.number_of_nodes() <= _effective_node_limit:
                    print(f"graph.html written - open in any browser, no server needed")
                if _over_cap:
                    sys.exit(0)

        elif subcmd == "obsidian":
            from omnigraph.export import to_obsidian as _to_obsidian, to_canvas as _to_canvas
            n = _to_obsidian(G, communities, str(obsidian_dir),
                             community_labels=labels or None, cohesion=cohesion or None)
            print(f"Obsidian vault: {n} notes in {obsidian_dir}/")
            _to_canvas(G, communities, str(obsidian_dir / "graph.canvas"),
                       community_labels=labels or None)
            print(f"Canvas: {obsidian_dir}/graph.canvas")
            print(f"Open {obsidian_dir}/ as a vault in Obsidian.")

        elif subcmd == "wiki":
            from omnigraph.wiki import to_wiki as _to_wiki
            from omnigraph.analyze import god_nodes as _god_nodes
            if not communities:
                print(
                    "error: .omnigraph_analysis.json is missing or empty — refusing to export wiki to prevent data loss.\n"
                    "Run `omnigraph extract .` (or `omnigraph cluster-only .`) to regenerate community data first.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if not gods_data:
                gods_data = _god_nodes(G)
            n = _to_wiki(G, communities, str(out_dir / "wiki"),
                         community_labels=labels or None, cohesion=cohesion or None,
                         god_nodes_data=gods_data)
            print(f"Wiki: {n} articles written to {out_dir}/wiki/")
            print(f"  {out_dir}/wiki/index.md  ->  agent entry point")

        elif subcmd == "svg":
            from omnigraph.export import to_svg as _to_svg
            _to_svg(G, communities, str(out_dir / "graph.svg"),
                    community_labels=labels or None)
            print(f"graph.svg written - embeds in Obsidian, Notion, GitHub READMEs")

        elif subcmd == "graphml":
            from omnigraph.export import to_graphml as _to_graphml
            _to_graphml(G, communities, str(out_dir / "graph.graphml"))
            print(f"graph.graphml written - open in Gephi, yEd, or any GraphML tool")

        elif subcmd == "neo4j":
            if push_uri:
                from omnigraph.export import push_to_neo4j as _push
                if push_password is None:
                    print("error: --password required for --push", file=sys.stderr)
                    sys.exit(1)
                result = _push(G, uri=push_uri, user=push_user,
                               password=push_password, communities=communities)
                print(f"Pushed to Neo4j: {result['nodes']} nodes, {result['edges']} edges")
            else:
                from omnigraph.export import to_cypher as _to_cypher
                _to_cypher(G, str(out_dir / "cypher.txt"))
                print(f"cypher.txt written - import with: cypher-shell < {out_dir}/cypher.txt")

        elif subcmd == "falkordb":
            if push_uri:
                from omnigraph.export import push_to_falkordb as _push
                result = _push(G, uri=push_uri, user=push_user,
                               password=push_password, communities=communities)
                print(f"Pushed to FalkorDB: {result['nodes']} nodes, {result['edges']} edges")
            else:
                from omnigraph.export import to_cypher as _to_cypher
                _to_cypher(G, str(out_dir / "cypher.txt"))
                print(f"cypher.txt written ({out_dir}/cypher.txt) - statements are OpenCypher. "
                      f"FalkorDB's GRAPH.QUERY runs one statement at a time (no bulk script "
                      f"import), so load a graph with: omnigraph export falkordb --push "
                      f"falkordb://localhost:6379")

    elif cmd == "benchmark":
        from omnigraph.benchmark import run_benchmark, print_benchmark

        graph_path = sys.argv[2] if len(sys.argv) > 2 else _default_graph_path()
        _enforce_graph_size_cap_or_exit(Path(graph_path))
        # Tente carregar corpus_words da saída de detecção
        corpus_words = None
        detect_path = Path(".omnigraph_detect.json")
        if detect_path.exists():
            try:
                detect_data = json.loads(detect_path.read_text(encoding="utf-8"))
                corpus_words = detect_data.get("total_words")
            except Exception:
                pass
        result = run_benchmark(graph_path, corpus_words=corpus_words)
        print_benchmark(result)

    elif cmd == "global":
        subcmd = sys.argv[2] if len(sys.argv) > 2 else ""
        from omnigraph.global_graph import (
            global_add as _global_add,
            global_remove as _global_remove,
            global_list as _global_list,
            global_path as _global_path,
        )
        if subcmd == "add":
            # zspekfy adição global <graph.json> [--as <tag>]
            args = sys.argv[3:]
            source = None
            tag = None
            i = 0
            while i < len(args):
                if args[i] == "--as" and i + 1 < len(args):
                    tag = args[i + 1]; i += 2
                elif not source:
                    source = Path(args[i]); i += 1
                else:
                    i += 1
            if not source:
                print("Usage: omnigraph global add <graph.json> [--as <repo-tag>]", file=sys.stderr)
                sys.exit(1)
            tag = tag or source.parent.parent.name
            try:
                result = _global_add(source, tag)
                if result["skipped"]:
                    print(f"'{tag}' unchanged since last add - global graph not modified.")
                else:
                    print(f"Added '{tag}' to global graph: +{result['nodes_added']} nodes, "
                          f"-{result['nodes_removed']} pruned. Global: {_global_path()}")
            except Exception as exc:
                print(f"error: {exc}", file=sys.stderr); sys.exit(1)
        elif subcmd == "remove":
            tag = sys.argv[3] if len(sys.argv) > 3 else ""
            if not tag:
                print("Usage: omnigraph global remove <repo-tag>", file=sys.stderr); sys.exit(1)
            try:
                removed = _global_remove(tag)
                print(f"Removed '{tag}' from global graph ({removed} nodes pruned).")
            except KeyError as exc:
                print(f"error: {exc}", file=sys.stderr); sys.exit(1)
        elif subcmd == "list":
            repos = _global_list()
            if not repos:
                print("Global graph is empty. Use 'omnigraph global add' to add a project.")
            else:
                print(f"Global graph: {_global_path()}")
                for tag, info in repos.items():
                    print(f"  {tag}: {info.get('node_count', '?')} nodes, added {info.get('added_at', '?')[:10]}")
        elif subcmd == "path":
            print(_global_path())
        else:
            print("Usage: omnigraph global [add|remove|list|path]", file=sys.stderr); sys.exit(1)

    elif cmd == "extract":
        # Extração de pipeline completo sem cabeçalho para CI/scripts.
        # Executa detecção -> extração AST no código -> extração semântica LLM em
        # docs/papers/images -> merge -> build -> cluster -> write outputs.
        # Ao contrário do caminho skill.md (que passa pelos subagentes do Código Claude),
        # isso chama extract_corpus_parallel diretamente usando qualquer back-end
        # tem um conjunto de chaves de API.
        if len(sys.argv) < 3:
            print(
                "Usage: omnigraph extract <path> [--backend gemini|kimi|claude|openai|deepseek|ollama] "
                "[--model M] [--mode deep] [--out DIR|--output DIR] [--google-workspace] [--no-cluster] "
                "[--no-gitignore] [--code-only] "
                "[--max-workers N] [--token-budget N] [--max-concurrency N] "
                "[--api-timeout S] [--postgres DSN] [--cargo] [--allow-partial] [--timing]",
                file=sys.stderr,
            )
            sys.exit(1)

        has_path = True
        if sys.argv[2].startswith("-"):
            has_path = False
            target = Path(".").resolve()
        else:
            target = Path(sys.argv[2]).resolve()
            if not target.exists():
                print(f"error: path not found: {target}", file=sys.stderr)
                sys.exit(1)

        backend: str | None = None
        model: str | None = None
        extract_mode: str | None = None
        out_dir: Path | None = None
        cli_postgres_dsn: str | None = None
        cli_cargo: bool = False
        cli_allow_partial: bool = False
        no_cluster = False
        dedup_llm = False
        google_workspace = False
        global_merge = False
        code_only = False
        no_gitignore = False
        global_repo_tag: str | None = None
        # Botões de desempenho/afinação (edição). Nenhum significa "usar padrão da biblioteca".
        cli_max_workers: int | None = None
        cli_token_budget: int | None = None
        cli_max_concurrency: int | None = None
        cli_api_timeout: float | None = None
        # Clustering tuning knobs
        cli_resolution: float = 1.0
        cli_exclude_hubs: float | None = None
        cli_excludes: list[str] = []
        cli_timing: bool = False
        # --force paridade com `omnigraph update`: o sinalizador ou OMNIGRAPH_FORCE=1
        # desativa a porta incremental e ignora as leituras do cache semântico.
        force = os.environ.get("OMNIGRAPH_FORCE", "").lower() in ("1", "true", "yes")

        def _parse_int(name: str, raw: str) -> int:
            try:
                v = int(raw)
            except ValueError:
                print(f"error: {name} must be a positive integer (got {raw!r})", file=sys.stderr)
                sys.exit(2)
            if v <= 0:
                print(f"error: {name} must be > 0 (got {v})", file=sys.stderr)
                sys.exit(2)
            return v

        def _parse_float(name: str, raw: str) -> float:
            try:
                v = float(raw)
            except ValueError:
                print(f"error: {name} must be a positive number (got {raw!r})", file=sys.stderr)
                sys.exit(2)
            if v <= 0:
                print(f"error: {name} must be > 0 (got {v})", file=sys.stderr)
                sys.exit(2)
            return v

        args = sys.argv[3:] if has_path else sys.argv[2:]
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--backend" and i + 1 < len(args):
                backend = args[i + 1]; i += 2
            elif a.startswith("--backend="):
                backend = a.split("=", 1)[1]; i += 1
            elif a == "--model" and i + 1 < len(args):
                model = args[i + 1]; i += 2
            elif a.startswith("--model="):
                model = a.split("=", 1)[1]; i += 1
            elif a == "--mode" and i + 1 < len(args):
                extract_mode = args[i + 1]; i += 2
            elif a.startswith("--mode="):
                extract_mode = a.split("=", 1)[1]; i += 1
            elif a in ("--out", "--output") and i + 1 < len(args):
                # --output is an alias of --out: it was silently dropped
                # before, and `omnigraph tree` already documents --output, so the
                # mistake is natural. (--output= does not startswith --out=.)
                out_dir = Path(args[i + 1]); i += 2
            elif a.startswith(("--out=", "--output=")):
                out_dir = Path(a.split("=", 1)[1]); i += 1
            elif a == "--no-cluster":
                no_cluster = True; i += 1
            elif a == "--dedup-llm":
                dedup_llm = True; i += 1
            elif a == "--code-only":
                code_only = True; i += 1
            elif a == "--google-workspace":
                google_workspace = True; i += 1
            elif a == "--no-gitignore":
                no_gitignore = True; i += 1
            elif a == "--global":
                global_merge = True; i += 1
            elif a == "--as" and i + 1 < len(args):
                global_repo_tag = args[i + 1]; i += 2
            elif a == "--max-workers" and i + 1 < len(args):
                cli_max_workers = _parse_int("--max-workers", args[i + 1]); i += 2
            elif a.startswith("--max-workers="):
                cli_max_workers = _parse_int("--max-workers", a.split("=", 1)[1]); i += 1
            elif a == "--token-budget" and i + 1 < len(args):
                cli_token_budget = _parse_int("--token-budget", args[i + 1]); i += 2
            elif a.startswith("--token-budget="):
                cli_token_budget = _parse_int("--token-budget", a.split("=", 1)[1]); i += 1
            elif a == "--max-concurrency" and i + 1 < len(args):
                cli_max_concurrency = _parse_int("--max-concurrency", args[i + 1]); i += 2
            elif a.startswith("--max-concurrency="):
                cli_max_concurrency = _parse_int("--max-concurrency", a.split("=", 1)[1]); i += 1
            elif a == "--api-timeout" and i + 1 < len(args):
                cli_api_timeout = _parse_float("--api-timeout", args[i + 1]); i += 2
            elif a.startswith("--api-timeout="):
                cli_api_timeout = _parse_float("--api-timeout", a.split("=", 1)[1]); i += 1
            elif a == "--resolution" and i + 1 < len(args):
                cli_resolution = _parse_float("--resolution", args[i + 1]); i += 2
            elif a.startswith("--resolution="):
                cli_resolution = _parse_float("--resolution", a.split("=", 1)[1]); i += 1
            elif a == "--exclude-hubs" and i + 1 < len(args):
                cli_exclude_hubs = float(args[i + 1]); i += 2
            elif a.startswith("--exclude-hubs="):
                cli_exclude_hubs = float(a.split("=", 1)[1]); i += 1
            elif a == "--exclude" and i + 1 < len(args):
                cli_excludes.append(args[i + 1]); i += 2
            elif a.startswith("--exclude="):
                cli_excludes.append(a.split("=", 1)[1]); i += 1
            elif a == "--postgres" and i + 1 < len(args):
                cli_postgres_dsn = args[i + 1]; i += 2
            elif a.startswith("--postgres="):
                cli_postgres_dsn = a.split("=", 1)[1]; i += 1
            elif a == "--cargo":
                cli_cargo = True
                i += 1
            elif a == "--force":
                force = True; i += 1
            elif a == "--allow-partial":
                cli_allow_partial = True; i += 1
            elif a == "--timing":
                cli_timing = True; i += 1
            else:
                i += 1

        if not has_path and cli_postgres_dsn is None:
            print("error: must specify a path to scan or a --postgres DSN", file=sys.stderr)
            sys.exit(1)

        _VALID_MODES = {"deep"}
        if extract_mode is not None and extract_mode not in _VALID_MODES:
            print(
                f"error: unknown --mode '{extract_mode}'. "
                f"Available: {', '.join(sorted(_VALID_MODES))}",
                file=sys.stderr,
            )
            sys.exit(2)
        deep_mode = extract_mode == "deep"
        if deep_mode:
            print("[omnigraph extract] deep mode enabled: richer semantic extraction")

        # O sinalizador CLI vence env var. Configurando OMNIGRAPH_API_TIMEOUT aqui então
        # _call_openai_compat o seleciona sem precisar de um novo caminho kwarg.
        if cli_api_timeout is not None:
            os.environ["OMNIGRAPH_API_TIMEOUT"] = str(cli_api_timeout)
        if cli_max_workers is not None:
            os.environ["OMNIGRAPH_MAX_WORKERS"] = str(cli_max_workers)

        # Resolva o diretório de saída. O contrato voltado para o usuário é "<out>/omnigraph-out/"
        # então um novo checkout escreve omnigraph-out/ na raiz do projeto, correspondendo
        # o pipeline habilidade.md.
        out_root = (out_dir.resolve() if out_dir else target)
        omnigraph_out = out_root / _OMNIGRAPH_OUT
        omnigraph_out.mkdir(parents=True, exist_ok=True)
        # Persist corpus-shaping options so later update/watch/hook rebuilds
        # use the same file set as the initial extraction.
        from omnigraph.watch import (
            _write_build_config as _write_build_cfg,
            _read_build_excludes as _read_build_ex,
            _read_build_gitignore as _read_build_gi,
        )
        # persistence: an explicit --no-gitignore persists False; a later
        # flag-less `omnigraph extract` must NOT clobber it back to True, which
        # would make the git-ignored code silently disappear again (the exact
        # complaint is about). Honor the persisted value for THIS run when
        # the flag is absent (read before the write below), and write False only
        # when the flag is set — None leaves the setting as-is, mirroring how
        # persists --exclude.
        _effective_gitignore = False if no_gitignore else _read_build_gi(omnigraph_out)
        # An explicit list replaces the persisted one; omission reuses it.
        _effective_excludes = cli_excludes or _read_build_ex(omnigraph_out)
        _write_build_cfg(
            omnigraph_out,
            excludes=cli_excludes or None,
            gitignore=False if no_gitignore else None,
        )

        stages = _StageTimer(cli_timing)

        from omnigraph.detect import (
            detect as _detect,
            detect_incremental as _detect_incremental,
            save_manifest as _save_manifest,
        )
        manifest_path = omnigraph_out / "manifest.json"
        existing_graph_path = omnigraph_out / "graph.json"
        # um manifest.json ausente não deve ser degradado para uma verificação completa que
        # descarta a camada semântica do grafo existente. Um grafo.json existente
        # é uma linha de base incremental suficiente: detect_incremental trata um
        # ausente manifestado como "tudo é novo" (reextrair tudo, nada
        # excluído) e build_merge + _stale_graph_sources reconciliado substituído
        # e fontes genuinamente excluídas em relação ao corpus atual, então doc/
        # nós de papel/imagem sobrevivem a uma reconstrução --code-only em vez de serem
        # descartado com o restante do grafo confirmado.
        incremental_mode = existing_graph_path.exists() if has_path else False
        # --force: varredura completa, não a diferença incremental controlada por manifesto - um caloroso
        # caso contrário, a árvore inalterada despacharia zero arquivos.
        incremental_mode = incremental_mode and not force
        if force:
            print("[omnigraph extract] --force: full re-scan, semantic cache reads skipped")
        elif incremental_mode and not manifest_path.exists():
            print(
                "[omnigraph extract] manifest.json missing; using existing "
                "graph.json as the incremental baseline (all files re-checked; "
                "nodes for files outside this run's scope are preserved)"
            )

        if not has_path:
            detection = {}
            code_files = []
            doc_files = []
            paper_files = []
            image_files = []
            deleted_files = []
            excluded_files = []
            graph_stale_sources = []
            unchanged_total = 0
            files_by_type = {}
        elif incremental_mode:
            print(f"[omnigraph extract] incremental scan of {target}")
            detection = _detect_incremental(
                target,
                manifest_path=str(manifest_path),
                google_workspace=google_workspace or None,
                extra_excludes=_effective_excludes or None,
                gitignore=_effective_gitignore,
            )
            files_by_type = detection.get("files", {})
            new_by_type = detection.get("new_files", {})
            code_files = [Path(p) for p in new_by_type.get("code", [])]
            doc_files = [Path(p) for p in new_by_type.get("document", [])]
            paper_files = [Path(p) for p in new_by_type.get("paper", [])]
            image_files = [Path(p) for p in new_by_type.get("image", [])]
            deleted_files = list(detection.get("deleted_files", []))
            excluded_files = list(detection.get("excluded_files", []))
            unchanged_total = sum(len(v) for v in detection.get("unchanged_files", {}).values())
            # derivar o conjunto de poda do próprio grafo existente, não
            # apenas o manifesto. Um arquivo que foi excluído sem nunca
            # estar listado no manifesto (todo grafo anterior ao está nesse estado)
            # ainda tem nós obsoletos transportados por build_merge, a menos que o
            # as próprias fontes do grafo são reconciliadas com o corpus atual.
            _seen_files = {f for _fl in files_by_type.values() for f in _fl}
            _seen_files.update(detection.get("unclassified", []))
            graph_stale_sources = _stale_graph_sources(
                existing_graph_path, target, _seen_files, detection=detection
            )
        else:
            print(f"[omnigraph extract] scanning {target}")
            detection = _detect(
                target,
                google_workspace=google_workspace or None,
                extra_excludes=_effective_excludes or None,
                cache_root=out_root,
                gitignore=_effective_gitignore,
            )
            files_by_type = detection.get("files", {})
            code_files = [Path(p) for p in files_by_type.get("code", [])]
            doc_files = [Path(p) for p in files_by_type.get("document", [])]
            paper_files = [Path(p) for p in files_by_type.get("paper", [])]
            image_files = [Path(p) for p in files_by_type.get("image", [])]
            deleted_files = []
            excluded_files = []
            graph_stale_sources = []
            unchanged_total = 0

        semantic_files = doc_files + paper_files + image_files
        # --code-only: código de índice (AST local puro, sem chave) e pula a semântica
        # (doc/paper/image) passa inteiramente, então um repositório misto não falha quando não
        # O back-end do LLM está configurado. Relate o que foi ignorado em vez de
        # deixando-o cair silenciosamente.
        if code_only and semantic_files:
            print(
                f"[omnigraph extract] --code-only: skipping {len(semantic_files)} "
                f"non-code file(s) ({len(doc_files)} docs, {len(paper_files)} papers, "
                f"{len(image_files)} images) — no LLM extraction"
            )
            semantic_files = []
            doc_files = []
            paper_files = []
            image_files = []
        if deep_mode and incremental_mode and not code_only:
            # O modo profundo lê/grava seu próprio namespace de cache
            # (cache/semantic-deep/), então a porta do arquivo alterado do manifesto é
            # não é um proxy válido para cobertura profunda: sobre uma árvore quente e inalterada
            # ele despacha zero arquivos e `--mode deep` silenciosamente sem operação
            #. Ampliar o passe semântico para o FULL live
            # doc/paper/image set (``files_by_type`` de detect_incremental,
            # que já exclui arquivos excluídos) e deixe o
            # cache com namespace de modo decide acertos/erros - a primeira execução profunda
            # re-dispatches everything (deep namespace cold), later deep runs
            # atingiu o cache profundo.
            _deep_all = [
                Path(p)
                for _ftype in ("document", "paper", "image")
                for p in files_by_type.get(_ftype, [])
            ]
            if len(_deep_all) != len(semantic_files):
                print(
                    f"[omnigraph extract] deep mode: widening semantic pass from "
                    f"{len(semantic_files)} changed to {len(_deep_all)} live "
                    f"doc/paper/image file(s); the deep semantic cache decides "
                    f"what is re-extracted"
                )
            semantic_files = _deep_all
        if incremental_mode:
            # Arquivos excluídos, mas ativos, são relatados separadamente das exclusões
            #: eles ainda existem no disco, a varredura simplesmente parou
            # covering them (ignore rules / --exclude changed).
            _excl_note = f"; {len(excluded_files)} excluded" if excluded_files else ""
            print(
                f"[omnigraph extract] {len(code_files)} code, {len(doc_files)} docs, "
                f"{len(paper_files)} papers, {len(image_files)} images changed; "
                f"{unchanged_total} unchanged; {len(deleted_files)} deleted"
                f"{_excl_note}"
            )
        else:
            print(
                f"[omnigraph extract] found {len(code_files)} code, "
                f"{len(doc_files)} docs, {len(paper_files)} papers, "
                f"{len(image_files)} images"
            )
        # Arquivos de superfície que foram vistos, mas não classificados (extensão não shebang
        # arquivos de projeto como Dockerfile/Makefile ou extensões não suportadas), então eles
        # não são mais invisíveis na própria saída do zspekfy.
        _unclassified = detection.get("unclassified", []) if isinstance(detection, dict) else []
        if _unclassified:
            _names = ", ".join(sorted({Path(p).name for p in _unclassified})[:6])
            _more = f" (+{len(_unclassified) - 6} more)" if len(_unclassified) > 6 else ""
            print(
                f"[omnigraph extract] {len(_unclassified)} file(s) not classified "
                f"(no supported extension or shebang), skipped: {_names}{_more}"
            )
        # Name the files dropped by the sensitive-file filter so a wrongly-flagged
        # source/doc is visible, not just a count. Operational skips
        # (symlink/office/Workspace) carry a " [reason]" suffix; exclude those here
        # so this line reports only the security-heuristic drops.
        _sensitive = detection.get("skipped_sensitive", []) if isinstance(detection, dict) else []
        _sec = [s for s in _sensitive if " [" not in s]
        if _sec:
            _snames = ", ".join(sorted({Path(p).name for p in _sec})[:6])
            _smore = f" (+{len(_sec) - 6} more)" if len(_sec) > 6 else ""
            print(
                f"[omnigraph extract] {len(_sec)} file(s) skipped as potentially sensitive "
                f"(rename or move if wrongly flagged): {_snames}{_smore}"
            )
        stages.mark("detect")

        # Resolva o back-end do LLM somente agora que sabemos se o corpus
        # precisa de um. Um corpus somente de código é AST local puro e não deve exigir
        # uma chave de API; a chave é aplicada abaixo somente quando há trabalho de LLM.
        from omnigraph.llm import (
            BACKENDS as _BACKENDS,
            detect_backend as _detect_backend,
            estimate_cost as _estimate_cost,
            extract_corpus_parallel as _extract_corpus_parallel,
            _format_backend_env_keys,
            _get_backend_api_key,
        )
        needs_llm = bool(semantic_files) or dedup_llm
        if backend is None and needs_llm:
            backend = _detect_backend()
        if backend is not None and backend not in _BACKENDS:
            print(
                f"error: unknown backend '{backend}'. "
                f"Available: {', '.join(sorted(_BACKENDS))}",
                file=sys.stderr,
            )
            sys.exit(1)
        if needs_llm:
            if backend is None:
                reasons = []
                if semantic_files:
                    reasons.append(
                        f"{len(semantic_files)} doc/paper/image file(s) need semantic extraction"
                    )
                if dedup_llm:
                    reasons.append("--dedup-llm was passed")
                hint = ""
                if semantic_files:
                    hint = (" Or pass --code-only to index just the code "
                            "(local AST, no key) and skip the non-code files.")
                print(
                    "error: no LLM API key found (" + "; ".join(reasons) + "). "
                    "Set GEMINI_API_KEY or GOOGLE_API_KEY (gemini), MOONSHOT_API_KEY "
                    "(kimi), ANTHROPIC_API_KEY (claude), OPENAI_API_KEY (openai), "
                    "DEEPSEEK_API_KEY (deepseek), or pass --backend. A code-only "
                    "corpus needs no key." + hint,
                    file=sys.stderr,
                )
                sys.exit(1)
            if backend == "ollama":
                from omnigraph.llm import _validate_ollama_base_url
                _oll_url = os.environ.get("OLLAMA_BASE_URL", _BACKENDS["ollama"].get("base_url", ""))
                try:
                    _validate_ollama_base_url(_oll_url, warn=False)
                except ValueError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    sys.exit(2)
            if not _get_backend_api_key(backend):
                allow_no_key = False
                if backend == "ollama":
                    from urllib.parse import urlparse
                    ollama_url = os.environ.get(
                        "OLLAMA_BASE_URL",
                        _BACKENDS["ollama"].get("base_url", ""),
                    )
                    try:
                        host = (urlparse(ollama_url).hostname or "").lower()
                    except Exception:
                        host = ""
                    allow_no_key = (
                        host in ("localhost", "127.0.0.1", "::1")
                        or host.startswith("127.")
                    )
                elif backend == "bedrock":
                    allow_no_key = bool(
                        os.environ.get("AWS_PROFILE")
                        or os.environ.get("AWS_REGION")
                        or os.environ.get("AWS_DEFAULT_REGION")
                        or os.environ.get("AWS_ACCESS_KEY_ID")
                    )
                elif backend == "claude-cli":
                    import shutil as _shutil
                    allow_no_key = _shutil.which("claude") is not None
                    if not allow_no_key:
                        print(
                            "error: backend 'claude-cli' requires the `claude` CLI on $PATH "
                            "(install Claude Code and run `claude` once to authenticate).",
                            file=sys.stderr,
                        )
                        sys.exit(1)
                if not allow_no_key:
                    print(
                        f"error: backend '{backend}' requires {_format_backend_env_keys(backend)} to be set.",
                        file=sys.stderr,
                    )
                    sys.exit(1)

        # Track whether this run's extraction was incomplete (a whole extractor
        # pass crashed, or some semantic chunks failed). A partial result must not
        # be force-written over a good complete graph — the final write falls back
        # to the shrink guard unless --allow-partial is set.
        _extraction_incomplete = False
        # A walk that couldn't fully enumerate the corpus (permission-denied
        # subtree, I/O error) yields a legitimately smaller graph that must not
        # be force-written over a complete one — same failure class as a crashed
        # pass. detect()/detect_incremental() already record these; consume them.
        if detection.get("walk_errors"):
            _extraction_incomplete = True

        # Extração AST em arquivos de código. A lista de códigos vazia (corpus somente para documentos) é
        # o caso do problema nº 698 - pule de forma limpa em vez de travar dentro de extract ().
        ast_result: dict = {"nodes": [], "edges": [], "input_tokens": 0, "output_tokens": 0}
        if code_files:
            from omnigraph.extract import extract as _ast_extract
            # Ancore o cache na raiz de saída, não no projeto verificado:
            # com --out, um <target>/omnigraph-out/cache/ vazaria um
            # omnigraph-out/ dir em um projeto que solicitou saída externa.
            # `root` stays the scanned project so source_file/ids relativize
            # against it; conflating the two basenamed every node.
            ast_kwargs: dict = {"cache_root": out_root, "root": target}
            if cli_max_workers is not None:
                ast_kwargs["max_workers"] = cli_max_workers
            print(f"[omnigraph extract] AST extraction on {len(code_files)} code files...")
            try:
                ast_result = _ast_extract(code_files, **ast_kwargs)
            except Exception as exc:
                print(f"[omnigraph extract] AST extraction failed: {exc}", file=sys.stderr)
                ast_result = {"nodes": [], "edges": [], "input_tokens": 0, "output_tokens": 0}
                _extraction_incomplete = True  # the whole AST pass was lost
        stages.mark("AST extract")

        # Extração semântica em documentos/artigos/imagens. Verifique o cache primeiro.
        from omnigraph.cache import (
            check_semantic_cache as _check_semantic_cache,
            prune_semantic_cache as _prune_semantic_cache,
            save_semantic_cache as _save_semantic_cache,
        )
        sem_result: dict = {
            "nodes": [], "edges": [], "hyperedges": [],
            "input_tokens": 0, "output_tokens": 0,
        }
        # Semantic files whose extraction truncated this run. They are left
        # unstamped in the manifest so detect_incremental re-queues them next run
        # (mirrors the failed-chunk handling); captured below before the
        # _partial markers are stripped from the corpus.
        _partial_semantic_files: set[str] = set()
        sem_cache_hits = 0
        sem_cache_misses = 0
        # O modo profundo usa seu próprio namespace (cache/semantic-deep/) tão profundo e
        # resultados padrão para o mesmo conteúdo nunca se obscurecem.
        sem_cache_mode = "deep" if deep_mode else None
        # Entries are attributed to the extraction prompt that produced them, so
        # a release that changes the prompt re-extracts rather than replaying the
        # older vintage alongside the new one. Read and write must pass
        # the same prompt, or the write lands where the next read won't look.
        from omnigraph.llm import _extraction_system as _sem_prompt_for
        sem_prompt = _sem_prompt_for(deep=deep_mode)
        if semantic_files:
            sem_paths_str = [str(p) for p in semantic_files]
            if force:
                # --force: ignora o READ do cache para que cada arquivo semântico seja
                # reenviado; o salvamento abaixo ainda funciona, então o novo
                # os resultados substituem as entradas obsoletas.
                cached_nodes, cached_edges, cached_hyperedges = [], [], []
                uncached_paths = list(sem_paths_str)
            else:
                cached_nodes, cached_edges, cached_hyperedges, uncached_paths = (
                    _check_semantic_cache(sem_paths_str, root=target, cache_root=out_root,
                                          mode=sem_cache_mode, prompt=sem_prompt)
                )
            sem_cache_hits = len(semantic_files) - len(uncached_paths)
            sem_cache_misses = len(uncached_paths)
            sem_result["nodes"].extend(cached_nodes)
            sem_result["edges"].extend(cached_edges)
            sem_result["hyperedges"].extend(cached_hyperedges)
            if sem_cache_hits:
                print(f"[omnigraph extract] semantic cache: {sem_cache_hits} hit / {sem_cache_misses} miss")

            if uncached_paths:
                print(f"[omnigraph extract] semantic extraction on {len(uncached_paths)} files via {backend}...")
                corpus_kwargs: dict = {
                    "backend": backend,
                    "model": model,
                    "root": target,
                    "cache_root": out_root,
                }
                if deep_mode:
                    corpus_kwargs["deep_mode"] = True
                if cli_token_budget is not None:
                    corpus_kwargs["token_budget"] = cli_token_budget
                if cli_max_concurrency is not None:
                    corpus_kwargs["max_concurrency"] = cli_max_concurrency

                # Retorno de chamada de progresso mínimo para que a CLI não fique mais silenciosa
                # during long local-inference runs (issue addendum).
                # Acompanhe também o sucesso por bloco para que possamos falhar ruidosamente quando
                # erros de cada pedaço (por exemplo, pacote SDK de back-end ausente).
                _chunk_stats = {"total": 0, "succeeded": 0}
                def _progress(idx: int, total: int, _result: dict) -> None:
                    _chunk_stats["total"] = total
                    _chunk_stats["succeeded"] += 1
                    print(
                        f"[omnigraph extract] chunk {idx + 1}/{total} done",
                        flush=True,
                    )
                corpus_kwargs["on_chunk_done"] = _progress

                try:
                    fresh = _extract_corpus_parallel(
                        [Path(p) for p in uncached_paths],
                        **corpus_kwargs,
                    )
                except ImportError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    sys.exit(1)
                except Exception as exc:
                    print(
                        f"[omnigraph extract] semantic extraction failed: {exc}",
                        file=sys.stderr,
                    )
                    fresh = {"nodes": [], "edges": [], "hyperedges": [], "input_tokens": 0, "output_tokens": 0}
                    _extraction_incomplete = True  # the semantic pass crashed

                # on_chunk_done só é acionado após um pedaço ser bem-sucedido. Se fresco
                # a extração semântica foi solicitada e nenhum pedaço foi concluído,
                # falhar em vez de escrever um grafo somente AST com saída 0.
                if uncached_paths and _chunk_stats["succeeded"] == 0:
                    print(
                        f"[omnigraph extract] error: all semantic chunks failed "
                        f"for backend '{backend}' ({len(uncached_paths)} uncached files) - "
                        f"see per-chunk errors above. If you see 'requires the X package', "
                        f"run `pip install X` and retry.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                # Some (but not all) chunks failed — the graph is missing nodes
                # from the failed chunks, so it must not clobber a larger complete
                # graph without an explicit --allow-partial override.
                if _chunk_stats["total"] and _chunk_stats["succeeded"] < _chunk_stats["total"]:
                    _extraction_incomplete = True
                # Which files truncated this run (item markers + the empty-parse
                # _partial_files set). Computed BEFORE the save so it can be passed
                # as partial_source_files: without it, a file whose only truncated
                # chunk parsed empty (so it has no item markers here) would be
                # written as a complete cache entry, re-promoting it.
                from omnigraph.llm import (
                    _partial_source_files as _partial_sf,
                    _strip_partial_markers as _strip_partial,
                )
                _partial_semantic_files = set(_partial_sf(fresh))
                try:
                    _save_semantic_cache(
                        fresh.get("nodes", []),
                        fresh.get("edges", []),
                        fresh.get("hyperedges", []),
                        root=target,
                        cache_root=out_root,
                        allowed_source_files=uncached_paths,
                        mode=sem_cache_mode,
                        prompt=sem_prompt,
                        partial_source_files=_partial_semantic_files or None,
                    )
                except Exception as exc:
                    print(f"[omnigraph extract] warning: could not write semantic cache: {exc}", file=sys.stderr)
                # Strip the markers before the corpus feeds the graph so the
                # internal flag never leaks into graph.json.
                _strip_partial(fresh)
                sem_result["nodes"].extend(fresh.get("nodes", []))
                sem_result["edges"].extend(fresh.get("edges", []))
                sem_result["hyperedges"].extend(fresh.get("hyperedges", []))
                sem_result["input_tokens"] += fresh.get("input_tokens", 0)
                sem_result["output_tokens"] += fresh.get("output_tokens", 0)

        # Eliminar entradas de cache semântico órfãos. O cache semântico é
        # com chave de hash de conteúdo e sem versionamento, para que nunca seja varrido pelo AST
        # limpeza de versão: cada alteração de conteúdo ou exclusão de arquivo deixa um
        # órfão permanente que acumula ilimitadamente. Varrer contra
        # o conjunto COMPLETO de documentos ativos (``files_by_type`` — presente em ambos os
        # ramificações incrementais e completas), NÃO os ``semantic_files`` incrementais
        # subconjunto alterado, que excluiria todas as entradas válidas de documentos inalterados.
        # Melhor esforço: uma falha de poda nunca deve interromper a extração.
        # Hash keys are anchored to the corpus (``target``) — the same anchor
        # the cache read/write above use — while the stat-index artifact
        # follows the cache location (``out_root``). Anchoring these hashes to
        # ``out_root`` instead would mismatch every key under ``--out`` and
        # sweep the entire fresh cache as orphaned.
        try:
            from omnigraph.cache import file_hash as _file_hash
            _live_hashes: set[str] = set()
            for _kind in ("document", "paper", "image"):
                for _fp in files_by_type.get(_kind, []):
                    _abs = Path(_fp)
                    if not _abs.is_absolute():
                        _abs = Path(target) / _abs
                    if not _abs.is_file():
                        continue  # excluído/ausente — deixe de fora para que sua entrada seja removida
                    try:
                        _live_hashes.add(_file_hash(_abs, target, cache_root=out_root))
                    except OSError:
                        pass
            # A pathless database extraction has no filesystem corpus to sweep.
            if has_path:
                _prune_semantic_cache(out_root, _live_hashes)
        except Exception as exc:
            print(f"[omnigraph extract] warning: could not prune semantic cache: {exc}", file=sys.stderr)
        stages.mark("semantic extract")

        pg_result: dict = {"nodes": [], "edges": []}
        if cli_postgres_dsn is not None:
            from omnigraph.pg_introspect import introspect_postgres
            print(f"[omnigraph extract] introspecting PostgreSQL schema...")
            try:
                pg_result = introspect_postgres(cli_postgres_dsn)
            except (ConnectionError, ImportError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                sys.exit(1)
            print(f"[omnigraph extract] PostgreSQL: {len(pg_result['nodes'])} nodes, "
                  f"{len(pg_result['edges'])} edges")

        cargo_result: dict = {"nodes": [], "edges": []}
        if cli_cargo:
            from omnigraph.cargo_introspect import introspect_cargo
            print("[omnigraph extract] introspecting Cargo workspace...")
            try:
                cargo_result = introspect_cargo(target)
            except (ConnectionError, ImportError, OSError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                sys.exit(1)
            print(f"[omnigraph extract] Cargo: {len(cargo_result['nodes'])} nodes, "
                  f"{len(cargo_result['edges'])} edges")

        # Mesclar AST + semântica + pg_result + cargo_result. O pedido é importante para desduplicação: aprovação no AST
        # primeiro significa que os atributos do nó semântico vencem na colisão (rótulos mais ricos
        # para símbolos também referenciados em documentos). As hiperarestas só vêm do
        # semantic side.
        merged: dict = {
            "nodes": list(ast_result.get("nodes", [])) + list(sem_result.get("nodes", [])) + list(pg_result.get("nodes", [])) + list(cargo_result.get("nodes", [])),
            "edges": list(ast_result.get("edges", [])) + list(sem_result.get("edges", [])) + list(pg_result.get("edges", [])) + list(cargo_result.get("edges", [])),
            "hyperedges": list(sem_result.get("hyperedges", [])),
            "input_tokens": ast_result.get("input_tokens", 0) + sem_result.get("input_tokens", 0),
            "output_tokens": ast_result.get("output_tokens", 0) + sem_result.get("output_tokens", 0),
        }

        graph_json_path = omnigraph_out / "graph.json"
        analysis_path = omnigraph_out / ".omnigraph_analysis.json"

        # Crie um dicionário de arquivos seguros para manifesto: carimbe apenas semantic_hash para arquivos
        # que realmente produziu saída (acerto de cache ou extração recente). Arquivos
        # cujo pedaço falhou não tem entrada source_file em sem_result - deixando
        # seu semantic_hash está vazio, então detect_incremental os coloca novamente na fila (# 933).
        # A normalização do caminho na raiz da varredura acontece dentro do auxiliar
        # então novos source_files relativos à raiz correspondem aos de detect()
        # absolute file lists.
        _manifest_files = _stamped_manifest_files(files_by_type, sem_result, target,
                                                   partial_source_files=_partial_semantic_files)

        # Files dispatched this run but dropped by _stamped_manifest_files
        # above (failed chunk, LLM omission, or any future exclusion) still
        # carry a stale semantic_hash from a prior successful run in the
        # on-disk manifest; save_manifest's seed loop would otherwise copy it
        # verbatim and mask the omission. Derived from semantic_files
        # — what was actually SENT to the backend this run (narrowed by the
        # incremental gate and --code-only, widened by deep mode) — NOT from
        # files_by_type: the full live corpus includes untouched files that
        # were never dispatched, and clearing those would blank the whole
        # manifest on every partial incremental run, forcing a full-corpus
        # re-extraction on the next one.
        _stamped_semantic = {
            f for _flist in _manifest_files.values() for f in _flist
        }
        _cleared_semantic = {str(p) for p in semantic_files} - _stamped_semantic

        # O manifesto de verificação completa salva linhas removidas para arquivos na raiz que deixaram o
        # digitaliza o corpus, mas ainda existe no disco. O corpus deve ser o
        # Saída de detecção RAW (files_by_type), NÃO a filtrada pelo selo # 933
        # _manifest_files acima – a remoção do conjunto filtrado apagaria
        # linhas falhadas/omitidas-doc e todas as linhas do documento em --code-only são executadas.
        _scan_corpus = (
            {f for _fl in files_by_type.values() for f in _fl}
            if has_path else None
        )

        def _invalidate_file_manifest_for_db_graph() -> None:
            if has_path:
                return
            try:
                manifest_path.unlink(missing_ok=True)
            except OSError as exc:
                print(f"error: could not invalidate file manifest: {exc}", file=sys.stderr)
                sys.exit(1)

        if no_cluster:
            # --no-cluster: despeja a extração mesclada bruta como graph.json.
            # Sem NetworkX, sem detecção de comunidade, sem sidecar de análise.
            # Desduplicar nós (por id) e arestas paralelas para que a saída bruta corresponda ao
            # caminho agrupado (cujo DiGraph recolhe ambos) e permanece determinístico
            # across modes (; node dedup also collapses shared Swift module
            # anchors emitted per importing file).
            from omnigraph.build import dedupe_edges as _dedupe_edges, dedupe_nodes as _dedupe_nodes
            from omnigraph.export import (
                backup_if_protected as _backup,
                existing_graph_node_count as _existing_graph_node_count,
            )
            if (
                incremental_mode
                and not code_files
                and not semantic_files
                and not deleted_files
                and not pg_result.get("nodes")
                and not pg_result.get("edges")
                and not cargo_result.get("nodes")
                and not cargo_result.get("edges")
            ):
                # Uma alteração apenas de exclusão chega a esta porta (arquivos excluídos
                # NÃO estão deliberadamente em Deleted_files), mas ainda devem
                # limpe as fontes recém-excluídas do grafo bruto.
                # Este caminho nunca executa build_merge, então ajuste-o no lugar.
                if graph_stale_sources:
                    _n_pruned = _prune_graph_json_sources(
                        existing_graph_path, graph_stale_sources
                    )
                    if _n_pruned:
                        print(
                            f"[omnigraph extract] pruned {_n_pruned} node(s) from "
                            f"{len(graph_stale_sources)} source file(s) no longer "
                            "in the scan (deleted or excluded)."
                        )
                print(
                    "[omnigraph extract] no incremental changes detected "
                    "(--no-cluster); outputs left untouched."
                )
                try:
                    _save_manifest(_manifest_files, manifest_path=str(manifest_path), kind="both", root=target, scan_corpus=_scan_corpus, clear_semantic=_cleared_semantic)
                except Exception as exc:
                    print(f"[omnigraph extract] warning: could not write manifest: {exc}", file=sys.stderr)
                stages.total()
                sys.exit(0)

            if incremental_mode:
                # this raw path used to write ONLY this run's extraction
                # over graph.json — on an incremental run that is just the
                # changed files, silently dropping every node/edge owned by an
                # unchanged file. Merge the existing graph forward first, with
                # the same replace/prune semantics as the clustered path's
                # build_merge: re-extracted sources replaced, deleted +
                # excluded + graph-stale sources pruned, everything else
                # carried. Survivors are prepended, so the dedupe below keeps
                # this run's fresh attributes for re-extracted nodes.
                from omnigraph.build import merge_raw_extraction as _merge_raw_extraction
                _raw_prune_sources: list[str] = list(deleted_files)
                for _src in list(excluded_files) + graph_stale_sources:
                    if _src not in _raw_prune_sources:
                        _raw_prune_sources.append(_src)
                try:
                    merged = _merge_raw_extraction(
                        merged,
                        graph_path=existing_graph_path,
                        prune_sources=_raw_prune_sources or None,
                        root=target,
                    )
                except RuntimeError as exc:
                    # Existing graph present but unparseable: refuse to
                    # raw-dump this run's partial extraction over it.
                    print(f"error: {exc}", file=sys.stderr)
                    sys.exit(1)
            merged["nodes"] = _dedupe_nodes(merged["nodes"])
            merged["edges"] = _dedupe_edges(merged["edges"])
            # Disambiguate colliding-basename file-node labels. This raw
            # --no-cluster path bypasses build_from_json (where the clustered path
            # gets this), so apply it directly on the merged node list.
            from omnigraph.build import disambiguate_file_labels_in_nodes as _disamb_labels
            _disamb_labels(merged["nodes"])
            # Preencher source_file dos nós do endpoint — esse caminho bruto ignora
            # O preenchimento de build_from_json e as arestas semânticas às vezes o omitem.
            _node_sf = {n.get("id"): n.get("source_file") for n in merged["nodes"]}
            for _e in merged["edges"]:
                if not _e.get("source_file"):
                    _e["source_file"] = (
                        _node_sf.get(_e.get("source")) or _node_sf.get(_e.get("target")) or ""
                    )
            # RT-parity for the raw path: an incomplete build must not force a
            # partial graph over a larger complete one here either. The clustered
            # path gets this from to_json's guard; this path never calls
            # to_json, so replicate the shrink check against the existing file and
            # exit before the write/manifest unless --allow-partial is set.
            if _extraction_incomplete and not cli_allow_partial:
                from omnigraph.export import MALFORMED_GRAPH as _MALFORMED_GRAPH
                _existing_n = _existing_graph_node_count(graph_json_path)
                _malformed = _existing_n is _MALFORMED_GRAPH
                _shrinks = isinstance(_existing_n, int) and len(merged["nodes"]) < _existing_n
                if _malformed or _shrinks:
                    _detail = (
                        f"the existing {graph_json_path} is present but unparseable "
                        "(corrupt or a mid-write), so a shrink cannot be ruled out"
                        if _malformed
                        else f"smaller than the existing {graph_json_path} "
                        f"({len(merged['nodes'])} < {_existing_n} nodes)"
                    )
                    print(
                        "[omnigraph extract] error: extraction was incomplete (an AST/"
                        f"semantic pass failed) and the resulting --no-cluster graph is {_detail}. "
                        "Refusing to overwrite a complete graph with a partial one. Re-run after "
                        "fixing the failures, or pass --allow-partial to overwrite anyway.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            _backup(omnigraph_out)
            _invalidate_file_manifest_for_db_graph()
            from omnigraph.paths import write_json_atomic as _write_json_atomic
            _write_json_atomic(graph_json_path, merged, indent=2)
            try:
                # Record the scan root so a later build_merge / update runbook can
                # relativize deleted-file paths correctly even for a custom --out
                # (its grandparent-of-graph.json fallback points at the wrong dir
                # otherwise, and deleted files never prune —/).
                (omnigraph_out / ".omnigraph_root").write_text(
                    str(Path(target).resolve()), encoding="utf-8"
                )
            except OSError:
                pass
            stages.mark("write")
            cost = _estimate_cost(
                backend, merged["input_tokens"], merged["output_tokens"]
            )
            print(
                f"[omnigraph extract] wrote {graph_json_path} — "
                f"{len(merged['nodes'])} nodes, {len(merged['edges'])} edges "
                f"(no clustering)"
            )
            if merged["input_tokens"] or merged["output_tokens"]:
                print(
                    f"[omnigraph extract] tokens: "
                    f"{merged['input_tokens']:,} in / "
                    f"{merged['output_tokens']:,} out, "
                    f"est. cost: ${cost:.4f}"
                )
            try:
                if has_path:
                    _save_manifest(_manifest_files, manifest_path=str(manifest_path), kind="both", root=target, scan_corpus=_scan_corpus, clear_semantic=_cleared_semantic)
            except Exception as exc:
                print(f"[omnigraph extract] warning: could not write manifest: {exc}", file=sys.stderr)
            if global_merge:
                from omnigraph.global_graph import global_add as _global_add
                _tag = global_repo_tag or target.name
                try:
                    result = _global_add(omnigraph_out / "graph.json", _tag)
                    if result["skipped"]:
                        print(f"[omnigraph global] '{_tag}' unchanged since last add - skipped.")
                    else:
                        print(f"[omnigraph global] '{_tag}' merged into global graph "
                              f"(+{result['nodes_added']} nodes, -{result['nodes_removed']} pruned).")
                except Exception as exc:
                    print(f"[omnigraph global] warning: failed to merge into global graph: {exc}", file=sys.stderr)
            stages.total()
            sys.exit(0)

        # Build graph + cluster + score + write.
        from omnigraph.build import (
            build as _build,
            build_from_json as _build_from_json,
            build_merge as _build_merge,
        )
        from omnigraph.cluster import cluster as _cluster, score_all as _score_all
        from omnigraph.export import to_json as _to_json
        from omnigraph.analyze import god_nodes as _god_nodes, surprising_connections as _surprising
        dedup_backend = backend if dedup_llm else None
        if incremental_mode:
            # Remova tudo o que a varredura atual não cobre mais: genuinamente
            # linhas de manifesto excluídas, linhas de manifesto excluídas, mas ativas,
            # e as próprias fontes obsoletas do grafo - que captura arquivos que
            # foi excluído sem nunca ter sido listado no manifesto.
            _prune_sources: list[str] = list(deleted_files)
            for _src in list(excluded_files) + graph_stale_sources:
                if _src not in _prune_sources:
                    _prune_sources.append(_src)
            G = _build_merge(
                [merged],
                graph_path=existing_graph_path,
                prune_sources=_prune_sources or None,
                dedup=True,
                dedup_llm_backend=dedup_backend,
                root=target,
            )
        else:
            G = _build([merged], dedup=True, dedup_llm_backend=dedup_backend, root=target)
        stages.mark("build")
        if G.number_of_nodes() == 0:
            print(
                "[omnigraph extract] graph is empty — extraction produced no nodes. "
                "Possible causes: all files skipped, binary-only corpus, or LLM "
                "returned no edges.",
                file=sys.stderr,
            )
            sys.exit(1)

        communities = _cluster(G, resolution=cli_resolution, exclude_hubs_percentile=cli_exclude_hubs)
        stages.mark("cluster")
        cohesion = _score_all(G, communities)
        try:
            gods = _god_nodes(G)
        except Exception:
            gods = []
        try:
            surprises = _surprising(G, communities)
        except Exception:
            surprises = []
        stages.mark("analyze")

        from omnigraph.export import backup_if_protected as _backup
        _backup(omnigraph_out)
        _invalidate_file_manifest_for_db_graph()
        # force=True bypasses the shrink guard entirely. A full build
        # legitimately shrinks (fuzzy dedup collapse, deleted code) so it keeps
        # force=True — EXCEPT when this run's extraction was incomplete (an
        # extractor pass crashed or some semantic chunks failed). Then a partial
        # graph could silently overwrite a good complete one, so fall back to the
        # shrink guard (force=False) unless the user opts in with --allow-partial.
        #
        # Both write paths are guarded: the clustered path here via to_json's
        # check, and the `--no-cluster` raw-dump path above via the same
        # shrink check against the existing file (existing_graph_node_count).
        #
        # Trade-off: this reuses to_json's coarse node-count guard, not the
        # source-aware _check_shrink that watch/update use. On an incremental run
        # a legitimate deletion that coincides with an unrelated transient chunk
        # failure can therefore be refused here — recoverable by re-running or
        # passing --allow-partial (the good graph is preserved and the manifest
        # is not stamped, so the retry re-extracts).
        _force_write = cli_allow_partial or not _extraction_incomplete
        _wrote = _to_json(G, communities, str(graph_json_path), force=_force_write)
        if not _wrote:
            # The shrink guard refused: this partial build is smaller than the
            # existing graph. Exit before writing the manifest/marker below, which
            # would otherwise stamp these files as done and make the next
            # incremental run skip re-extracting them (poisoning the manifest
            # against the graph we declined to write). Exit non-zero so a retry
            # re-attempts.
            print(
                "[omnigraph extract] error: extraction was incomplete (an AST/semantic "
                f"pass failed) and the resulting graph is smaller than the existing "
                f"{graph_json_path}. Refusing to overwrite a complete graph with a "
                "partial one. Re-run after fixing the failures, or pass --allow-partial "
                "to overwrite anyway.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            # See the --no-cluster path above: persist the scan root so build_merge
            # can relativize deleted-file paths under a custom --out.
            (omnigraph_out / ".omnigraph_root").write_text(
                str(Path(target).resolve()), encoding="utf-8"
            )
        except OSError:
            pass
        stages.mark("export")
        if merged.get("output_tokens", 0) > 0:
            (omnigraph_out / ".omnigraph_semantic_marker").write_text(
                json.dumps({"output_tokens": merged["output_tokens"]}), encoding="utf-8"
            )
        if global_merge:
            from omnigraph.global_graph import global_add as _global_add
            _tag = global_repo_tag or target.name
            try:
                result = _global_add(omnigraph_out / "graph.json", _tag)
                if result["skipped"]:
                    print(f"[omnigraph global] '{_tag}' unchanged since last add - skipped.")
                else:
                    print(f"[omnigraph global] '{_tag}' merged into global graph "
                          f"(+{result['nodes_added']} nodes, -{result['nodes_removed']} pruned).")
            except Exception as exc:
                print(f"[omnigraph global] warning: failed to merge into global graph: {exc}", file=sys.stderr)
        analysis = {
            "communities": {str(k): v for k, v in communities.items()},
            "cohesion": {str(k): v for k, v in cohesion.items()},
            "gods": gods,
            "surprises": surprises,
            "tokens": {
                "input": merged["input_tokens"],
                "output": merged["output_tokens"],
            },
        }
        from omnigraph.paths import write_json_atomic as _wja
        _wja(analysis_path, analysis, indent=2)
        try:
            if has_path:
                _save_manifest(_manifest_files, manifest_path=str(manifest_path), kind="both", root=target, scan_corpus=_scan_corpus, clear_semantic=_cleared_semantic)
        except Exception as exc:
            print(f"[omnigraph extract] warning: could not write manifest: {exc}", file=sys.stderr)

        cost = _estimate_cost(backend, merged["input_tokens"], merged["output_tokens"])
        print(
            f"[omnigraph extract] wrote {graph_json_path}: "
            f"{G.number_of_nodes()} nodes, {G.number_of_edges()} edges, "
            f"{len(communities)} communities"
        )
        print(f"[omnigraph extract] wrote {analysis_path}")
        if incremental_mode:
            _excl_note = f", {len(excluded_files)} excluded" if excluded_files else ""
            print(
                f"[omnigraph extract] incremental summary: "
                f"{sem_cache_hits + unchanged_total} files cached/unchanged, "
                f"{len(code_files) + sem_cache_misses} re-extracted, "
                f"{len(deleted_files)} deleted{_excl_note}"
            )
        elif sem_cache_hits:
            print(f"[omnigraph extract] semantic cache: {sem_cache_hits} cached, {sem_cache_misses} re-extracted")
        if merged["input_tokens"] or merged["output_tokens"]:
            print(
                f"[omnigraph extract] tokens: "
                f"{merged['input_tokens']:,} in / "
                f"{merged['output_tokens']:,} out, "
                f"est. cost (~{backend}): ${cost:.4f}"
            )
        # extrair para intencionalmente em graph.json + análise; o relatório e
        # rótulos de comunidade são produzidos por `cluster-only` (ou pela Etapa 5 de um agente).
        # Aponte usuários autônomos para que as comunidades sejam nomeadas.
        print(
            "[omnigraph extract] next: run "
            f"`omnigraph cluster-only {omnigraph_out.parent}` "
            "to generate GRAPH_REPORT.md and name communities"
        )
        stages.total()

    elif cmd == "cache-check":
        # omnigraph cache-check <files_from> [--root <dir>] [--mode <m> | --deep]
        #                       [--prompt-file <path>]
        # Lê caminhos de arquivos (um por linha) de <files_from>, verifica o cache semântico.
        # --mode deep (ou --deep) verifica o cache/semantic-deep/ namespace
        # escrito por `extract --mode deep` em vez de cache/semantic/.
        # --prompt-file names the extraction prompt the caller will use (an agent's
        # references/extraction-spec.md), restricting hits to entries produced by
        # that same prompt. Omitting it reads the unattributed layout, which
        # cannot see entries a fingerprinted run wrote.
        # Writes:
        #   omnigraph-out/.omnigraph_cached.json   — already-cached nodes/edges/hyperedges
        #   omnigraph-out/.omnigraph_uncached.txt — caminhos que precisam de extração
        # Stdout: "Cache: N hit, M miss"
        from omnigraph.cache import check_semantic_cache
        if len(sys.argv) < 3:
            print("Usage: omnigraph cache-check <files_from> [--root <dir>] "
                  "[--mode <m> | --deep] [--prompt-file <path>]", file=sys.stderr)
            sys.exit(1)
        files_from = Path(sys.argv[2])
        root = Path(".")
        cache_mode: str | None = None
        prompt_file: str | None = None
        i = 3
        while i < len(sys.argv):
            if sys.argv[i] == "--root" and i + 1 < len(sys.argv):
                root = Path(sys.argv[i + 1])
                i += 2
            elif sys.argv[i] == "--mode" and i + 1 < len(sys.argv):
                cache_mode = sys.argv[i + 1]
                i += 2
            elif sys.argv[i].startswith("--mode="):
                cache_mode = sys.argv[i].split("=", 1)[1]
                i += 1
            elif sys.argv[i] == "--deep":
                cache_mode = "deep"
                i += 1
            elif sys.argv[i] == "--prompt-file" and i + 1 < len(sys.argv):
                prompt_file = sys.argv[i + 1]
                i += 2
            elif sys.argv[i].startswith("--prompt-file="):
                prompt_file = sys.argv[i].split("=", 1)[1]
                i += 1
            else:
                i += 1
        files = [f for f in files_from.read_text(encoding="utf-8").splitlines() if f.strip()]
        cached_nodes, cached_edges, cached_hyperedges, uncached = check_semantic_cache(
            files, root, mode=cache_mode, prompt_file=prompt_file
        )
        out = root / _OMNIGRAPH_OUT
        out.mkdir(parents=True, exist_ok=True)
        if cached_nodes or cached_edges or cached_hyperedges:
            (out / ".omnigraph_cached.json").write_text(
                json.dumps({"nodes": cached_nodes, "edges": cached_edges, "hyperedges": cached_hyperedges},
                           ensure_ascii=False),
                encoding="utf-8",
            )
        (out / ".omnigraph_uncached.txt").write_text("\n".join(uncached), encoding="utf-8")
        print(f"Cache: {len(files) - len(uncached)} hit, {len(uncached)} miss")

    elif cmd == "merge-chunks":
        # omnigraph merge-chunks <chunk_glob_or_files...> --out <path>
        # Concatena arquivos .omnigraph_chunk_*.json escritos por subagentes semânticos.
        # Desduplica nós por ID (o primeiro gravador vence). Soma contagens de tokens.
        import glob as _glob
        if len(sys.argv) < 3:
            print("Usage: omnigraph merge-chunks <chunk_files...> --out <path>", file=sys.stderr)
            sys.exit(1)
        out_path: Path | None = None
        chunk_args: list[str] = []
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--out" and i + 1 < len(sys.argv):
                out_path = Path(sys.argv[i + 1])
                i += 2
            else:
                chunk_args.append(sys.argv[i])
                i += 1
        if not out_path:
            print("error: --out <path> required", file=sys.stderr)
            sys.exit(1)
        chunk_files: list[str] = []
        for arg in chunk_args:
            expanded = _glob.glob(arg)
            chunk_files.extend(sorted(expanded) if expanded else [arg])
        merged: dict = {"nodes": [], "edges": [], "hyperedges": [], "input_tokens": 0, "output_tokens": 0}
        seen_ids: set[str] = set()
        valid_chunks = 0
        # These chunk files are untrusted subagent output. load_validated_...
        # stats the file size BEFORE reading it (so a multi-GB chunk can't blow up
        # memory), parses the JSON, and validates the security caps + the node/
        # edge id charset that blocks path traversal — the same enforcement
        # the skill merge path applies. A bad chunk is skipped with a warning
        # while valid siblings still merge; if every chunk is invalid, fail
        # closed instead of reporting success and replacing --out with an empty
        # semantic layer. Deliberately NOT wired into
        # build_from_json/load_graph_json, which must keep loading valid
        # pre-existing graphs. file_type is left to build's coercion.
        from omnigraph.semantic_cleanup import load_validated_semantic_fragment
        for cf in chunk_files:
            chunk, _chunk_errs = load_validated_semantic_fragment(Path(cf))
            if _chunk_errs:
                print(
                    f"[omnigraph merge-chunks] warning: skipping invalid chunk {cf}: "
                    f"{'; '.join(_chunk_errs[:3])}",
                    file=sys.stderr,
                )
                continue
            valid_chunks += 1
            for n in chunk.get("nodes", []):
                if n.get("id") not in seen_ids:
                    seen_ids.add(n["id"])
                    merged["nodes"].append(n)
            merged["edges"].extend(chunk.get("edges", []))
            merged["hyperedges"].extend(chunk.get("hyperedges", []))
            # Coerce token counts: a chunk is untrusted, so a non-numeric
            # input_tokens/output_tokens must not abort the whole merge with a
            # TypeError after other chunks already merged.
            for _tok in ("input_tokens", "output_tokens"):
                _v = chunk.get(_tok, 0)
                merged[_tok] += _v if isinstance(_v, (int, float)) else 0
        if not valid_chunks:
            print(
                f"[omnigraph merge-chunks] error: no valid chunks to merge; "
                f"refusing to write {out_path}",
                file=sys.stderr,
            )
            sys.exit(1)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        from omnigraph.paths import write_json_atomic as _wja
        _wja(out_path, merged, ensure_ascii=False)
        chunk_summary = (
            f"{valid_chunks} chunks"
            if valid_chunks == len(chunk_files)
            else f"{valid_chunks} of {len(chunk_files)} chunks"
        )
        print(
            f"Merged {chunk_summary}: {len(merged['nodes'])} nodes, {len(merged['edges'])} edges, "
            f"{merged['input_tokens']:,} in / {merged['output_tokens']:,} out tokens"
        )

    elif cmd == "merge-semantic":
        # omnigraph merge-semantic --cached <path> --new <path> --out <path>
        # Mescla resultados semânticos armazenados em cache com resultados de pedaços recém-extraídos.
        # Desduplica nós por ID (as entradas em cache têm prioridade sobre as novas).
        if len(sys.argv) < 3:
            print("Usage: omnigraph merge-semantic --cached <path> --new <path> --out <path>", file=sys.stderr)
            sys.exit(1)
        cached_path: Path | None = None
        new_path: Path | None = None
        out_path2: Path | None = None
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--cached" and i + 1 < len(sys.argv):
                cached_path = Path(sys.argv[i + 1]); i += 2
            elif sys.argv[i] == "--new" and i + 1 < len(sys.argv):
                new_path = Path(sys.argv[i + 1]); i += 2
            elif sys.argv[i] == "--out" and i + 1 < len(sys.argv):
                out_path2 = Path(sys.argv[i + 1]); i += 2
            else:
                i += 1
        if not out_path2:
            print("error: --out <path> required", file=sys.stderr)
            sys.exit(1)
        empty: dict = {"nodes": [], "edges": [], "hyperedges": []}
        cached_data = json.loads(cached_path.read_text(encoding="utf-8")) if cached_path and cached_path.exists() else empty
        new_data = json.loads(new_path.read_text(encoding="utf-8")) if new_path and new_path.exists() else empty
        seen_ids2: set[str] = set()
        all_nodes: list[dict] = []
        for n in cached_data.get("nodes", []) + new_data.get("nodes", []):
            if n.get("id") not in seen_ids2:
                seen_ids2.add(n["id"])
                all_nodes.append(n)
        merged2 = {
            "nodes": all_nodes,
            "edges": cached_data.get("edges", []) + new_data.get("edges", []),
            "hyperedges": cached_data.get("hyperedges", []) + new_data.get("hyperedges", []),
        }
        out_path2.parent.mkdir(parents=True, exist_ok=True)
        from omnigraph.paths import write_json_atomic as _wja
        _wja(out_path2, merged2, ensure_ascii=False)
        print(f"Merged: {len(merged2['nodes'])} nodes, {len(merged2['edges'])} edges")

    elif Path(cmd).exists() or cmd in (".", "..") or cmd.startswith(("./", "../", "/", "~")):
        # O usuário executou `omnigraph <caminho>` diretamente - trate como `omnigraph extract <caminho>`.
        # Comum ao seguir a nota do PowerShell no README (`omnigraph.`) ou
        # invocações de habilidades de copiar e colar sem a barra inicial.
        sys.argv.insert(2, sys.argv[1])
        sys.argv[1] = "extract"
        _reenter_main()
    else:
        print(f"error: unknown command '{cmd}'", file=sys.stderr)
        print("Run 'omnigraph --help' for usage.", file=sys.stderr)
        sys.exit(1)
