from __future__ import annotations
import contextlib
import json
import os
import posixpath
import re
import sys
import time
from pathlib import Path
from typing import Callable

# Fonte única de verdade em zspekfy.paths; reexportado como _OMNIGRAPH_OUT.
from omnigraph.paths import OMNIGRAPH_OUT as _OMNIGRAPH_OUT, is_absolute_any_platform
_PENDING_FILENAME = ".pending_changes"
_PENDING_DRAIN_MAX_PASSES = 20


def _queue_pending(out_dir: Path, changed_paths: list[Path]) -> None:
    """Append ``changed_paths`` to ``out_dir/.pending_changes`` (one per line).

    Used by a post-commit hook process that cannot acquire ``_rebuild_lock``
    so its change set is not silently dropped (#1059). The lock-holding
    process drains this file before and after its rebuild and merges the
    contents with its own change set.

    Opened in append mode so concurrent writers do not clobber each other on
    POSIX; each ``write()`` of a small payload is effectively atomic. A
    trailing newline is always written so partial-line corruption stays
    confined to the offending entry and is skipped on drain.
    """
    if not changed_paths:
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    pending = out_dir / _PENDING_FILENAME
    payload = "".join(f"{os.fspath(p)}\n" for p in changed_paths)
    with open(pending, "a", encoding="utf-8") as fh:
        fh.write(payload)


def _drain_pending(out_dir: Path) -> list[Path]:
    """Read + unlink ``out_dir/.pending_changes`` and return deduplicated paths.

    Returns an empty list if the file does not exist. Empty/whitespace lines
    are silently skipped so a partial concurrent write that left only a
    fragment cannot poison the merge.
    """
    pending = out_dir / _PENDING_FILENAME
    if not pending.exists():
        return []
    try:
        raw = pending.read_text(encoding="utf-8")
    except OSError:
        return []
    # dados na visualização do próximo chamador através das linhas que estamos prestes a retornar -
    # ou seja, perder o arquivo após a leitura é bom, perdê-lo antes seria um
    with contextlib.suppress(FileNotFoundError):
        pending.unlink()
    seen: set[str] = set()
    out: list[Path] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(Path(s))
    return out


# Opções de construção que devem sobreviver em reconstruções posteriores. O `extrato` inicial
# scan honra `--exclude`, mas `update`/`watch`/hook reconstrói novamente detect()
# e reincluiria silenciosamente os caminhos excluídos, a menos que os padrões persistissem
#. Nós os armazenamos ao lado do grafo para que qualquer driver de reconstrução possa reaplicá-los.
_BUILD_CONFIG_FILENAME = ".omnigraph_build.json"


def _write_build_config(
    out_dir: Path,
    *,
    excludes: "list[str] | None",
    gitignore: bool | None = None,
) -> None:
    """Persist corpus-shaping options under ``out_dir``.

    Best effort and non clobbering: omitted options retain their existing values.
    """
    if not excludes and gitignore is None:
        return
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / _BUILD_CONFIG_FILENAME
        try:
            config = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            config = {}
        if not isinstance(config, dict):
            config = {}
        if excludes:
            config["excludes"] = list(excludes)
        if gitignore is not None:
            config["gitignore"] = gitignore
        path.write_text(json.dumps(config), encoding="utf-8")
    except OSError:
        pass


def _read_build_excludes(out_dir: Path) -> list[str]:
    """Return the persisted ``--exclude`` patterns for this graph, or []."""
    try:
        path = out_dir / _BUILD_CONFIG_FILENAME
        if path.is_file():
            cfg = json.loads(path.read_text(encoding="utf-8"))
            ex = cfg.get("excludes") if isinstance(cfg, dict) else None
            if isinstance(ex, list):
                return [str(x) for x in ex if isinstance(x, str) and x]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _read_build_gitignore(out_dir: Path) -> bool:
    """Return whether rebuilds should honor VCS ignore files (default True)."""
    try:
        path = out_dir / _BUILD_CONFIG_FILENAME
        if path.is_file():
            cfg = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(cfg, dict) and isinstance(cfg.get("gitignore"), bool):
                return cfg["gitignore"]
    except (OSError, json.JSONDecodeError):
        pass
    return True


def _merge_changed_paths(*sources: "list[Path] | None") -> list[Path]:
    """Concatenate path lists, preserving order and dropping duplicates.

    Used to combine a hook process's own ``changed_paths`` with the drained
    contents of ``.pending_changes`` so the lock-holding rebuild covers
    every queued commit's worth of files (#1059).
    """
    seen: set[str] = set()
    out: list[Path] = []
    for src in sources:
        if not src:
            continue
        for p in src:
            key = os.fspath(p)
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    return out


@contextlib.contextmanager
def _rebuild_lock(out_dir: Path, *, blocking: bool = False):
    """Per-repo advisory lock around a rebuild.

    Yields True if acquired, False if another rebuild is already running and
    ``blocking`` is False. Uses fcntl.flock so the lock is released
    automatically if the process is killed (no stale-lock cleanup needed).

    While the lock is held, ``.rebuild.lock`` contains the owning PID followed
    by a newline so external pollers (publish scripts, etc.) can read it.
    On successful release the file is unlinked so downstream tooling that
    waits for the lock to clear by polling for its absence unblocks promptly.

    Falls back to a no-op yield(True) on platforms without fcntl (Windows).
    """
    try:
        import fcntl
    except ImportError:
        yield True
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    lock_path = out_dir / ".rebuild.lock"
    # Carga útil do PID – importante porque outro processo pode já ter sido escrito
    fh = open(lock_path, "a+", encoding="utf-8")
    acquired = False
    try:
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fh.fileno(), flags)
        except BlockingIOError:
            yield False
            return
        acquired = True
        # Substitua o PID de qualquer proprietário anterior pelo nosso para que os leitores externos vejam um
        # única linha analisável, não uma concatenação de dígitos entre reconstruções.
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
        except OSError:
            pass
        yield True
    finally:
        if acquired:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()
        # Sinalize "reconstrução concluída" removendo o arquivo de bloqueio. Somente o titular
        # desvincula; um chamador que não adquire deixa o bloqueio existente no lugar.
        if acquired:
            with contextlib.suppress(OSError):
                lock_path.unlink()


def _apply_resource_limits() -> None:
    """Best-effort nice + memory cap. Called from inline hook scripts.

    OMNIGRAPH_REBUILD_MEMORY_LIMIT_MB caps RSS-ish memory. Uses RLIMIT_DATA on
    macOS (RLIMIT_AS is unreliable under Apple's libmalloc) and RLIMIT_AS on
    Linux. Silently skips if the platform doesn't support it.
    """
    try:
        os.nice(10)
    except (OSError, AttributeError):
        pass
    mb = os.environ.get("OMNIGRAPH_REBUILD_MEMORY_LIMIT_MB", "").strip()
    if not mb:
        return
    try:
        limit = int(mb) * 1024 * 1024
    except ValueError:
        return
    try:
        import resource
        which = resource.RLIMIT_DATA if sys.platform == "darwin" else resource.RLIMIT_AS
        soft, hard = resource.getrlimit(which)
        new_hard = hard if hard != resource.RLIM_INFINITY and hard < limit else limit
        resource.setrlimit(which, (limit, new_hard))
    except (ImportError, ValueError, OSError):
        pass


def _git_head(cwd: Path | str | None = None) -> str | None:
    """Return current git HEAD commit hash, or None outside a repo.

    ``cwd`` selects the repository to ask (#2316). Without it the command
    inherits the caller's working directory, so `omnigraph update <target>`
    stamped the *invoking* repo's commit into the target's graph.json — the
    same CWD-anchoring mistake as the manifest path, but writing wrong
    provenance rather than a misplaced file.
    """
    import subprocess as _sp
    try:
        r = _sp.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=3,
            cwd=str(cwd) if cwd is not None else None,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


