"""Shared constants/helpers for the omnigraph exporters package.

Symbols used by more than one exporter live here so each exporter module can be
split out of omnigraph/export.py without a circular import (export.py and the
per-format modules both import from here, never from each other).
"""
from __future__ import annotations

# Paleta categórica para coloração da comunidade, compartilhada pelo HTML, SVG e
# Exportadores de obsidiana. Movido literalmente de omnigraph/export.py.
COMMUNITY_COLORS = [
    "#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F",
    "#EDC948", "#B07AA1", "#FF9DA7", "#9C755F", "#BAB0AC",
]
