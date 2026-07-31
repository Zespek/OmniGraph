# integração com git hook - instalar/desinstalar ganchos pós-commit e pós-checkout omnigraph
from __future__ import annotations
import os
import re
import sys
from pathlib import Path

_HOOK_MARKER = "# omnigraph-hook-start"
_HOOK_MARKER_END = "# omnigraph-hook-end"
_CHECKOUT_MARKER = "# omnigraph-checkout-hook-start"
_CHECKOUT_MARKER_END = "# omnigraph-checkout-hook-end"

# __PINNED_PYTHON__ é substituído no momento da instalação pelo caminho absoluto do
# Intérprete Python que executou `omnigraph hook install`.  Para ferramenta UV e pipx
# instala o intérprete dentro de um ambiente isolado, então o inicializador em
# PATH é o único ponto de entrada - e os clientes git da GUI/executores de CI geralmente têm um
# PATH mínimo que omite ~/.local/bin.  Fixando sys.executable no momento da instalação
# faz o gancho funcionar independentemente do PATH no momento do gatilho do git.
_PYTHON_DETECT = """\
# Detect the correct Python interpreter (handles uv tool, pipx, venv, system installs).
# _PINNED was recorded at hook-install time; tried first so the hook works even
# when the omnigraph launcher is not on PATH (common in GUI clients and CI).
#
# Probes check availability with importlib.util.find_spec instead of importing
# the package: a probe that imports omnigraph wholesale executes the full package
# import (10s+ cold on machines with AV-scanned or large site-packages) and used
# to run up to FOUR times synchronously, stalling every commit before the
# detached launch even started. find_spec locates the package without executing
# it, so each probe costs interpreter startup only. The detached rebuild still
# fails loudly in the log if the package is broken under that interpreter.
_GFY_PROBE="import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('omnigraph') else 1)"
OMNIGRAPH_PYTHON=""
_PINNED='__PINNED_PYTHON__'
if [ -n "$_PINNED" ] && [ -x "$_PINNED" ] && "$_PINNED" -c "$_GFY_PROBE" 2>/dev/null; then
    OMNIGRAPH_PYTHON="$_PINNED"
fi
# Second probe: read omnigraph-out/.omnigraph_python (written by the skill and
# CLI; survives uv-tool reinstalls and is the same source the README documents).
if [ -z "$OMNIGRAPH_PYTHON" ]; then
    _GFY_PYTHON_FILE="omnigraph-out/.omnigraph_python"
    if [ -f "$_GFY_PYTHON_FILE" ]; then
        _FROM_FILE=$(cat "$_GFY_PYTHON_FILE" 2>/dev/null | tr -d '[:space:]')
        case "$_FROM_FILE" in
            *[!a-zA-Z0-9/_.@:\\\\-]*) _FROM_FILE="" ;;  # allowlist (covers Windows paths)
        esac
        if [ -n "$_FROM_FILE" ] && [ -x "$_FROM_FILE" ] && "$_FROM_FILE" -c "$_GFY_PROBE" 2>/dev/null; then
            OMNIGRAPH_PYTHON="$_FROM_FILE"
        fi
    fi
fi
# Third probe: resolve via the omnigraph launcher on PATH.
if [ -z "$OMNIGRAPH_PYTHON" ]; then
    OMNIGRAPH_BIN=$(command -v omnigraph 2>/dev/null)
    if [ -n "$OMNIGRAPH_BIN" ]; then
        # Windows pip layout: Scripts/omnigraph(.exe) sits beside ..\\python.exe
        # (or .\\python.exe inside a venv's Scripts dir). NOTE: command -v may
        # return the launcher path WITHOUT the .exe suffix, so this cannot key
        # on the extension.
        _GFY_BINDIR=$(dirname "$OMNIGRAPH_BIN")
        if [ -x "$_GFY_BINDIR/../python.exe" ] && "$_GFY_BINDIR/../python.exe" -c "$_GFY_PROBE" 2>/dev/null; then
            OMNIGRAPH_PYTHON="$_GFY_BINDIR/../python.exe"
        elif [ -x "$_GFY_BINDIR/python.exe" ] && "$_GFY_BINDIR/python.exe" -c "$_GFY_PROBE" 2>/dev/null; then
            OMNIGRAPH_PYTHON="$_GFY_BINDIR/python.exe"
        fi
    fi
    if [ -z "$OMNIGRAPH_PYTHON" ] && [ -n "$OMNIGRAPH_BIN" ]; then
        # POSIX launcher: parse the shebang. head -c + tr strip NUL bytes first —
        # when the launcher is a Windows binary reached without its .exe suffix,
        # a raw `head -1` reads binary into the command substitution and the
        # shell warns about ignored null bytes on every commit.
        case "$OMNIGRAPH_BIN" in
            *.exe) _SHEBANG="" ;;
            *)     _SHEBANG=$(head -c 256 "$OMNIGRAPH_BIN" 2>/dev/null | tr -d '\\000' | head -n 1 | sed 's/^#![[:space:]]*//') ;;
        esac
        case "$_SHEBANG" in
            */env\\ *) OMNIGRAPH_PYTHON="${_SHEBANG#*/env }" ;;
            *)         OMNIGRAPH_PYTHON="$_SHEBANG" ;;
        esac
        # Allowlist: only keep characters valid in a filesystem path to prevent
        # injection if the shebang contains shell metacharacters.
        case "$OMNIGRAPH_PYTHON" in
            *[!a-zA-Z0-9/_.@:\\\\-]*) OMNIGRAPH_PYTHON="" ;;
        esac
        if [ -n "$OMNIGRAPH_PYTHON" ] && ! "$OMNIGRAPH_PYTHON" -c "$_GFY_PROBE" 2>/dev/null; then
            OMNIGRAPH_PYTHON=""
        fi
    fi
fi
# Last resort: try python3 / python (works for system/venv installs on PATH).
if [ -z "$OMNIGRAPH_PYTHON" ]; then
    if command -v python3 >/dev/null 2>&1 && python3 -c "$_GFY_PROBE" 2>/dev/null; then
        OMNIGRAPH_PYTHON="python3"
    elif command -v python >/dev/null 2>&1 && python -c "$_GFY_PROBE" 2>/dev/null; then
        OMNIGRAPH_PYTHON="python"
    else
        echo "[omnigraph hook] could not locate a Python with omnigraph installed. Add the omnigraph bin dir to PATH or re-run 'omnigraph hook install' from the env where omnigraph lives." >&2
        exit 0
    fi
fi
"""