from omnigraph.detect import (
    CODE_EXTENSIONS,
    DOC_EXTENSIONS,
    PAPER_EXTENSIONS,
    IMAGE_EXTENSIONS,
    _load_omnigraphignore,
    _is_ignored,
)

_WATCHED_EXTENSIONS = CODE_EXTENSIONS | DOC_EXTENSIONS | PAPER_EXTENSIONS | IMAGE_EXTENSIONS
_CODE_EXTENSIONS = CODE_EXTENSIONS


def _report_root_label(watch_path: Path) -> str:
    if watch_path.is_absolute():
        return watch_path.name or str(watch_path)
    return Path.cwd().name if watch_path == Path(".") else str(watch_path)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _changed_path_candidates(raw: Path, *, change_root: Path, watch_root: Path) -> list[Path]:
    """Return plausible absolute locations for a hook-provided changed path.

    Git hooks pass paths relative to the repository root. Watch callers may
    also pass paths relative to the watched root. Keep both interpretations so
    a graph rooted at ``src`` accepts ``src/app.py`` and ``app.py``.
    """
    if raw.is_absolute():
        lexical = Path(os.path.abspath(raw))
        resolved = raw.resolve()
        return [lexical] if lexical == resolved else [lexical, resolved]

    candidates: list[Path] = []
    seen: set[str] = set()
    for base in (change_root, watch_root):
        lexical = Path(os.path.abspath(base / raw))
        for cand in (lexical, lexical.resolve()):
            key = os.fspath(cand)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(cand)
    return candidates


def _relativize_source_files(payload: dict, root: Path, *, scope: Path | None = None) -> None:
    for bucket in ("nodes", "edges", "hyperedges"):
        for item in payload.get(bucket, []):
            source = item.get("source_file")
            if not source:
                continue
            source_path = Path(source)
            if not source_path.is_absolute():
                continue
            try:
                resolved = source_path.resolve()
                if scope is not None and not _is_relative_to(resolved, scope):
                    continue
                item["source_file"] = resolved.relative_to(root).as_posix()
            except ValueError:
                continue


def _rebase_relative_source_files(payload: dict, source_root: Path, target_root: Path) -> None:
    """Rebase cache-root-relative source paths onto the project root."""
    if source_root == target_root:
        return
    for bucket in ("nodes", "edges", "hyperedges"):
        for item in payload.get(bucket, []):
            source = item.get("source_file")
            if not source or Path(source).is_absolute():
                continue
            try:
                item["source_file"] = (source_root / source).relative_to(target_root).as_posix()
            except ValueError:
                continue


class _StoredSourcePaths:
    """Resolve source_file values across current and legacy graph roots."""

    def __init__(
        self,
        existing: dict,
        *,
        out: Path,
        project_root: Path,
        watch_root: Path,
        normalize_source,
    ) -> None:
        self.project_root = project_root
        self.watch_root = watch_root
        self._normalize_source = normalize_source
        self.existing_source_root = project_root
        relative_marker_prefix: str | None = None

        root_marker = out / ".omnigraph_root"
        if root_marker.exists():
            try:
                saved_root = Path(root_marker.read_text(encoding="utf-8").strip())
                if saved_root.is_absolute():
                    resolved = saved_root.resolve()
                    if self._anchors_stored_sources(existing, resolved):
                        self.existing_source_root = resolved
                    else:
                        for candidate in (self.project_root, Path.cwd().resolve()):
                            if self._anchors_stored_sources(existing, candidate):
                                self.existing_source_root = candidate
                                break
                        else:
                            self.existing_source_root = resolved
                else:
                    invocation_root = Path.cwd().resolve()
                    if (invocation_root / saved_root).resolve() == watch_root:
                        self.existing_source_root = invocation_root
                        relative_marker_prefix = posixpath.normpath(saved_root.as_posix())
            except (OSError, ValueError):
                pass

        self.legacy_watch_relative = False
        if relative_marker_prefix not in (None, "."):
            has_project_relative_source = False
            for bucket in ("nodes", "links", "edges", "hyperedges"):
                for item in existing.get(bucket, []):
                    stored = normalize_source(item.get("source_file"))
                    if not stored or Path(stored).is_absolute():
                        continue
                    normalized = posixpath.normpath(stored)
                    if (
                        normalized == relative_marker_prefix
                        or normalized.startswith(relative_marker_prefix + "/")
                    ):
                        has_project_relative_source = True
                        break
                if has_project_relative_source:
                    break
            self.legacy_watch_relative = not has_project_relative_source

    def _anchors_stored_sources(self, existing: dict, root: Path, sample: int = 25) -> bool:
        """Whether stored relative source_file paths resolve under ``root``.

        Samples the first ``sample`` relative entries: the first hit accepts
        the anchor; ``sample`` consecutive misses reject it. A graph with no
        relative sources returns True (any anchor is harmless there). The
        bound keeps the check O(sample) on large graphs; its known limit is a
        commit that deletes ``sample``-plus files whose nodes happen to sort
        first — the anchor then falls back to the marker, matching the
        pre-fix behavior (no worse).
        """
        checked = 0
        for bucket in ("nodes", "links", "edges", "hyperedges"):
            for item in existing.get(bucket, []):
                raw = item.get("source_file") if isinstance(item, dict) else None
                stored = self._normalize_source(raw) if raw else None
                if not stored or is_absolute_any_platform(stored):
                    continue
                checked += 1
                if (root / Path(posixpath.normpath(stored))).exists():
                    return True
                if checked >= sample:
                    return False
        return checked == 0

    def normalize(self, source_file: str | None) -> str | None:
        normalized = self._normalize_source(source_file, str(self.project_root))
        return posixpath.normpath(normalized) if normalized else normalized

    def absolute_identity(self, source_file: str | None, root: Path) -> str | None:
        normalized = self._normalize_source(source_file)
        if not normalized:
            return normalized
        source_path = Path(posixpath.normpath(normalized))
        if not source_path.is_absolute():
            source_path = root / source_path
        return Path(os.path.abspath(source_path)).as_posix()

    def identity(self, source_file: str | None) -> str | None:
        normalized = self._normalize_source(source_file)
        if normalized and not Path(normalized).is_absolute() and self.legacy_watch_relative:
            return self.absolute_identity(normalized, self.watch_root)
        return self.absolute_identity(normalized, self.existing_source_root)

    def in_watch_root(self, source_file: str | None) -> bool:
        identity = self.identity(source_file)
        return bool(identity) and _is_relative_to(Path(identity), self.watch_root)

    def is_evicted(self, item: dict, identities: set[str]) -> bool:
        return self.identity(item.get("source_file")) in identities

    def rebase_preserved(self, item: dict) -> None:
        identity = self.identity(item.get("source_file"))
        if not identity:
            return
        identity_path = Path(identity)
        if not _is_relative_to(identity_path, self.watch_root):
            normalized = self.normalize(item.get("source_file"))
            if normalized:
                item["source_file"] = normalized
            return
        try:
            item["source_file"] = identity_path.relative_to(self.project_root).as_posix()
        except ValueError:
            item["source_file"] = identity


_REMOTE_SOURCE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]+://?")


def _is_remote_source(source_file: str) -> bool:
    return bool(_REMOTE_SOURCE_RE.match(source_file))


