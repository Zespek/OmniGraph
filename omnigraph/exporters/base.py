"""Shared constants/helpers for the omnigraph exporters package.

Symbols used by more than one exporter live here so each exporter module can be
split out of omnigraph/export.py without a circular import (export.py and the
per-format modules both import from here, never from each other).
"""
from __future__ import annotations

# Paleta categórica para coloração da comunidade, compartilhada pelo HTML, SVG e
COMMUNITY_COLORS = [
    "#bd00ff", "#00e5ff", "#ec4899", "#f59e0b", "#10b981",
    "#8b5cf6", "#ef4444", "#3b82f6", "#d946ef", "#eab308",
]
