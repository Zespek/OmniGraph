#!/usr/bin/env python3
"""Busca semântica (RAG) local para o omnigraph-perguntar.

Em vez de busca por palavra-chave (que não casa "orçamento" com "proposal"),
usa embeddings do Ollama para achar o código semanticamente relevante — mesmo
com a pergunta em português e o código em inglês.

Uso:
  omnigraph-rag.py index  <pasta> [--model M] [--host H]
  omnigraph-rag.py search <pasta> "pergunta" [--k 8] [--host H]
  omnigraph-rag.py fresh  <pasta>      # 0 se o índice está atual, 1 se precisa reindexar
"""
import sys, os, json, math, argparse, urllib.request

EMB_MODEL_DEFAULT = "bge-m3"
EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".go", ".java",
        ".kt", ".rb", ".php", ".cs", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp",
        ".swift", ".m", ".mm", ".scala", ".dart", ".vue", ".svelte", ".sql",
        ".md", ".graphql", ".prisma"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", ".next",
             ".expo", "__pycache__", "omnigraph-out", ".turbo", "coverage",
             ".idea", ".gradle", "Pods", "vendor"}
MAX_BYTES = 400_000
CHUNK = 1600
STEP = 1300


def _emb(text, model, host):
    data = json.dumps({"model": model, "prompt": text}).encode()
    req = urllib.request.Request("http://%s/api/embeddings" % host, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r).get("embedding")


def _files(root):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in fns:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in EXTS:
                continue
            p = os.path.join(dp, fn)
            try:
                if os.path.getsize(p) <= MAX_BYTES:
                    yield p
            except OSError:
                pass


def _chunks(path):
    try:
        t = open(path, encoding="utf-8", errors="ignore").read()
    except Exception:
        return
    if not t.strip():
        return
    i = 0
    while i < len(t):
        c = t[i:i + CHUNK].strip()
        if c:
            yield i, c
        i += STEP


def _idx_path(root):
    return os.path.join(root, "omnigraph-out", ".rag.json")


def _newest_source(root):
    newest = 0.0
    for p in _files(root):
        try:
            newest = max(newest, os.path.getmtime(p))
        except OSError:
            pass
    return newest


def cmd_fresh(root):
    ip = _idx_path(root)
    if not os.path.exists(ip):
        return 1
    try:
        return 0 if os.path.getmtime(ip) >= _newest_source(root) else 1
    except OSError:
        return 1


def cmd_index(root, model, host):
    items = []
    files = list(_files(root))
    if not files:
        sys.stderr.write("nenhum arquivo de código para indexar\n")
        return 1
    for n, p in enumerate(files):
        rel = os.path.relpath(p, root)
        for off, c in _chunks(p):
            v = _emb(c, model, host)
            if v:
                items.append({"file": rel, "off": off, "text": c[:1400], "vec": v})
        sys.stderr.write("\r  indexando %d/%d arquivos (%d trechos)..." % (n + 1, len(files), len(items)))
    sys.stderr.write("\n")
    os.makedirs(os.path.dirname(_idx_path(root)), exist_ok=True)
    json.dump({"model": model, "items": items}, open(_idx_path(root), "w"))
    return 0


def _cos(a, b):
    s = na = nb = 0.0
    for x, y in zip(a, b):
        s += x * y; na += x * x; nb += y * y
    return s / math.sqrt(na * nb) if na and nb else 0.0


def cmd_search(root, q, k, host):
    ip = _idx_path(root)
    if not os.path.exists(ip):
        sys.stderr.write("SEM_INDICE\n")
        return 2
    idx = json.load(open(ip))
    qv = _emb(q, idx.get("model", EMB_MODEL_DEFAULT), host)
    if not qv:
        return 3
    ranked = sorted(idx["items"], key=lambda it: _cos(qv, it["vec"]), reverse=True)
    # diversidade: no máximo 2 trechos por arquivo, para cobrir fluxos espalhados
    # em vários arquivos (proposal, offer, service-request...) em vez de 8 do mesmo.
    perfile, shown = {}, 0
    for it in ranked:
        f = it["file"]
        if perfile.get(f, 0) >= 2:
            continue
        perfile[f] = perfile.get(f, 0) + 1
        sys.stdout.write("----- %s -----\n%s\n\n" % (f, it["text"]))
        shown += 1
        if shown >= k:
            break
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["index", "search", "fresh"])
    ap.add_argument("root")
    ap.add_argument("question", nargs="?", default="")
    ap.add_argument("--model", default=os.environ.get("OMNIGRAPH_EMBED_MODEL", EMB_MODEL_DEFAULT))
    ap.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "localhost:11434"))
    ap.add_argument("--k", type=int, default=8)
    a = ap.parse_args()
    try:
        if a.cmd == "fresh":
            sys.exit(cmd_fresh(a.root))
        elif a.cmd == "index":
            sys.exit(cmd_index(a.root, a.model, a.host))
        else:
            sys.exit(cmd_search(a.root, a.question, a.k, a.host))
    except Exception as e:
        sys.stderr.write("erro: %s\n" % e)
        sys.exit(4)


if __name__ == "__main__":
    main()