def _reconcile_existing_graph(
    existing_graph: Path,
    result: dict,
    *,
    out: Path,
    project_root: Path,
    watch_root: Path,
    code_files: list[Path],
    extract_targets: list[Path],
    full_rebuild: bool,
    deleted_paths: set[str],
    deleted_source_identities: set[str],
    is_ignored_always: Callable[[Path], bool] | None = None,
    is_ignored_full: Callable[[Path], bool] | None = None,
) -> tuple[dict, dict]:
    """Merge fresh extraction with preserved graph entries and evict stale sources.

    ``is_ignored_always``/``is_ignored_full`` (#2495) report whether a LIVE
    ignore rule matches a path, mirroring the detect() call that produced
    ``code_files`` (detect.ignored_predicate). ``is_ignored_always`` covers
    .omnigraphignore + persisted ``--exclude`` patterns — unambiguous
    graph-level intent, honored on every rebuild. ``is_ignored_full`` adds
    .gitignore-driven rules, honored only on a full rebuild (an explicit
    ``omnigraph update``) so the incremental/hook path keeps preserving a
    deliberately-graphed .gitignore'd tree (#1795).
    """
    existing_graph_data: dict = {}
    if not existing_graph.exists():
        return result, existing_graph_data

    from omnigraph.build import _load_existing_graph
    if _load_existing_graph(existing_graph) is None:
        return result, existing_graph_data
    existing = json.loads(existing_graph.read_text(encoding="utf-8"))
    existing_graph_data = existing

    from omnigraph.build import _is_ast_tier
    for _bucket in ("nodes", "links", "edges"):
        for _item in existing.get(_bucket, []):
            if isinstance(_item, dict):
                _item.setdefault("_origin", "ast" if _is_ast_tier(_item) else "semantic")

    try:
        from omnigraph.build import _norm_source_file as _nsf
        from omnigraph.extract import _get_extractor
        source_paths = _StoredSourcePaths(
            existing,
            out=out,
            project_root=project_root,
            watch_root=watch_root,
            normalize_source=_nsf,
        )
        new_ast_ids = {n["id"] for n in result["nodes"]}
        current_sources = {
            source_paths.absolute_identity(str(path), project_root) for path in code_files
        }
        rebuilt_source_identities = {
            source_paths.absolute_identity(str(path), project_root) for path in extract_targets
        }
        node_evicted_source_identities = set(deleted_source_identities)
        hyperedge_evicted_source_identities = set(deleted_source_identities)
        # A exclusão despeja arestas independentemente do nível; a reextração possui apenas um
        edge_evicted_source_identities = set(deleted_source_identities)
        if not full_rebuild:
            node_evicted_source_identities.update(rebuilt_source_identities)

        # Reconcilie cada reconstrução com o corpus monitorado atual. Mudança de gancho
        # listas podem conter apenas um destino de renomeação, portanto, apenas caminhos explícitos
        # não é possível identificar a fonte obsoleta. Mantenha a comparação com escopo para o
        # assistiu root para que as atualizações de subpastas preservem os registros fora dessa subárvore.
        # Despejo com falha: uma identidade de origem ausente no corpus é apenas
        # Evidência DELETION quando o arquivo realmente saiu do disco. Um arquivo que
        excluded_alive_files: set[str] = set()
        excluded_alive_nodes = 0
        newly_ignored_files: set[str] = set()
        newly_ignored_nodes = 0
        _alive_cache: dict[str, bool] = {}
        _ignored_cache: dict[str, bool] = {}

        def _ignored_now(identity: str) -> bool:
            """True when a live ignore rule matches this alive, corpus-absent source.

            .omnigraphignore/--exclude matches evict on every rebuild;
            .gitignore-driven matches only on a full rebuild (see docstring).
            """
            ignored = _ignored_cache.get(identity)
            if ignored is None:
                target = Path(identity)
                ignored = bool(
                    (is_ignored_always is not None and is_ignored_always(target))
                    or (
                        full_rebuild
                        and is_ignored_full is not None
                        and is_ignored_full(target)
                    )
                )
                _ignored_cache[identity] = ignored
            return ignored
        for node in existing.get("nodes", []):
            source_file = node.get("source_file")
            if not source_file or _is_remote_source(source_file):
                continue
            identity = source_paths.identity(source_file)
            if not source_paths.in_watch_root(source_file):
                continue
            if _get_extractor(Path(source_file)) is None:
                if identity:
                    alive = _alive_cache.get(identity)
                    if alive is None:
                        alive = Path(identity).exists()
                        _alive_cache[identity] = alive
                    ignored = alive and _ignored_now(identity)
                    if ignored:
                        newly_ignored_files.add(identity)
                        newly_ignored_nodes += 1
                    if not alive or ignored:
                        normalized = source_paths.normalize(source_file)
                        if normalized:
                            deleted_paths.add(normalized)
                        node_evicted_source_identities.add(identity)
                        edge_evicted_source_identities.add(identity)
                        hyperedge_evicted_source_identities.add(identity)
                continue
            if identity not in current_sources:
                if identity:
                    alive = _alive_cache.get(identity)
                    if alive is None:
                        alive = Path(identity).exists()
                        _alive_cache[identity] = alive
                    if alive:
                        if _ignored_now(identity):
                            newly_ignored_files.add(identity)
                            newly_ignored_nodes += 1
                        else:
                            excluded_alive_files.add(identity)
                            excluded_alive_nodes += 1
                            continue
                normalized = source_paths.normalize(source_file)
                if normalized:
                    deleted_paths.add(normalized)
                if identity:
                    node_evicted_source_identities.add(identity)
                    edge_evicted_source_identities.add(identity)
                    hyperedge_evicted_source_identities.add(identity)
        if newly_ignored_files:
            print(
                f"[omnigraph watch] pruned {newly_ignored_nodes} node(s) from "
                f"{len(newly_ignored_files)} newly-ignored file(s) "
                "(matched by a live ignore rule while absent from the scan corpus)."
            )
        if excluded_alive_files:
            print(
                f"[omnigraph watch] fail-closed: kept {excluded_alive_nodes} node(s) "
                f"from {len(excluded_alive_files)} file(s) that left the scan corpus "
                "but still exist on disk and match no current ignore rule (filters "
                "changed?). Add them to .omnigraphignore if the exclusion is intentional."
            )

        preserved_nodes = [
            node
            for node in existing.get("nodes", [])
            if node["id"] not in new_ast_ids
            and not (
                _is_ast_tier(node)
                and (
                    (
                        not node.get("source_file")
                        and (full_rebuild or not code_files)
                    )
                    or (
                        full_rebuild
                        and source_paths.is_evicted(node, rebuilt_source_identities)
                    )
                )
            )
            and not source_paths.is_evicted(node, node_evicted_source_identities)
        ]
        all_ids = new_ast_ids | {node["id"] for node in preserved_nodes}

        # As arestas pertencem ao source_file, mas a propriedade tem escopo de nível: o AST
        # pass substitui as arestas AST de uma fonte reextraída, enquanto essa fonte
        # arestas semânticas/LLM - que a passagem AST não pode regenerar - sobrevivem
        # até que uma reextração semântica os substitua. Mesma regra de proveniência
        # a reconciliação de nós acima se aplica via _origin. Eliminação
        preserved_edges = [
            edge
            for edge in existing.get("links", existing.get("edges", []))
            if edge.get("source") in all_ids
            and edge.get("target") in all_ids
            and not source_paths.is_evicted(edge, edge_evicted_source_identities)
            and not (
                _is_ast_tier(edge)
                and source_paths.is_evicted(edge, rebuilt_source_identities)
            )
        ]

        new_hyperedge_ids = {
            edge.get("id") for edge in result.get("hyperedges", []) if edge.get("id")
        }
        preserved_hyperedges = []
        for edge in existing.get("hyperedges", []):
            members = edge.get("nodes", edge.get("members", edge.get("node_ids", [])))
            if edge.get("id") in new_hyperedge_ids or source_paths.is_evicted(
                edge, hyperedge_evicted_source_identities
            ):
                continue
            if isinstance(members, list) and any(member not in all_ids for member in members):
                continue
            preserved_hyperedges.append(edge)

        for item in preserved_nodes + preserved_edges + preserved_hyperedges:
            source_paths.rebase_preserved(item)

        return {
            "nodes": result["nodes"] + preserved_nodes,
            "edges": result["edges"] + preserved_edges,
            "hyperedges": result.get("hyperedges", []) + preserved_hyperedges,
            "input_tokens": 0,
            "output_tokens": 0,
        }, existing_graph_data
    except Exception as exc:
        print(
            "[omnigraph watch] reconcile of existing graph failed "
            f"({exc.__class__.__name__}: {exc}); proceeding with fresh "
            "extraction only.",
            file=sys.stderr,
        )
        return result, existing_graph_data


