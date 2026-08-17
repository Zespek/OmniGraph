"""Per-language extractors, incrementally migrated out of omnigraph/extract.py.

Dispatch still flows through omnigraph.extract (the facade re-exports every
moved name), so importing from omnigraph.extract keeps working unchanged.
LANGUAGE_EXTRACTORS is the registry seed; wiring dispatch through it is a
later, separate step. See MIGRATION.md for how to port another language.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from omnigraph.extractors.apex import extract_apex
from omnigraph.extractors.bash import extract_bash
from omnigraph.extractors.blade import extract_blade
from omnigraph.extractors.commonlisp import extract_commonlisp
from omnigraph.extractors.dart import extract_dart
from omnigraph.extractors.dm import extract_dm, extract_dmf, extract_dmi, extract_dmm
from omnigraph.extractors.elixir import extract_elixir
from omnigraph.extractors.fortran import extract_fortran
from omnigraph.extractors.go import extract_go
from omnigraph.extractors.json_config import extract_json
from omnigraph.extractors.julia import extract_julia
from omnigraph.extractors.markdown import extract_markdown
from omnigraph.extractors.objc import extract_objc
from omnigraph.extractors.pascal import extract_pascal
from omnigraph.extractors.pascal_forms import extract_delphi_form, extract_lazarus_form
from omnigraph.extractors.powershell import extract_powershell, extract_powershell_manifest
from omnigraph.extractors.razor import extract_razor
from omnigraph.extractors.rust import extract_rust
from omnigraph.extractors.sln import extract_sln
from omnigraph.extractors.sql import extract_sql
from omnigraph.extractors.terraform import extract_terraform
from omnigraph.extractors.verilog import extract_verilog
from omnigraph.extractors.zig import extract_zig

LANGUAGE_EXTRACTORS: dict[str, Callable[[Path], dict]] = {
    "apex": extract_apex,
    "bash": extract_bash,
    "blade": extract_blade,
    "commonlisp": extract_commonlisp,
    "dart": extract_dart,
    "delphi_form": extract_delphi_form,
    "dm": extract_dm,
    "dmf": extract_dmf,
    "dmi": extract_dmi,
    "dmm": extract_dmm,
    "elixir": extract_elixir,
    "fortran": extract_fortran,
    "go": extract_go,
    "json": extract_json,
    "julia": extract_julia,
    "lazarus_form": extract_lazarus_form,
    "markdown": extract_markdown,
    "objc": extract_objc,
    "pascal": extract_pascal,
    "powershell": extract_powershell,
    "powershell_manifest": extract_powershell_manifest,
    "razor": extract_razor,
    "rust": extract_rust,
    "sln": extract_sln,
    "sql": extract_sql,
    "terraform": extract_terraform,
    "verilog": extract_verilog,
    "zig": extract_zig,
}