# O Python que a reconstrução executa, compartilhado por ambos os ganchos. Incorporado literalmente em
# o iniciador abaixo e executado novamente no filho desanexado. Não deve conter o
# caracteres de aspas duplas, $, crase ou barra invertida: é transportado dentro de um
# shell double-quoted `-c "..."` argument (see _detached_launch).
_REBUILD_BODY_COMMIT = """\
import os, signal, sys, threading
from pathlib import Path

changed_raw = os.environ.get('OMNIGRAPH_CHANGED', '')
changed = [Path(f.strip()) for f in changed_raw.strip().splitlines() if f.strip()]

if not changed:
    sys.exit(0)

print(f'[omnigraph hook] {len(changed)} file(s) changed - rebuilding graph...')

try:
    from omnigraph.watch import _rebuild_code, _apply_resource_limits
    _apply_resource_limits()
    _timeout = int(os.environ.get('OMNIGRAPH_REBUILD_TIMEOUT', '600'))
    if _timeout > 0:
        if hasattr(signal, 'SIGALRM'):
            signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError(f'omnigraph rebuild exceeded {_timeout}s')))
            signal.alarm(_timeout)
        else:
            def _bail():
                print(f'[omnigraph hook] omnigraph rebuild exceeded {_timeout}s', flush=True)
                os._exit(1)
            _watchdog = threading.Timer(_timeout, _bail)
            _watchdog.daemon = True
            _watchdog.start()
    _force = os.environ.get('OMNIGRAPH_FORCE', '').lower() in ('1', 'true', 'yes')
    _root = Path('.')
    _out = os.environ.get('OMNIGRAPH_OUT', 'omnigraph-out')
    _saved = Path(_out) / '.omnigraph_root'
    if _saved.exists():
        _txt = _saved.read_text(encoding='utf-8').strip()
        if _txt:
            _root = Path(_txt)
    _rebuild_code(_root, changed_paths=changed, force=_force)
    # Refresh the work-memory lessons doc when saved Q&A outcomes exist
    # (best-effort; never fails the hook).
    try:
        _md = (_root / _out) / 'memory'
        if _md.is_dir() and any(_md.glob('*.md')):
            from omnigraph.reflect import reflect as _reflect
            _gj = (_root / _out) / 'graph.json'
            _reflect(memory_dir=_md, out_path=(_root / _out) / 'reflections' / 'LESSONS.md',
                     graph_path=_gj if _gj.exists() else None)
    except Exception:
        pass
except TimeoutError as exc:
    print(f'[omnigraph hook] {exc}')
    sys.exit(1)
except Exception as exc:
    print(f'[omnigraph hook] Rebuild failed: {exc}')
    sys.exit(1)
"""

