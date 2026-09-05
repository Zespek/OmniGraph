"""omnigraph - extract · build · cluster · analyze · report."""

__version__ = "1.3.0"


def __getattr__(name):
    # Importações lentas para que `omnigraph install` funcione antes que dependências pesadas sejam implementadas.
    _map = {
        "extract": ("omnigraph.extract", "extract"),
        "collect_files": ("omnigraph.extract", "collect_files"),
        "build_from_json": ("omnigraph.build", "build_from_json"),
        "cluster": ("omnigraph.cluster", "cluster"),
        "score_all": ("omnigraph.cluster", "score_all"),
        "cohesion_score": ("omnigraph.cluster", "cohesion_score"),
        "god_nodes": ("omnigraph.analyze", "god_nodes"),
        "surprising_connections": ("omnigraph.analyze", "surprising_connections"),
        "suggest_questions": ("omnigraph.analyze", "suggest_questions"),
        "generate": ("omnigraph.report", "generate"),
        "to_json": ("omnigraph.export", "to_json"),
        "to_html": ("omnigraph.export", "to_html"),
        "to_svg": ("omnigraph.export", "to_svg"),
        "to_canvas": ("omnigraph.export", "to_canvas"),
        "to_wiki": ("omnigraph.wiki", "to_wiki"),
        "reflect": ("omnigraph.reflect", "reflect"),
        "save_query_result": ("omnigraph.ingest", "save_query_result"),
    }
    if name in _map:
        import importlib
        mod_name, attr = _map[name]
        mod = importlib.import_module(mod_name)
        return getattr(mod, attr)
    raise AttributeError(f"module 'omnigraph' has no attribute {name!r}")