def _node_community_map(graph_data: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for node in graph_data.get("nodes", []):
        node_id = node.get("id")
        cid = node.get("community")
        if node_id is None or cid is None:
            continue
        try:
            out[str(node_id)] = int(cid)
        except (TypeError, ValueError):
            print(
                f"[omnigraph watch] Skipping node with invalid community id: "
                f"node_id={node_id!r} community={cid!r}",
                file=sys.stderr,
            )
            continue
    return out


def _canonical_graph_for_compare(graph_data: dict) -> dict:
    canonical = dict(graph_data)
    canonical.pop("built_at_commit", None)
    canonical["directed"] = bool(canonical.get("directed", False))
    for key in ("nodes", "links", "edges", "hyperedges"):
        if key in canonical and isinstance(canonical[key], list):
            canonical[key] = sorted(
                canonical[key],
                key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False, default=str),
            )
    return canonical


def _canonical_topology_for_compare(graph_data: dict) -> dict:
    canonical = dict(graph_data)
    canonical.pop("built_at_commit", None)

    nodes = canonical.get("nodes")
    if isinstance(nodes, list):
        norm_nodes = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            n = dict(node)
            n.pop("community", None)
            n.pop("community_name", None)
            n.pop("norm_label", None)
            norm_nodes.append(n)
        canonical["nodes"] = sorted(
            norm_nodes,
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False, default=str),
        )

    for key in ("links", "edges"):
        items = canonical.get(key)
        if not isinstance(items, list):
            continue
        norm_edges = []
        for edge in items:
            if not isinstance(edge, dict):
                continue
            e = dict(edge)
            # to_json escreve _src/_tgt como os endpoints direcionados canônicos e
            # substitui a origem/destino por eles antes da serialização, então o
            # o grafo no disco não tem _src/_tgt. A topologia candidata (fresca de
            # ambos os lados comparáveis: existente recebe pops autônomos (Nenhum), candidato
            # obtém origem/destino sobrescrito de _src/_tgt - mesmo resultado.
            true_src = e.pop("_src", None)
            true_tgt = e.pop("_tgt", None)
            if true_src is not None and true_tgt is not None:
                e["source"] = true_src
                e["target"] = true_tgt
            e.pop("confidence_score", None)
            norm_edges.append(e)
        canonical[key] = sorted(
            norm_edges,
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False, default=str),
        )

    hyperedges = canonical.get("hyperedges")
    if isinstance(hyperedges, list):
        canonical["hyperedges"] = sorted(
            hyperedges,
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False, default=str),
        )

    return canonical


def _topology_from_graph(G) -> dict:
    from networkx.readwrite import json_graph
    try:
        data = json_graph.node_link_data(G, edges="links")
    except TypeError:
        data = json_graph.node_link_data(G)
    data["hyperedges"] = getattr(G, "graph", {}).get("hyperedges", [])
    return data


def _check_shrink(
    force: bool,
    existing_data: dict,
    new_data: dict,
    tmp: "Path | None" = None,
    *,
    had_explicit_deletions: bool = False,
    rebuilt_sources: "set[str] | None" = None,
    failed_sources: "set[str] | None" = None,
) -> bool:
    """Return True (ok to proceed) or False (shrink refused).

    When False, cleans up *tmp* if provided and prints a warning to stderr.

    The shrink-guard exists to catch SILENT shrinkage from failed extraction
    chunks (a half-written semantic pass leaving thousands of nodes
    unaccounted for). When ``had_explicit_deletions`` is True, the caller
    has declared which files were removed (e.g. the post-commit hook saw
    a ``D`` in ``git diff --name-only``) and a smaller graph is the expected
    outcome — skip the guard so legitimate refactors don't require ``--force``.

    ``rebuilt_sources`` (when given) is the set of source files re-extracted this
    run. A net shrink is legitimate — not a failed chunk — when every *lost* node
    belonged to one of those files (a symbol removed from a re-extracted file) or
    carries no source_file. Only an unexplained loss (a node from a file we did
    NOT touch — e.g. a dropped semantic/doc node) refuses the write. This lets a
    plain ``omnigraph update`` after deleting a function refresh the graph without
    ``--force`` (#1116 left stale nodes write-blocked even though build dropped them).
    Files in ``failed_sources`` never account for lost nodes: extraction did not
    complete, so their disappearance is the silent shrink this guard protects.
    """
    if force or not existing_data:
        return True
    if had_explicit_deletions and rebuilt_sources is None:
        return True
    existing_nodes = existing_data.get("nodes", [])
    new_nodes = new_data.get("nodes", [])
    if len(new_nodes) >= len(existing_nodes):
        return True
    if rebuilt_sources is not None:
        from omnigraph.build import _norm_source_file
        new_ids = {n.get("id") for n in new_nodes}
        lost = [n for n in existing_nodes if n.get("id") not in new_ids]

        def _accounted(n: dict) -> bool:
            sf = n.get("source_file")
            if sf and failed_sources and _norm_source_file(sf) in failed_sources:
                return False
            return (not sf
                    or sf in rebuilt_sources
                    or _norm_source_file(sf) in rebuilt_sources)
        if all(_accounted(n) for n in lost):
            return True
    if tmp is not None:
        tmp.unlink(missing_ok=True)
    print(
        f"[omnigraph] WARNING: new graph has {len(new_nodes)} nodes but existing "
        f"graph.json has {len(existing_nodes)}. Refusing to overwrite — you may be "
        f"missing chunk files from a previous session. "
        f"Pass --force to override.",
        file=sys.stderr,
    )
    return False


def _report_for_compare(report_text: str) -> str:
    return re.sub(r"^- Built from commit: `[^`]+`\n?", "", report_text, flags=re.MULTILINE)