_REBUILD_BODY_CHECKOUT = """\
from omnigraph.watch import _rebuild_code, _apply_resource_limits
from pathlib import Path
import os, signal, sys, threading
try:
    _apply_resource_limits()
    _timeout = int(os.environ.get('OMNIGRAPH_REBUILD_TIMEOUT', '600'))
    if _timeout > 0:
        if hasattr(signal, 'SIGALRM'):
            signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError(f'omnigraph rebuild exceeded {_timeout}s')))
            signal.alarm(_timeout)
        else:
            def _bail():
                print(f'[omnigraph] omnigraph rebuild exceeded {_timeout}s', flush=True)
                os._exit(1)
            _watchdog = threading.Timer(_timeout, _bail)
            _watchdog.daemon = True
            _watchdog.start()
    _force = os.environ.get('OMNIGRAPH_FORCE', '').lower() in ('1', 'true', 'yes')
    # post-checkout: branch switch can touch arbitrary files; full rebuild path
    # (no changed_paths) is correct here. The flock inside _rebuild_code still
    # prevents pile-ups when commit + checkout fire back-to-back.
    _root = Path('.')
    _out = os.environ.get('OMNIGRAPH_OUT', 'omnigraph-out')
    _saved = Path(_out) / '.omnigraph_root'
    if _saved.exists():
        _txt = _saved.read_text(encoding='utf-8').strip()
        if _txt:
            _root = Path(_txt)
    _rebuild_code(_root, force=_force)
    # Refresh the work-memory lessons doc when saved Q&A outcomes exist
    # (best-effort; never fails the hook).
    try:
        _md = (_root / _out) / 'memory'
        if _md.is_dir() and any(_md.glob('*.md')):
            from omnigraph.reflect import reflect as _reflect
            _gj = (_root / _out) / 'graph.json'
            _reflect(memory_dir=_md, out_path=(_root / _out) / 'reflections' / 'LESSONS.md',
                     graph_path=_gj if _gj.exists() else None)
    except Exception:
        pass
except TimeoutError as exc:
    print(f'[omnigraph] {exc}')
    sys.exit(1)
except Exception as exc:
    print(f'[omnigraph] Rebuild failed: {exc}')
    sys.exit(1)
"""

# Calço de lançamento independente de plataforma cruzada. Os ganchos usados ​​para fundo
# reconstruir com `nohup "$OMNIGRAPH_PYTHON" -c "..." &`, mas Git para Windows' incluído
# O shell MSYS não envia nohup (nem setsid), então essa linha morreu com
# 'nohup: comando não encontrado' e a reconstrução silenciosamente nunca foi executada - git commit/pull
# ainda retornou 0, então o grafo ficou obsoleto e sem sinal. omnigraph já
# requer Python, então deixamos o Python fazer a separação: um pequeno processo externo gera
# a reconstrução real é totalmente desvinculada e retorna imediatamente, para que o gancho nunca
# blocos. POSIX usa start_new_session (o equivalente setsid); Windows usa
# CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, breaking away from any job object
# quando permitido. Esta carga útil é transportada dentro de um argumento -c entre aspas duplas do shell,
# portanto, ele usa deliberadamente apenas strings Python entre aspas simples (sem ", $, ` ou \\).
_LAUNCHER_TEMPLATE = """\
import os, subprocess, sys
_src = '''
__REBUILD_BODY__
'''
_log = os.environ.get('OMNIGRAPH_REBUILD_LOG') or os.path.join(os.path.expanduser('~'), '.cache', 'omnigraph-rebuild.log')
try:
    os.makedirs(os.path.dirname(_log), exist_ok=True)
    _out = open(_log, 'a', buffering=1, encoding='utf-8', errors='replace')
except OSError:
    _out = subprocess.DEVNULL
_kw = dict(stdout=_out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=os.getcwd(), close_fds=True)
_cmd = [sys.executable, '-c', _src]
if os.name == 'nt':
    _flags = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(_cmd, creationflags=_flags | 0x01000000, **_kw)  # + CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        subprocess.Popen(_cmd, creationflags=_flags, **_kw)
else:
    subprocess.Popen(_cmd, start_new_session=True, **_kw)
"""


