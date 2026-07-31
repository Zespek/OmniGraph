# reexportar auxiliares de manifesto da detecção para compatibilidade com versões anteriores
from omnigraph.detect import save_manifest, load_manifest, detect_incremental

__all__ = ["save_manifest", "load_manifest", "detect_incremental"]