def _json_text(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _stabilize_rebuild_cwd(watch_path: Path) -> bool:
    """Ensure relative rebuild paths have a usable CWD before queue/lock setup.

    Detached git hooks can inherit a transient working directory that is deleted
    before the background rebuild starts. In that state Path.cwd(),
    Path('.').resolve(), and relative omnigraph-out mkdirs raise FileNotFoundError
    before the normal rebuild error handling can run. Hooks that know the repo
    root export OMNIGRAPH_REPO_ROOT so the rebuild can recover by chdir'ing there.
    """
    if watch_path.is_absolute():
        return True

    repo_root = os.environ.get("OMNIGRAPH_REPO_ROOT", "").strip()
    if repo_root and Path(repo_root).is_dir():
        try:
            os.chdir(repo_root)
            return True
        except OSError:
            pass

    try:
        Path.cwd()
        return True
    except FileNotFoundError:
        print(
            "[omnigraph watch] Rebuild failed: current working directory "
            "no longer exists and OMNIGRAPH_REPO_ROOT is not set."
        )
        return False


def _rebuild_code(
    watch_path: Path,
    *,
    changed_paths: list[Path] | None = None,
    follow_symlinks: bool = False,
    force: bool = False,
    no_cluster: bool = False,
    acquire_lock: bool = True,
    block_on_lock: bool = False,
) -> bool:
    """Re-run AST extraction + build + optional cluster + report for code files. No LLM needed.

    When ``force`` is True the node-count safety check in ``to_json`` is bypassed
    so the rebuilt graph overwrites graph.json even if it has fewer nodes.
    Use this after refactors that legitimately delete code.

    When ``changed_paths`` is provided, only those files are re-extracted; nodes
    for unchanged files are preserved from the existing graph. Deleted paths
    in ``changed_paths`` (paths that no longer exist on disk) are dropped from
    the preserved set. When ``changed_paths`` is None the full code corpus is
    re-extracted (used by the watcher and post-checkout hook).

    ``acquire_lock`` (default True) takes a non-blocking per-repo flock around
    the rebuild so concurrent post-commit hooks across multiple repos do not
    pile up. Returns False with a log line if the lock is held. Pass
    ``block_on_lock=True`` to wait instead of skip (used by the interactive
    ``omnigraph update`` CLI).

    ``no_cluster`` skips community detection and writes raw merged extraction
    JSON to omnigraph-out/graph.json (mirrors ``extract --no-cluster``).

    Returns True on success, False on error or skipped-due-to-lock.
    """
    if not _stabilize_rebuild_cwd(watch_path):
        return False

    out = watch_path / _OMNIGRAPH_OUT
    if acquire_lock:
        # ganchos incrementais (changed_paths não são None) não devem ser eliminados
        # seu conjunto de alterações quando outra reconstrução já estiver em execução. Fila
        # as reconstruções do corpus ignoram totalmente a fila - elas já cobrem todos
        # arquivo, então não há nada para mesclar.
        if changed_paths is not None and not block_on_lock:
            _queue_pending(out, list(changed_paths))
        with _rebuild_lock(out, blocking=block_on_lock) as got:
            if not got:
                print("[omnigraph watch] Rebuild already in progress for "
                      f"{watch_path.resolve()} - changes queued.")
                return False
            # e mesclar com nosso próprio conjunto de alterações para que uma única reconstrução cubra
            if changed_paths is not None:
                merged = _merge_changed_paths(changed_paths, _drain_pending(out))
            else:
                # A reconstrução completa do corpus substitui qualquer trabalho incremental na fila.
                _drain_pending(out)
                merged = None
            ok = _rebuild_code(
                watch_path,
                changed_paths=merged,
                follow_symlinks=follow_symlinks,
                force=force,
                no_cluster=no_cluster,
                acquire_lock=False,
            )
            # estavam reconstruindo. Faça um loop até _PENDING_DRAIN_MAX_PASSES vezes para que um
            # reconstruir já viu tudo, então pule isso, pois changed_paths é None.
            if merged is not None:
                for _ in range(_PENDING_DRAIN_MAX_PASSES):
                    late = _drain_pending(out)
                    if not late:
                        break
                    ok = _rebuild_code(
                        watch_path,
                        changed_paths=late,
                        follow_symlinks=follow_symlinks,
                        force=force,
                        no_cluster=no_cluster,
                        acquire_lock=False,
                    ) and ok
            return ok

    watch_root = watch_path.resolve()
    project_root = Path.cwd().resolve() if not watch_path.is_absolute() else watch_root
    report_root = _report_root_label(watch_path)
    try:
        from omnigraph.extract import extract, _get_extractor
        from omnigraph.detect import detect
        from omnigraph.build import build_from_json, _is_ast_tier, _norm_source_file as _nsf
        from omnigraph.cluster import cluster, remap_communities_to_previous, score_all
        from omnigraph.analyze import god_nodes, surprising_connections, suggest_questions
        from omnigraph.report import generate
        from omnigraph.export import to_json, to_html
        from omnigraph.security import check_graph_file_size_cap

        # Aplique novamente as exclusões da extração inicial registrada, portanto, uma atualização/observação/
        # a reconstrução do gancho não reinclui silenciosamente caminhos deliberadamente excluídos
        _persisted_excludes = _read_build_excludes(out)
        _gitignore_enabled = _read_build_gitignore(out)
        detected = detect(
            watch_path, follow_symlinks=follow_symlinks,
            extra_excludes=_persisted_excludes or None,
            gitignore=_gitignore_enabled,
        )
        code_files = [Path(f) for f in detected['files']['code']]

        from omnigraph.detect import ignored_predicate
        _ignored_always = ignored_predicate(
            watch_root, extra_excludes=_persisted_excludes or None, gitignore=False,
        )
        _ignored_full = ignored_predicate(
            watch_root, extra_excludes=_persisted_excludes or None, gitignore=True,
        ) if _gitignore_enabled else _ignored_always

        ast_doc_files: list[Path] = []
        for doc_file in detected['files'].get('document', []):
            p = Path(doc_file)
            if _get_extractor(p) is not None:
                code_files.append(p)
                ast_doc_files.append(p)

        existing_graph = out / "graph.json"
        if not code_files and not existing_graph.exists():
            print("[omnigraph watch] No code files found - nothing to rebuild.")
            return False

        # um documento que já traz nós SEMÂNTICOS (LLM) no
        # grafo existente TAMBÉM não deve ser verificado rapidamente pelo AST - caso contrário, cada
        # reconstruir os nós de cabeçalho do mints sobre os nós semânticos preservados
        # e o documento é representado duas vezes (~4x inchaço do grafo versus a atualização da CLI
        # associação ao corpus (evidência de exclusão com falha no fechamento # 1795) e o
        # grafo deve ter permissão para se auto-curar em uma reconstrução completa sem o
        # encolher guarda recusando a gravação menor.
        semantic_doc_files: set[Path] = set()
        if ast_doc_files and existing_graph.exists():
            try:
                check_graph_file_size_cap(existing_graph)
                prior = json.loads(existing_graph.read_text(encoding="utf-8"))
                prior_paths = _StoredSourcePaths(
                    prior,
                    out=out,
                    project_root=project_root,
                    watch_root=watch_root,
                    normalize_source=_nsf,
                )
                semantic_doc_identities: set[str] = set()
                for node in prior.get("nodes", []):
                    if _is_ast_tier(node):
                        continue
                    if node.get("file_type") not in (
                        "document", "concept", "rationale", "paper", "code"
                    ):
                        continue
                    identity = prior_paths.identity(node.get("source_file"))
                    if identity:
                        semantic_doc_identities.add(identity)
                if semantic_doc_identities:
                    semantic_doc_files = {
                        p for p in ast_doc_files
                        if prior_paths.absolute_identity(str(p), project_root)
                        in semantic_doc_identities
                    }
            except Exception:
                semantic_doc_files = set()

        # Caminho incremental: quando o chamador passou uma lista de alterações explícita,
        # extraia apenas arquivos alterados e ainda existentes. Os caminhos excluídos são
        # rastreados separadamente para que seus nós obsoletos possam ser despejados abaixo.
        deleted_paths: set[str] = set()
        deleted_source_identities: set[str] = set()
        def _add_deleted_source(path: Path) -> None:
            deleted_source_identities.add(Path(os.path.abspath(path)).as_posix())
            for root in (project_root, watch_root):
                deleted_paths.add(_nsf(str(path), str(root)) or str(path))

        if changed_paths is not None:
            code_set = {Path(os.path.abspath(p)) for p in code_files}
            # documentos com suporte semântico nunca são verificados rapidamente pelo AST; deles
            # nós semânticos são a única representação. Espelhando # 1865
            # regra de aresta com escopo de camada no nível do nó, eles também NÃO devem
            # insira extract_targets (portanto, identidades reconstruídas/despejadas de nó) em
            # uma reconstrução incremental, ou seus nós semânticos seriam apagados.
            semantic_doc_set = {Path(os.path.abspath(p)) for p in semantic_doc_files}
            wanted: list[Path] = []
            change_root = Path.cwd().resolve()
            for raw in changed_paths:
                candidates = _changed_path_candidates(
                    raw,
                    change_root=change_root,
                    watch_root=watch_root,
                )
                tracked = next((cand for cand in candidates if cand.exists() and cand in code_set), None)
                if tracked is not None:
                    if tracked not in wanted and tracked not in semantic_doc_set:
                        wanted.append(tracked)
                    continue

                existing_in_root = next(
                    (
                        cand for cand in candidates
                        if cand.exists() and _is_relative_to(cand, watch_root)
                    ),
                    None,
                )
                if existing_in_root is not None:
                    continue

                deleted_in_root = next(
                    (cand for cand in candidates if _is_relative_to(cand, watch_root)),
                    None,
                )
                if deleted_in_root is not None:
                    # O arquivo foi excluído ou renomeado dentro da raiz monitorada.
                    # Remova os nós preservados que ainda reivindicam esse caminho de origem.
                    _add_deleted_source(deleted_in_root)
            if not wanted and not deleted_paths:
                print("[omnigraph watch] No tracked code files in change set - skipping rebuild.")
                return True
            extract_targets = wanted
        else:
            # Reconstrução completa: ignore a verificação rápida do AST para documentos com suporte semântico
            extract_targets = [p for p in code_files if p not in semantic_doc_files]

        resolution_context_nodes: list[dict] = []
        resolution_context_edges: list[dict] = []
        if changed_paths is not None and existing_graph.exists():
            try:
                check_graph_file_size_cap(existing_graph)
                ctx_graph = json.loads(existing_graph.read_text(encoding="utf-8"))
                ctx_paths = _StoredSourcePaths(
                    ctx_graph,
                    out=out,
                    project_root=project_root,
                    watch_root=watch_root,
                    normalize_source=_nsf,
                )
                ctx_live = {
                    ctx_paths.absolute_identity(str(p), project_root) for p in code_files
                }
                ctx_live -= {
                    ctx_paths.absolute_identity(str(p), project_root) for p in extract_targets
                }
                ctx_live -= deleted_source_identities
                ctx_live.discard(None)
                for node in ctx_graph.get("nodes", []):
                    if not node.get("id") or not _is_ast_tier(node):
                        continue
                    source_file = node.get("source_file")
                    if not source_file or ctx_paths.identity(source_file) not in ctx_live:
                        continue
                    ctx_node = {
                        "id": node["id"],
                        "label": node.get("label"),
                        "source_file": source_file,
                        "file_type": node.get("file_type"),
                        "type": node.get("type"),
                    }
                    for marker in ("_callable", "_callable_class"):
                        if node.get(marker):
                            ctx_node[marker] = node[marker]
                    resolution_context_nodes.append(ctx_node)
                for edge in ctx_graph.get("links", ctx_graph.get("edges", [])):
                    if edge.get("relation") not in ("contains", "method"):
                        continue
                    if not _is_ast_tier(edge):
                        continue
                    source_file = edge.get("source_file")
                    if not source_file or ctx_paths.identity(source_file) not in ctx_live:
                        continue
                    resolution_context_edges.append({
                        "source": edge.get("source"),
                        "target": edge.get("target"),
                        "relation": edge.get("relation"),
                        "source_file": source_file,
                    })
            except Exception:
                resolution_context_nodes = []
                resolution_context_edges = []

        commit = _git_head(cwd=watch_root)
        result = extract(
            extract_targets,
            cache_root=watch_root,
            resolution_context_nodes=resolution_context_nodes or None,
            resolution_context_edges=resolution_context_edges or None,
        ) if extract_targets else {
            "nodes": [], "edges": [], "hyperedges": [],
            "input_tokens": 0, "output_tokens": 0,
        }
        _rebase_relative_source_files(result, watch_root, project_root)

        _failed_ast_sources = set(result.get("failed_sources") or [])

        def _ast_manifest_files() -> dict[str, list[str]]:
            """detected["files"] minus this run's failed AST sources (#2543).

            Only the STAMPED set shrinks; scan_corpus at the save sites stays
            the raw detect output so #1908 pruning is unaffected.
            """
            if not _failed_ast_sources:
                return detected["files"]
            failed_res = set(_failed_ast_sources)
            for p in _failed_ast_sources:
                try:
                    failed_res.add(str(Path(p).resolve()))
                except (OSError, RuntimeError):
                    pass

            def _failed(f: str) -> bool:
                if f in failed_res:
                    return True
                try:
                    return str(Path(f).resolve()) in failed_res
                except (OSError, RuntimeError):
                    return False

            return {
                ftype: [f for f in flist if not _failed(f)]
                for ftype, flist in detected["files"].items()
            }

        # Preservar nós/arestas semânticos de uma execução completa anterior.
        # A reconstrução somente AST substitui nós por arquivos alterados; todo o resto é mantido.
        # Filtrar por associação de ID de nó na nova saída AST, não por file_type —
        # Nós INFERIDOS/AMBÍGUOS extraídos de arquivos de código também carregam file_type="code"
        # Quando o chamador forneceu changed_paths, também remova os nós preservados cujos
        # source_file corresponde a um caminho que foi alterado (reextraído) ou excluído —
        # caso contrário, os nós antigos desses arquivos sobreviveriam para sempre.
        try:
            result, existing_graph_data = _reconcile_existing_graph(
                existing_graph,
                result,
                out=out,
                project_root=project_root,
                watch_root=watch_root,
                code_files=code_files,
                extract_targets=extract_targets,
                full_rebuild=changed_paths is None,
                deleted_paths=deleted_paths,
                deleted_source_identities=deleted_source_identities,
                is_ignored_always=_ignored_always,
                is_ignored_full=_ignored_full,
            )
        except (RuntimeError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return False

        _relativize_source_files(result, project_root, scope=watch_root)
        # Arquivos de origem reextraídos nesta execução — seus conjuntos de símbolos podem legitimamente
        # encolher (uma função removida), então a proteção contra encolhimento não deve bloquear o
        # escreva quando cada nó perdido pertencer a um deles (ou a um arquivo excluído).
        _rebuilt_root = str(project_root)
        if changed_paths is None:
            rebuilt_sources = {
                _nsf(str(p.relative_to(project_root)), _rebuilt_root)
                for p in code_files if p.is_relative_to(project_root)
            }
        else:
            rebuilt_sources = {(_nsf(str(p), _rebuilt_root) or str(p)) for p in extract_targets}
        rebuilt_sources |= set(deleted_paths)
        failed_sources = {
            _nsf(source, _rebuilt_root) or source
            for source in _failed_ast_sources
        }
        out.mkdir(exist_ok=True)

        if no_cluster:
            from omnigraph.build import dedupe_edges as _dedupe_edges, dedupe_nodes as _dedupe_nodes
            candidate_graph_data = {
                **{k: v for k, v in result.items() if k not in ("edges", "nodes")},
                "nodes": _dedupe_nodes(result.get("nodes", [])),
                "links": _dedupe_edges(result.get("edges", [])),
                "directed": bool((existing_graph_data or {}).get("directed", False)),
            }
            candidate_graph_text = _json_text(candidate_graph_data)
            same_graph = False
            if existing_graph.exists():
                try:
                    check_graph_file_size_cap(existing_graph)
                    existing_payload = json.loads(existing_graph.read_text(encoding="utf-8"))
                except Exception as exc:
                    print(
                        f"error: Cannot read {existing_graph}: {exc}. "
                        "Refusing to overwrite; delete the file and run a "
                        "full rebuild.",
                        file=sys.stderr,
                    )
                    return False
                try:
                    same_graph = (
                        json.dumps(_canonical_graph_for_compare(existing_payload), sort_keys=True, ensure_ascii=False)
                        == json.dumps(_canonical_graph_for_compare(candidate_graph_data), sort_keys=True, ensure_ascii=False)
                    )
                except Exception:
                    same_graph = False
            if not same_graph:
                if not _check_shrink(
                    force, existing_graph_data, candidate_graph_data,
                    had_explicit_deletions=bool(deleted_paths),
                    rebuilt_sources=rebuilt_sources,
                    failed_sources=failed_sources,
                ):
                    return False
                from omnigraph.export import backup_if_protected as _backup
                _backup(out)
                graph_tmp = out / ".graph.tmp.json"
                graph_tmp.write_text(candidate_graph_text, encoding="utf-8")
                graph_tmp.replace(existing_graph)

            # Escreva o caminho fornecido pelo usuário somente depois que o grafo candidato for
            # aceito, portanto, uma redução recusada não pode combinar grafo e marcador incompatíveis.
            (out / ".omnigraph_root").write_text(str(watch_path), encoding="utf-8")

            try:
                from omnigraph.detect import save_manifest
                # detectado["arquivos"] é uma detecção COMPLETA da raiz monitorada, então
                # passe-o também como corpus de verificação: linhas para arquivos que saíram do
                # digitalizam, mas ainda existem no disco (recém-excluídos) são removidos
                # em vez de sobreviver como entradas fantasmas "excluídas".
                save_manifest(
                    _ast_manifest_files(), manifest_path=str(out / "manifest.json"),
                    kind="ast", root=watch_root,
                    scan_corpus={f for _fl in detected["files"].values() for f in _fl},
                    clear_ast=_failed_ast_sources or None,
                )
            except Exception:
                pass

            flag = out / "needs_update"
            if flag.exists():
                flag.unlink()

            if same_graph:
                print("[omnigraph watch] No code-graph changes detected (--no-cluster); outputs left untouched.")
            else:
                print(
                    "[omnigraph watch] Rebuilt (no clustering): "
                    f"{len(candidate_graph_data.get('nodes', []))} nodes, "
                    f"{len(candidate_graph_data.get('links', []))} edges"
                )
                print(f"[omnigraph watch] graph.json updated in {out}")
            return True

        detection = {
            "files": {"code": [str(f) for f in code_files], "document": [], "paper": [], "image": []},
            "total_files": len(code_files),
            "total_words": detected.get("total_words", 0),
        }

        G = build_from_json(result, directed=bool((existing_graph_data or {}).get("directed", False)))
        candidate_topology = _topology_from_graph(G)
        if existing_graph_data:
            try:
                same_topology = (
                    json.dumps(_canonical_topology_for_compare(existing_graph_data), sort_keys=True, ensure_ascii=False)
                    == json.dumps(_canonical_topology_for_compare(candidate_topology), sort_keys=True, ensure_ascii=False)
                )
            except Exception:
                same_topology = False
            if same_topology:
                try:
                    from omnigraph.detect import save_manifest
                    save_manifest(
                        _ast_manifest_files(), manifest_path=str(out / "manifest.json"),
                        kind="ast", root=watch_root,
                        scan_corpus={f for _fl in detected["files"].values() for f in _fl},
                        clear_ast=_failed_ast_sources or None,
                    )
                except Exception:
                    pass
                flag = out / "needs_update"
                if flag.exists():
                    flag.unlink()
                print("[omnigraph watch] No code-graph topology changes detected; outputs left untouched.")
                return True

        communities = cluster(G)
        previous_node_community = _node_community_map(existing_graph_data)
        if previous_node_community:
            communities = remap_communities_to_previous(communities, previous_node_community)
        cohesion = score_all(G, communities)
        gods = god_nodes(G)
        surprises = surprising_connections(G, communities)
        labels_file = out / ".omnigraph_labels.json"
        sig_file = out / (".omnigraph_labels.json" + ".sig")
        try:
            raw = json.loads(labels_file.read_text(encoding="utf-8")) if labels_file.exists() else {}
            labels = {
                int(k): v for k, v in raw.items()
                if int(k) in communities and v != f"Community {int(k)}"
            }
        except Exception:
            raw = {}
            labels = {}
        from omnigraph.cluster import community_member_sigs
        cur_sigs = community_member_sigs(communities)
        saved_sigs: dict[int, str] = {}
        if sig_file.exists():
            try:
                saved_sigs = {
                    int(k): v for k, v in
                    json.loads(sig_file.read_text(encoding="utf-8")).items()
                    if isinstance(v, str)
                }
            except Exception:
                saved_sigs = {}
        if saved_sigs:
            stale = {cid for cid in labels if saved_sigs.get(cid) != cur_sigs.get(cid)}
        else:
            stale = set(labels) if len(raw) != len(communities) else set()
        for cid in stale:
            del labels[cid]
        missing = {cid: members for cid, members in communities.items() if cid not in labels}
        if missing:
            # O nome determinístico do hub (membro de mais alto grau) supera um simples "Comunidade N"
            # espaço reservado para qualquer comunidade sem um rótulo salvo.
            from omnigraph.cluster import label_communities_by_hub
            labels.update(label_communities_by_hub(G, missing))
        if stale:
            print(
                f"[omnigraph watch] community set changed since labeling "
                f"({len(raw)} saved labels, {len(communities)} communities now; "
                f"renamed {len(stale)} community(ies) by their hub). "
                f"Run `omnigraph label` to refresh names with the LLM.",
                file=sys.stderr,
            )
        questions = suggest_questions(G, communities, labels)
        from omnigraph.report import load_learning_for_report as _llfr
        report = generate(G, communities, cohesion, labels, gods, surprises, detection,
                          {"input": 0, "output": 0}, report_root, suggested_questions=questions,
                          built_at_commit=commit, learning=_llfr(out / "graph.json"))
        report_path = out / "GRAPH_REPORT.md"
        labels_json = json.dumps({str(k): v for k, v in sorted(labels.items())}, ensure_ascii=False, indent=2) + "\n"
        graph_tmp = out / ".graph.tmp.json"
        json_written = to_json(G, communities, str(graph_tmp), force=True, built_at_commit=commit, community_labels=labels)
        if not json_written:
            return False
        candidate_graph_data = json.loads(graph_tmp.read_text(encoding="utf-8"))
        same_graph = False
        same_report = False
        if existing_graph.exists():
            try:
                check_graph_file_size_cap(existing_graph)
                existing_payload = json.loads(existing_graph.read_text(encoding="utf-8"))
            except Exception as exc:
                graph_tmp.unlink(missing_ok=True)
                print(
                    f"error: Cannot read {existing_graph}: {exc}. "
                    "Refusing to overwrite; delete the file and run a "
                    "full rebuild.",
                    file=sys.stderr,
                )
                return False
            try:
                same_graph = (
                    json.dumps(_canonical_graph_for_compare(existing_payload), sort_keys=True, ensure_ascii=False)
                    == json.dumps(_canonical_graph_for_compare(candidate_graph_data), sort_keys=True, ensure_ascii=False)
                )
            except Exception:
                same_graph = False
        if report_path.exists():
            old_report = report_path.read_text(encoding="utf-8")
            same_report = _report_for_compare(old_report) == _report_for_compare(report)
        no_change = same_graph and same_report
        if no_change:
            graph_tmp.unlink(missing_ok=True)
            print("[omnigraph watch] No code-graph changes detected; graph.json/GRAPH_REPORT.md left untouched.")
        else:
            if not _check_shrink(
                force, existing_graph_data, candidate_graph_data,
                tmp=graph_tmp,
                had_explicit_deletions=bool(deleted_paths),
                rebuilt_sources=rebuilt_sources,
                failed_sources=failed_sources,
            ):
                return False
            from omnigraph.export import backup_if_protected as _backup
            _backup(out)
            graph_tmp.replace(existing_graph)
            report_path.write_text(report, encoding="utf-8")
            labels_file.write_text(labels_json, encoding="utf-8")
            sig_file.write_text(
                json.dumps({str(k): v for k, v in cur_sigs.items()}), encoding="utf-8")

        (out / ".omnigraph_root").write_text(str(watch_path), encoding="utf-8")

        try:
            from omnigraph.detect import save_manifest
            save_manifest(
                _ast_manifest_files(), manifest_path=str(out / "manifest.json"),
                kind="ast", root=watch_root,
                scan_corpus={f for _fl in detected["files"].values() for f in _fl},
                clear_ast=_failed_ast_sources or None,
            )
        except Exception:
            pass

        # Enrole para que as saídas principais (graph.json + GRAPH_REPORT.md) sempre cheguem.
        html_written = False
        if not no_change:
            html_target = out / "graph.html"
            try:
                to_html(G, communities, str(html_target), community_labels=labels or None)
                html_written = True
            except ValueError as viz_err:
                from omnigraph.exporters.html import _viz_node_limit
                if html_target.exists():
                    html_target.unlink()
                limit = _viz_node_limit()
                if limit <= 0:
                    print(f"[omnigraph watch] Skipped graph.html: {viz_err}")
                else:
                    try:
                        to_html(G, communities, str(html_target),
                                community_labels=labels or None, node_limit=limit)
                        html_written = html_target.exists()
                    except Exception as fallback_err:
                        print(f"[omnigraph watch] Skipped graph.html: {viz_err} "
                              f"(aggregated view also failed: {fallback_err})")

        # Gere novamente o HTML do fluxo de chamada se o usuário gerou um anteriormente -
        # opte por existência para que os usuários que nunca executaram callflow-html não sejam afetados.
        callflow_files = list(out.glob("*-callflow.html"))
        if callflow_files and not no_change:
            try:
                from omnigraph.callflow_html import write_callflow_html
                for cf in callflow_files:
                    write_callflow_html(
                        graph=out / "graph.json",
                        report=out / "GRAPH_REPORT.md",
                        labels=out / ".omnigraph_labels.json",
                        output=cf,
                        verbose=False,
                    )
            except Exception as cf_err:
                print(f"[omnigraph watch] callflow HTML update skipped: {cf_err}")

        flag = out / "needs_update"
        if flag.exists():
            flag.unlink()

        if not no_change:
            print(f"[omnigraph watch] Rebuilt: {G.number_of_nodes()} nodes, "
                  f"{G.number_of_edges()} edges, {len(communities)} communities")
            products = "graph.json" + (", graph.html" if html_written else "") + " and GRAPH_REPORT.md"
            if callflow_files:
                products += f", {len(callflow_files)} callflow HTML"
            print(f"[omnigraph watch] {products} updated in {out}")
        return True

    except Exception as exc:
        print(f"[omnigraph watch] Rebuild failed: {exc}")
        return False


def check_update(watch_path: Path) -> bool:
    """Check for pending semantic update flag and notify the user if set.

    Cron-safe: always returns True so cron jobs do not alarm.
    Non-code file changes (docs, papers, images) require LLM-backed
    re-extraction via `/omnigraph --update` — this function only signals
    that the update is needed.
    """
    flag = Path(watch_path) / _OMNIGRAPH_OUT / "needs_update"
    if flag.exists():
        print(f"[omnigraph check-update] Pending non-code changes in {watch_path}.")
        print("[omnigraph check-update] Run `/omnigraph --update` to apply semantic re-extraction.")
    return True


def _notify_only(watch_path: Path) -> None:
    """Write a flag file and print a notification (fallback for non-code-only corpora)."""
    flag = watch_path / _OMNIGRAPH_OUT / "needs_update"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("1", encoding="utf-8")
    print(f"\n[omnigraph watch] New or changed files detected in {watch_path}")
    print("[omnigraph watch] Non-code files changed - semantic re-extraction requires LLM.")
    print("[omnigraph watch] Run `/omnigraph --update` in Claude Code to update the graph.")
    print(f"[omnigraph watch] Flag written to {flag}")


def _has_non_code(changed_paths: list[Path]) -> bool:
    return any(p.suffix.lower() not in _CODE_EXTENSIONS for p in changed_paths)


def _batch_triggers_rebuild(batch: list[Path]) -> bool:
    """True when a debounced watch batch needs an immediate rebuild.

    Code changes always rebuild (AST extraction needs no LLM). Deletions of
    ANY watched file also rebuild: eviction needs no LLM either — the full
    corpus reconcile drops nodes whose source is gone from disk. Without
    this, a doc-only deletion batch would sit behind the needs_update flag
    until the next code event or a manual `omnigraph update` (#2580).
    """
    has_code = any(p.suffix.lower() in _CODE_EXTENSIONS for p in batch)
    has_deletion = any(not p.exists() for p in batch)
    return has_code or has_deletion


def _batch_needs_llm_flag(batch: list[Path]) -> bool:
    """True when the batch contains a non-code file that still exists on disk.

    Only surviving non-code files need the needs_update flag (LLM-backed
    re-extraction); deleted ones are already handled by the rebuild's
    reconcile sweep, so a pure-deletion batch must not leave a stale flag.
    """
    return _has_non_code([p for p in batch if p.exists()])


def watch(watch_path: Path, debounce: float = 3.0) -> None:
    """
    Watch watch_path for new or modified files and auto-update the graph.

    For code-only changes: re-runs AST extraction + rebuild immediately (no LLM).
    For doc/paper/image changes: writes a needs_update flag and notifies the user
    to run /omnigraph --update (LLM extraction required).

    debounce: seconds to wait after the last change before triggering (avoids
    running on every keystroke when many files are saved at once).
    """
    try:
        from watchdog.observers import Observer
        from watchdog.observers.polling import PollingObserver
        from watchdog.events import FileSystemEventHandler
    except ImportError as e:
        raise ImportError("watchdog not installed. Run: pip install watchdog") from e

    last_trigger: float = 0.0
    pending: bool = False
    changed: set[Path] = set()

    # Carregue os padrões .omnigraphignore UMA VEZ na inicialização para que o manipulador não
    # analise novamente o arquivo em cada evento do sistema de arquivos. O manipulador do Watchdog é executado
    # o thread do observador e é invocado para cada evento que o sistema operacional entrega
    # sem este curto-circuito, um volume ocupado pode saturar um núcleo da CPU
    # descartando eventos, uma extensão por vez. (gh-928)
    watch_root_for_ignore = watch_path.resolve()
    ignore_patterns = _load_omnigraphignore(
        watch_root_for_ignore,
        gitignore=_read_build_gitignore(watch_path / _OMNIGRAPH_OUT),
    )

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            nonlocal last_trigger, pending
            if event.is_directory:
                return
            path = Path(os.fsdecode(event.src_path))
            # Verifique .omnigraphignore ANTES dos filtros de extensão/dotfile/out para
            # o curto-circuito mais barato para usuários com padrões amplos de ignorância
            # relative_to guarda, então um evento perdido com link simbólico não será gerado.
            if ignore_patterns and _is_ignored(path, watch_root_for_ignore, ignore_patterns):
                return
            if path.suffix.lower() not in _WATCHED_EXTENSIONS:
                return
            try:
                filter_parts = path.relative_to(watch_root_for_ignore).parts
            except ValueError:
                filter_parts = path.parts
            if any(part.startswith(".") for part in filter_parts):
                return
            if _OMNIGRAPH_OUT in filter_parts:
                return
            last_trigger = time.monotonic()
            pending = True
            changed.add(path)

    handler = Handler()
    # Use o polling observer no macOS – FSEvents pode perder salvamentos rápidos em alguns editores
    observer = PollingObserver() if sys.platform == "darwin" else Observer()
    observer.schedule(handler, str(watch_path), recursive=True)
    observer.start()

    print(f"[omnigraph watch] Watching {watch_path.resolve()} - press Ctrl+C to stop")
    print(f"[omnigraph watch] Code changes rebuild graph automatically. "
          f"Doc/image changes require /omnigraph --update.")
    print(f"[omnigraph watch] Debounce: {debounce}s")

    try:
        while True:
            time.sleep(0.5)
            if pending and (time.monotonic() - last_trigger) >= debounce:
                pending = False
                batch = list(changed)
                changed.clear()
                print(f"\n[omnigraph watch] {len(batch)} file(s) changed")
                if _batch_triggers_rebuild(batch):
                    _rebuild_code(watch_path)
                if _batch_needs_llm_flag(batch):
                    _notify_only(watch_path)
    except KeyboardInterrupt:
        print("\n[omnigraph watch] Stopped.")
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Watch a folder and auto-update the omnigraph graph")
    parser.add_argument("path", nargs="?", default=".", help="Folder to watch (default: .)")
    parser.add_argument("--debounce", type=float, default=3.0,
                        help="Seconds to wait after last change before updating (default: 3)")
    args = parser.parse_args()
    watch(Path(args.path), debounce=args.debounce)