def _detached_launch(rebuild_body: str) -> str:
    """Return a POSIX-sh line that runs ``rebuild_body`` as a detached background
    Python process via ``$OMNIGRAPH_PYTHON``.

    Replaces the old ``nohup ... &`` form, which failed on Git for Windows'
    shell (no nohup/setsid) and let the rebuild silently never run (#1161).
    The launcher writes the child's output to ``$OMNIGRAPH_REBUILD_LOG`` and
    returns the instant the child is spawned, so the git hook never blocks.
    """
    launcher = _LAUNCHER_TEMPLATE.replace("__REBUILD_BODY__", rebuild_body)
    return '"$OMNIGRAPH_PYTHON" -c "' + launcher + '"\n'


# Ignore a reconstrução dentro de uma árvore de trabalho vinculada (git worktree add), compartilhada por ambos
# ganchos. Com core.hooksPath compartilhado entre árvores de trabalho, um commit em qualquer árvore de trabalho
# dispara esses ganchos; o canônico omnigraph-out/ pertence ao checkout primário,
# então reconstruir a partir de uma árvore de trabalho é um desperdício, escreve um grafo desonesto somente delta no
# o usuário nunca solicitou e corre o deploy/CI `git clean` contra o desanexado
# reconstruir ("falha ao remover omnigraph-out/: diretório não vazio").
# Uma árvore de trabalho vinculada possui git-dir! = git-common-dir. Ambos são resolvidos para absoluto
# via `cd ... && pwd` antes de comparar: o GIT_DIR / --git-dir exportado do git pode ser
# absoluto enquanto --git-common-dir é o relativo ".git", e uma comparação bruta seria
# falso positivo no checkout PRIMARY e ignorá-lo erroneamente.
_WORKTREE_GUARD = """\
_GFY_GITDIR=$(cd "$(git rev-parse --git-dir 2>/dev/null)" 2>/dev/null && pwd)
_GFY_COMMONDIR=$(cd "$(git rev-parse --git-common-dir 2>/dev/null)" 2>/dev/null && pwd)
if [ -n "$_GFY_COMMONDIR" ] && [ "$_GFY_GITDIR" != "$_GFY_COMMONDIR" ]; then
    exit 0
fi
"""


_HOOK_SCRIPT = """\
# omnigraph-hook-start
# Auto-rebuilds the knowledge graph after each commit (code files only, no LLM needed).
# Installed by: omnigraph hook install

# Deterministic clustering: networkx louvain iterates string-keyed sets whose
# order is randomized per-process by PYTHONHASHSEED, so community assignments
# churn run-to-run. Pinning it makes omnigraph-out reproducible.
export PYTHONHASHSEED=0

# Git for Windows/MSYS hooks can inherit fragile pipe handles from GUI clients
# and agent shells. Keep hook-triggered rebuilds sequential by default there;
# explicit OMNIGRAPH_MAX_WORKERS still wins for users who want parallelism.
if [ -n "${WINDIR:-}" ] || [ -n "${MSYSTEM:-}" ]; then
    export OMNIGRAPH_MAX_WORKERS="${OMNIGRAPH_MAX_WORKERS:-1}"
fi

# Skip during rebase/merge/cherry-pick to avoid blocking --continue with unstaged changes
# git exports GIT_DIR to hooks; the rev-parse fallback only runs when invoked by
# hand (each git exec costs 1s+ on AV-scanned Windows machines).
GIT_DIR=${GIT_DIR:-$(git rev-parse --git-dir 2>/dev/null)}
[ -d "$GIT_DIR/rebase-merge" ] && exit 0
[ -d "$GIT_DIR/rebase-apply" ] && exit 0
[ -f "$GIT_DIR/MERGE_HEAD" ] && exit 0
[ -f "$GIT_DIR/CHERRY_PICK_HEAD" ] && exit 0

[ "${OMNIGRAPH_SKIP_HOOK:-0}" = "1" ] && exit 0

""" + _WORKTREE_GUARD + """
CHANGED=$(git diff --name-only HEAD~1 HEAD 2>/dev/null || git diff --name-only HEAD 2>/dev/null)
if [ -z "$CHANGED" ]; then
    exit 0
fi

# Skip when only omnigraph-out/ artifacts changed (avoids rebuild loop when graph outputs are tracked in git)
_NON_GRAPH=$(echo "$CHANGED" | grep -v '^omnigraph-out/' || true)
if [ -z "$_NON_GRAPH" ]; then
    exit 0
fi

""" + _PYTHON_DETECT + """
export OMNIGRAPH_CHANGED="$CHANGED"

# Run the rebuild detached so git commit returns immediately. Full-repo rebuilds
# can take hours; blocking the post-commit hook stalls the shell. The Python
# launcher below detaches the child cross-platform, so it works on Git for
# Windows' shell too (which lacks the coreutils backgrounding tools) (#1161).
_OMNIGRAPH_LOG="${HOME}/.cache/omnigraph-rebuild.log"
mkdir -p "$(dirname "$_OMNIGRAPH_LOG")"
export OMNIGRAPH_REBUILD_LOG="$_OMNIGRAPH_LOG"
echo "[omnigraph hook] launching background rebuild (log: $_OMNIGRAPH_LOG)"
""" + _detached_launch(_REBUILD_BODY_COMMIT) + """# omnigraph-hook-end
"""


_CHECKOUT_SCRIPT = """\
# omnigraph-checkout-hook-start
# Auto-rebuilds the knowledge graph (code only) when switching branches.
# Installed by: omnigraph hook install

# Deterministic clustering: networkx louvain iterates string-keyed sets whose
# order is randomized per-process by PYTHONHASHSEED, so community assignments
# churn run-to-run. Pinning it makes omnigraph-out reproducible.
export PYTHONHASHSEED=0

# Git for Windows/MSYS hooks can inherit fragile pipe handles from GUI clients
# and agent shells. Keep hook-triggered rebuilds sequential by default there;
# explicit OMNIGRAPH_MAX_WORKERS still wins for users who want parallelism.
if [ -n "${WINDIR:-}" ] || [ -n "${MSYSTEM:-}" ]; then
    export OMNIGRAPH_MAX_WORKERS="${OMNIGRAPH_MAX_WORKERS:-1}"
fi

PREV_HEAD=$1
NEW_HEAD=$2
BRANCH_SWITCH=$3

# Only run on branch switches, not file checkouts
if [ "$BRANCH_SWITCH" != "1" ]; then
    exit 0
fi

# Only run if omnigraph-out/ exists (graph has been built before)
if [ ! -d "omnigraph-out" ]; then
    exit 0
fi

# Skip during rebase/merge/cherry-pick
# git exports GIT_DIR to hooks; the rev-parse fallback only runs when invoked by
# hand (each git exec costs 1s+ on AV-scanned Windows machines).
GIT_DIR=${GIT_DIR:-$(git rev-parse --git-dir 2>/dev/null)}
[ -d "$GIT_DIR/rebase-merge" ] && exit 0
[ -d "$GIT_DIR/rebase-apply" ] && exit 0
[ -f "$GIT_DIR/MERGE_HEAD" ] && exit 0
[ -f "$GIT_DIR/CHERRY_PICK_HEAD" ] && exit 0

# Honor the same opt-out as post-commit: without this, OMNIGRAPH_SKIP_HOOK=1
# suppressed commit-triggered rebuilds but not branch-switch ones (#1809).
[ "${OMNIGRAPH_SKIP_HOOK:-0}" = "1" ] && exit 0

""" + _WORKTREE_GUARD + _PYTHON_DETECT + """
_OMNIGRAPH_LOG="${HOME}/.cache/omnigraph-rebuild.log"
mkdir -p "$(dirname "$_OMNIGRAPH_LOG")"
export OMNIGRAPH_REBUILD_LOG="$_OMNIGRAPH_LOG"
echo "[omnigraph] Branch switched - launching background rebuild (log: $_OMNIGRAPH_LOG)"
""" + _detached_launch(_REBUILD_BODY_CHECKOUT) + """# omnigraph-checkout-hook-end
"""


def _git_root(path: Path) -> Path | None:
    """Walk up to find .git directory."""
    current = path.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _reject_windows_path(value: str, source: str) -> None:
    """Raise if a hooks path looks like a Windows absolute path (#1385).

    On POSIX/WSL ``Path("C:\\Users\\...").is_absolute()`` is False, so an absolute
    Windows hooks path gets joined under the repo root and mkdir'd as a literal
    junk directory (backslashes and all), while install reports success and the
    real ``.git/hooks`` gets nothing. Fail loudly instead so the user can fix it.
    """
    if os.name == "nt":
        return
    if _WINDOWS_DRIVE_RE.match(value) or "\\" in value:
        raise RuntimeError(
            f"git hooks path from {source} looks like a Windows path: {value!r}. "
            f"On WSL/POSIX this can't resolve to a real directory. Unset it with "
            f"`git config --local --unset core.hooksPath`, or set a POSIX path."
        )


def _hooks_dir(root: Path) -> Path:
    """Return the git hooks directory, respecting core.hooksPath if set (e.g. Husky).

    Asks git itself via ``rev-parse --git-path hooks`` rather than parsing
    ``.git/config`` with configparser: git legally allows duplicate keys and
    sections (VS Code writes such configs), which a strict configparser rejects
    with DuplicateOptionError/DuplicateSectionError, so every hook command
    printed a spurious "could not read core.hooksPath" warning (#1907). git
    resolves core.hooksPath, includeIf, and linked worktrees (where .git is a
    file, not a directory) correctly in one place. Genuinely corrupt configs
    are still surfaced: git itself fails on them, and its stderr is printed.
    """
    # NOTA: NÃO passe --path-format=absolute — adicionado no git 2.31; idiota mais velho
    # ecoa de volta como um argumento literal, contaminando stdout e causando um
    # diretório fantasma a ser criado. git -C <root> já retorna um
    # caminho absoluto para casos de worktree/external-gitdir e um caminho relativo a
    # <root> para repositórios normais — a ancoragem na raiz cobre ambos.
    import subprocess as _sp
    try:
        res = _sp.run(
            ["git", "-C", str(root), "rev-parse", "--git-path", "hooks"],
            capture_output=True, text=True,
        )
        if res.returncode != 0:
            # git falhar aqui é um sinal real (.git/config corrompido, adulteração,
            # permissão muda por outra ferramenta). Superfície do próprio stderr do git
            # do que cair silenciosamente no diretório de ganchos padrão.
            err = (res.stderr or "").strip()
            print(
                f"[omnigraph hooks] git could not resolve the hooks path for "
                f"{root}: {err or f'git exited with code {res.returncode}'}",
                file=sys.stderr,
            )
        else:
            raw = res.stdout.strip()
            # Um caminho de ganchos válido nunca pode conter novas linhas ou NUL. A presença deles
            # significa que o git repetiu um sinalizador não reconhecido (comportamento antigo do git).
            if raw and not any(c in raw for c in ("\n", "\r", "\x00")):
                _reject_windows_path(raw, "git rev-parse --git-path hooks")
                d = (root / raw).resolve()
                d.mkdir(parents=True, exist_ok=True)
                return d
    except (OSError, FileNotFoundError):
        pass
    d = root / ".git" / "hooks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _install_hook(hooks_dir: Path, name: str, script: str, marker: str) -> str:
    """Install a single git hook, appending if an existing hook is present."""
    hook_path = hooks_dir / name
    if hook_path.exists():
        content = hook_path.read_text(encoding="utf-8")
        if marker in content:
            return f"already installed at {hook_path}"
        hook_path.write_text(content.rstrip() + "\n\n" + script, encoding="utf-8", newline="\n")
        return f"appended to existing {name} hook at {hook_path}"
    hook_path.write_text("#!/bin/sh\n" + script, encoding="utf-8", newline="\n")
    hook_path.chmod(0o755)
    return f"installed at {hook_path}"


def _uninstall_hook(hooks_dir: Path, name: str, marker: str, marker_end: str) -> str:
    """Remove omnigraph section from a git hook using start/end markers."""
    hook_path = hooks_dir / name
    if not hook_path.exists():
        return f"no {name} hook found - nothing to remove."
    content = hook_path.read_text(encoding="utf-8")
    if marker not in content:
        return f"omnigraph hook not found in {name} - nothing to remove."
    new_content = re.sub(
        rf"{re.escape(marker)}.*?{re.escape(marker_end)}\n?",
        "",
        content,
        flags=re.DOTALL,
    ).strip()
    if not new_content or new_content in ("#!/bin/bash", "#!/bin/sh"):
        hook_path.unlink()
        return f"removed {name} hook at {hook_path}"
    hook_path.write_text(new_content + "\n", encoding="utf-8", newline="\n")
    return f"omnigraph removed from {name} at {hook_path} (other hook content preserved)"


def _pinned_python() -> str:
    """Return sys.executable if its path is shell-safe, else an empty string.

    Applies the same allowlist used in _PYTHON_DETECT: rejects any character
    that is not a valid plain filesystem path character, preventing $(...),
    backtick, double-quote, semicolon, etc. from being injected into generated
    shell scripts or the merge-driver command line. The allowlist includes ':'
    and '\\' so Windows paths (C:\\...) are accepted, and a plain space so
    Windows profile paths (C:\\Users\\First Last\\...) are too — a space cannot
    start a substitution or a new command, and every consumer quotes the value:
    the hook scripts embed it as '$_PINNED' (single-quoted, then referenced as
    "$_PINNED") and _register_merge_driver double-quotes it (#2166). Before that
    a space rejected the whole path, so hooks installed under any Windows user
    whose profile name contains a space silently pinned nothing. An empty return
    means callers must fall back to the `omnigraph` launcher on PATH — safe
    degradation.
    """
    if re.search(r"[^a-zA-Z0-9/_.@: \\-]", sys.executable):
        return ""
    return sys.executable


def _merge_attr_line() -> str:
    """The .gitattributes line assigning the omnigraph merge driver to graph.json.

    The graph lives under the configured output directory (omnigraph.paths,
    OMNIGRAPH_OUT env override). gitattributes patterns are repo-relative, so an
    absolute output-dir override cannot be expressed there — fall back to the
    default name in that case.
    """
    from omnigraph.paths import OMNIGRAPH_OUT
    out = OMNIGRAPH_OUT
    if not out or Path(out).is_absolute() or "\\" in out:
        out = "omnigraph-out"
    return f"{out.rstrip('/')}/graph.json merge=omnigraph"


def _has_merge_attr(content: str) -> bool:
    """True if a (non-comment) `<...>graph.json ... merge=omnigraph` line exists."""
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if fields and fields[0].endswith("graph.json") and "merge=omnigraph" in fields[1:]:
            return True
    return False


def _register_merge_driver(root: Path) -> str:
    """Register the graph.json union merge driver in git config + .gitattributes (#1902).

    README and CHANGELOG 0.7.0 document `omnigraph merge-driver` as being set up
    by `hook install`, but install never actually registered it. Writes go
    through `git config` (never hand-edit .git/config — in a linked worktree the
    effective config is not at root/.git/config). The interpreter is pinned the
    same way the hook scripts pin it, so the driver works even when the omnigraph
    launcher is not on PATH at merge time.
    """
    import subprocess as _sp
    pinned = _pinned_python()
    if pinned:
        # Double-quoted: the allowlist in _pinned_python() permits a space (Windows
        # profile paths), and git runs this driver string through a shell, so an
        # unquoted "C:\\Users\\First Last\\...\\python.exe" would split into two
        # words and the driver would never run. The same allowlist keeps
        # '$' and backticks out, so double quotes cannot introduce expansion.
        driver = f'"{pinned}" -m omnigraph merge-driver %O %A %B'
    else:
        driver = "omnigraph merge-driver %O %A %B"
    try:
        for key, value in (
            ("merge.omnigraph.name", "omnigraph graph.json union merge"),
            ("merge.omnigraph.driver", driver),
        ):
            _sp.run(
                ["git", "-C", str(root), "config", key, value],
                check=True, capture_output=True, text=True,
            )
    except (OSError, _sp.CalledProcessError) as exc:
        return f"not registered (git config failed: {exc})"

    line = _merge_attr_line()
    attrs = root / ".gitattributes"
    if attrs.exists():
        content = attrs.read_text(encoding="utf-8")
        if _has_merge_attr(content):
            return f"already registered ({line})"
        # Nunca destrua outras entradas; preservar uma nova linha final.
        if content and not content.endswith("\n"):
            content += "\n"
        attrs.write_text(content + line + "\n", encoding="utf-8", newline="\n")
    else:
        attrs.write_text(line + "\n", encoding="utf-8", newline="\n")
    return f"registered ({line})"


def _unregister_merge_driver(root: Path) -> str:
    """Remove the merge-driver git config keys and the .gitattributes line."""
    import subprocess as _sp
    for key in ("merge.omnigraph.name", "merge.omnigraph.driver"):
        try:
            # --unset sai diferente de zero se a chave estiver ausente; tudo bem.
            _sp.run(
                ["git", "-C", str(root), "config", "--unset", key],
                capture_output=True, text=True,
            )
        except OSError:
            pass
    attrs = root / ".gitattributes"
    if not attrs.exists():
        return "not registered - nothing to remove."
    content = attrs.read_text(encoding="utf-8")
    kept = [
        raw for raw in content.splitlines()
        if not _has_merge_attr(raw)
    ]
    if kept == content.splitlines():
        return "gitattributes entry not found - nothing to remove."
    if kept:
        # Outras entradas sobreviveram; o arquivo permanece.
        attrs.write_text("\n".join(kept) + "\n", encoding="utf-8", newline="\n")
        return "removed from .gitattributes (other entries preserved)"
    attrs.unlink()
    return "removed (.gitattributes deleted - no other entries)"


def _merge_driver_status(root: Path) -> str:
    """Report whether the merge driver is registered (config + gitattributes)."""
    import subprocess as _sp
    try:
        res = _sp.run(
            ["git", "-C", str(root), "config", "--get", "merge.omnigraph.driver"],
            capture_output=True, text=True,
        )
        cfg_ok = res.returncode == 0 and bool(res.stdout.strip())
    except OSError:
        cfg_ok = False
    attrs = root / ".gitattributes"
    attr_ok = attrs.exists() and _has_merge_attr(attrs.read_text(encoding="utf-8"))
    if cfg_ok and attr_ok:
        return "registered"
    if cfg_ok:
        return "partially registered (git config set, .gitattributes line missing)"
    if attr_ok:
        return "partially registered (.gitattributes line set, git config missing)"
    return "not registered"


def _user_hooks_dir(hooks_dir: Path) -> Path:
    """Return the user-editable hooks directory.

    Husky 9 sets core.hooksPath to .husky/_ (wrapper scripts auto-generated by
    Husky), while user-editable hooks live in the parent .husky/. Return the
    parent when the resolved dir ends in '_' so install/status/uninstall target
    the correct location (#987).
    """
    if hooks_dir.name == "_":
        return hooks_dir.parent
    return hooks_dir


def install(path: Path = Path(".")) -> str:
    """Install omnigraph post-commit and post-checkout hooks in the nearest git repo."""
    root = _git_root(path)
    if root is None:
        raise RuntimeError(f"No git repository found at or above {path.resolve()}")

    hooks_dir = _user_hooks_dir(_hooks_dir(root))

    # Fixe o intérprete atual para que o gancho funcione mesmo quando o omnigraph
    # o iniciador não está no PATH no momento do disparo do git (ferramenta uv/isolamento pipx).
    # sys.executable é o Python executando este comando de instalação, então é
    # sempre o intérprete correto de venv isolado.  O espaço reservado é substituído
    # em ambos os roteiros antes de escrever; a lista de permissões em faixas _pinned_python()
    # quaisquer caracteres inseguros em um caminho do shell (resultado vazio -> o teste fixado é
    # ignorado) e a verificação de importação captura um caminho fixado obsoleto para que seja seguro
    # passa para a detecção dinâmica.
    pinned = _pinned_python()
    hook = _HOOK_SCRIPT.replace("__PINNED_PYTHON__", pinned)
    checkout = _CHECKOUT_SCRIPT.replace("__PINNED_PYTHON__", pinned)

    commit_msg = _install_hook(hooks_dir, "post-commit", hook, _HOOK_MARKER)
    checkout_msg = _install_hook(hooks_dir, "post-checkout", checkout, _CHECKOUT_MARKER)
    merge_msg = _register_merge_driver(root)

    return f"post-commit: {commit_msg}\npost-checkout: {checkout_msg}\nmerge driver: {merge_msg}"


def uninstall(path: Path = Path(".")) -> str:
    """Remove omnigraph post-commit and post-checkout hooks."""
    root = _git_root(path)
    if root is None:
        raise RuntimeError(f"No git repository found at or above {path.resolve()}")

    hooks_dir = _user_hooks_dir(_hooks_dir(root))
    commit_msg = _uninstall_hook(hooks_dir, "post-commit", _HOOK_MARKER, _HOOK_MARKER_END)
    checkout_msg = _uninstall_hook(hooks_dir, "post-checkout", _CHECKOUT_MARKER, _CHECKOUT_MARKER_END)
    merge_msg = _unregister_merge_driver(root)

    return f"post-commit: {commit_msg}\npost-checkout: {checkout_msg}\nmerge driver: {merge_msg}"


def status(path: Path = Path(".")) -> str:
    """Check if omnigraph hooks are installed."""
    root = _git_root(path)
    if root is None:
        return "Not in a git repository."
    hooks_dir = _user_hooks_dir(_hooks_dir(root))

    def _check(name: str, marker: str) -> str:
        p = hooks_dir / name
        if not p.exists():
            return "not installed"
        return "installed" if marker in p.read_text(encoding="utf-8") else "not installed (hook exists but omnigraph not found)"

    commit = _check("post-commit", _HOOK_MARKER)
    checkout = _check("post-checkout", _CHECKOUT_MARKER)
    merge = _merge_driver_status(root)
    return f"post-commit: {commit}\npost-checkout: {checkout}\nmerge driver: {merge}"
