"""Deterministic structural extraction from source code using tree-sitter. Outputs nodes+edges dicts."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sys
import textwrap
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, Callable

from .cache import load_cached, save_cached
from .mcp_ingest import extract_mcp_config, is_mcp_config_path
from .manifest_ingest import extract_package_manifest, is_package_manifest_path
from .resolver_registry import (
    LanguageResolver,
    register as register_language_resolver,
    run_language_resolvers,
)
from .ruby_resolution import resolve_ruby_member_calls
from .pascal_resolution import resolve_pascal_inherited_calls

from omnigraph.extractors.base import (  # noqa: F401
    _LANGUAGE_BUILTIN_GLOBALS,
    _file_stem,
    _make_id,
    _read_text,
)
from omnigraph.extractors.apex import extract_apex  # noqa: F401
from omnigraph.extractors.bash import extract_bash  # noqa: F401
from omnigraph.extractors.blade import extract_blade  # noqa: F401
from omnigraph.extractors.csharp import (
    CsharpNameResolver,
    _resolve_cross_file_csharp_imports,
    _resolve_csharp_type_references,
)
from omnigraph.extractors.dart import extract_dart  # noqa: F401
from omnigraph.extractors.dm import extract_dm, extract_dmf, extract_dmi, extract_dmm  # noqa: F401
from omnigraph.extractors.elixir import extract_elixir  # noqa: F401
from omnigraph.extractors.fortran import _cpp_preprocess, extract_fortran  # noqa: F401
from omnigraph.extractors.go import _GO_PREDECLARED_FUNCS, extract_go  # noqa: F401
from omnigraph.extractors.json_config import extract_json  # noqa: F401
from omnigraph.extractors.commonlisp import extract_commonlisp  # noqa: F401
from omnigraph.extractors.markdown import extract_markdown  # noqa: F401
from omnigraph.extractors.ocaml import extract_ocaml  # noqa: F401
from omnigraph.extractors.pascal_forms import extract_delphi_form, extract_lazarus_form  # noqa: F401
from omnigraph.extractors.powershell import extract_powershell, extract_powershell_manifest  # noqa: F401
from omnigraph.extractors.razor import extract_razor  # noqa: F401
from omnigraph.extractors.rust import extract_rust  # noqa: F401
from omnigraph.extractors.sln import extract_sln  # noqa: F401
from omnigraph.extractors.sql import extract_sql  # noqa: F401
from omnigraph.extractors.terraform import extract_terraform  # noqa: F401
from omnigraph.extractors.verilog import extract_verilog  # noqa: F401
from omnigraph.extractors.zig import extract_zig  # noqa: F401
from omnigraph.security import sanitize_metadata
from omnigraph.paths import disambiguate_ambiguous_candidates

from omnigraph.extractors.models import LanguageConfig, _JS_CACHE_BYPASS_SUFFIXES, _NamespaceExportFact, _StarExportFact, _SymbolAliasFact, _SymbolDeclarationFact, _SymbolExportFact, _SymbolImportFact, _SymbolResolutionFacts, _SymbolUseFact, _WORKSPACE_PACKAGE_CACHE  # noqa: E402,F401

from omnigraph.extractors.resolution import (  # noqa: E402,F401
    _DECLDEF_HEADER_SUFFIXES,
    _DECLDEF_IMPL_SUFFIXES,
    _EXPORT_CONDITION_PRIORITY,
    _JS_INDEX_FILES,
    _JS_PRIMITIVE_TYPES,
    _JS_RESOLVE_EXTS,
    _TSCONFIG_ALIAS_CACHE,
    _VUE_SCRIPT_LANG_RE,
    _VUE_SCRIPT_RE,
    _WORKSPACE_MANIFEST_NAMES,
    _apply_symbol_resolution_facts,
    _augment_symbol_resolution_edges,
    _collect_js_symbol_resolution_facts,
    _collect_python_symbol_resolution_facts,
    _contained_in_package,
    _decldef_class_stem,
    _disambiguate_colliding_node_ids,
    _find_workspace_root,
    _go_import_path_for_file,
    _is_type_like_definition,
    _js_call_identifier,
    _js_default_export_name,
    _js_default_import_name,
    _js_export_clause,
    _js_export_statement_is_star,
    _js_exported_declaration_names,
    _js_lexical_aliases,
    _js_module_specifier,
    _js_named_specifiers,
    _js_namespace_export_name,
    _js_source_path,
    _js_top_level_function_bodies,
    _load_tsconfig_aliases,
    _load_tsconfig_base_url,
    _load_workspace_packages,
    _match_tsconfig_alias,
    _merge_decl_def_classes,
    _node_disambiguation_source_key,
    _package_entry_candidates,
    _parse_js_tree,
    _parse_python_tree,
    _pascal_class_stem_cache,
    _pascal_project_root,
    _pascal_resolve_class,
    _pascal_resolve_unit,
    _pascal_unit_cache,
    _pnpm_workspace_globs,
    _python_call_identifier,
    _python_import_from_module,
    _python_imported_names,
    _python_top_level_function_bodies,
    _read_tsconfig_aliases,
    _resolve_c_include_path,
    _resolve_cross_file_imports,
    _resolve_cross_file_java_imports,
    _resolve_export_target,
    _resolve_go_type_references,
    _resolve_java_type_references,
    _resolve_php_type_references,
    _resolve_js_import_path,
    _resolve_js_import_target,
    _resolve_js_module_path,
    _resolve_lua_import_target,
    _probe_python_module_candidate,
    _resolve_python_module_path,
    _resolve_tsconfig_alias,
    _resolve_workspace_import,
    _source_key,
    _strip_jsonc,
    _ts_collect_type_refs,
    _ts_heritage_clause_entries,
    _ts_walk_class_members,
    _vue_mask_non_script,
    _walk_js_tree,
    _walk_python_tree,
    _workspace_globs,
)

from omnigraph.symbol_resolution import resolve_bash_source_edges  # noqa: E402

from omnigraph.extractors.engine import REFERENCE_CONTEXTS, _CSHARP_TYPE_PARAMETER_SCOPE_DECLARATIONS, _C_PRIMITIVE_TYPE_NODES, _JAVA_BUILTIN_TYPES, _JAVA_TYPE_PARAMETER_SCOPE_DECLARATIONS, _JS_FUNCTION_VALUE_TYPES, _JS_SCOPE_BOUNDARY, _PYTHON_ANNOTATION_NOISE, _PYTHON_TYPE_CONTAINERS, _RUBY_CLASS_FACTORIES, _c_collect_type_refs, _cpp_collect_type_refs, _cpp_declarator_name, _cpp_local_var_types, _csharp_attribute_names, _csharp_classify_base, _csharp_collect_type_refs, _csharp_extra_walk, _csharp_namespace_id, _csharp_namespace_name, _csharp_pre_scan_interfaces, _csharp_type_parameters_in_scope, _dynamic_import_js, _extract_generic, _find_body, _find_require_call, _get_cpp_func_name, _java_annotation_names, _java_collect_type_refs, _java_extra_walk, _java_type_parameters_in_scope, _js_collect_pattern_idents, _js_dispatch_value_idents, _js_extra_walk, _js_local_bound_names, _js_member_assignment_target, _js_module_bound_names, _kotlin_collect_type_refs, _kotlin_function_return_type_node, _kotlin_property_type_node, _kotlin_user_type_name, _php_collect_type_refs, _php_method_return_type_node, _php_name_text, _python_collect_assignment_targets, _python_collect_param_refs, _python_collect_type_refs, _python_local_bound_names, _python_module_bound_names, _python_param_names, _read_csharp_type_name, _require_imports_js, _ruby_const_last_name, _ruby_extra_walk, _ruby_local_class_bindings, _ruby_new_class_name, _scala_collect_type_refs, _semantic_reference_edge, _source_location, _swift_classify_base, _swift_collect_type_refs, _swift_constructor_type, _swift_declaration_keyword, _swift_extra_walk, _swift_local_var_types, _swift_pre_scan, _swift_property_name, _swift_property_type_node, _swift_receiver_name, _swift_user_type_name, _ts_decorator_name, _ts_descendant_decorators, _ts_emit_decorator_edges, _ts_extra_walk, _ts_method_name, _ts_receiver_type_table  # noqa: E402,F401

from omnigraph.extractors.pascal import _PAS_BEGIN_END_TOKEN_RE, _PAS_CALL_RE, _PAS_END_SEMI_RE, _PAS_IMPL_HEADER_RE, _PAS_KEYWORDS, _PAS_METHOD_DECL_RE, _PAS_MODULE_RE, _PAS_TOKEN_RE, _PAS_TYPE_HEADER_RE, _PAS_USES_RE, _extract_pascal_regex, _pascal_find_body, _pascal_split_bases, _pascal_split_sections, _pascal_split_uses, _pascal_strip_comments, extract_pascal  # noqa: E402,F401

from omnigraph.extractors.objc import _objc_local_var_types, extract_objc  # noqa: E402,F401

from omnigraph.extractors.julia import extract_julia  # noqa: E402,F401

_RECURSION_LIMIT = 10_000

# Globais integrados à linguagem que o AST pode classificar como alvos de chamada quando usados ​​como
# construtores ou funções de coerção (por exemplo, String(x), Number(x), Boolean(x)).
# Sem esse filtro, eles se tornam god node, acumulando arestas espúrias de
# cada site de chamada. Filtro aplicado na resolução do mesmo arquivo e entre arquivos.


def _raise_recursion_limit() -> None:
    if sys.getrecursionlimit() < _RECURSION_LIMIT:
        sys.setrecursionlimit(_RECURSION_LIMIT)


def _safe_extract(extractor: Callable, path: Path) -> dict:
    try:
        return extractor(path)
    except RecursionError:
        print(f"  warning: skipped {path} (recursion limit exceeded)", file=sys.stderr, flush=True)
        return {"nodes": [], "edges": [], "error": "recursion_limit_exceeded"}
    except Exception as e:
        if os.environ.get("OMNIGRAPH_DEBUG"):
            import traceback
            traceback.print_exc(file=sys.stderr)
        print(f"  warning: skipped {path} ({type(e).__name__}: {e})", file=sys.stderr, flush=True)
        return {"nodes": [], "edges": [], "error": f"{type(e).__name__}: {e}"}


def _file_node_id(rel_path: Path) -> str:
    """File-level node ID matching the skill.md spec: ``{parent_dir}_{stem}`` —
    one parent directory level, no extension. ``rel_path`` MUST be relative to
    the project root so top-level files collapse to a bare stem (``setup.py`` ->
    ``setup``) instead of picking up the root directory name. This must equal the
    ID semantic subagents generate, or AST and semantic extraction split a file
    into two disconnected ghost nodes (#1033)."""
    return _make_id(_file_stem(rel_path))


def _repoint_python_package_imports(paths, all_nodes, all_edges, root) -> None:
    """Repoint Python absolute-import edges to the real file node under a nested
    (e.g. ``src/``) package root (#2072).

    Absolute imports target an id derived from the dotted module path
    (``_make_id('pkg.mod')`` -> ``pkg_mod``), but file-node ids are
    scan-root-relative (``src_pkg_mod`` when the code lives under ``src/``), so
    the edge dangles and is silently dropped — the graph loses most ``imports``
    edges purely because of where the scan started. Build an alias map from the
    dotted-module id to the real file-node id by detecting each ``.py`` file's
    package root (the contiguous run of ancestor dirs carrying ``__init__.py``)
    and rewrite matching ``imports``/``imports_from`` edge targets. Guards: never
    shadow an existing node id, and drop an alias claimed by more than one file
    (ambiguous -> leave dangling, as before). Files whose package root IS the
    scan root are skipped (ids already coincide)."""
    try:
        root = Path(root).resolve()
    except OSError:
        root = Path(root)
    node_ids = {n.get("id") for n in all_nodes if isinstance(n, dict)}
    alias_to_files: dict[str, set[str]] = {}
    for p in paths:
        if p.suffix.lower() not in (".py", ".pyi"):
            continue
        try:
            rel = Path(p).resolve().relative_to(root)
        except (ValueError, OSError):
            continue
        parts = rel.parts
        if len(parts) < 2:
            continue
        d = Path(p).resolve().parent
        levels = 0
        while levels < len(parts) - 1 and (d / "__init__.py").is_file():
            levels += 1
            d = d.parent
        if levels == 0:
            continue
        mod_parts = parts[-(levels + 1):]
        if len(mod_parts) == len(parts):
            continue
        file_node = _file_node_id(rel)
        alias = _make_id(str(Path(*mod_parts).with_suffix("")))
        alias_to_files.setdefault(alias, set()).add(file_node)
        if p.name in ("__init__.py", "__init__.pyi") and len(mod_parts) > 1:
            pkg_alias = _make_id(str(Path(*mod_parts[:-1])))
            alias_to_files.setdefault(pkg_alias, set()).add(file_node)
    alias_map = {
        a: next(iter(fs))
        for a, fs in alias_to_files.items()
        if len(fs) == 1 and a not in node_ids
    }
    if not alias_map:
        return
    for e in all_edges:
        if (
            isinstance(e, dict)
            and e.get("relation") in ("imports", "imports_from")
            and str(e.get("source_file", "")).lower().endswith((".py", ".pyi"))
        ):
            tgt = e.get("target")
            if tgt in alias_map:
                e["target"] = alias_map[tgt]


SEMANTIC_RELATIONS = frozenset({
    "inherits", "implements", "mixes_in", "embeds", "references",
    "calls", "imports", "imports_from", "re_exports", "contains", "method",
})


# Chaves de condição consultadas ao resolver um alvo de `exportações`, em prioridade
# ordem. `default` é o genérico do Node e deve ser consultado POR ÚLTIMO para uma abordagem mais
# condição específica (fonte/importação/módulo/etc.) vence quando várias correspondem.






# Construções escalares e nomes de simulação de teste que aparecem como anotações de tipo, mas carregam
# nenhum significado semântico útil como nós de grafo. Suprimido na anotação
# nível do walker para que nunca sejam criados como nós ou emitidos como arestas.


# aparecem como anotações de campo, parâmetro, retorno e argumento genérico. Eles nunca
# resolver para um nó do projeto, portanto, emitir arestas de 'referências' para eles é puro ruído
# type-ref walker para que nunca sejam criados como nós ou emitidos como arestas. O
# As primitivas boxed-scalar/`void` já foram eliminadas pelo tipo de nó gramatical acima;
# esses são os nomes de classe/interface que a gramática relata como identificadores.






def _resolve_name(node, source: bytes, config: LanguageConfig) -> str | None:
    """Get the name from a node using config.name_field, falling back to child types."""
    if config.resolve_function_name_fn is not None:
        # Para C/C++ onde o nome está dentro de um declarador
        return None
    n = node.child_by_field_name(config.name_field)
    if n:
        return _read_text(n, source)
    for child in node.children:
        if child.type in config.name_fallback_child_types:
            return _read_text(child, source)
    return None



def _import_python(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None = None) -> None:
    t = node.type
    if t == "import_statement":
        for child in node.children:
            if child.type in ("dotted_name", "aliased_import"):
                raw = _read_text(child, source)
                raw_module, _, raw_alias = raw.partition(" as ")
                module_name = raw_module.strip().lstrip(".")
                tgt_nid = _make_id(module_name)
                edge = {
                    "source": file_nid,
                    "target": tgt_nid,
                    "relation": "imports",
                    "context": "import",
                    "confidence": "EXTRACTED",
                    "source_file": str_path,
                    "source_location": f"L{node.start_point[0] + 1}",
                    "weight": 1.0,
                }
                if raw_alias:
                    edge["local_alias"] = raw_alias.strip()
                edges.append(edge)
    elif t == "import_from_statement":
        module_node = node.child_by_field_name("module_name")
        if module_node:
            raw = _read_text(module_node, source)
            target_path: "Path | None" = None
            if raw.startswith("."):
                # Importação relativa - resolva o caminho completo para que os IDs correspondam aos IDs dos nós do arquivo
                dots = len(raw) - len(raw.lstrip("."))
                module_name = raw.lstrip(".")
                base = Path(str_path).parent
                for _ in range(dots - 1):
                    base = base.parent
                candidate = base / module_name.replace(".", "/") if module_name else base
                resolved = _probe_python_module_candidate(candidate)
                if resolved is not None:
                    target_path = resolved
                else:
                    rel = (module_name.replace(".", "/") + ".py") if module_name else "__init__.py"
                    target_path = base / rel
                tgt_nid = _make_id(str(target_path))
            else:
                tgt_nid = _make_id(raw)
            edge = {
                "source": file_nid,
                "target": tgt_nid,
                "relation": "imports_from",
                "context": "import",
                "confidence": "EXTRACTED",
                "source_file": str_path,
                "source_location": f"L{node.start_point[0] + 1}",
                "weight": 1.0,
            }
            if target_path is not None:
                try:
                    if target_path.is_file():
                        edge["target_file"] = str(target_path)
                except OSError:
                    pass
            edges.append(edge)


def _import_js(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None = None) -> None:
    is_reexport = node.type == "export_statement"
    # Manuseie export_statement apenas se tiver uma cláusula `from` (reexportação).
    # Exportações puras como `export const x = 1` ou `export { localVar }` não possuem módulo de origem.
    if is_reexport:
        has_from = any(child.type == "from" or (_read_text(child, source) == "from") for child in node.children if child.type in ("from", "identifier"))
        if not has_from:
            # Verifique a string filho (caminho de origem) como um indicador mais confiável
            has_from = any(child.type == "string" for child in node.children)
            if not has_from:
                return

    resolved_path: "Path | None" = None
    module_string = None
    for child in node.children:
        if child.type == "string":
            module_string = child
            break
        if child.type == "import_require_clause":
            # Formulário de importação igual a TS: `import x = require("./m")`. O módulo
            # string fica dentro da cláusula, não na import_statement
            # em si, então a varredura direta para crianças acima nunca o vê.
            module_string = next(
                (sub for sub in child.children if sub.type == "string"), None
            )
            break
    if module_string is not None:
        raw = _read_text(module_string, source).strip("'\"` ")
        resolved = _resolve_js_import_target(raw, str_path)
        if resolved is not None:
            tgt_nid, resolved_path = resolved
            if resolved_path is not None and not resolved_path.is_file():
                tgt_nid = _make_id("ref", raw)
                resolved_path = None
            edge = {
                "source": file_nid,
                "target": tgt_nid,
                "relation": "imports_from",
                "context": "re-export" if is_reexport else "import",
                "confidence": "EXTRACTED",
                "source_file": str_path,
                "source_location": f"L{node.start_point[0] + 1}",
                "weight": 1.0,
            }
            if resolved_path is not None:
                edge["target_file"] = str(resolved_path)
            edges.append(edge)

    # Emite arestas em nível de símbolo para importações/reexportações nomeadas de arquivos locais/alias.
    # definindo o símbolo, de forma que essas arestas conectem os importadores diretamente aos nós de símbolo existentes.
    if resolved_path is not None:
        target_stem = _file_stem(resolved_path)
        line = node.start_point[0] + 1

        if is_reexport:
            #         exportar {padrão como baz} de './module'
            for child in node.children:
                if child.type == "export_clause":
                    for spec in child.children:
                        if spec.type == "export_specifier":
                            # O nome exportado é o nome local do módulo de origem
                            name_node = spec.child_by_field_name("name")
                            if name_node:
                                sym = _read_text(name_node, source)
                                if sym == "default":
                                    continue  # pular reexportações padrão para correspondência de ID
                                edges.append({
                                    "source": file_nid,
                                    "target": _make_id(target_stem, sym),
                                    "relation": "re_exports",
                                    "context": "re-export",
                                    "confidence": "EXTRACTED",
                                    "source_file": str_path,
                                    "source_location": f"L{line}",
                                    "weight": 1.0,
                                    "target_file": str(resolved_path),
                                })
        else:
            for child in node.children:
                if child.type == "import_clause":
                    for sub in child.children:
                        if sub.type == "named_imports":
                            for spec in sub.children:
                                if spec.type == "import_specifier":
                                    name_node = spec.child_by_field_name("name")
                                    if name_node:
                                        sym = _read_text(name_node, source)
                                        edges.append({
                                            "source": file_nid,
                                            "target": _make_id(target_stem, sym),
                                            "relation": "imports",
                                            "context": "import",
                                            "confidence": "EXTRACTED",
                                            "source_file": str_path,
                                            "source_location": f"L{line}",
                                            "weight": 1.0,
                                            "target_file": str(resolved_path),
                                        })


def _import_java(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None = None) -> None:
    def _walk_scoped(n) -> str:
        parts: list[str] = []
        cur = n
        while cur:
            if cur.type == "scoped_identifier":
                name_node = cur.child_by_field_name("name")
                if name_node:
                    parts.append(_read_text(name_node, source))
                cur = cur.child_by_field_name("scope")
            elif cur.type == "identifier":
                parts.append(_read_text(cur, source))
                break
            else:
                break
        parts.reverse()
        return ".".join(parts)

    for child in node.children:
        if child.type in ("scoped_identifier", "identifier"):
            path_str = _walk_scoped(child)
            module_name = path_str.split(".")[-1].strip("*").strip(".") or (
                path_str.split(".")[-2] if len(path_str.split(".")) > 1 else path_str
            )
            if module_name:
                tgt_nid = _make_id(module_name)
                edges.append({
                    "source": file_nid,
                    "target": tgt_nid,
                    "relation": "imports",
                    "context": "import",
                    "confidence": "EXTRACTED",
                    "source_file": str_path,
                    "source_location": f"L{node.start_point[0] + 1}",
                    "weight": 1.0,
                })
            break


def _import_c(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None = None) -> None:
    for child in node.children:
        if child.type in ("string_literal", "system_lib_string", "string"):
            raw = _read_text(child, source).strip('"<> ')
            # As citações incluem: tente resolver para um arquivo real para que o ID de destino
            # corresponde ao ID do nó que _extract_generic cria para esse arquivo.
            if child.type != "system_lib_string":
                resolved = _resolve_c_include_path(raw, str_path)
                if resolved is not None:
                    tgt_nid = _make_id(str(resolved))
                    edges.append({
                        "source": file_nid,
                        "target": tgt_nid,
                        "relation": "imports",
                        "context": "import",
                        "confidence": "EXTRACTED",
                        "source_file": str_path,
                        "source_location": f"L{node.start_point[0] + 1}",
                        "weight": 1.0,
                        "target_file": str(resolved),
                    })
                    break
            module_name = raw.split("/")[-1].split(".")[0]
            if module_name:
                tgt_nid = _make_id(module_name)
                edges.append({
                    "source": file_nid,
                    "target": tgt_nid,
                    "relation": "imports",
                    "context": "import",
                    "confidence": "EXTRACTED",
                    "source_file": str_path,
                    "source_location": f"L{node.start_point[0] + 1}",
                    "weight": 1.0,
                })
            break


def _import_csharp(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None = None) -> None:
    text = _read_text(node, source).strip().rstrip(";")
    if text.startswith("global "):
        text = text[len("global "):].strip()
    if not text.startswith("using"):
        return
    body = text[len("using"):].strip()
    using_kind, alias, target_fqn = "namespace", None, body
    if body.startswith("static "):
        using_kind, target_fqn = "static", body[len("static "):].strip()
    elif "=" in body:
        lhs, rhs = body.split("=", 1)
        using_kind, alias, target_fqn = "alias", lhs.strip(), rhs.strip()
    if not target_fqn:
        return
    edges.append({
        "source": file_nid,
        "target": _make_id(target_fqn),
        "relation": "imports",
        "context": "import",
        "confidence": "EXTRACTED",
        "source_file": str_path,
        "source_location": f"L{node.start_point[0] + 1}",
        "weight": 1.0,
        "metadata": sanitize_metadata({k: v for k, v in
            {"using_kind": using_kind, "alias": alias, "target_fqn": target_fqn,
             "scope_kind": "namespace" if scope_stack else "file",
             "scope_id": scope_stack[-1] if scope_stack else None}.items() if v is not None}),
    })


def _import_kotlin(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None = None) -> None:
    path_node = node.child_by_field_name("path")
    if path_node is None:
        path_node = next(
            (c for c in node.children if c.type == "qualified_identifier"), None
        )
    if path_node is not None:
        raw = _read_text(path_node, source).strip()
    else:
        raw = next(
            (_read_text(c, source).strip() for c in node.children
             if c.type == "identifier"),
            "",
        )
    if not raw:
        return
    if raw.endswith(".*") or raw == "*" or any(c.type == "*" for c in node.children):
        return
    alias = None
    saw_as = False
    for child in node.children:
        if not saw_as:
            saw_as = child.type == "as"
        elif child.type in ("identifier", "simple_identifier"):
            alias = _read_text(child, source).strip() or None
            break
    module_name = raw.split(".")[-1].strip()
    if not module_name:
        return
    edges.append({
        "source": file_nid,
        "target": _make_id(module_name),
        "relation": "imports",
        "context": "import",
        "confidence": "EXTRACTED",
        "source_file": str_path,
        "source_location": f"L{node.start_point[0] + 1}",
        "weight": 1.0,
        "metadata": sanitize_metadata({k: v for k, v in
            {"target_fqn": raw, "alias": alias}.items() if v is not None}),
    })


def _import_scala(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None = None) -> None:
    for child in node.children:
        if child.type in ("stable_id", "identifier"):
            raw = _read_text(child, source)
            module_name = raw.split(".")[-1].strip("{} ")
            if module_name and module_name != "_":
                tgt_nid = _make_id(module_name)
                edges.append({
                    "source": file_nid,
                    "target": tgt_nid,
                    "relation": "imports",
                    "context": "import",
                    "confidence": "EXTRACTED",
                    "source_file": str_path,
                    "source_location": f"L{node.start_point[0] + 1}",
                    "weight": 1.0,
                })
            break


def _import_php(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None = None) -> None:
    for child in node.children:
        if child.type in ("qualified_name", "name", "identifier"):
            raw = _read_text(child, source)
            module_name = raw.split("\\")[-1].strip()
            if module_name:
                tgt_nid = _make_id(module_name)
                edges.append({
                    "source": file_nid,
                    "target": tgt_nid,
                    "relation": "imports",
                    "context": "import",
                    "confidence": "EXTRACTED",
                    "source_file": str_path,
                    "source_location": f"L{node.start_point[0] + 1}",
                    "weight": 1.0,
                })
            break



def _get_c_func_name(node, source: bytes) -> str | None:
    """Recursively unwrap declarator to find the innermost identifier (C)."""
    if node.type == "identifier":
        return _read_text(node, source)
    decl = node.child_by_field_name("declarator")
    if decl:
        return _get_c_func_name(decl, source)
    for child in node.children:
        if child.type == "identifier":
            return _read_text(child, source)
    return None


# ── Caminhada extra JS/TS para funções de seta ──────────────────────────────────────


# Tipos de nós cujo valor pode ser chamado, para a atribuição JS/TS/campo de classe
# rotular uma expressão de função `função`; os atuais usam `function_expression`.


# ── Caminhada extra de TS para declarações de namespace/módulo ─────────────────────────


# ── Caminhada extra em C# para declarações de namespace ──────────────────────────────────


# ── Caminhada extra rápida para casos enum ───────────────────── ─────────────────────





_PYTHON_CONFIG = LanguageConfig(
    ts_module="tree_sitter_python",
    class_types=frozenset({"class_definition"}),
    function_types=frozenset({"function_definition"}),
    import_types=frozenset({"import_statement", "import_from_statement"}),
    call_types=frozenset({"call"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"attribute"}),
    call_accessor_field="attribute",
    call_accessor_object_field="object",
    function_boundary_types=frozenset({"function_definition"}),
    import_handler=_import_python,
)

_JS_CONFIG = LanguageConfig(
    ts_module="tree_sitter_javascript",
    class_types=frozenset({"class_declaration"}),
    function_types=frozenset({"function_declaration", "generator_function_declaration", "method_definition"}),
    import_types=frozenset({"import_statement", "export_statement"}),
    call_types=frozenset({"call_expression", "new_expression"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"member_expression"}),
    call_accessor_field="property",
    call_accessor_object_field="object",
    function_boundary_types=frozenset({"function_declaration", "generator_function_declaration", "arrow_function", "method_definition", "function_expression", "generator_function"}),
    import_handler=_import_js,
)

_TS_CONFIG = LanguageConfig(
    ts_module="tree_sitter_typescript",
    ts_language_fn="language_typescript",
    class_types=frozenset({
        "class_declaration",
        "abstract_class_declaration",
        "interface_declaration",
        "enum_declaration",
        "type_alias_declaration",
    }),
    function_types=frozenset({"function_declaration", "generator_function_declaration", "method_definition", "method_signature"}),
    import_types=frozenset({"import_statement", "export_statement"}),
    call_types=frozenset({"call_expression", "new_expression"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"member_expression"}),
    call_accessor_field="property",
    call_accessor_object_field="object",
    function_boundary_types=frozenset({"function_declaration", "generator_function_declaration", "arrow_function", "method_definition", "function_expression", "generator_function"}),
    import_handler=_import_js,
)

# Os arquivos .tsx devem usar a gramática TSX (com reconhecimento de JSX), não a gramática TypeScript simples.
# language_tsx (para .tsx). A análise de .tsx com language_typescript falha silenciosamente
# Expressões JSX, descartando qualquer call_expression aninhada dentro de JSX (por exemplo, {fmtDate(x)}).
_TSX_CONFIG = LanguageConfig(
    ts_module="tree_sitter_typescript",
    ts_language_fn="language_tsx",
    class_types=_TS_CONFIG.class_types,
    function_types=_TS_CONFIG.function_types,
    import_types=_TS_CONFIG.import_types,
    call_types=_TS_CONFIG.call_types,
    call_function_field=_TS_CONFIG.call_function_field,
    call_accessor_node_types=_TS_CONFIG.call_accessor_node_types,
    call_accessor_field=_TS_CONFIG.call_accessor_field,
    call_accessor_object_field=_TS_CONFIG.call_accessor_object_field,
    function_boundary_types=_TS_CONFIG.function_boundary_types,
    import_handler=_TS_CONFIG.import_handler,
)

_JAVA_CONFIG = LanguageConfig(
    ts_module="tree_sitter_java",
    # então ele se torna um nó de tipo de primeira classe em vez de um arquivo isolado.
    # Enums e declarações de anotação usam o mesmo contrato de nome/corpo.
    class_types=frozenset({
        "class_declaration", "interface_declaration", "record_declaration",
        "enum_declaration", "annotation_type_declaration",
    }),
    function_types=frozenset({"method_declaration", "constructor_declaration"}),
    import_types=frozenset({"import_declaration"}),
    # object_creation_expression (`new Foo(...)`) é tratado por um Java dedicado
    # branch em walk_calls abaixo - seu receptor está no campo `type`, não em `name`.
    call_types=frozenset({"method_invocation", "object_creation_expression"}),
    call_function_field="name",
    call_accessor_node_types=frozenset(),
    function_boundary_types=frozenset({"method_declaration", "constructor_declaration"}),
    import_handler=_import_java,
)

_GROOVY_CONFIG = LanguageConfig(
    ts_module="tree_sitter_groovy",
    class_types=frozenset({"class_declaration", "interface_declaration"}),
    function_types=frozenset({"method_declaration", "constructor_declaration"}),
    import_types=frozenset({"import_declaration"}),
    call_types=frozenset({"method_invocation"}),
    call_function_field="name",
    call_accessor_node_types=frozenset(),
    function_boundary_types=frozenset({"method_declaration", "constructor_declaration"}),
    import_handler=_import_java,
)

_C_CONFIG = LanguageConfig(
    ts_module="tree_sitter_c",
    class_types=frozenset(),
    function_types=frozenset({"function_definition"}),
    import_types=frozenset({"preproc_include"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"field_expression"}),
    call_accessor_field="field",
    function_boundary_types=frozenset({"function_definition"}),
    import_handler=_import_c,
    resolve_function_name_fn=_get_c_func_name,
)

_CPP_CONFIG = LanguageConfig(
    ts_module="tree_sitter_cpp",
    class_types=frozenset({"class_specifier", "struct_specifier"}),
    function_types=frozenset({"function_definition"}),
    import_types=frozenset({"preproc_include"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"field_expression", "qualified_identifier"}),
    call_accessor_field="field",
    function_boundary_types=frozenset({"function_definition"}),
    import_handler=_import_c,
    resolve_function_name_fn=_get_cpp_func_name,
)

_RUBY_CONFIG = LanguageConfig(
    ts_module="tree_sitter_ruby",
    # `module Foo` é um nó de contêiner assim como `class Foo` no tree-sitter's
    # Gramática Ruby (nome em um filho `constante`, corpo em `body_statement`), então
    # obtém um nó e seus métodos anexados via `método`. Sem isso, claro
    # módulos utilitário/`module_function` não produziram nenhum nó e seus métodos travaram
    # desligue o arquivo via `contém` com rótulos sem pontos.
    class_types=frozenset({"class", "module"}),
    function_types=frozenset({"method", "singleton_method"}),
    import_types=frozenset(),
    call_types=frozenset({"call"}),
    call_function_field="method",
    call_accessor_node_types=frozenset(),
    name_fallback_child_types=("constant", "scope_resolution", "identifier"),
    body_fallback_child_types=("body_statement",),
    function_boundary_types=frozenset({"method", "singleton_method"}),
)

_CSHARP_CONFIG = LanguageConfig(
    ts_module="tree_sitter_c_sharp",
    class_types=frozenset({
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "struct_declaration",
        "record_declaration",
    }),
    function_types=frozenset({"method_declaration"}),
    import_types=frozenset({"using_directive"}),
    call_types=frozenset({"invocation_expression"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"member_access_expression"}),
    call_accessor_field="name",
    body_fallback_child_types=("declaration_list",),
    function_boundary_types=frozenset({"method_declaration"}),
    import_handler=_import_csharp,
)

_KOTLIN_CONFIG = LanguageConfig(
    ts_module="tree_sitter_kotlin",
    class_types=frozenset({"class_declaration", "object_declaration"}),
    function_types=frozenset({"function_declaration"}),
    import_types=frozenset({"import_header", "import"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="",
    call_accessor_node_types=frozenset({"navigation_expression"}),
    call_accessor_field="",
    # nós de maneira diferente: `tree_sitter_kotlin` do PyPI usa `identifier`,
    name_fallback_child_types=("simple_identifier", "identifier"),
    body_fallback_child_types=("function_body", "class_body", "enum_class_body"),
    function_boundary_types=frozenset({"function_declaration"}),
    import_handler=_import_kotlin,
)

_SCALA_CONFIG = LanguageConfig(
    ts_module="tree_sitter_scala",
    class_types=frozenset({"class_definition", "object_definition"}),
    function_types=frozenset({"function_definition"}),
    import_types=frozenset({"import_declaration"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="",
    call_accessor_node_types=frozenset({"field_expression"}),
    call_accessor_field="field",
    name_fallback_child_types=("identifier",),
    body_fallback_child_types=("template_body",),
    function_boundary_types=frozenset({"function_definition"}),
    import_handler=_import_scala,
)

_PHP_CONFIG = LanguageConfig(
    ts_module="tree_sitter_php",
    ts_language_fn="language_php",
    class_types=frozenset({"class_declaration"}),
    function_types=frozenset({"function_definition", "method_declaration"}),
    import_types=frozenset({"namespace_use_clause"}),
    call_types=frozenset({"function_call_expression", "member_call_expression", "scoped_call_expression", "class_constant_access_expression"}),
    static_prop_types=frozenset({"scoped_property_access_expression"}),
    helper_fn_names=frozenset({"config"}),
    container_bind_methods=frozenset({"bind", "singleton", "scoped", "instance"}),
    event_listener_properties=frozenset({"listen", "subscribe"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"member_call_expression"}),
    call_accessor_field="name",
    name_fallback_child_types=("name",),
    body_fallback_child_types=("declaration_list", "compound_statement"),
    function_boundary_types=frozenset({"function_definition", "method_declaration"}),
    import_handler=_import_php,
)


def _import_lua(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None = None) -> None:
    """Extract require('module') from Lua variable_declaration nodes."""
    text = _read_text(node, source)
    import re
    m = re.search(r"""require\s*[\('"]\s*['"]?([^'")\s]+)""", text)
    if m:
        raw_module = m.group(1)
        if raw_module:
            tgt_nid = _resolve_lua_import_target(raw_module, str_path)
            if tgt_nid:
                edges.append({
                    "source": file_nid,
                    "target": tgt_nid,
                    "relation": "imports",
                    "context": "import",
                    "confidence": "EXTRACTED",
                    "confidence_score": 1.0,
                    "source_file": str_path,
                    "source_location": str(node.start_point[0] + 1),
                    "weight": 1.0,
                })


_LUA_CONFIG = LanguageConfig(
    ts_module="tree_sitter_lua",
    ts_language_fn="language",
    class_types=frozenset(),
    function_types=frozenset({"function_declaration"}),
    import_types=frozenset({"variable_declaration"}),
    call_types=frozenset({"function_call"}),
    call_function_field="name",
    call_accessor_node_types=frozenset({"method_index_expression"}),
    call_accessor_field="name",
    name_fallback_child_types=("identifier", "method_index_expression"),
    body_fallback_child_types=("block",),
    function_boundary_types=frozenset({"function_declaration"}),
    import_handler=_import_lua,
)


def _import_swift(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None = None) -> list[tuple[str, str]]:
    """Emit module-level ``imports`` edges and report the imported modules.

    A Swift ``import CoreKit`` names a module, not a file path, so — unlike the
    file-resolving JS/TS handlers — there is no existing node for the edge to
    point at. The returned ``(id, label)`` pairs let the extractor materialize a
    ``type=module`` anchor node so the edge survives; without it ``build_from_json``
    prunes every Swift import edge as a dangling/external reference (#1327).
    """
    modules: list[tuple[str, str]] = []
    for child in node.children:
        if child.type == "identifier":
            raw = _read_text(child, source)
            tgt_nid = _make_id(raw)
            edges.append({
                "source": file_nid,
                "target": tgt_nid,
                "relation": "imports",
                "context": "import",
                "confidence": "EXTRACTED",
                "source_file": str_path,
                "source_location": f"L{node.start_point[0] + 1}",
                "weight": 1.0,
            })
            modules.append((tgt_nid, raw))
            break
    return modules


_SWIFT_CONFIG = LanguageConfig(
    ts_module="tree_sitter_swift",
    class_types=frozenset({"class_declaration", "protocol_declaration"}),
    function_types=frozenset({"function_declaration", "init_declaration", "deinit_declaration", "subscript_declaration"}),
    import_types=frozenset({"import_declaration"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="",
    call_accessor_node_types=frozenset({"navigation_expression"}),
    call_accessor_field="",
    name_fallback_child_types=("simple_identifier", "type_identifier", "user_type"),
    body_fallback_child_types=("class_body", "protocol_body", "function_body", "enum_class_body"),
    function_boundary_types=frozenset({"function_declaration", "init_declaration", "deinit_declaration", "subscript_declaration"}),
    import_handler=_import_swift,
)

# ── Inferência de tipo local Ruby (para resolução de chamada de membro) ─────────────────────


# constante. tree-sitter analisa cada um como uma `atribuição`, não uma `classe`, então o





_RATIONALE_PREFIXES = ("# NOTE:", "# IMPORTANT:", "# HACK:", "# WHY:", "# RATIONALE:", "# TODO:", "# FIXME:")


def _shorten_rationale_label(text: str, width: int = 80) -> str:
    """Collapse whitespace and truncate ``text`` to ``width`` chars for a
    rationale node label, cutting on a word boundary rather than mid-word.
    Shared by the Python and JS/TS rationale extractors (#2206).

    ``textwrap.shorten`` collapses to just the placeholder when the first
    "word" alone exceeds ``width`` (e.g. a docstring/comment that opens with
    an unbroken URL) -- that would emit a content-free label, so fall back to
    a plain character truncation of the normalized text in that case.
    """
    label = textwrap.shorten(text, width=width, placeholder="…")
    if label in ("", "…"):
        flat = " ".join(text.split())
        label = flat if len(flat) <= width else flat[: width - 1] + "…"
    return label


def _is_autogenerated_python(source: bytes) -> bool:
    """Return True if this Python file is auto-generated and its module docstring is noise.

    Covers: Alembic/Flask-Migrate revisions, Django migrations, protobuf/gRPC/OpenAPI stubs.
    Module docstrings in these files are change annotations or boilerplate, not rationale.
    """
    head = source[:2048].decode("utf-8", errors="replace")
    if any(m in head for m in ("DO NOT EDIT", "@generated", "Generated by the protocol buffer")):
        return True
    if (re.search(r"^revision\s*[:=]", head, re.MULTILINE)
            and "def upgrade(" in head
            and "down_revision" in head):
        return True
    if "class Migration(migrations.Migration)" in head and "operations" in head:
        return True
    return False


def _extract_python_rationale(path: Path, result: dict) -> None:
    """Post-pass: extract docstrings and rationale comments from Python source.
    Mutates result in-place by appending to result['nodes'] and result['edges'].
    """
    try:
        import tree_sitter_python as tspython
        from tree_sitter import Language, Parser
        language = Language(tspython.language())
        parser = Parser(language)
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception:
        return

    stem = _file_stem(path)
    str_path = str(path)
    nodes = result["nodes"]
    edges = result["edges"]
    seen_ids = {n["id"] for n in nodes}
    file_nid = _make_id(str(path))

    def _get_docstring(body_node) -> tuple[str, int] | None:
        if not body_node:
            return None
        for child in body_node.children:
            if child.type == "expression_statement":
                for sub in child.children:
                    if sub.type in ("string", "concatenated_string"):
                        text = source[sub.start_byte:sub.end_byte].decode("utf-8", errors="replace")
                        text = text.strip("\"'").strip('"""').strip("'''").strip()
                        if len(text) > 20:
                            return text, child.start_point[0] + 1
            break
        return None

    def _add_rationale(text: str, line: int, parent_nid: str) -> None:
        label = _shorten_rationale_label(text)
        rid = _make_id(stem, "rationale", str(line))
        if rid not in seen_ids:
            seen_ids.add(rid)
            nodes.append({
                "id": rid,
                "label": label,
                "file_type": "rationale",
                "source_file": str_path,
                "source_location": f"L{line}",
            })
        edges.append({
            "source": rid,
            "target": parent_nid,
            "relation": "rationale_for",
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        })

    # Documentação em nível de módulo - pule para arquivos gerados automaticamente (Alambic, Django
    # migrações, stubs protobuf, etc.) cujos documentos do módulo são de revisão
    # anotações, não lógica arquitetônica.
    if not _is_autogenerated_python(source):
        ds = _get_docstring(root)
        if ds:
            _add_rationale(ds[0], ds[1], file_nid)

    # Documentos de classe e função
    def walk_docstrings(node, parent_nid: str) -> None:
        t = node.type
        if t == "class_definition":
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node and body:
                class_name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
                nid = _make_id(stem, class_name)
                ds = _get_docstring(body)
                if ds:
                    _add_rationale(ds[0], ds[1], nid)
                for child in body.children:
                    walk_docstrings(child, nid)
            return
        if t == "function_definition":
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node and body:
                func_name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
                nid = _make_id(parent_nid, func_name) if parent_nid != file_nid else _make_id(stem, func_name)
                ds = _get_docstring(body)
                if ds:
                    _add_rationale(ds[0], ds[1], nid)
            return
        for child in node.children:
            walk_docstrings(child, parent_nid)

    walk_docstrings(root, file_nid)

    # Comentários de justificativa (# NOTA:, # IMPORTANTE:, etc.)
    source_text = source.decode("utf-8", errors="replace")
    for lineno, line_text in enumerate(source_text.splitlines(), start=1):
        stripped = line_text.strip()
        if any(stripped.startswith(p) for p in _RATIONALE_PREFIXES):
            _add_rationale(stripped, lineno, file_nid)



def extract_python(path: Path) -> dict:
    """Extract classes, functions, and imports from a .py file via tree-sitter AST."""
    result = _extract_generic(path, _PYTHON_CONFIG)
    if "error" not in result:
        _extract_python_rationale(path, result)
    return result


def extract_js(path: Path) -> dict:
    """Extract classes, functions, arrow functions, and imports from a .js/.ts/.tsx/.mts/.cts file."""
    suffix = path.suffix.lower()
    if suffix == ".tsx":
        config = _TSX_CONFIG
    elif suffix in (".ts", ".mts", ".cts"):
        config = _TS_CONFIG
    else:
        config = _JS_CONFIG
    result = _extract_generic(path, config)
    if "error" not in result:
        _extract_js_rationale(path, result)
        _rescue_js_dynamic_imports(path, result)
    return result


def _rescue_js_dynamic_imports(path: Path, result: dict) -> None:
    """Recover ``import('…')`` edges the AST pass does not emit for plain JS/TS.

    tree-sitter models ``await import('x')`` as a ``call_expression``, not an
    ``import_statement``, so the specifier only reaches the graph when
    ``walk_calls`` visits that call — which it never does at module scope
    (only function bodies are walked for calls). The Svelte/Astro/Vue
    extractors already patch the same gap by regex because their AST pass
    fails wholesale; plain ``.ts``/``.js`` was left out on the reasoning that
    its AST pass "works". It works for STATIC imports; dynamic ones outside a
    walked body fell through silently (#2575), and because they cluster under
    hub modules the loss compounds with ``affected`` traversal depth.

    Dedupe: a dynamic import the AST pass DID capture is already in the graph
    as an ``imports_from`` edge marked ``deferred`` (``_dynamic_import_js``).
    Re-emitting it here as a second ``dynamic_import`` edge would state the
    same fact twice, so a match whose resolved target already has a deferred
    edge FROM THIS FILE'S NODE is skipped. The source check matters: the AST
    pass anchors the edge on the enclosing function when the ``import()`` is
    written inside one, and that is a different fact from "this file depends on
    that module" — the only one file-level traversal can use (#2584).

    Regex false positives in comments/strings are the precedented trade of
    the Svelte/Vue rescues; a ``//``-prefix guard covers the common case.
    """
    try:
        import re as _re
        src = path.read_text(encoding="utf-8", errors="replace")
        if "import(" not in src:
            return
        existing_ids = {n["id"] for n in result.get("nodes", [])}
        file_node_id = _make_id(str(path))
        aliases = _load_tsconfig_aliases(path.parent)
        base_url = _load_tsconfig_base_url(path.parent)
        deferred_ids: set[str] = set()
        deferred_files: set[str] = set()
        rescued_targets: set[str] = set()
        for e in result.get("edges", []):
            if (e.get("deferred") and e.get("relation") == "imports_from"
                    and e.get("source") == file_node_id):
                deferred_ids.add(e.get("target"))
                tf = e.get("target_file")
                if tf:
                    try:
                        deferred_files.add(str(Path(tf).resolve()))
                    except OSError:
                        deferred_files.add(str(tf))
        for m in _re.finditer(
            r"""(?<!\w)import\(\s*(?:'([^'\n]+)'|"([^"\n]+)"|`([^`$\n]+)`)\s*\)""",
            src,
        ):
            raw = m.group(1) or m.group(2) or m.group(3)
            if not raw:
                continue
            line_start = src.rfind("\n", 0, m.start()) + 1
            if "//" in src[line_start:m.start()]:
                continue
            resolution = _resolve_rescued_specifier(path, raw, aliases, base_url)
            if resolution is None:
                continue
            node_id, _stub_sf, resolved_file = resolution
            if node_id in deferred_ids or _make_id("ref", raw) in deferred_ids:
                continue
            if resolved_file is not None:
                try:
                    if str(resolved_file.resolve()) in deferred_files:
                        continue
                except OSError:
                    pass
            emit_key = str(resolved_file.resolve()) if resolved_file is not None else raw
            if emit_key in rescued_targets:
                continue
            rescued_targets.add(emit_key)
            _emit_rescued_import(
                result, existing_ids, file_node_id, path, raw,
                "dynamic_import", aliases, base_url,
            )
    except Exception:
        pass


# Paridade com _extract_python_rationale: arquivos Python obtêm nós de lógica de
# docstrings e comentários no estilo `# NOTE:`, mas os comentários JS/TS foram descartados
#   1. comentários de justificativa (`// NOTA:`, `// POR QUE:`, ...) - o mesmo que Python;
#   2. referências de decisão de arquitetura (`ADR-0011`, `RFC 793`) que equipes
#      citar convencionalmente em cabeçalhos de arquivos/funções. Estes são os naturais
#      juntar pontos entre códigos e documentos de design no mesmo grafo - sem
#      neles, as arestas do código<->ADR nunca se formam, mesmo quando o código cita o ADR.

_JS_RATIONALE_PREFIXES = (
    "// NOTE:", "// IMPORTANT:", "// HACK:", "// WHY:", "// RATIONALE:",
    "// TODO:", "// FIXME:",
    "* NOTE:", "* IMPORTANT:", "* HACK:", "* WHY:", "* RATIONALE:",
    "* TODO:", "* FIXME:",
)

# Tokens de referência a documentos que merecem virar nós do grafo. De propósito
# conservador: ADR-NNNN (registros de decisão de arquitetura, qualquer preenchimento com zeros)
_JS_DOC_REF_RE = re.compile(r"\b(ADR[- ]?\d{1,5}|RFC[- ]?\d{1,5})\b", re.IGNORECASE)

# Procure apenas referências de documentos dentro de comentários, não literais de string ou código.
_JS_COMMENT_LINE_RE = re.compile(r"^\s*(//|/\*|\*)")


def _extract_js_rationale(path: Path, result: dict) -> None:
    """Post-pass: extract rationale comments and doc references from JS/TS source.
    Mutates result in-place by appending to result['nodes'] and result['edges'].
    """
    try:
        source_text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return

    stem = _file_stem(path)
    str_path = str(path)
    nodes = result["nodes"]
    edges = result["edges"]
    seen_ids = {n["id"] for n in nodes}
    file_nid = _make_id(str(path))
    seen_doc_refs: set[str] = set()

    def _add_rationale(text: str, line: int) -> None:
        label = _shorten_rationale_label(text)
        rid = _make_id(stem, "rationale", str(line))
        if rid not in seen_ids:
            seen_ids.add(rid)
            nodes.append({
                "id": rid,
                "label": label,
                "file_type": "rationale",
                "source_file": str_path,
                "source_location": f"L{line}",
            })
        edges.append({
            "source": rid,
            "target": file_nid,
            "relation": "rationale_for",
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        })

    def _add_doc_ref(token: str, line: int) -> None:
        # Normalize a grafia "adr 11"/"ADR-0011" para um "ADR-0011" canônico
        # rótulo de estilo para que as referências ao mesmo documento sejam recolhidas para um nó.
        kind, num = re.match(r"([A-Za-z]+)[- ]?(\d+)", token).groups()
        kind = kind.upper()
        label = f"{kind}-{num.zfill(4)}" if kind == "ADR" else f"{kind}-{num}"
        if label in seen_doc_refs:
            return
        seen_doc_refs.add(label)
        rid = _make_id("docref", label)
        if rid not in seen_ids:
            seen_ids.add(rid)
            nodes.append({
                "id": rid,
                "label": label,
                "file_type": "doc_ref",
                "source_file": str_path,
                "source_location": f"L{line}",
            })
        edges.append({
            "source": file_nid,
            "target": rid,
            "relation": "cites",
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        })

    for lineno, line_text in enumerate(source_text.splitlines(), start=1):
        stripped = line_text.strip()
        if any(stripped.startswith(p) for p in _JS_RATIONALE_PREFIXES):
            _add_rationale(stripped.lstrip("/* "), lineno)
        if _JS_COMMENT_LINE_RE.match(line_text):
            for m in _JS_DOC_REF_RE.finditer(stripped):
                _add_doc_ref(m.group(1), lineno)


def _resolve_rescued_specifier(
    path: Path,
    raw: str,
    aliases,
    base_url,
) -> "tuple[str, str, Path | None] | None":
    """Resolve a regex-rescued import specifier the way ``_import_js`` does.

    Returns ``(node_id, stub_source_file, resolved_file)`` — ``resolved_file``
    is the target as a real on-disk file, or None when the specifier is
    external or dangling. Returns None when no target can be minted at all
    (empty bare-import segment). Split out of :func:`_emit_rescued_import` so
    :func:`_rescue_js_dynamic_imports` can resolve a match FIRST and skip
    specifiers the AST pass already emitted, without duplicating the
    resolution rules.
    """
    if raw.startswith("."):
        resolved = _resolve_js_module_path(
            Path(os.path.normpath(path.parent / raw))
        )
        resolved_file = resolved if resolved is not None and resolved.is_file() else None
        return _make_id(str(resolved)), str(resolved), resolved_file
    resolved_alias = _resolve_tsconfig_alias(raw, aliases, base_url=base_url)
    if resolved_alias is not None:
        resolved_alias = _resolve_js_module_path(resolved_alias)
        resolved_file = (resolved_alias if resolved_alias is not None
                         and resolved_alias.is_file() else None)
        return _make_id(str(resolved_alias)), str(resolved_alias), resolved_file
    # Importação simples/com escopo (node_modules) - use o último segmento;
    # build_from_json será descartado como externo se não existir nenhum nó correspondente.
    module_name = raw.split("/")[-1]
    if not module_name:
        return None
    return _make_id(module_name), raw, None


def _emit_rescued_import(
    result: dict,
    existing_ids: set,
    file_node_id: str,
    path: Path,
    raw: str,
    relation: str,
    aliases,
    base_url,
) -> None:
    """Shared edge/stub emit for the Svelte/Astro/Vue regex-rescue import passes.

    Resolves the specifier the same way ``_import_js`` does — relative paths and
    tsconfig aliases both go through :func:`_resolve_js_module_path` so
    extensionless specifiers probe real on-disk extensions (``../lib/content``
    -> ``content.ts``) instead of a naive ``.js``->``.ts`` suffix swap.

    When the resolved target is a real file on disk, mirror ``_import_js``:
    emit ONLY the edge, stamped with ``target_file``, and mint no stub node.
    The #2169 canonicalization loop in :func:`extract` reads the stamp and
    repoints the edge at the real file node's canonical id. Minting a stub
    here would carry an absolute-path-derived id when the input path is
    absolute — a ghost node (e.g. ``private_tmp_..._src_lib_content``)
    duplicating the real ``src_lib_content`` node and clobbering its label on
    dedupe (#2195). Stub nodes are still minted for unresolved specifiers
    (externals, not-yet-created files) so prior behavior is preserved.
    """
    resolution = _resolve_rescued_specifier(path, raw, aliases, base_url)
    if resolution is None:
        return
    node_id, stub_source_file, resolved_file = resolution
    edge = {
        "source": file_node_id, "target": node_id,
        "relation": relation, "confidence": "EXTRACTED",
        "source_file": str(path),
    }
    if resolved_file is not None:
        edge["target_file"] = str(resolved_file)
        result.setdefault("edges", []).append(edge)
        return
    if node_id in existing_ids:
        # O alvo da aresta já é um nó real - basta adicionar a aresta, não adicionar um nó.
        result.setdefault("edges", []).append(edge)
        return
    result.setdefault("nodes", []).append({
        "id": node_id, "label": raw,
        "file_type": "code", "source_file": stub_source_file,
        "confidence": "EXTRACTED",
    })
    result.setdefault("edges", []).append(edge)
    existing_ids.add(node_id)


def extract_svelte(path: Path) -> dict:
    """Extract imports from .svelte files: script-block via JS AST + template regex fallback.

    Tree-sitter only sees the <script> block. Svelte template syntax like
    {#await import('./X.svelte')} lives in the markup layer and is invisible
    to the JS parser, so a regex pass covers those dynamic imports.
    """
    result = _extract_generic(path, _JS_CONFIG)
    try:
        import re as _re
        src = path.read_text(encoding="utf-8", errors="replace")
        existing_ids = {n["id"] for n in result.get("nodes", [])}
        # O ID do nó do arquivo de origem deve corresponder àquele que _extract_generic cria:
        # _make_id(str(path)) - argumento único, sem prefixo radical. Caso contrário a fonte
        # endpoint é um nó fantasma e build_from_json elimina a aresta (# 701).
        file_node_id = _make_id(str(path))
        aliases = _load_tsconfig_aliases(path.parent)
        base_url = _load_tsconfig_base_url(path.parent)
        for m in _re.finditer(r"""import\(\s*['"]([^'"]+)['"]\s*\)""", src):
            raw = m.group(1)
            if not raw:
                continue
            _emit_rescued_import(
                result, existing_ids, file_node_id, path, raw,
                "dynamic_import", aliases, base_url,
            )
        # Importações estáticas dentro de blocos <script>. O analisador de árvore JS alimentado
        # o arquivo .svelte completo produz um nó ERROR de nível superior (marcação HTML
        # não é JS válido), então os nós import_statement nunca são alcançados e
        # as importações estáticas são descartadas silenciosamente. Regex sobre cada script
        script_re = _re.compile(
            r"<script\b[^>]*>([\s\S]*?)</script\s*>", _re.IGNORECASE
        )
        static_import_re = _re.compile(
            r"""import\s+(?:[^'"`;]+?\s+from\s+)?['"]([^'"]+)['"]"""
        )
        for script_match in script_re.finditer(src):
            script_body = script_match.group(1)
            for m in static_import_re.finditer(script_body):
                raw = m.group(1)
                if not raw:
                    continue
                _emit_rescued_import(
                    result, existing_ids, file_node_id, path, raw,
                    "imports_from", aliases, base_url,
                )
    except Exception:
        pass
    return result


def extract_astro(path: Path) -> dict:
    """Extract imports from .astro files: frontmatter (TS) + template regex fallback.

    Astro files start with a ``---\\n...\\n---`` frontmatter block of TypeScript
    setup code (where almost all imports live), followed by an HTML-with-expressions
    template body, and optionally ``<script>`` blocks for client-side JS. Tree-sitter
    only sees the file usefully through the frontmatter — feeding the whole file to
    the JS parser produces a top-level ERROR node because the template is not valid
    JS, so ``import_statement`` nodes are never reached and static imports are
    silently dropped (#850). Mirrors :func:`extract_svelte` — same regex-rescue
    approach, scanning the frontmatter block and any client-side ``<script>`` blocks
    for static and dynamic imports.
    """
    result = _extract_generic(path, _JS_CONFIG)
    try:
        import re as _re
        src = path.read_text(encoding="utf-8", errors="replace")
        existing_ids = {n["id"] for n in result.get("nodes", [])}
        file_node_id = _make_id(str(path))
        aliases = _load_tsconfig_aliases(path.parent)
        base_url = _load_tsconfig_base_url(path.parent)
        # Importações dinâmicas em qualquer lugar do arquivo: `import('./X.astro')` é legal em
        # código de configuração do frontmatter e dentro dos slots de expressão.
        for m in _re.finditer(r"""import\(\s*['"]([^'"]+)['"]\s*\)""", src):
            raw = m.group(1)
            if not raw:
                continue
            _emit_rescued_import(
                result, existing_ids, file_node_id, path, raw,
                "dynamic_import", aliases, base_url,
            )
        # Importações estáticas: verifique o frontmatter `---...---` no cabeçalho do arquivo mais qualquer
        # blocos <script> do lado do cliente. Ambas são regiões TS/JS, mas residem dentro de um arquivo
        # o analisador de árvore JS não pode validar como um todo.
        frontmatter_re = _re.compile(
            r"\A\s*---\s*\r?\n([\s\S]*?)\r?\n---\s*(?:\r?\n|\Z)"
        )
        script_re = _re.compile(
            r"<script\b[^>]*>([\s\S]*?)</script\s*>", _re.IGNORECASE
        )
        static_import_re = _re.compile(
            r"""import\s+(?:[^'"`;]+?\s+from\s+)?['"]([^'"]+)['"]"""
        )
        regions: list[str] = []
        fm = frontmatter_re.search(src)
        if fm:
            regions.append(fm.group(1))
        for script_match in script_re.finditer(src):
            regions.append(script_match.group(1))
        for region in regions:
            for m in static_import_re.finditer(region):
                raw = m.group(1)
                if not raw:
                    continue
                _emit_rescued_import(
                    result, existing_ids, file_node_id, path, raw,
                    "imports_from", aliases, base_url,
                )
    except Exception:
        pass
    return result


# O matcher de tag aberta ignora os valores dos atributos citados, então um `>` dentro de um
# generic="T extends Record<string, desconhecido>">`) não encerra prematuramente a tag.


def extract_vue(path: Path) -> dict:
    """Extract imports, symbols, and type refs from a ``.vue`` SFC.

    Masks the non-``<script>`` regions and parses the script with the grammar
    its ``lang`` implies (``tsx``→TSX, ``js``/``jsx``→JS, ``ts`` or unset→TS;
    TS is a superset of JS so it is a safe default). A regex pass then recovers
    ``import('…')`` dynamic imports the AST does not edge.
    """
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"nodes": [], "edges": []}

    masked, lang = _vue_mask_non_script(src)
    if lang == "tsx":
        config = _TSX_CONFIG
    elif lang in ("js", "jsx"):
        config = _JS_CONFIG
    else:  # "ts" ou não especificado — padrão para a gramática TS (superconjunto de JS)
        config = _TS_CONFIG

    result = _extract_generic(path, config, source_override=masked.encode("utf-8"))

    # Chamadas dinâmicas `import('…')` não são limitadas pela passagem AST; recuperar por regex,
    try:
        existing_ids = {n["id"] for n in result.get("nodes", [])}
        file_node_id = _make_id(str(path))
        aliases = _load_tsconfig_aliases(path.parent)
        base_url = _load_tsconfig_base_url(path.parent)
        for m in re.finditer(r"""import\(\s*['"]([^'"]+)['"]\s*\)""", src):
            raw = m.group(1)
            if not raw:
                continue
            _emit_rescued_import(
                result, existing_ids, file_node_id, path, raw,
                "dynamic_import", aliases, base_url,
            )
    except Exception:
        pass
    return result


def extract_java(path: Path) -> dict:
    """Extract classes, interfaces, methods, constructors, and imports from a .java file."""
    return _extract_generic(path, _JAVA_CONFIG)


def _is_spock_file(path: Path, ts_result: dict) -> bool:
    """Return True when the file contains Spock-style ``def "feature"()`` methods
    that tree-sitter-groovy cannot parse, detected by checking the raw source."""
    import re as _re
    _SPOCK_FEATURE_RE = _re.compile(r"""^\s*def\s+[\"']""", _re.MULTILINE)
    try:
        return bool(_SPOCK_FEATURE_RE.search(path.read_text(errors="replace")))
    except OSError:
        return False


def _extract_spock_fallback(path: Path, ts_result: dict) -> dict:
    """Regex-based fallback for Spock spec files where tree-sitter-groovy cannot parse
    ``def "feature name"()`` methods. Merges import edges from the tree-sitter pass
    (which survive reliably) with class and feature-method nodes extracted via regex.
    """
    import re as _re
    source = path.read_text(errors="replace")
    str_path = str(path)
    stem = _file_stem(path)

    # Apenas mantenha o nó do arquivo longe da passagem do tree-sitter (presente garantido e
    # corretamente identificados) além de todas as arestas de importação.  Todos os outros nós ts são descartados para
    # evite nós de método/construtor órfãos cujas arestas pai foram eliminadas.
    file_node = next((n for n in ts_result.get("nodes", []) if n.get("label") == path.name), None)
    nodes: list[dict] = [file_node] if file_node else []
    edges: list[dict] = [e for e in ts_result.get("edges", []) if e.get("context") == "import"]
    seen_ids: set[str] = {n["id"] for n in nodes}

    def _add_node(nid: str, label: str, line: int) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": label,
                "file_type": "code",
                "source_file": str_path,
                "source_location": f"L{line}",
            })

    def _add_edge(src: str, tgt: str, relation: str, line: int,
                  confidence: str = "EXTRACTED") -> None:
        edges.append({
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": confidence,
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        })

    lines_text = source.splitlines()

    class_re = _re.compile(r"^\s*(?:[\w@]+\s+)*class\s+(\w+)")
    # Extraia os métodos do recurso Spock: def "..." () ou def '...' ()
    # Dois grupos de captura separados por estilo de citação, com apóstrofos dentro
    # nomes entre aspas duplas (por exemplo, "não deveria") são capturados corretamente.
    feature_re = _re.compile(r"""^\s*def\s+(?:\"([^\"]+)\"|'([^']+)')\s*\(""")
    # Extraia métodos de definição simples (nomes sem string) também
    plain_method_re = _re.compile(r"""^\s*def\s+(\w+)\s*\(""")

    current_class_nid: str | None = None
    file_nid = _make_id(str_path)

    # Certifique-se de que o nó do arquivo exista (o passe do tree-sitter pode tê-lo emitido)
    if file_nid not in seen_ids:
        _add_node(file_nid, path.name, 1)

    for lineno, line_text in enumerate(lines_text, start=1):
        cm = class_re.match(line_text)
        if cm:
            class_name = cm.group(1)
            class_nid = _make_id(stem, class_name)
            _add_node(class_nid, class_name, lineno)
            _add_edge(file_nid, class_nid, "contains", lineno)
            current_class_nid = class_nid
            continue

        if current_class_nid is None:
            continue

        fm = feature_re.match(line_text)
        if fm:
            method_name = fm.group(1) or fm.group(2)
            method_label = f'"{method_name}"'
            method_nid = _make_id(current_class_nid, method_name)
            _add_node(method_nid, method_label, lineno)
            _add_edge(current_class_nid, method_nid, "method", lineno)
            continue

        pm = plain_method_re.match(line_text)
        if pm:
            method_name = pm.group(1)
            if method_name not in ("if", "while", "for", "switch", "catch"):
                method_label = f".{method_name}()"
                method_nid = _make_id(current_class_nid, method_name)
                _add_node(method_nid, method_label, lineno)
                _add_edge(current_class_nid, method_nid, "method", lineno)

    return {"nodes": nodes, "edges": edges}


def extract_groovy(path: Path) -> dict:
    """Extract classes, methods, constructors, and imports from a .groovy/.gradle file.

    Falls back to a regex-based Spock extractor when tree-sitter-groovy cannot parse
    ``def "feature name"()`` methods (common in Spock specification classes).
    """
    result = _extract_generic(path, _GROOVY_CONFIG)
    if _is_spock_file(path, result):
        result = _extract_spock_fallback(path, result)
    return result


def extract_c(path: Path) -> dict:
    """Extract functions and includes from a .c/.h file."""
    return _extract_generic(path, _C_CONFIG)


_CPP_STRING_TEST_MACROS = (
    "TEST_CASE", "TEST_CASE_TEMPLATE", "SCENARIO",
)
_CPP_STRING_TEST_RE = re.compile(
    r'^[ \t]*(?:' + "|".join(_CPP_STRING_TEST_MACROS) + r')\s*\(\s*"((?:[^"\\]|\\.)+)"',
    re.MULTILINE,
)


def _augment_cpp_string_tests(path: Path, result: dict) -> dict:
    """Append callable nodes for doctest/Catch2 string-named test cases that
    tree-sitter-cpp drops as ERROR nodes (issue #2594).

    The generic C++ pass still recovers the surrounding functions and include
    edges reliably, so this only adds the missing ``TEST_CASE("...")`` nodes and
    their ``contains`` edge from the file node — it does not rebuild the result.

    Matching is line-anchored raw text, mirroring the Spock fallback above; it is
    deliberately not comment/preprocessor aware (a ``TEST_CASE`` disabled behind
    a block comment or ``#if 0`` may still surface as a node, exactly as a
    commented Spock ``def "feature"()`` would).
    """
    try:
        source = path.read_text(errors="replace")
    except OSError:
        return result
    matches = list(_CPP_STRING_TEST_RE.finditer(source))
    if not matches:
        return result

    str_path = str(path)
    stem = _file_stem(path)
    file_nid = _make_id(str_path)
    stem_collapse_id = _make_id(stem)
    nodes = result.setdefault("nodes", [])
    edges = result.setdefault("edges", [])
    seen_ids = {n.get("id") for n in nodes}

    for m in matches:
        test_name = m.group(1)
        line = source.count("\n", 0, m.start()) + 1
        test_nid = _make_id(stem, test_name)
        if test_nid == stem_collapse_id:
            test_nid = _make_id(stem, "test", f"L{line}")
        if test_nid in seen_ids:
            continue
        seen_ids.add(test_nid)
        nodes.append({
            "id": test_nid,
            "label": f'"{test_name}"',
            "file_type": "code",
            "source_file": str_path,
            "source_location": f"L{line}",
        })
        edges.append({
            "source": file_nid,
            "target": test_nid,
            "relation": "contains",
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        })
    return result


def extract_cpp(path: Path) -> dict:
    """Extract functions, classes, and includes from a .cpp/.cc/.cxx/.hpp file.

    Recovers doctest/Catch2 ``TEST_CASE("name")`` test cases that tree-sitter-cpp
    drops as ERROR nodes (issue #2594), mirroring the Spock fallback for Groovy.
    """
    result = _extract_generic(path, _CPP_CONFIG)
    return _augment_cpp_string_tests(path, result)


def extract_ruby(path: Path) -> dict:
    """Extract classes, methods, singleton methods, and calls from a .rb file."""
    return _extract_generic(path, _RUBY_CONFIG)


def extract_csharp(path: Path) -> dict:
    """Extract C# type declarations, methods, namespaces, and usings from a .cs file."""
    return _extract_generic(path, _CSHARP_CONFIG)


def extract_kotlin(path: Path) -> dict:
    """Extract classes, objects, functions, and imports from a .kt/.kts file."""
    return _extract_generic(path, _KOTLIN_CONFIG)


def extract_scala(path: Path) -> dict:
    """Extract classes, objects, functions, and imports from a .scala file."""
    return _extract_generic(path, _SCALA_CONFIG)


def extract_php(path: Path) -> dict:
    """Extract classes, functions, methods, namespace uses, and calls from a .php file."""
    return _extract_generic(path, _PHP_CONFIG)


# Um nível de parênteses balanceados (por exemplo, `Foo #(Bar #(int))`) - limitado de forma malformada


def extract_lua(path: Path) -> dict:
    """Extract functions, methods, require() imports, and calls from a .lua file."""
    return _extract_generic(path, _LUA_CONFIG)


def extract_swift(path: Path) -> dict:
    """Extract classes, structs, protocols, functions, imports, and calls from a .swift file."""
    return _extract_generic(path, _SWIFT_CONFIG)







# Nomes comuns de métodos Rust/stdlib que aparecem em praticamente todas as bases de código.
# A resolução desses arquivos cruzados produz arestas INFERIDAS espúrias na caixa
# limites (problema nº 908) - ignore-os totalmente da fila de chamadas não resolvidas.







# Chaves em um .psd1 cujos valores são nomes/caminhos de módulos que tratamos como importações.




def _canonicalize_csharp_namespace_nodes(all_nodes: list[dict], all_edges: list[dict]) -> None:
    """Collapse duplicate C# namespace node entries to one canonical node per label."""
    by_label: dict[str, list[dict]] = {}
    for node in all_nodes:
        if node.get("type") != "namespace":
            continue
        label = node.get("label")
        if isinstance(label, str):
            by_label.setdefault(label, []).append(node)

    remap: dict[str, str] = {}
    drop_node_ids: set[int] = set()
    for group in by_label.values():
        if len(group) < 2:
            continue
        canonical = sorted(
            group,
            key=lambda node: (
                str(node.get("source_file") or ""),
                str(node.get("source_location") or ""),
                str(node.get("id") or ""),
            ),
        )[0]
        canonical_id = canonical.get("id")
        for node in group:
            if node is canonical:
                continue
            drop_node_ids.add(id(node))
            dup_id = node.get("id")
            if isinstance(dup_id, str) and isinstance(canonical_id, str):
                remap[dup_id] = canonical_id

    if remap:
        for edge in all_edges:
            if edge.get("source") in remap:
                edge["source"] = remap[str(edge["source"])]
            if edge.get("target") in remap:
                edge["target"] = remap[str(edge["target"])]

    if drop_node_ids:
        all_nodes[:] = [node for node in all_nodes if id(node) not in drop_node_ids]


# Idiomas cujos identificadores não diferenciam maiúsculas de minúsculas, portanto, resolução de nomes entre arquivos
# pode dobrar a caixa. Em todos os outros lugares, case é semântico (`Path` a classe vs `PATH` a
# env var são distintos) e dobrar produz arestas falsas/super-hubs.
_CASE_INSENSITIVE_EXTS = frozenset({
    ".php", ".phtml", ".php3", ".php4", ".php5", ".php7", ".phps",
    ".sql",
    ".nim", ".nims", ".nimble",
})


def _lang_is_case_insensitive(source_file: object) -> bool:
    """True when the file's language resolves identifiers case-insensitively (#1581)."""
    if not source_file:
        return False
    return Path(str(source_file)).suffix.lower() in _CASE_INSENSITIVE_EXTS


# Famílias de interoperabilidade de linguagem para resolução de chamadas entre arquivos. Uma chamada em um idioma
# nunca pode ser vinculado por nome a uma definição em outra família - um componente TSX não
# não invoca um método Kotlin e uma função Python não invoca um método Java.
# As famílias são agrupadas por interoperabilidade REAL, para que a resolução entre idiomas seja legítima
# compartilhar cabeçalhos e símbolos (pontes Swift para Objective-C) e variantes JS/TS
# Extensões ausentes neste mapa (documentos, configurações, idiomas desconhecidos) resolvem para
# não têm família e nunca são filtrados – o mesmo padrão permissivo de antes.
_LANG_FAMILY_BY_EXT: dict[str, str] = {
    ".js": "jsts", ".jsx": "jsts", ".mjs": "jsts", ".cjs": "jsts",
    ".ts": "jsts", ".tsx": "jsts", ".mts": "jsts", ".cts": "jsts",
    ".vue": "jsts", ".svelte": "jsts", ".astro": "jsts",
    ".java": "jvm", ".kt": "jvm", ".kts": "jvm",
    ".scala": "jvm", ".groovy": "jvm", ".gradle": "jvm",
    ".c": "native", ".h": "native", ".cpp": "native", ".cc": "native",
    ".cxx": "native", ".hpp": "native", ".cu": "native", ".cuh": "native",
    ".metal": "native", ".m": "native", ".mm": "native", ".swift": "native",
    ".py": "python",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby", ".rake": "ruby",
    ".php": "php", ".phtml": "php", ".php3": "php", ".php4": "php",
    ".php5": "php", ".php7": "php", ".phps": "php",
    ".cs": "dotnet", ".razor": "dotnet", ".cshtml": "dotnet", ".xaml": "dotnet",
    ".lua": "lua", ".luau": "lua",
    ".zig": "zig",
    ".ex": "elixir", ".exs": "elixir",
    ".jl": "julia",
    ".dart": "dart",
    ".sh": "shell", ".bash": "shell",
    ".ps1": "powershell", ".psm1": "powershell", ".psd1": "powershell",
}


def _lang_family(source_file: object) -> str | None:
    """Interop family of the file's language, or None when unknown/not code."""
    if not source_file:
        return None
    return _LANG_FAMILY_BY_EXT.get(Path(str(source_file)).suffix.lower())


def _node_label_key(node: dict, fold: bool = False) -> str:
    label = str(node.get("label", "")).strip()
    key = re.sub(r"[^a-zA-Z0-9]+", "", label)
    return key.lower() if fold else key


def _is_top_level_function_definition(node: dict) -> bool:
    """A free/top-level function def (label ``name()``), not a method or type.

    Methods carry a leading dot (``.foo()``) or a qualifier (``Class.foo()``);
    excluding those keeps a bare-name reference from binding to a receiver-scoped
    method, which the receiver-typed resolvers own (#1781).
    """
    label = str(node.get("label", "")).strip()
    return (
        node.get("file_type") == "code"
        and label.endswith(")")
        and not label.startswith(".")
        and "." not in label
    )


def _rewire_unique_stub_nodes(nodes: list[dict], edges: list[dict]) -> None:
    """Map unresolved no-source stubs to a unique real definition with the same label."""
    real_by_label: dict[str, list[dict]] = {}
    real_by_label_ci: dict[str, list[dict]] = {}
    func_by_label: dict[str, list[dict]] = {}
    stubs: list[dict] = []

    for node in nodes:
        key = _node_label_key(node)
        if not key:
            continue
        if node.get("source_file"):
            if _is_type_like_definition(node):
                # Match stubs com distinção entre maiúsculas e minúsculas: uma referência `Path` não deve ser reconectada a um
                # `PATH` env var. Dobre apenas para genuinamente insensível a maiúsculas e minúsculas
                real_by_label.setdefault(key, []).append(node)
                if _lang_is_case_insensitive(node.get("source_file")):
                    real_by_label_ci.setdefault(
                        _node_label_key(node, fold=True), []).append(node)
            elif _is_top_level_function_definition(node):
                func_by_label.setdefault(key, []).append(node)
            continue
        stubs.append(node)

    # Famílias de idiomas que fazem referência a cada stub, para o protetor de mesclagem de funções:
    # uma aresta de `referências` de módulo cruzado para uma função usada para oscilar em um sem fonte
    # stub somente nome porque as funções foram excluídas como destinos de religação. Nós agora permitimos
    # uma definição de função ÚNICA para absorvê-la, mas somente quando ela compartilha uma linguagem
    # família com os referenciadores do stub - portanto, uma referência `get_db` do Python não pode ser vinculada a
    stub_ids = {str(s.get("id")) for s in stubs if s.get("id")}
    stub_families: dict[str, set] = {}
    supertype_stub_ids: set[str] = set()  # stubs usados ​​​​como tipo base - nunca uma função
    _SUPERTYPE_RELATIONS = {"inherits", "implements", "extends"}
    for edge in edges:
        rel = edge.get("relation")
        for endpoint in ("source", "target"):
            nid = edge.get(endpoint)
            if nid in stub_ids:
                fam = _lang_family(edge.get("source_file"))
                if fam is not None:
                    stub_families.setdefault(str(nid), set()).add(fam)
                # não é uma função com o mesmo nome (você não herda de uma função).
                if endpoint == "target" and rel in _SUPERTYPE_RELATIONS:
                    supertype_stub_ids.add(str(nid))

    remap: dict[str, str] = {}
    for stub in stubs:
        stub_id = str(stub.get("id", ""))
        if not stub_id:
            continue
        candidates = real_by_label.get(_node_label_key(stub), [])
        if len(candidates) != 1:
            # Nenhuma correspondência de tipo exato exclusiva — volte para uma correspondência que não diferencia maiúsculas de minúsculas, mas
            # apenas contra definições de linguagem que não diferenciam maiúsculas de minúsculas (portanto, uma distinção entre maiúsculas e minúsculas
            # `PATH` nunca pode absorver uma referência `Path`).
            candidates = real_by_label_ci.get(_node_label_key(stub, fold=True), [])
        if len(candidates) != 1:
            # nenhum tipo exclusivo — tente uma definição FUNCTION exclusiva de nível superior,
            # fechado por (a) o stub não sendo usado como um supertipo e (b) um
            # correspondência da família de idiomas com os referenciadores do stub.
            fcands = func_by_label.get(_node_label_key(stub), [])
            if len(fcands) == 1 and stub_id not in supertype_stub_ids:
                fams = stub_families.get(stub_id, set())
                cand_fam = _lang_family(fcands[0].get("source_file"))
                if not fams or cand_fam is None or cand_fam in fams:
                    candidates = fcands
        if len(candidates) != 1:
            continue
        target_id = candidates[0].get("id")
        if isinstance(target_id, str) and target_id and target_id != stub_id:
            remap[stub_id] = target_id

    if not remap:
        return

    by_id = {node.get("id"): node for node in nodes if node.get("id")}
    csharp_scoped_relations = {"inherits", "implements", "references", "imports"}
    for edge in edges:
        is_csharp_scoped_edge = (
            str(edge.get("source_file", "")).endswith(".cs")
            and edge.get("relation") in csharp_scoped_relations
        )
        source = edge.get("source")
        if source in remap:
            remapped_source = remap[str(source)]
            if not (
                is_csharp_scoped_edge
                and str(by_id.get(remapped_source, {}).get("source_file", "")).endswith(".cs")
            ):
                edge["source"] = remapped_source
        target = edge.get("target")
        if target in remap:
            remapped_target = remap[str(target)]
            if not (
                is_csharp_scoped_edge
                and str(by_id.get(remapped_target, {}).get("source_file", "")).endswith(".cs")
            ):
                edge["target"] = remapped_target

    referenced = {x for e in edges for x in (e.get("source"), e.get("target"))}
    drop_ids = {stub_id for stub_id in remap if stub_id not in referenced}
    nodes[:] = [node for node in nodes if node.get("id") not in drop_ids]


def _augment_js_reexport_edges(
    paths: list[Path],
    nodes: list[dict],
    edges: list[dict],
    root: Path,
) -> None:
    """Compatibility wrapper for the JS/TS symbol-resolution post-pass."""
    facts = _SymbolResolutionFacts()
    _collect_js_symbol_resolution_facts(paths, facts)
    _apply_symbol_resolution_facts(paths, nodes, edges, root, facts)


# Emparelhamento de extensão de arquivo de cabeçalho/implementação para a mesclagem de classe decl/def.


def _merge_swift_extensions(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Collapse cross-file Swift `extension Foo` nodes into the canonical `Foo`.

    tree-sitter-swift reuses `class_declaration` for both `class Foo` and
    `extension Foo`, and node ids carry the file stem, so each file that
    extends `Foo` produces its own `Foo` node. The match is done by label:
    when exactly one non-extension declaration shares the label, extension
    nodes redirect onto it. Extensions of types outside the corpus (no match)
    and ambiguous labels (more than one match) are left untouched — picking
    arbitrarily would invent edges.
    """
    extension_nids: set[str] = set()
    extension_labels: dict[str, str] = {}
    for result in per_file:
        for ext in result.get("swift_extensions", []) or []:
            extension_nids.add(ext["nid"])
            extension_labels[ext["nid"]] = ext["label"]

    if not extension_nids:
        return

    contained = {e.get("target") for e in all_edges if e.get("relation") == "contains"}

    label_to_canonical: dict[str, list[str]] = {}
    for n in all_nodes:
        if n.get("id") in extension_nids:
            continue
        label = n.get("label")
        if not label:
            continue
        if _lang_family(n.get("source_file")) != "native":
            continue
        if label in _LANGUAGE_BUILTIN_GLOBALS:
            continue
        if not (n.get("source_file") and n.get("id") in contained and _is_type_like_definition(n)):
            continue
        label_to_canonical.setdefault(label, []).append(n["id"])

    remap: dict[str, str] = {}
    for ext_nid in extension_nids:
        candidates = label_to_canonical.get(extension_labels[ext_nid], [])
        if len(candidates) != 1:
            continue
        canonical_nid = candidates[0]
        if canonical_nid != ext_nid:
            remap[ext_nid] = canonical_nid

    if not remap:
        return

    all_nodes[:] = [n for n in all_nodes if n.get("id") not in remap]

    # A aresta `contém` de cada arquivo de extensão acaba apontando para o canônico
    # type – vários arquivos contendo o mesmo nó têm a forma pretendida:
    # o tipo possui os métodos, os arquivos possuem sua fatia. Os auto-loops são
    # descartado (por exemplo, um método de extensão no arquivo cuja chamada já apontava para
    # o tipo canônico).
    def _key_of(e: dict, src: str, tgt: str) -> tuple:
        return (src, tgt, e.get("relation"), e.get("source_file"), e.get("source_location"))

    rewritten: list[dict] = []
    seen_keys: set[tuple] = set()
    for e in all_edges:
        src0, tgt0 = e.get("source"), e.get("target")
        src = remap.get(src0, src0)
        tgt = remap.get(tgt0, tgt0)
        if src == src0 and tgt == tgt0:
            seen_keys.add(_key_of(e, src0, tgt0))
            rewritten.append(e)
            continue
        if src == tgt:
            continue
        e["source"] = src
        e["target"] = tgt
        key = _key_of(e, src, tgt)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        rewritten.append(e)
    all_edges[:] = rewritten


def _merge_csharp_partial_class_nodes(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
    paths: list[Path],
    root: Path,
) -> None:
    """Collapse C# `partial class Foo` halves split across files into ONE node
    (#2332), without crossing assembly boundaries (#2411).

    The per-file extractor mints class ids with the file stem, so each file
    declaring `partial class Foo` produces its own `Foo` node: members split
    across the halves and cross-half calls don't resolve (two candidate types
    make every receiver-typed lookup bail as ambiguous). Group partial-stamped
    type nodes by (assembly, namespace, label) — same-named types in different
    namespaces are distinct types, non-partial same-named types are separate
    declarations, and nested partials are excluded (their ids omit the
    enclosing type, so a same-named nested pair under different outers would
    falsely merge). The `partial` keyword only fuses declarations compiled into
    the SAME assembly, so the key also carries the nearest ancestor directory
    holding a `*.csproj`/`*.fsproj`/`*.vbproj` — same-named halves under
    different project dirs are genuinely distinct types and stay apart. Halves
    with NO project file on any ancestor (up to the scan root) all key to ""
    and still merge together, so single-project/snippet corpora behave exactly
    as before; the probe runs only for groups that are otherwise ambiguous.
    The canonical node is the sorted-first half by (source_file,
    source_location, id); every edge endpoint and raw-call caller is remapped
    onto it. Member node ids are left untouched — only the class-level nodes
    collapse.

    Must run BEFORE _disambiguate_colliding_node_ids / _rewire_unique_stub_nodes /
    _resolve_csharp_type_references and the resolver registry, so every later
    pass sees one definition per partial type.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for n in all_nodes:
        if not str(n.get("source_file", "")).endswith(".cs"):
            continue
        if n.get("file_type") != "code":
            continue
        md = n.get("metadata") or {}
        if not md.get("is_partial") or md.get("is_nested_type"):
            continue
        label = n.get("label")
        if not label:
            continue
        groups.setdefault((str(md.get("namespace", "")), str(label)), []).append(n)

    if not any(len(members) >= 2 for members in groups.values()):
        return

    nid_to_path: dict[str, Path] = {}
    for result, path in zip(per_file, paths):
        for pn in result.get("nodes") or []:
            nid_to_path.setdefault(pn["id"], path)

    proj_exts = (".csproj", ".fsproj", ".vbproj")
    project_dirs: set[Path] = set()
    for p in paths:
        if p.suffix.lower() in proj_exts:
            try:
                project_dirs.add(p.resolve().parent)
            except OSError:
                pass
    try:
        stop = root.resolve()
    except OSError:
        stop = root
    dir_assembly: dict[Path, str] = {}

    def _assembly_of_dir(d: Path) -> str:
        """Nearest ancestor dir (self included) holding a project file, "" if
        none up to the scan root; memoized along the walked chain."""
        chain: list[Path] = []
        key = ""
        while True:
            cached = dir_assembly.get(d)
            if cached is not None:
                key = cached
                break
            chain.append(d)
            if d in project_dirs:
                key = str(d)
                break
            try:
                has_project = any(
                    c.suffix.lower() in proj_exts for c in d.iterdir()
                )
            except OSError:
                has_project = False
            if has_project:
                key = str(d)
                break
            if d == stop or d.parent == d:
                break
            d = d.parent
        for c in chain:
            dir_assembly[c] = key
        return key

    def _assembly_of_node(nid: str) -> str:
        path = nid_to_path.get(nid)
        if path is None:
            return ""
        try:
            d = path.resolve().parent
        except OSError:
            return ""
        return _assembly_of_dir(d)

    remap: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        by_assembly: dict[str, list[dict]] = {}
        for n in members:
            by_assembly.setdefault(_assembly_of_node(n["id"]), []).append(n)
        for halves in by_assembly.values():
            if len(halves) < 2:
                continue
            halves.sort(key=lambda n: (
                str(n.get("source_file", "")),
                str(n.get("source_location", "")),
                str(n.get("id", "")),
            ))
            canonical_nid = halves[0]["id"]
            for other in halves[1:]:
                if other["id"] != canonical_nid:
                    remap[other["id"]] = canonical_nid

    if not remap:
        return

    all_nodes[:] = [n for n in all_nodes if n.get("id") not in remap]

    rewritten: list[dict] = []
    seen_keys: set[tuple] = set()
    for e in all_edges:
        src = remap.get(e.get("source"), e.get("source"))
        tgt = remap.get(e.get("target"), e.get("target"))
        if src == tgt:
            continue
        e["source"] = src
        e["target"] = tgt
        key = (src, tgt, e.get("relation"), e.get("source_file"), e.get("source_location"))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        rewritten.append(e)
    all_edges[:] = rewritten

    for result in per_file:
        for rc in result.get("raw_calls", []) or []:
            cn = rc.get("caller_nid")
            if cn in remap:
                rc["caller_nid"] = remap[cn]


def _resolve_swift_member_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve cross-file Swift member calls (``recv.method()``) to the real
    definition of the receiver's type (#1356).

    The shared cross-file call pass drops every ``is_member_call`` because a bare
    method name (``update``) collides across the corpus and inflates god-nodes
    (#543/#1219). Swift extractors record the receiver of each member call and a
    per-file ``name -> type`` table (``swift_type_table``); this pass uses them to
    type the receiver, then emits an edge ONLY when that type name resolves to
    exactly one definition. A type-qualified call (``Type.staticMethod()``) is
    EXTRACTED (the type is named explicitly in source); an instance call typed via
    local inference (``obj.method()``) is INFERRED. The shared-pass member-call drop
    stays intact: this is purely additive and fires only on receiver-typed Swift calls.

    Must run after id-disambiguation so node ids and caller_nids are final.
    """
    type_table_by_file: dict[str, dict[str, str]] = {}
    for result in per_file:
        tt = result.get("swift_type_table")
        if tt and tt.get("path"):
            type_table_by_file[tt["path"]] = tt.get("table", {})
    if not type_table_by_file:
        return

    def _key(label: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]+", "", str(label)).lower()

    # Um tipo Swift genuíno é o alvo de uma aresta `contém` de seu nó de arquivo.
    # Referências de tipo simples criam um nó de sombra com o mesmo rótulo (via ensure_named_node)
    # que carrega um source_file mas NÃO está contido; excluindo não contido
    # nós evita que essa sombra faça com que um nome de tipo real pareça ambíguo.
    contained = {e.get("target") for e in all_edges if e.get("relation") == "contains"}

    # Nome do tipo -> IDs do nó de definição (somente definições reais, baseadas na fonte e semelhantes a tipo).
    # len != 1 é o guarda do god node: um nome de tipo ambíguo bails.
    type_def_nids: dict[str, list[str]] = {}
    node_by_id: dict[str, dict] = {}
    for n in all_nodes:
        node_by_id[n.get("id")] = n
        if n.get("source_file") and n.get("id") in contained and _is_type_like_definition(n):
            type_def_nids.setdefault(_key(n.get("label", "")), []).append(n["id"])

    # (type_node_id, method_key) -> method_node_id, das arestas do `método`.
    method_index: dict[tuple[str, str], str] = {}
    for e in all_edges:
        if e.get("relation") != "method":
            continue
        src, tgt = e.get("source"), e.get("target")
        tnode = node_by_id.get(tgt)
        if tnode is not None:
            method_index[(src, _key(tnode.get("label", "")))] = tgt

    factory_by_file: dict[str, dict] = {}
    for result in per_file:
        tt = result.get("swift_type_table")
        if tt and tt.get("path") and tt.get("factory"):
            factory_by_file[tt["path"]] = tt["factory"]
    if factory_by_file:
        return_targets_by_method: dict[str, set[str]] = {}
        for e in all_edges:
            if (e.get("relation") == "references"
                    and e.get("context") == "return_type"
                    and (e.get("metadata") or {}).get("swift_plain_return")):
                return_targets_by_method.setdefault(
                    e.get("source"), set()).add(e.get("target"))
        for path, pending in factory_by_file.items():
            table = dict(type_table_by_file.get(path, {}))
            type_table_by_file[path] = table
            for receiver, bind in pending.items():
                try:
                    factory_type, factory_method = bind
                except (TypeError, ValueError):
                    continue
                if factory_type in _LANGUAGE_BUILTIN_GLOBALS:
                    continue
                factory_defs = type_def_nids.get(_key(factory_type), [])
                if len(factory_defs) != 1:
                    continue
                method_nid = method_index.get((factory_defs[0], _key(factory_method)))
                if method_nid is None:
                    continue
                targets = return_targets_by_method.get(method_nid, set())
                if len(targets) != 1:
                    continue
                tnode = node_by_id.get(next(iter(targets)))
                ret_label = str(tnode.get("label", "")) if tnode else ""
                if not ret_label or ret_label in _LANGUAGE_BUILTIN_GLOBALS:
                    continue
                if len(type_def_nids.get(_key(ret_label), [])) != 1:
                    continue
                table.setdefault(receiver, ret_label)

    all_raw_calls: list[dict] = []
    for result in per_file:
        all_raw_calls.extend(result.get("raw_calls", []))

    existing_pairs = {(e.get("source"), e.get("target")) for e in all_edges}
    for rc in all_raw_calls:
        if not rc.get("is_member_call"):
            continue
        receiver = rc.get("receiver")
        callee = rc.get("callee")
        if not receiver or not callee:
            continue
        # Determine o tipo do receptor. Um receptor maiúsculo é em si um tipo
        # (Type.staticMethod(), Singleton.shared.x()); caso contrário, procure no
        if receiver[:1].isupper():
            type_name = receiver
            type_qualified = True
        else:
            type_name = type_table_by_file.get(rc.get("source_file", ""), {}).get(receiver)
            type_qualified = False
        if not type_name:
            continue
        if type_name in _LANGUAGE_BUILTIN_GLOBALS:
            continue
        type_defs = type_def_nids.get(_key(type_name), [])
        if len(type_defs) != 1:  # ambíguo ou ausente -> fiança (guarda do god node)
            continue
        type_nid = type_defs[0]
        caller = rc.get("caller_nid")
        if not caller:
            continue
        method_nid = method_index.get((type_nid, _key(callee)))
        target = method_nid or type_nid
        relation = "calls" if method_nid else "references"
        if target == caller or (caller, target) in existing_pairs:
            continue
        existing_pairs.add((caller, target))
        # explicitamente na fonte, portanto é uma referência exata - EXTRACTED, correspondente
        # a passagem do método de classe qualificada Python. Uma chamada de instância cujo
        # o tipo de receptor veio da inferência local (`obj.method()`) permanece INFERRED.
        all_edges.append({
            "source": caller,
            "target": target,
            "relation": relation,
            "context": "call",
            "confidence": "EXTRACTED" if type_qualified else "INFERRED",
            "confidence_score": 1.0 if type_qualified else 0.8,
            "source_file": rc.get("source_file", ""),
            "source_location": rc.get("source_location"),
            "weight": 1.0,
        })


def _resolve_python_member_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve cross-file Python qualified class-method calls (``ClassName.method()``)
    to the class-qualified method node (#1446).

    The shared cross-file call pass drops every ``is_member_call`` because a bare
    method name (``log``) collides across the corpus and inflates god-nodes
    (#543/#1219). That guard is right for *instance* calls (``obj.method()``) but
    misses *class-qualified* calls (``ClassName.method()``), where the receiver is
    an explicitly-named class — an exact, unambiguous reference. This pass uses the
    receiver captured by the extractor, and when it is a capitalized name resolving
    to exactly one class node that owns the called method, emits an EXTRACTED
    ``calls`` edge. Purely additive (only member calls the shared pass skipped),
    with a single-definition god-node guard.

    Must run after id-disambiguation so node ids and caller_nids are final.
    """
    def _key(label: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]+", "", str(label)).lower()

    node_by_id: dict[str, dict] = {n.get("id"): n for n in all_nodes}

    # Uma classe possui métodos: ela é a fonte de uma ou mais arestas de `método`. Índice
    # rótulo de classe -> possuir ids de nó de classe (len! = 1 é o guarda do god node) e
    class_def_nids: dict[str, list[str]] = {}
    method_index: dict[tuple[str, str], str] = {}
    for e in all_edges:
        if e.get("relation") != "method":
            continue
        src, tgt = e.get("source"), e.get("target")
        cnode = node_by_id.get(src)
        if cnode is not None:
            class_def_nids.setdefault(_key(cnode.get("label", "")), []).append(src)
        tnode = node_by_id.get(tgt)
        if tnode is not None:
            method_index[(src, _key(tnode.get("label", "")))] = tgt
    # Uma classe com N métodos produziu N entradas; colapsar para um conjunto único. (Não
    # retorno antecipado quando não há aulas: o braço do módulo abaixo resolve
    # `module.func()` onde o chamável é uma função simples, não um método.)
    for k in list(class_def_nids):
        class_def_nids[k] = sorted(set(class_def_nids[k]))

    all_raw_calls: list[dict] = []
    for result in per_file:
        all_raw_calls.extend(result.get("raw_calls", []))

    # Índice de braço de alias de módulo: `module.func()` onde `module` é importado.
    # Chave em IDs de nós estáveis, não em strings source_file (source_file é relativizado
    # pela passagem CLI id-remap, mas raw_calls mantêm seu caminho original, portanto, uma string
    # join falharia em um cache_root explícito). A fonte da aresta `imports`
    # é o próprio nó de arquivo do chamador; `contém` mapeia um nó de arquivo para seus filhos.
    contains_children: dict[str, dict[str, list[str]]] = {}
    file_of_node: dict[str, str] = {}
    for e in all_edges:
        if e.get("relation") == "contains":
            src, tgt = e.get("source"), e.get("target")
            tnode = node_by_id.get(tgt)
            if tnode is not None:
                contains_children.setdefault(src, {}).setdefault(
                    _key(tnode.get("label", "")), []).append(tgt)
                file_of_node[tgt] = src
    imported_by_filenode: dict[str, set[str]] = {}
    import_alias_by_filenode: dict[str, dict[str, str]] = {}
    for e in all_edges:
        if e.get("relation") in ("imports", "imports_from"):
            imported_by_filenode.setdefault(e.get("source"), set()).add(e.get("target"))
            alias = e.get("local_alias")
            if alias:
                import_alias_by_filenode.setdefault(e.get("source"), {})[e.get("target")] = _key(alias)

    def _module_stem_key(nid: str) -> str:
        n = node_by_id.get(nid)
        if not n:
            return ""
        sf = n.get("source_file") or ""
        stem = Path(sf).stem if sf else ""
        return _key(stem or n.get("label", ""))

    existing_pairs = {(e.get("source"), e.get("target")) for e in all_edges}

    def _emit_call(caller: str, target_nid: "str | None", rc: dict) -> None:
        if not target_nid or target_nid == caller or (caller, target_nid) in existing_pairs:
            return
        existing_pairs.add((caller, target_nid))
        # EXTRAÍDO: uma chamada qualificada (`ClassName.method()` ou `module.func()`) é
        # uma referência estática explícita e inequívoca resolvida para exatamente um
        # definição (cada braço aplica um guarda de god node de definição única).
        all_edges.append({
            "source": caller,
            "target": target_nid,
            "relation": "calls",
            "context": "call",
            "confidence": "EXTRACTED",
            "confidence_score": 1.0,
            "source_file": rc.get("source_file", ""),
            "source_location": rc.get("source_location"),
            "weight": 1.0,
        })

    for rc in all_raw_calls:
        if not rc.get("is_member_call"):
            continue
        receiver = rc.get("receiver")
        callee = rc.get("callee")
        caller = rc.get("caller_nid")
        if not receiver or not callee or not caller:
            continue
        if receiver[:1].isupper():
            # Braço de classe: um receptor maiúsculo é uma referência de classe; um
            class_nids = class_def_nids.get(_key(receiver), [])
            if len(class_nids) != 1:  # ausente ou ambíguo -> fiança (guarda do god node)
                continue
            _emit_call(caller, method_index.get((class_nids[0], _key(callee))), rc)
        else:
            # Braço do módulo: um receptor em minúsculas pode ser um módulo importado.
            # Resolva-o em relação aos módulos importados para o arquivo do próprio chamador
            # (então instâncias `self`/`obj`/local, que não são módulos importados,
            rkey = _key(receiver)
            caller_file = file_of_node.get(caller)
            file_aliases = import_alias_by_filenode.get(caller_file, {})
            mods = [t for t in imported_by_filenode.get(caller_file, ())
                    if t in contains_children
                    and (_module_stem_key(t) == rkey or file_aliases.get(t) == rkey)]
            if len(mods) != 1:  # não é um módulo importado ou ambíguo -> fiança
                continue
            children = contains_children[mods[0]].get(_key(callee), [])
            if len(children) != 1:  # ausente ou ambíguo exigível -> fiança
                continue
            _emit_call(caller, children[0], rc)


def _resolve_typescript_member_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve cross-file TS/JS member calls via constructor-injection type tables (#1316).

    ``this.repo.findById()`` drops out in the shared cross-file pass because bare
    ``findById`` collides across the corpus (god-node guard).  TS constructors with
    parameter-property modifiers (``private repo: IUserRepository``) produce a
    per-file type table mapping field names to their declared types.  This pass
    looks up the receiver field's type, finds a single-definition class/interface
    owning a method with the callee name, and emits a ``calls`` edge — EXTRACTED
    when the receiver names the type in source (``Type.method()``), INFERRED when
    the type came from the table (the Swift/C#/Java tiering).

    Origin gate (#2553): a name-only match is not evidence the caller can even
    see the matched type. ``import type { Repo } from 'external-pkg'`` plus
    ``this.repo.save()`` must not fabricate an edge to an unrelated local
    ``class Repo`` in another file. The matched type must be origin-verified:
    defined in the caller's own file, a named import of the caller's file, or
    contained in a module the caller's file imports. Otherwise EMIT NOTHING —
    a false call edge is worse than a missing one (the C++ resolver's bar).
    """
    type_table_by_file: dict[str, dict[str, str]] = {}
    for result in per_file:
        tt = result.get("ts_type_table")
        if tt and tt.get("path"):
            type_table_by_file[tt["path"]] = tt.get("table", {})
    if not type_table_by_file:
        return

    def _key(label: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]+", "", str(label)).lower()

    contained = {e.get("target") for e in all_edges if e.get("relation") == "contains"}

    type_def_nids: dict[str, list[str]] = {}
    node_by_id: dict[str, dict] = {}
    for n in all_nodes:
        node_by_id[n.get("id")] = n
        if n.get("source_file") and n.get("id") in contained and _is_type_like_definition(n):
            type_def_nids.setdefault(_key(n.get("label", "")), []).append(n["id"])

    method_index: dict[tuple[str, str], str] = {}
    for e in all_edges:
        if e.get("relation") != "method":
            continue
        src, tgt = e.get("source"), e.get("target")
        tnode = node_by_id.get(tgt)
        if tnode is not None:
            method_index[(src, _key(tnode.get("label", "")))] = tgt

    file_of_node: dict[str, str] = {}
    for e in all_edges:
        if e.get("relation") == "contains":
            file_of_node[e.get("target")] = e.get("source")
    for e in all_edges:
        if e.get("relation") == "method":
            owner_file = file_of_node.get(e.get("source"))
            if owner_file is not None:
                file_of_node.setdefault(e.get("target"), owner_file)
    imported_by_filenode: dict[str, set[str]] = {}
    for e in all_edges:
        if e.get("relation") in ("imports", "imports_from"):
            imported_by_filenode.setdefault(e.get("source"), set()).add(e.get("target"))

    all_raw_calls: list[dict] = []
    for result in per_file:
        all_raw_calls.extend(result.get("raw_calls", []))

    existing_pairs = {(e.get("source"), e.get("target")) for e in all_edges}
    for rc in all_raw_calls:
        if not rc.get("is_member_call"):
            continue
        receiver = rc.get("receiver")
        callee = rc.get("callee")
        caller = rc.get("caller_nid")
        if not receiver or not callee or not caller:
            continue
        if receiver[:1].isupper():
            type_name = receiver
            type_qualified = True
        else:
            type_qualified = False
            type_name = type_table_by_file.get(rc.get("source_file", ""), {}).get(receiver)
        if not type_name:
            continue
        # Um tipo de receptor global integrado (Data, Promessa, Mapa, ...) não deve resolver
        # para um símbolo de usuário. _key() casefolds, então `x: Date; x.getTime()` ligaria
        # o chamador para um usuário de mesmo nome `class DATE` em outro arquivo, inventando
        # arestas fantasmas `references[call]` e um nó falso deus. O
        # o resolvedor CALL entre arquivos já ignora esses globais; faça o mesmo aqui.
        if type_name in _LANGUAGE_BUILTIN_GLOBALS:
            continue
        type_defs = type_def_nids.get(_key(type_name), [])
        if len(type_defs) != 1:
            continue
        type_nid = type_defs[0]
        caller_file = file_of_node.get(caller)
        type_file = file_of_node.get(type_nid)
        imported = imported_by_filenode.get(caller_file, set())
        if not (
            (caller_file is not None and caller_file == type_file)
            or type_nid in imported
            or (type_file is not None and type_file in imported)
        ):
            continue
        method_nid = method_index.get((type_nid, _key(callee)))
        if not method_nid:
            continue
        if method_nid == caller or (caller, method_nid) in existing_pairs:
            continue
        existing_pairs.add((caller, method_nid))
        all_edges.append({
            "source": caller,
            "target": method_nid,
            "relation": "calls",
            "context": "call",
            "confidence": "EXTRACTED" if type_qualified else "INFERRED",
            "confidence_score": 1.0 if type_qualified else 0.8,
            "source_file": rc.get("source_file", ""),
            "source_location": rc.get("source_location"),
            "weight": 1.0,
        })


def _resolve_cpp_member_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve cross-file C++ member calls (``f.bar()``, ``f->bar()``,
    ``Foo::bar()``, ``this->bar()``) to the real definition of the receiver's type
    (#1547).

    The shared cross-file pass drops every ``is_member_call`` because a bare method
    name (``bar``) collides across the corpus and inflates god-nodes (#543/#1219).
    The C++ extractor records each member call's receiver and a per-file
    ``var -> ClassName`` table (``cpp_type_table``) built from local declarations.
    This pass types the receiver, then emits an edge ONLY when that type resolves
    to exactly ONE definition (the god-node guard).

    Receiver typing, by precision tier:
      * ``Foo::bar()`` — the scope ``Foo`` names the type explicitly -> EXTRACTED.
      * ``this->bar()`` — the receiver is the caller's own enclosing class -> EXTRACTED.
      * ``f.bar()`` / ``f->bar()`` — ``f`` typed via the file's local table -> INFERRED.
    A receiver whose type can't be inferred locally is SKIPPED (no guess): a false
    call edge is worse than a missing one. The ``_merge_decl_def_classes`` pass has
    already folded each header/impl class pair into one node, so a paired class is a
    single definition and clears the single-definition guard.

    Must run after id-disambiguation so node ids and caller_nids are final.
    """
    type_table_by_file: dict[str, dict[str, str]] = {}
    for result in per_file:
        tt = result.get("cpp_type_table")
        if tt and tt.get("path"):
            type_table_by_file[tt["path"]] = tt.get("table", {})

    def _key(label: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]+", "", str(label)).lower()

    # Um tipo C++ genuíno é o alvo de uma aresta `contém` de seu nó de arquivo;
    # nós de sombra de referência simples (stubs ensure_named_node) não estão contidos, então
    # excluir nós não contidos evita que eles tornem um tipo real ambíguo.
    contained = {e.get("target") for e in all_edges if e.get("relation") == "contains"}

    type_def_nids: dict[str, list[str]] = {}
    node_by_id: dict[str, dict] = {}
    for n in all_nodes:
        node_by_id[n.get("id")] = n
        if n.get("source_file") and n.get("id") in contained and _is_type_like_definition(n):
            type_def_nids.setdefault(_key(n.get("label", "")), []).append(n["id"])

    # (a classe proprietária) para chamadas `this->`. Uma classe C++ possui seus membros via
    # `método` arestas (definições fora de linha) E `define` arestas (dentro da classe
    # declarações, que o extrator modela como campos); indexe ambos para um cabeçalho-
    method_index: dict[tuple[str, str], str] = {}
    enclosing_type: dict[str, str] = {}
    for rel in ("defines", "method"):
        for e in all_edges:
            if e.get("relation") != rel:
                continue
            src, tgt = e.get("source"), e.get("target")
            tnode = node_by_id.get(tgt)
            if tnode is None:
                continue
            enclosing_type.setdefault(tgt, src)
            method_index[(src, _key(tnode.get("label", "")))] = tgt

    all_raw_calls: list[dict] = []
    for result in per_file:
        all_raw_calls.extend(result.get("raw_calls", []))

    existing_pairs = {(e.get("source"), e.get("target")) for e in all_edges}
    for rc in all_raw_calls:
        if not rc.get("is_member_call"):
            continue
        receiver = rc.get("receiver")
        callee = rc.get("callee")
        caller = rc.get("caller_nid")
        if not receiver or not callee or not caller:
            continue
        src_file = rc.get("source_file", "")
        # um `.h` pode ser roteado para extract_cpp ou extract_objc por conteúdo, então o
        # tag `lang` carimbada pelo extrator - não o sufixo - é a porta inequívoca).
        if rc.get("lang") != "cpp":
            continue
        # Determine o tipo do receptor e a confiança resultante.
        if receiver == "this":
            # this->bar(): receiver é a classe envolvente do chamador.
            type_nid = enclosing_type.get(caller)
            if not type_nid:
                continue
            type_qualified = True
        elif receiver[:1].isupper():
            # Foo::bar(): o tipo é nomeado explicitamente na fonte.
            type_defs = type_def_nids.get(_key(receiver), [])
            if len(type_defs) != 1:  # ambíguo ou ausente -> fiança (guarda do god node)
                continue
            type_nid = type_defs[0]
            type_qualified = True
        else:
            # f.bar() / f->bar(): digite o receptor através da tabela local do arquivo.
            type_name = type_table_by_file.get(src_file, {}).get(receiver)
            if not type_name:
                continue
            type_defs = type_def_nids.get(_key(type_name), [])
            if len(type_defs) != 1:  # ambíguo ou ausente -> fiança (guarda do god node)
                continue
            type_nid = type_defs[0]
            type_qualified = False
        method_nid = method_index.get((type_nid, _key(callee)))
        target = method_nid or type_nid
        relation = "calls" if method_nid else "references"
        if target == caller or (caller, target) in existing_pairs:
            continue
        existing_pairs.add((caller, target))
        all_edges.append({
            "source": caller,
            "target": target,
            "relation": relation,
            "context": "call",
            "confidence": "EXTRACTED" if type_qualified else "INFERRED",
            "confidence_score": 1.0 if type_qualified else 0.8,
            "source_file": src_file,
            "source_location": rc.get("source_location"),
            "weight": 1.0,
        })


def _resolve_csharp_member_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve C# member calls (``recv.Method()``) to the receiver's declared type
    (#1609), namespace-aware (#1620).

    The shared cross-file pass drops every ``is_member_call`` because a bare method
    name collides across the corpus — and for C# an in-file bare match silently
    mis-bound ``_server.Save()`` to an unrelated ``Cache.Save()``. The C# extractor
    records each member call's receiver and stamps ``receiver_type`` on the raw
    call from a METHOD-scoped ``name -> Type`` table of class fields/properties
    plus the declaring method's params/locals (#2299 — per-method like Java, so a
    name rebound in a different method never poisons this one; same-method
    conflicts and untypable rebindings are still POISONED, so a shadowing local of
    a different type produces no edge rather than a wrong one). This pass resolves
    the stamped type name with the same namespace/using/alias scoping machinery the
    type-reference pass uses (``CsharpNameResolver``), so a class name duplicated
    across namespaces still binds to the one in scope; only when scoping knows
    nothing about the name does it fall back to the corpus-wide unique bare-name
    match (the god-node guard). An untypable/ambiguous receiver is skipped — never
    a guess.

    Receiver typing, by precision tier:
      * ``this.M()`` — receiver is the caller's own enclosing class -> EXTRACTED.
      * ``base.M()`` — the caller's single resolvable base class -> EXTRACTED.
      * ``Type.M()`` (capitalized) — the type is named explicitly in source -> EXTRACTED.
      * ``recv.M()`` / ``this.recv.M()`` — ``recv`` typed via the extractor's
        method-scoped field/property/param/local table (``receiver_type`` on the
        raw call) -> INFERRED.

    A method not declared on the receiver's type is looked up through its
    ``inherits`` chain; a chain containing an unresolvable (out-of-corpus) base
    poisons the lookup — the method may live there, so no edge is emitted.

    Must run after id-disambiguation so node ids and caller_nids are final.
    """
    def _key(label: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]+", "", str(label)).lower()

    contained = {e.get("target") for e in all_edges if e.get("relation") == "contains"}

    type_def_nids: dict[str, list[str]] = {}
    node_by_id: dict[str, dict] = {}
    for n in all_nodes:
        node_by_id[n.get("id")] = n
        if n.get("source_file") and n.get("id") in contained and _is_type_like_definition(n):
            type_def_nids.setdefault(_key(n.get("label", "")), []).append(n["id"])

    resolver = CsharpNameResolver(all_nodes, all_edges)

    # C# possui seus métodos por meio de arestas de `método`.
    method_index: dict[tuple[str, str], str] = {}
    enclosing_type: dict[str, str] = {}
    for e in all_edges:
        if e.get("relation") != "method":
            continue
        src, tgt = e.get("source"), e.get("target")
        tnode = node_by_id.get(tgt)
        if tnode is None:
            continue
        enclosing_type.setdefault(tgt, src)
        method_index[(src, _key(tnode.get("label", "")))] = tgt

    bases_of: dict[str, list[str]] = {}
    unresolved_base: set[str] = set()
    for e in all_edges:
        if e.get("relation") != "inherits":
            continue
        src_file = e.get("source_file")
        if not (isinstance(src_file, str) and src_file.endswith(".cs")):
            continue
        src, tgt = e.get("source"), e.get("target")
        if not (isinstance(src, str) and isinstance(tgt, str)):
            continue
        tnode = node_by_id.get(tgt)
        if tnode is None or not tnode.get("source_file"):
            unresolved_base.add(src)
        else:
            bucket = bases_of.setdefault(src, [])
            if tgt not in bucket:
                bucket.append(tgt)

    def _method_on_type_or_bases(type_nid: str, callee_key: str) -> str | None:
        """The method's definition on the type or its resolvable base chain.

        A type that declares the method directly wins (overrides shadow the
        base). Otherwise walk `inherits` upward; an unresolved base anywhere the
        walk actually reaches poisons the lookup (no edge), as does anything
        other than exactly one declaration found.
        """
        hits: set[str] = set()
        seen: set[str] = set()
        frontier = [type_nid]
        while frontier:
            nid = frontier.pop()
            if nid in seen:
                continue
            seen.add(nid)
            method_nid = method_index.get((nid, callee_key))
            if method_nid:
                hits.add(method_nid)
                continue
            if nid in unresolved_base:
                return None
            frontier.extend(bases_of.get(nid, []))
        return next(iter(hits)) if len(hits) == 1 else None

    def _resolve_type_name_nid(type_name: str | None, caller_node: dict | None,
                               src_file: str) -> str | None:
        """Resolve a declared type name to exactly one definition node id.

        Namespace/using/alias scoping first (so `Svc` duplicated across
        namespaces binds to the one in scope); when scoping is decisive but
        ambiguous, bail. Only when scoping knows nothing about the name fall
        back to the corpus-wide unique bare-name match (which also covers
        nested types, absent from the scoped index).
        """
        if not type_name:
            return None
        if caller_node is not None:
            resolved, decisive = resolver.resolve_type_name(
                type_name, caller_node, src_file
            )
            if resolved:
                return resolved
            if decisive:
                return None
        type_defs = type_def_nids.get(_key(type_name), [])
        return type_defs[0] if len(type_defs) == 1 else None

    all_raw_calls: list[dict] = []
    for result in per_file:
        all_raw_calls.extend(result.get("raw_calls", []))

    existing_pairs = {(e.get("source"), e.get("target")) for e in all_edges}
    for rc in all_raw_calls:
        if rc.get("lang") != "csharp" or not rc.get("is_member_call"):
            continue
        receiver = rc.get("receiver")
        callee = rc.get("callee")
        caller = rc.get("caller_nid")
        if not receiver or not callee or not caller:
            continue
        src_file = rc.get("source_file", "")
        caller_node = node_by_id.get(caller)
        if receiver == "this":
            type_nid = enclosing_type.get(caller)
            if not type_nid:
                continue
            type_qualified = True
        elif receiver == "base":
            enclosing = enclosing_type.get(caller)
            if not enclosing or enclosing in unresolved_base:
                continue
            bases = bases_of.get(enclosing, [])
            if len(bases) != 1:
                continue
            type_nid = bases[0]
            type_qualified = True
        elif receiver[:1].isupper():
            # Type.M() — o tipo é nomeado explicitamente (também cobre um caso Pascal
            # local cujo nome é igual ao seu tipo, resolvido através da tabela abaixo se o
            type_nid = _resolve_type_name_nid(receiver, caller_node, src_file)
            if not type_nid:
                type_name = rc.get("receiver_type")
                type_nid = _resolve_type_name_nid(type_name, caller_node, src_file)
                if not type_nid:
                    continue
            type_qualified = True
        else:
            type_name = rc.get("receiver_type")
            if not type_name:
                continue
            type_nid = _resolve_type_name_nid(type_name, caller_node, src_file)
            if not type_nid:  # ambíguo ou ausente -> fiança (guarda do god node)
                continue
            type_qualified = False
        method_nid = _method_on_type_or_bases(type_nid, _key(callee))
        if not method_nid:
            continue  # receptor digitado, mas o tipo não possui tal método - pule
        if method_nid == caller or (caller, method_nid) in existing_pairs:
            continue
        existing_pairs.add((caller, method_nid))
        all_edges.append({
            "source": caller,
            "target": method_nid,
            "relation": "calls",
            "context": "call",
            "confidence": "EXTRACTED" if type_qualified else "INFERRED",
            "confidence_score": 1.0 if type_qualified else 0.8,
            "source_file": src_file,
            "source_location": rc.get("source_location"),
            "weight": 1.0,
        })


def _resolve_java_member_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve Java member calls against the receiver's declared type.

    Explicit type receivers and ``this`` are exact. Fields declared on the
    caller's class plus method parameters and explicit locals are inferred from
    the extractor's method-scoped type table. A missing or ambiguous receiver
    type is skipped rather than falling back to a bare method-name match.
    """
    def key(label: str) -> str:
        return str(label).strip().removeprefix(".").removesuffix("()")

    contained = {edge.get("target") for edge in all_edges
                 if edge.get("relation") == "contains"}
    node_by_id = {node.get("id"): node for node in all_nodes}

    type_def_nids: dict[str, list[str]] = {}
    for node in all_nodes:
        if (
            node.get("source_file")
            and node.get("id") in contained
            and _is_type_like_definition(node)
        ):
            type_def_nids.setdefault(key(node.get("label", "")), []).append(node["id"])

    method_index: dict[tuple[str, str], set[str]] = {}
    enclosing_type: dict[str, str] = {}
    for edge in all_edges:
        if edge.get("relation") != "method":
            continue
        owner, method = edge.get("source"), edge.get("target")
        method_node = node_by_id.get(method)
        if method_node is None:
            continue
        enclosing_type.setdefault(method, owner)
        method_index.setdefault((owner, key(method_node.get("label", ""))), set()).add(method)

    existing_pairs = {(edge.get("source"), edge.get("target")) for edge in all_edges}
    for result in per_file:
        for raw_call in result.get("raw_calls", []):
            if raw_call.get("lang") != "java" or not raw_call.get("is_member_call"):
                continue
            receiver = raw_call.get("receiver")
            callee = raw_call.get("callee")
            caller = raw_call.get("caller_nid")
            if not receiver or not callee or not caller:
                continue

            exact = False
            if receiver == "this":
                type_nid = enclosing_type.get(caller)
                exact = True
                if not type_nid:
                    continue
            else:
                type_name = raw_call.get("receiver_type")
                if not type_name and receiver[:1].isupper():
                    type_name = receiver
                    exact = True
                if not type_name:
                    continue
                type_defs = type_def_nids.get(key(type_name), [])
                if len(type_defs) != 1:
                    continue
                type_nid = type_defs[0]

            method_nids = method_index.get((type_nid, key(callee)), set())
            if len(method_nids) != 1:
                continue
            method_nid = next(iter(method_nids))
            if method_nid == caller or (caller, method_nid) in existing_pairs:
                continue
            existing_pairs.add((caller, method_nid))
            all_edges.append({
                "source": caller,
                "target": method_nid,
                "relation": "calls",
                "context": "call",
                "confidence": "EXTRACTED" if exact else "INFERRED",
                "confidence_score": 1.0 if exact else 0.8,
                "source_file": raw_call.get("source_file", ""),
                "source_location": raw_call.get("source_location"),
                "weight": 1.0,
            })


def _resolve_objc_member_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve cross-file Objective-C message sends (``[recv sel]``) to the real
    definition of the receiver's type (#1556).

    The ObjC extractor keeps its same-file selector matching (alloc/init refs,
    dot-syntax accesses, @selector) and additionally emits ``raw_calls`` for every
    message send, with the receiver and the reconstructed selector as the callee.
    This pass types the receiver and emits a cross-file ``calls`` edge ONLY when the
    type resolves to exactly ONE definition (the god-node guard).

    Receiver typing:
      * ``self`` / ``super`` — the caller's own enclosing class -> EXTRACTED.
      * Capitalized receiver (``[Foo new]``) — the type named explicitly -> EXTRACTED.
      * ``[f doThing]`` — ``f`` typed via the file's ``Foo *f`` local table -> INFERRED.
      * ``[self.bar doIt]`` / ``[_ivarBar doIt]`` — the field typed via the class's
        ``@property``/ivar table (locals shadow fields for the bare-identifier
        form) -> INFERRED. Only the exact ``self.<field>`` receiver shape is
        captured; a dotted receiver like ``Foo.shared`` is never passed through,
        because ``_key`` would strip the dot and collide with a real ``FooShared``.
    An uninferable receiver is SKIPPED (no guess), so an ambiguous selector across
    classes never fans out. ``_merge_decl_def_classes`` folds each @interface/@impl
    pair into one node, so a paired class clears the single-definition guard.
    ``@protocol`` declarations are excluded from the receiver-type index: a protocol
    is a contract, not a message receiver, and ObjC keeps protocol and class names in
    separate namespaces, so a same-named pair used to both mis-bind a message to the
    protocol's declaration and, when a real class existed, trip the god-node guard.

    Must run after id-disambiguation so node ids and caller_nids are final.
    """
    type_table_by_file: dict[str, dict[str, str]] = {}
    for result in per_file:
        tt = result.get("objc_type_table")
        if tt and tt.get("path"):
            type_table_by_file[tt["path"]] = tt.get("table", {})

    field_types_by_class: dict[str, dict[str, str]] = {}
    field_conflicts: set[tuple[str, str]] = set()
    for result in per_file:
        ft = result.get("objc_field_types")
        if not ft:
            continue
        for cls_nid, tbl in (ft.get("tables") or {}).items():
            merged = field_types_by_class.setdefault(cls_nid, {})
            for field, tname in tbl.items():
                if (cls_nid, field) in field_conflicts:
                    continue
                prev = merged.get(field)
                if prev is None:
                    merged[field] = tname
                elif prev != tname:
                    del merged[field]
                    field_conflicts.add((cls_nid, field))

    def _key(label: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]+", "", str(label)).lower()

    contained = {e.get("target") for e in all_edges if e.get("relation") == "contains"}

    def _is_protocol_declaration(n: dict) -> bool:
        """A ``@protocol`` declaration, which the ObjC extractor labels ``<Name>``.

        A protocol is a contract, never a message receiver, so it must not be a
        receiver-typing candidate. It stays a valid target for `implements`; only
        this pass's type index excludes it.
        """
        label = str(n.get("label", "")).strip()
        return label.startswith("<") and label.endswith(">")

    type_def_nids: dict[str, list[str]] = {}
    node_by_id: dict[str, dict] = {}
    for n in all_nodes:
        node_by_id[n.get("id")] = n
        if (n.get("source_file") and n.get("id") in contained
                and _is_type_like_definition(n) and not _is_protocol_declaration(n)):
            type_def_nids.setdefault(_key(n.get("label", "")), []).append(n["id"])

    method_index: dict[tuple[str, str], str] = {}
    enclosing_type: dict[str, str] = {}
    for e in all_edges:
        if e.get("relation") != "method":
            continue
        src, tgt = e.get("source"), e.get("target")
        enclosing_type.setdefault(tgt, src)
        tnode = node_by_id.get(tgt)
        if tnode is not None:
            # Os rótulos dos métodos ObjC carregam um sigilo +/- (`-doThing`); tire-o para que
            # seletor `doThing` chaves para o método.
            method_index[(src, _key(tnode.get("label", "")))] = tgt

    all_raw_calls: list[dict] = []
    for result in per_file:
        all_raw_calls.extend(result.get("raw_calls", []))

    existing_pairs = {(e.get("source"), e.get("target")) for e in all_edges}
    for rc in all_raw_calls:
        if not rc.get("is_member_call"):
            continue
        receiver = rc.get("receiver")
        callee = rc.get("callee")
        caller = rc.get("caller_nid")
        if not receiver or not callee or not caller:
            continue
        src_file = rc.get("source_file", "")
        if rc.get("lang") != "objc":
            continue
        if rc.get("receiver_kind") == "self_field":
            cls = enclosing_type.get(caller)
            type_name = field_types_by_class.get(cls, {}).get(receiver) if cls else None
            if not type_name:
                continue
            type_defs = type_def_nids.get(_key(type_name), [])
            if len(type_defs) != 1:  # ambíguo ou ausente -> fiança (guarda do god node)
                continue
            type_nid = type_defs[0]
            type_qualified = False
        elif receiver in ("self", "super"):
            type_nid = enclosing_type.get(caller)
            if not type_nid:
                continue
            type_qualified = True
        elif receiver[:1].isupper():
            type_defs = type_def_nids.get(_key(receiver), [])
            if len(type_defs) != 1:  # ambíguo ou ausente -> fiança (guarda do god node)
                continue
            type_nid = type_defs[0]
            type_qualified = True
        else:
            type_name = type_table_by_file.get(src_file, {}).get(receiver)
            if not type_name:
                cls = enclosing_type.get(caller)
                type_name = field_types_by_class.get(cls, {}).get(receiver) if cls else None
            if not type_name:
                continue
            type_defs = type_def_nids.get(_key(type_name), [])
            if len(type_defs) != 1:  # ambíguo ou ausente -> fiança (guarda do god node)
                continue
            type_nid = type_defs[0]
            type_qualified = False
        method_nid = method_index.get((type_nid, _key(callee)))
        target = method_nid or type_nid
        relation = "calls" if method_nid else "references"
        if target == caller or (caller, target) in existing_pairs:
            continue
        existing_pairs.add((caller, target))
        all_edges.append({
            "source": caller,
            "target": target,
            "relation": relation,
            "context": "call",
            "confidence": "EXTRACTED" if type_qualified else "INFERRED",
            "confidence_score": 1.0 if type_qualified else 0.8,
            "source_file": src_file,
            "source_location": rc.get("source_location"),
            "weight": 1.0,
        })


def _kotlin_package_index(per_file: list[dict]) -> dict[str, list[dict]]:
    """Group per-file results by the Kotlin package they declare.

    ``kotlin_package`` is stamped by the generic engine from the file's
    ``package_header`` (see extractors/engine.py); every node in the file
    inherits it. Files with no package header contribute nothing.
    """
    pkg_results: dict[str, list[dict]] = {}
    for result in per_file:
        pkg = result.get("kotlin_package")
        if pkg:
            pkg_results.setdefault(pkg, []).append(result)
    return pkg_results


def _resolve_kotlin_import_targets(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Rewrite Kotlin ``imports`` edge targets from the bare last segment to the
    node the written FQN actually names (#2526).

    ``_import_kotlin`` emits ``file --imports--> _make_id(last_segment)`` with
    the full dotted path stamped as ``metadata.target_fqn``. That target dangles
    (node ids carry a file-stem prefix), so build pruned every Kotlin import and
    the import-evidence promotion in the shared call pass never fired. Here the
    per-file ``kotlin_package`` declarations index each package's importable
    (non-member) symbols by exact label; an edge whose ``target_fqn`` splits
    into a known package P plus a Name defined exactly ONCE in P is rewritten to
    that node id. The FQN is written verbatim in source, so the match is exact —
    confidence stays EXTRACTED. Anything else (external dependency, ambiguous
    name) is left untouched and dangles like other languages' external imports.

    Must run BEFORE the shared call pass builds its import-evidence index (it is
    invoked directly in extract(), not via the tail registry run).
    """
    pkg_results = _kotlin_package_index(per_file)
    if not pkg_results:
        return
    pkg_symbols: dict[str, dict[str, list[str]]] = {}
    for pkg, results in pkg_results.items():
        by_label = pkg_symbols.setdefault(pkg, {})
        for result in results:
            for n in result.get("nodes", []):
                if not n.get("source_file") or n.get("type") == "namespace":
                    continue
                label = str(n.get("label", ""))
                if not label or label.startswith("."):
                    continue
                by_label.setdefault(label.strip("()"), []).append(n["id"])
    for e in all_edges:
        if e.get("relation") != "imports":
            continue
        if not str(e.get("source_file", "")).endswith((".kt", ".kts")):
            continue
        fqn = (e.get("metadata") or {}).get("target_fqn", "")
        pkg, _, name = str(fqn).rpartition(".")
        if not pkg or not name:
            continue
        candidates = pkg_symbols.get(pkg, {}).get(name, [])
        if len(candidates) == 1:
            e["target"] = candidates[0]


def _resolve_kotlin_qualified_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve Kotlin fully-qualified call expressions (#2550).

    ``com.example.nav.NavGraph()`` parses to a nested navigation_expression
    chain; the engine flattens it and stamps the raw_call with
    ``qualified_prefix="com.example.nav"`` + ``lang="kotlin"`` when EVERY chain
    segment is a plain identifier. The shared pass skips member calls, so these
    raw_calls produced no edge at all — this pass is strictly additive.

    Resolution, guarded by exactly-one-candidate at every step:
      * prefix == a declared package FQN P -> candidates are P's top-level
        callables (functions/classes the file node `contains`) named callee;
      * prefix == P + "." + TypeName where TypeName is a class/object declared
        in P -> candidates are that type's methods (`method` edges, `.callee()`
        label).
    Zero or 2+ candidates -> no edge. The FQN is written verbatim in source, so
    a unique match is EXTRACTED.
    """
    pkg_results = _kotlin_package_index(per_file)
    if not pkg_results:
        return
    raw = [
        rc
        for result in per_file
        for rc in result.get("raw_calls", [])
        if rc.get("lang") == "kotlin" and rc.get("qualified_prefix")
        and rc.get("callee") and rc.get("caller_nid")
    ]
    if not raw:
        return

    node_by_id: dict[str, dict] = {n.get("id"): n for n in all_nodes}
    contains_by_source: dict[str, list[str]] = {}
    methods_by_type: dict[str, list[str]] = {}
    for e in all_edges:
        rel = e.get("relation")
        if rel == "contains":
            contains_by_source.setdefault(e.get("source"), []).append(e.get("target"))
        elif rel == "method":
            methods_by_type.setdefault(e.get("source"), []).append(e.get("target"))

    pkg_callables: dict[str, dict[str, list[str]]] = {}
    pkg_types: dict[str, dict[str, list[str]]] = {}
    for pkg, results in pkg_results.items():
        callables = pkg_callables.setdefault(pkg, {})
        types = pkg_types.setdefault(pkg, {})
        for result in results:
            file_nid = next(
                (n["id"] for n in result.get("nodes", [])
                 if n.get("source_file")
                 and n.get("label") == Path(str(n["source_file"])).name),
                None,
            )
            if file_nid is None:
                continue
            for tgt in contains_by_source.get(file_nid, []):
                n = node_by_id.get(tgt)
                if n is None or not n.get("source_file"):
                    continue
                name = str(n.get("label", "")).strip("()")
                if not name or name.startswith("."):
                    continue
                if n.get("_callable"):
                    callables.setdefault(name, []).append(tgt)
                if n.get("_callable_class"):
                    types.setdefault(name, []).append(tgt)

    existing_pairs = {(e.get("source"), e.get("target")) for e in all_edges}
    for rc in raw:
        prefix = rc["qualified_prefix"]
        callee = rc["callee"]
        caller = rc["caller_nid"]
        candidates: list[str] = []
        if prefix in pkg_callables:
            candidates = pkg_callables[prefix].get(callee, [])
        else:
            pkg, _, type_name = prefix.rpartition(".")
            type_nids = pkg_types.get(pkg, {}).get(type_name, []) if pkg else []
            if len(type_nids) == 1:
                wanted = f".{callee}"
                candidates = [
                    m for m in methods_by_type.get(type_nids[0], [])
                    if str(node_by_id.get(m, {}).get("label", "")).strip("()") == wanted
                ]
        if len(candidates) != 1:
            continue
        tgt = candidates[0]
        if tgt == caller or (caller, tgt) in existing_pairs:
            continue
        existing_pairs.add((caller, tgt))
        all_edges.append({
            "source": caller,
            "target": tgt,
            "relation": "calls",
            "context": "call",
            "confidence": "EXTRACTED",
            "confidence_score": 1.0,
            "source_file": rc.get("source_file", ""),
            "source_location": rc.get("source_location"),
            "weight": 1.0,
        })


_KOTLIN_IMPORT_TARGET_RESOLVER = LanguageResolver(
    "kotlin_import_targets", frozenset({".kt", ".kts"}), _resolve_kotlin_import_targets
)


# Registre os resolvedores de chamada de membro de arquivo cruzado e específico do idioma no arquivo compartilhado
# adicionando uma chamada de register() abaixo — sem edições no corpo de extract(). Ordem
# preservado da fiação embutida anterior: Swift antes de Python.
register_language_resolver(
    LanguageResolver("swift_member_calls", frozenset({".swift"}), _resolve_swift_member_calls)
)
register_language_resolver(
    LanguageResolver("python_member_calls", frozenset({".py"}), _resolve_python_member_calls)
)
# Resolução de chamada de membro com reconhecimento de tipo Ruby (Class.new + var.method digitado). Mora em
register_language_resolver(
    LanguageResolver("ruby_member_calls", frozenset({".rb", ".rake"}), resolve_ruby_member_calls)
)
register_language_resolver(
    LanguageResolver("typescript_member_calls", frozenset({".ts", ".tsx", ".mts", ".cts", ".js", ".jsx"}), _resolve_typescript_member_calls)
)
# Resolução de chamada de membro digitada por receptor C++ e ObjC. `.h` está em
# ambos os conjuntos de sufixos porque são roteados para extract_cpp ou extract_objc por conteúdo; o
# cada resolvedor reivindica apenas suas próprias raw_calls por meio do `lang` carimbado pelo extrator.
register_language_resolver(
    LanguageResolver(
        "cpp_member_calls",
        frozenset({".cpp", ".cc", ".cxx", ".hpp", ".cu", ".cuh", ".metal", ".h"}),
        _resolve_cpp_member_calls,
    )
)
register_language_resolver(
    LanguageResolver(
        "objc_member_calls",
        frozenset({".m", ".mm", ".h"}),
        _resolve_objc_member_calls,
    )
)
# vinculado ao tipo declarado do receptor em vez de uma correspondência simples com o mesmo nome.
register_language_resolver(
    LanguageResolver("csharp_member_calls", frozenset({".cs"}), _resolve_csharp_member_calls)
)
register_language_resolver(
    LanguageResolver("java_member_calls", frozenset({".java"}), _resolve_java_member_calls)
)
# Resolução de chamada de método herdado de arquivo cruzado Pascal/Delphi: uma chamada de um
# classe descendente manual para um método que ela herda de um ancestral declarado
# em um arquivo DIFERENTE (a divisão base gerada/descendente manual comum,
# por exemplo Th0Xxx/Th5Xxx da Sistec) está fora do próprio extrator por arquivo
register_language_resolver(
    LanguageResolver(
        "pascal_inherited_calls",
        frozenset({".pas", ".pp", ".dpr", ".dpk", ".inc"}),
        resolve_pascal_inherited_calls,
    )
)
register_language_resolver(
    LanguageResolver(
        "kotlin_qualified_calls", frozenset({".kt", ".kts"}), _resolve_kotlin_qualified_calls
    )
)


# Link de marcação in-line: [texto](destino "título opcional"). O olhar negativo para trás
# exclui imagens (![alt](src)). O alvo para no espaço em branco/parêntese de fechamento, então
# um "título" opcional após o URL ser descartado; um wrapper <...> opcional também é.

# Extensões para as quais omnigraph cria nós de arquivos de documentos. Um link para um desses
# resolve para o nó desse arquivo; links para código/ativos são ignorados (da esquerda para o




# Arquivos reais .csproj/.fsproj/.vbproj/.lpk têm menos de 2 MiB; qualquer coisa
# maior é malformado ou hostil.
_PROJECT_XML_MAX_BYTES = 2 * 1024 * 1024


def _project_xml_is_safe(src: bytes) -> bool:
    """Reject XML that declares DTDs or entities.

    Stdlib ``xml.etree.ElementTree`` does not cap entity expansion, so a
    crafted project file could trigger a billion-laughs style DoS. External
    entity resolution is already disabled by pyexpat defaults, but rejecting
    ``<!DOCTYPE`` / ``<!ENTITY`` outright is defense in depth.

    Legitimate MSBuild and Lazarus package files never contain a DOCTYPE
    or ENTITY declaration, so this is a zero-false-positive screen.
    """
    # Somente o prólogo pode conter um subconjunto DTD/interno, mas seja conservador
    # a correspondência de substring sem distinção entre maiúsculas e minúsculas é suficiente.
    lowered = src.lower()
    return b"<!doctype" not in lowered and b"<!entity" not in lowered


def extract_lazarus_package(path: Path) -> dict:
    """Extract package metadata from Lazarus .lpk package files (XML format).

    .lpk is an XML file listing the package name, required dependencies,
    and the Pascal units that belong to the package.

    Produces nodes for:
    - The package file itself
    - The package (by name)
    - Each required package (dependency)
    - Each listed unit file (resolved to path-based IDs where possible)

    Produces edges for:
    - file --contains--> package
    - package --imports--> required dependency (context: "import")
    - package --contains--> listed unit
    """
    try:
        import defusedxml.ElementTree as ET
        src = path.read_bytes()
    except OSError as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    if len(src) > _PROJECT_XML_MAX_BYTES:
        return {"nodes": [], "edges": [], "error": "package file too large"}
    if not _project_xml_is_safe(src):
        return {"nodes": [], "edges": [],
                "error": "refusing XML with DOCTYPE/ENTITY declaration"}

    try:
        xml_root = ET.fromstring(src)
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    str_path = str(path)
    stem = _file_stem(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()

    def add_node(nid: str, label: str) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid, "label": label, "file_type": "code",
                "source_file": str_path, "source_location": "L1",
            })

    def add_edge(src: str, tgt: str, relation: str, context: str | None = None) -> None:
        edge: dict[str, Any] = {
            "source": src, "target": tgt, "relation": relation,
            "confidence": "EXTRACTED", "source_file": str_path,
            "source_location": "L1", "weight": 1.0,
        }
        if context:
            edge["context"] = context
        edges.append(edge)

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name)

    name_elem = xml_root.find(".//Package/Name")
    pkg_name = name_elem.get("Value") if name_elem is not None else path.stem
    pkg_nid = _make_id(stem, pkg_name)
    add_node(pkg_nid, pkg_name)
    add_edge(file_nid, pkg_nid, "contains")

    for item in xml_root.findall(".//RequiredPkgs/"):
        dep_elem = item.find("PackageName")
        if dep_elem is not None:
            dep_name = dep_elem.get("Value", "")
            if dep_name:
                dep_nid = _make_id(dep_name)
                add_node(dep_nid, dep_name)
                add_edge(pkg_nid, dep_nid, "imports", context="import")

    # Unidades listadas → contém arestas, resolvidas para IDs baseados em caminho sempre que possível
    for item in xml_root.findall(".//Files/"):
        unit_elem = item.find("UnitName")
        if unit_elem is not None:
            unit_name = unit_elem.get("Value", "")
            if unit_name:
                unit_nid = _pascal_resolve_unit(path, unit_name)
                add_node(unit_nid, unit_name)
                add_edge(pkg_nid, unit_nid, "contains")

    return {"nodes": nodes, "edges": edges, "input_tokens": 0, "output_tokens": 0}


# ── Extração principal e collect_files ────────────────────── ──────────────────────


def _check_tree_sitter_version() -> None:
    """Raise a clear error if tree-sitter is too old for the new Language API."""
    try:
        from tree_sitter import LANGUAGE_VERSION
    except ImportError:
        raise ImportError(
            "tree-sitter is not installed. Run: pip install 'tree-sitter>=0.23.0'"
        )
    # API de linguagem v2 começa em LANGUAGE_VERSION 14
    if LANGUAGE_VERSION < 14:
        import tree_sitter as _ts
        raise RuntimeError(
            f"tree-sitter {getattr(_ts, '__version__', 'unknown')} is too old. "
            f"omnigraph requires tree-sitter >= 0.23.0 (Language API v2). "
            f"Run: pip install --upgrade tree-sitter"
        )




def extract_slnx(path: Path) -> dict:
    """Extract projects and inter-project dependencies from a .slnx file.

    .slnx is the XML-based replacement for the legacy .sln format. Projects
    are listed as ``<Project Path="..."/>`` elements (optionally nested inside
    ``<Folder>`` elements) and build-order dependencies as ``<BuildDependency
    Project="..."/>`` children. Unlike .sln there are no GUIDs -- projects are
    identified by their path.
    """
    import defusedxml.ElementTree as ET

    try:
        src = path.read_bytes()
    except OSError:
        return {"nodes": [], "edges": [], "error": f"cannot read {path}"}

    if len(src) > _PROJECT_XML_MAX_BYTES:
        return {"nodes": [], "edges": [], "error": "project file too large"}
    if not _project_xml_is_safe(src):
        return {"nodes": [], "edges": [],
                "error": "refusing XML with DOCTYPE/ENTITY declaration"}

    try:
        tree = ET.fromstring(src)
    except ET.ParseError as e:
        return {"nodes": [], "edges": [], "error": f"XML parse error: {e}"}

    file_nid = _make_id(str(path))
    str_path = str(path)
    nodes: list[dict] = [{"id": file_nid, "label": path.name, "file_type": "code",
                          "source_file": str_path, "source_location": None}]
    edges: list[dict] = []
    seen_ids: set[str] = set()
    seen_ids.add(file_nid)

    ns = ""
    if tree.tag.startswith("{"):
        ns = tree.tag.split("}")[0] + "}"

    def _resolve(proj_path: str) -> str:
        proj_path = proj_path.replace("\\", "/")
        try:
            return str((path.parent / proj_path).resolve())
        except Exception:
            return proj_path

    # Primeira passagem: colete projetos (em qualquer lugar da árvore, inclusive <Pasta>).
    project_nids: set[str] = set()
    for proj in tree.iter(f"{ns}Project"):
        proj_path = proj.get("Path")
        if not proj_path:
            continue
        abs_proj = _resolve(proj_path)
        proj_nid = _make_id(abs_proj)
        if proj_nid and proj_nid not in seen_ids:
            seen_ids.add(proj_nid)
            label = Path(proj_path).stem
            nodes.append({"id": proj_nid, "label": label,
                          "file_type": "code", "source_file": abs_proj,
                          "source_location": None})
            edges.append({"source": file_nid, "target": proj_nid,
                          "relation": "contains", "confidence": "EXTRACTED",
                          "source_file": str_path, "weight": 1.0})
        if proj_nid:
            project_nids.add(proj_nid)

    for proj in tree.iter(f"{ns}Project"):
        proj_path = proj.get("Path")
        if not proj_path:
            continue
        from_nid = _make_id(_resolve(proj_path))
        for dep in proj.iter(f"{ns}BuildDependency"):
            dep_path = dep.get("Project")
            if not dep_path:
                continue
            to_nid = _make_id(_resolve(dep_path))
            if (from_nid and to_nid and from_nid != to_nid
                    and to_nid in project_nids):
                edges.append({"source": from_nid, "target": to_nid,
                              "relation": "imports", "confidence": "EXTRACTED",
                              "source_file": str_path, "weight": 1.0})

    return {"nodes": nodes, "edges": edges}


def extract_csproj(path: Path) -> dict:
    """Extract packages, project refs, and target framework from a .csproj/.fsproj/.vbproj."""
    import defusedxml.ElementTree as ET

    try:
        src = path.read_bytes()
    except OSError:
        return {"nodes": [], "edges": [], "error": f"cannot read {path}"}

    if len(src) > _PROJECT_XML_MAX_BYTES:
        return {"nodes": [], "edges": [], "error": "project file too large"}
    if not _project_xml_is_safe(src):
        return {"nodes": [], "edges": [],
                "error": "refusing XML with DOCTYPE/ENTITY declaration"}

    try:
        tree = ET.fromstring(src)
    except ET.ParseError as e:
        return {"nodes": [], "edges": [], "error": f"XML parse error: {e}"}

    file_nid = _make_id(str(path))
    str_path = str(path)
    nodes: list[dict] = [{"id": file_nid, "label": path.name, "file_type": "code",
                          "source_file": str_path, "source_location": None}]
    edges: list[dict] = []
    seen_ids: set[str] = set()
    seen_ids.add(file_nid)

    ns = ""
    root_tag = tree.tag
    if root_tag.startswith("{"):
        ns = root_tag.split("}")[0] + "}"

    def find_all(tag: str):
        return tree.iter(f"{ns}{tag}")

    for tf in find_all("TargetFramework"):
        if tf.text:
            fw_nid = _make_id("framework", tf.text.strip())
            if fw_nid and fw_nid not in seen_ids:
                seen_ids.add(fw_nid)
                nodes.append({"id": fw_nid, "label": tf.text.strip(),
                              "file_type": "concept", "source_file": str_path,
                              "source_location": None})
                edges.append({"source": file_nid, "target": fw_nid,
                              "relation": "references", "confidence": "EXTRACTED",
                              "source_file": str_path, "weight": 1.0})

    for tf in find_all("TargetFrameworks"):
        if tf.text:
            for fw in tf.text.strip().split(";"):
                fw = fw.strip()
                if fw:
                    fw_nid = _make_id("framework", fw)
                    if fw_nid and fw_nid not in seen_ids:
                        seen_ids.add(fw_nid)
                        nodes.append({"id": fw_nid, "label": fw,
                                      "file_type": "concept", "source_file": str_path,
                                      "source_location": None})
                        edges.append({"source": file_nid, "target": fw_nid,
                                      "relation": "references", "confidence": "EXTRACTED",
                                      "source_file": str_path, "weight": 1.0})

    for pkg in find_all("PackageReference"):
        name = pkg.get("Include") or pkg.get("include") or ""
        version = pkg.get("Version") or pkg.get("version") or ""
        if not name:
            continue
        pkg_nid = _make_id("nuget", name)
        label = f"{name} ({version})" if version else name
        if pkg_nid and pkg_nid not in seen_ids:
            seen_ids.add(pkg_nid)
            nodes.append({"id": pkg_nid, "label": label,
                          "file_type": "code", "source_file": str_path,
                          "source_location": None})
        edges.append({"source": file_nid, "target": pkg_nid,
                      "relation": "imports", "confidence": "EXTRACTED",
                      "source_file": str_path, "weight": 1.0})

    for proj in find_all("ProjectReference"):
        ref_path = proj.get("Include") or proj.get("include") or ""
        if not ref_path:
            continue
        ref_path_norm = ref_path.replace("\\", "/")
        try:
            abs_ref = str((path.parent / ref_path_norm).resolve())
        except Exception:
            abs_ref = ref_path_norm
        proj_nid = _make_id(abs_ref)
        if proj_nid and proj_nid not in seen_ids:
            seen_ids.add(proj_nid)
            proj_label = Path(ref_path_norm).name
            nodes.append({"id": proj_nid, "label": proj_label,
                          "file_type": "code", "source_file": abs_ref,
                          "source_location": None})
        edges.append({"source": file_nid, "target": proj_nid,
                      "relation": "imports", "confidence": "EXTRACTED",
                      "source_file": str_path, "weight": 1.0})

    sdk = tree.get("Sdk") or ""
    if sdk:
        sdk_nid = _make_id("sdk", sdk)
        if sdk_nid and sdk_nid not in seen_ids:
            seen_ids.add(sdk_nid)
            nodes.append({"id": sdk_nid, "label": sdk,
                          "file_type": "concept", "source_file": str_path,
                          "source_location": None})
            edges.append({"source": file_nid, "target": sdk_nid,
                          "relation": "references", "confidence": "EXTRACTED",
                          "source_file": str_path, "weight": 1.0})

    return {"nodes": nodes, "edges": edges}


def _xml_local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1] if name.startswith("{") else name


# para diferenciar um manipulador de eventos real no code-behind de um método comum
# tipo de args qualificado para namespace e um `EventArgs<T>` genérico.
_EVENT_HANDLER_SIGNATURE_RE = re.compile(
    r"\(\s*object\??\s+\w+\s*,\s*[\w.]*EventArgs(?:<[^>]*>)?\s+\w+\s*\)"
)

# um manipulador de eventos. Eles são ignorados ao combinar valores de atributos com code-behind
# métodos, por exemplo Content="Save" ou Tag="Refresh" não podem fabricar uma aresta de evento.
_XAML_NON_EVENT_ATTRS = frozenset({
    "Name", "Content", "Text", "Title", "Tag", "ToolTip", "Header",
    "Class", "Key", "Uid", "DataContext", "Style", "Source",
})

# Um valor de atributo do manipulador é um nome de método simples (por exemplo, Click="Save_Click"), não
# marcação, um caminho ou uma frase. Usado para pular valores como "{Binding ...}" ou
# conteúdo de formato livre antes de considerá-los como métodos de code-behind.
_XAML_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_XAML_DESIGN_INSTANCE_TYPE_RE = re.compile(
    r"\bType\s*=\s*(?:\{x:Type\s+)?(?P<type>[\w.:+]+)"
)


def _xaml_markup_extension(value: str) -> tuple[str, str] | None:
    value = value.strip()
    if not (value.startswith("{") and value.endswith("}")):
        return None
    inner = value[1:-1].strip()
    if not inner or inner.startswith("}"):
        return None
    name, _, args = inner.partition(" ")
    return name, args.strip()


def _xaml_split_markup_args(args: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    for idx, ch in enumerate(args):
        if ch == "{":
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(args[start:idx].strip())
            start = idx + 1
    tail = args[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _xaml_static_resource_key(value: str) -> str | None:
    markup = _xaml_markup_extension(value)
    if not markup:
        return None
    name, args = markup
    if name != "StaticResource":
        return None
    for part in _xaml_split_markup_args(args):
        if "=" not in part:
            return part.strip() or None
        key, resource = part.split("=", 1)
        if key.strip() == "ResourceKey":
            return resource.strip() or None
    return None


def _xaml_binding_refs(value: str) -> tuple[str | None, str | None]:
    markup = _xaml_markup_extension(value)
    if not markup:
        return None, None
    name, args = markup
    if name != "Binding":
        return None, None

    path_ref = None
    converter_ref = None
    for part in _xaml_split_markup_args(args):
        if not part:
            continue
        if "=" not in part:
            if path_ref is None:
                path_ref = part.strip()
            continue
        key, raw_value = part.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if key == "Path":
            path_ref = raw_value
        elif key == "Converter":
            converter_ref = _xaml_static_resource_key(raw_value)

    if path_ref and ("{" in path_ref or "}" in path_ref):
        path_ref = None
    return path_ref or None, converter_ref or None


def _xaml_codebehind_path(path: Path) -> Path | None:
    expected = path.with_suffix(path.suffix + ".cs")
    if expected.exists():
        return expected
    try:
        for sibling in path.parent.iterdir():
            if sibling.name.casefold() == expected.name.casefold():
                return sibling
    except OSError:
        return None
    return None


def _xaml_codebehind_symbols(
    path: Path,
    class_name: str | None,
) -> tuple[dict | None, dict[str, dict], list[dict]]:
    codebehind = _xaml_codebehind_path(path)
    if not codebehind:
        return None, {}, []
    result = extract_csharp(codebehind)
    if result.get("error"):
        return None, {}, []

    class_simple = class_name.rsplit(".", 1)[-1] if class_name else None
    class_node = None
    if class_simple:
        for node in result.get("nodes", []):
            if node.get("label") == class_simple:
                class_node = node
                break

    class_method_edges: list[dict] = []
    if class_node:
        class_id = class_node.get("id")
        for edge in result.get("edges", []):
            if edge.get("source") == class_id and edge.get("relation") == "method":
                class_method_edges.append(edge)
    method_ids = {edge.get("target") for edge in class_method_edges} if class_node else None

    # Somente métodos com assinatura de manipulador de eventos .NET - (remetente do objeto,
    # <T>EventArgs e) - são elegíveis para serem conectados a um atributo XAML como um
    # nome do método (por exemplo, Content="Save" próximo a um método de negócios Save()) seria
    # produzir uma aresta de "evento" espúria. O extrator C# não registra o
    # lista de parâmetros nos nós do método, então a lemos na fonte code-behind
    # na linha gravada do método.
    try:
        cb_lines = codebehind.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        cb_lines = []

    def _has_event_handler_signature(node: dict) -> bool:
        loc = str(node.get("source_location") or "")
        m = re.match(r"L(\d+)", loc)
        if not m or not cb_lines:
            return False
        start = int(m.group(1)) - 1
        snippet = " ".join(cb_lines[start:start + 3])
        return _EVENT_HANDLER_SIGNATURE_RE.search(snippet) is not None

    methods: dict[str, dict] = {}
    for node in result.get("nodes", []):
        if method_ids is not None and node.get("id") not in method_ids:
            continue
        label = str(node.get("label", ""))
        if label.startswith(".") and label.endswith("()") and _has_event_handler_signature(node):
            methods[label.strip("()").lstrip(".")] = node
    return class_node, methods, class_method_edges


def _xaml_type_simple_name(type_ref: str) -> str | None:
    type_ref = type_ref.strip().strip("{}")
    if not type_ref:
        return None
    type_ref = type_ref.split(",", 1)[0].strip()
    if type_ref.startswith("x:Type "):
        type_ref = type_ref[len("x:Type "):].strip()
    if ":" in type_ref:
        type_ref = type_ref.rsplit(":", 1)[-1]
    if "." in type_ref:
        type_ref = type_ref.rsplit(".", 1)[-1]
    if "+" in type_ref:
        type_ref = type_ref.rsplit("+", 1)[-1]
    return type_ref if _XAML_IDENT_RE.fullmatch(type_ref) else None


def _xaml_explicit_viewmodel_names(tree) -> tuple[bool, list[str]]:
    has_data_context = False
    names: list[str] = []
    for elem in tree.iter():
        elem_type = _xml_local_name(elem.tag)
        if elem_type.endswith(".DataContext") or elem_type == "DataContext":
            has_data_context = True
            for child in list(elem):
                vm_name = _xaml_type_simple_name(_xml_local_name(child.tag))
                if vm_name and vm_name not in names:
                    names.append(vm_name)
        for key, value in elem.attrib.items():
            if _xml_local_name(key) != "DataContext" or not value:
                continue
            has_data_context = True
            match = _XAML_DESIGN_INSTANCE_TYPE_RE.search(value)
            if match:
                vm_name = _xaml_type_simple_name(match.group("type"))
                if vm_name and vm_name not in names:
                    names.append(vm_name)
    return has_data_context, names


def _xaml_prism_autowire_viewmodel(tree) -> bool:
    for elem in tree.iter():
        for key, value in elem.attrib.items():
            if (
                _xml_local_name(key).endswith("ViewModelLocator.AutoWireViewModel")
                and value.strip().lower() == "true"
            ):
                return True
    return False


def _xaml_inferred_viewmodel_names(view_name: str | None) -> list[str]:
    if not view_name:
        return []
    names: list[str] = []

    def add(name: str) -> None:
        if name.endswith("ViewModel") and name not in names:
            names.append(name)

    if view_name == "MainWindow":
        add("MainWindowViewModel")
        add("MainViewModel")
    for suffix in ("UserControl", "View", "Page", "Control"):
        if view_name.endswith(suffix) and len(view_name) > len(suffix):
            add(view_name[:-len(suffix)] + "ViewModel")
            break
    return names


def _xaml_project_root(path: Path) -> Path:
    project_markers = (".csproj", ".fsproj", ".vbproj", ".sln", ".slnx")
    root = path.parent
    for directory in (path.parent, *path.parent.parents):
        try:
            if any(child.suffix in project_markers for child in directory.iterdir()):
                root = directory
                break
        except OSError:
            continue
    if _XAML_ACTIVE_EXTRACT_ROOT is None:
        return root
    boundary = _XAML_ACTIVE_EXTRACT_ROOT.resolve()
    try:
        root.resolve().relative_to(boundary)
        return root
    except ValueError:
        return boundary


def _xaml_csharp_class_nodes(path: Path) -> dict[str, list[dict]]:
    from omnigraph.detect import _is_ignored, _is_noise_dir, _load_omnigraphignore
    root = _xaml_project_root(path)
    cache_key = str(root.resolve()) if _XAML_ACTIVE_EXTRACT_ROOT is not None else None
    if cache_key and cache_key in _XAML_CSHARP_CLASS_CACHE:
        return _XAML_CSHARP_CLASS_CACHE[cache_key]
    classes: dict[str, list[dict]] = {}
    patterns = _load_omnigraphignore(root)
    ignore_cache: dict[Path, bool] = {}
    import os as _os
    _DIR_CAP = 20000
    cs_files: list[Path] = []
    visited = 0
    try:
        for dirpath, dirnames, filenames in _os.walk(root):
            dirnames[:] = [
                d for d in dirnames if not d.startswith(".") and not _is_noise_dir(d)
            ]
            for fn in filenames:
                if fn.endswith(".cs"):
                    cs_files.append(Path(dirpath) / fn)
            visited += 1
            if visited >= _DIR_CAP:
                break
    except OSError:
        return classes
    cs_files.sort()
    for cs_path in cs_files:
        if patterns and _is_ignored(cs_path, root, patterns, _cache=ignore_cache):
            continue
        result = extract_csharp(cs_path)
        if result.get("error"):
            continue
        for node in result.get("nodes", []):
            label = str(node.get("label", ""))
            if not label.endswith("ViewModel") or not _XAML_IDENT_RE.fullmatch(label):
                continue
            if node.get("source_file"):
                classes.setdefault(label, []).append(node)
    if cache_key:
        _XAML_CSHARP_CLASS_CACHE[cache_key] = classes
    return classes


def _xaml_pascal_name(name: str) -> str | None:
    name = name.strip().lstrip("_")
    if name.startswith("m_"):
        name = name[2:]
    return name[:1].upper() + name[1:] if _XAML_IDENT_RE.fullmatch(name) else None


_XAML_TOOLKIT_FIELD_RE = re.compile(r"\b(?P<name>_?m?_?[A-Za-z_]\w*)\s*(?:=.*)?;")
_XAML_TOOLKIT_METHOD_RE = re.compile(r"\b(?P<name>[A-Za-z_]\w*)\s*\(")
_XAML_ACTIVE_EXTRACT_ROOT: Path | None = None
_XAML_CSHARP_CLASS_CACHE: dict[str, dict[str, list[dict]]] = {}


def _xaml_communitytoolkit_members(vm_node: dict) -> tuple[dict[str, dict], list[dict]]:
    source_file = vm_node.get("source_file")
    vm_id = vm_node.get("id")
    if not source_file or not vm_id:
        return {}, []
    try:
        # erros = "substituir" para que um code-behind não UTF8 não possa gerar UnicodeDecodeError
        lines = Path(source_file).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}, []

    members: dict[str, dict] = {}
    edges: list[dict] = []

    def add_member(label: str, line_no: int, context: str) -> None:
        nid = _make_id(vm_id, label)
        members[label] = {
            "id": nid,
            "label": label,
            "file_type": "code",
            "source_file": source_file,
            "source_location": f"L{line_no}",
        }
        edges.append({
            "source": vm_id,
            "target": nid,
            "relation": "defines",
            "confidence": "INFERRED",
            "source_file": source_file,
            "source_location": f"L{line_no}",
            "weight": 1.0,
            "context": context,
        })

    pending: tuple[str, int] | None = None
    for line_no, line in enumerate(lines, 1):
        remainder = line.split("]", 1)[1].strip() if "]" in line else ""
        if "[" in line and "ObservableProperty" in line:
            pending = ("property", line_no)
            if not remainder:
                continue
            line = remainder
        if "[" in line and "RelayCommand" in line:
            pending = ("command", line_no)
            if not remainder:
                continue
            line = remainder
        if not pending or not line.strip() or line.lstrip().startswith("["):
            continue

        kind, attr_line = pending
        pending = None
        if kind == "property":
            match = _XAML_TOOLKIT_FIELD_RE.search(line)
            label = _xaml_pascal_name(match.group("name")) if match else None
            if label:
                add_member(label, attr_line, "communitytoolkit_observable_property")
        else:
            match = _XAML_TOOLKIT_METHOD_RE.search(line)
            if match:
                method = match.group("name").removesuffix("Async")
                add_member(f"{method}Command", attr_line, "communitytoolkit_relay_command")

    return members, edges


def extract_xaml(path: Path) -> dict:
    """Extract WPF/XAML structure, bindings, x:Class, and event handler references."""
    import defusedxml.ElementTree as ET

    try:
        src = path.read_bytes()
    except OSError:
        return {"nodes": [], "edges": [], "error": f"cannot read {path}"}

    if len(src) > _PROJECT_XML_MAX_BYTES:
        return {"nodes": [], "edges": [], "error": "xaml file too large"}
    if not _project_xml_is_safe(src):
        return {"nodes": [], "edges": [],
                "error": "refusing XML with DOCTYPE/ENTITY declaration"}

    try:
        tree = ET.fromstring(src)
    except ET.ParseError as e:
        return {"nodes": [], "edges": [], "error": f"XML parse error: {e}"}

    text = src.decode("utf-8", errors="replace")
    lines = text.splitlines()
    str_path = str(path)
    stem = _file_stem(path)
    file_nid = _make_id(str(path))
    root_type = _xml_local_name(tree.tag)
    root_nid = _make_id(stem, root_type)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    seen_edges: set[tuple[str, str, str, str | None]] = set()

    def line_for(value: str | None) -> int:
        if value:
            for idx, line in enumerate(lines, 1):
                if value in line:
                    return idx
        return 1

    def add_node(
        nid: str,
        label: str,
        line: int | None,
        *,
        file_type: str = "code",
        source_file: str = str_path,
    ) -> None:
        if nid in seen_ids:
            return
        seen_ids.add(nid)
        nodes.append({
            "id": nid, "label": label, "file_type": file_type,
            "source_file": source_file,
            "source_location": f"L{line}" if line else None,
        })

    def add_existing_node(node: dict | None) -> None:
        if not node:
            return
        nid = node.get("id")
        if not nid or nid in seen_ids:
            return
        seen_ids.add(nid)
        nodes.append(dict(node))

    def add_edge(
        src_nid: str,
        tgt_nid: str,
        relation: str,
        line: int,
        *,
        context: str | None = None,
        source_file: str = str_path,
        confidence: str = "EXTRACTED",
    ) -> None:
        key = (src_nid, tgt_nid, relation, context)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edge = {
            "source": src_nid, "target": tgt_nid, "relation": relation,
            "confidence": confidence, "source_file": source_file,
            "source_location": f"L{line}", "weight": 1.0,
        }
        if context:
            edge["context"] = context
        edges.append(edge)

    def add_existing_edge(edge: dict) -> None:
        key = (edge.get("source"), edge.get("target"), edge.get("relation"), edge.get("context"))
        if key in seen_edges:
            return
        seen_edges.add(key)
        edges.append(dict(edge))

    add_node(file_nid, path.name, 1)
    add_node(root_nid, root_type, 1)
    add_edge(file_nid, root_nid, "contains", 1)

    class_name = None
    for key, value in tree.attrib.items():
        if _xml_local_name(key) == "Class" and value:
            class_name = value.strip()
            break

    class_node, codebehind_methods, class_method_edges = _xaml_codebehind_symbols(path, class_name)
    if class_name:
        if class_node:
            class_nid = class_node["id"]
            add_existing_node(class_node)
        else:
            class_label = class_name.rsplit(".", 1)[-1]
            class_nid = _make_id(stem, class_label)
            add_node(class_nid, class_label, line_for(class_name))
        add_edge(root_nid, class_nid, "references", line_for(class_name), context="x_class")

    has_data_context, vm_names = _xaml_explicit_viewmodel_names(tree)
    prism_autowire = _xaml_prism_autowire_viewmodel(tree)
    vm_confidence = "EXTRACTED"
    if not has_data_context:
        view_name = class_name.rsplit(".", 1)[-1] if class_name else None
        view_name = view_name or (path.stem if prism_autowire else None)
        vm_names = _xaml_inferred_viewmodel_names(view_name)
        vm_confidence = "INFERRED"
    generated_members: dict[str, dict] = {}
    generated_member_edges: list[dict] = []
    if vm_names:
        csharp_classes = _xaml_csharp_class_nodes(path)
        vm_candidates = []
        for vm_name in vm_names:
            vm_candidates.extend(csharp_classes.get(vm_name, []))
        by_id = {node.get("id"): node for node in vm_candidates if node.get("id")}
        if len(by_id) == 1:
            vm_node = next(iter(by_id.values()))
            add_existing_node(vm_node)
            add_edge(
                root_nid,
                vm_node["id"],
                "references",
                line_for(vm_node["label"]),
                context="view_model",
                confidence=vm_confidence,
            )
            generated_members, generated_member_edges = _xaml_communitytoolkit_members(vm_node)
            for member in generated_members.values():
                add_existing_node(member)
            for member_edge in generated_member_edges:
                add_existing_edge(member_edge)

    for elem in tree.iter():
        elem_type = _xml_local_name(elem.tag)
        elem_name = None
        for key, value in elem.attrib.items():
            if _xml_local_name(key) == "Name" and value:
                elem_name = value.strip()
                break
        owner_nid = root_nid
        if elem_name:
            owner_nid = _make_id(stem, elem_name)
            add_node(owner_nid, elem_name, line_for(elem_name))
            add_edge(root_nid, owner_nid, "contains", line_for(elem_name))
            type_nid = _make_id("xaml", elem_type)
            add_node(type_nid, elem_type, line_for(elem_name), file_type="concept")
            add_edge(owner_nid, type_nid, "references", line_for(elem_name), context="type")

        for key, value in elem.attrib.items():
            value = value or ""
            # Ligação de eventos: um atributo faz referência a um manipulador somente quando seu local
            # nome não é uma propriedade de identidade/forma livre conhecida, seu valor é simples
            # identificador (um nome de método, não uma marcação ou uma frase) e o correspondente
            # O método code-behind na verdade tem uma assinatura de manipulador de eventos (o portão
            # de fabricar arestas de eventos contra métodos comuns com o mesmo nome.
            attr_local = _xml_local_name(key)
            if attr_local not in _XAML_NON_EVENT_ATTRS and _XAML_IDENT_RE.fullmatch(value):
                method = codebehind_methods.get(value)
                if method:
                    add_existing_node(method)
                    add_edge(owner_nid, method["id"], "references", line_for(value), context="event")
                    for method_edge in class_method_edges:
                        if method_edge.get("target") == method["id"]:
                            add_existing_node(class_node)
                            add_existing_edge(method_edge)
                            break
            binding_path, binding_converter = _xaml_binding_refs(value)
            if binding_path:
                bind_nid = _make_id("binding", binding_path)
                add_node(bind_nid, binding_path, line_for(value), file_type="concept")
                binding_context = (
                    "binding_command"
                    if attr_local == "Command" or attr_local.endswith(".Command")
                    else "binding_path"
                )
                add_edge(owner_nid, bind_nid, "references", line_for(value), context=binding_context)
                generated_member = generated_members.get(binding_path)
                if generated_member:
                    add_existing_node(generated_member)
                    add_edge(
                        owner_nid,
                        generated_member["id"],
                        "references",
                        line_for(value),
                        context=binding_context,
                        confidence="INFERRED",
                    )
            if binding_converter:
                converter_nid = _make_id("binding_converter", binding_converter)
                add_node(converter_nid, binding_converter, line_for(value), file_type="concept")
                add_edge(owner_nid, converter_nid, "references", line_for(value), context="binding_converter")
            if elem_type == "Binding" and attr_local == "Path":
                direct_path = value.strip()
                if direct_path and "{" not in direct_path and "}" not in direct_path:
                    bind_nid = _make_id("binding", direct_path)
                    add_node(bind_nid, direct_path, line_for(value), file_type="concept")
                    add_edge(owner_nid, bind_nid, "references", line_for(value), context="binding_path")
            if elem_type == "Binding" and attr_local == "Converter":
                direct_converter = _xaml_static_resource_key(value)
                if direct_converter:
                    converter_nid = _make_id("binding_converter", direct_converter)
                    add_node(converter_nid, direct_converter, line_for(value), file_type="concept")
                    add_edge(owner_nid, converter_nid, "references", line_for(value), context="binding_converter")

    return {"nodes": nodes, "edges": edges}


# Nomes de arquivos JSON de configuração/manifesto que o extrator estrutural entende. Qualquer coisa
# else (acessórios de avaliação, conjuntos de dados, GeoJSON, dumps de API) são *dados* e NÃO devem ser
# AST entrou em nós por chave - que inunda o grafo com nós-chave órfãos
# e comunidades quase duplicadas. Os dados JSON são deixados para a semântica do LLM
# passe em vez disso. Correspondido sem distinção entre maiúsculas e minúsculas em relação ao nome do arquivo simples.

# Chaves de nível superior que provam que um objeto JSON é uma configuração/manifesto que o extrator pode
# desenhe arestas *arquivos cruzados* de (deps, estende cadeias, referências de esquema).


# A identidade DM é baseada em caminho (`/datum/object/proc/New()`), não baseada em bloco, então
# o andador genérico de classe não se encaixa bem.


# .dmi é um PNG com um pedaço de "Descrição" tEXt/zTXt contendo o estado BYOND
# metadados. Queremos os nomes dos estados dos ícones (icon_state = "X" no código DM


# Um .dmm começa com um dicionário de blocos — cada "chave" = (tipo, tipo{var=val}, ...)
# nomeia um ou mais tipos que compõem um bloco - depois uma grade. Precisamos apenas do
# seção de dicionário: todo caminho de tipo referenciado é uma aresta `uses`.




# Tokens principais em uma travessia HCL que são meta/builtins, não referências a um


_DISPATCH: dict[str, Any] = {
    ".py": extract_python,
    ".js": extract_js,
    ".jsx": extract_js,
    ".mjs": extract_js,
    ".cjs": extract_js,
    ".ts": extract_js,
    ".tsx": extract_js,
    ".mts": extract_js,
    ".cts": extract_js,
    ".go": extract_go,
    ".rs": extract_rust,
    ".java": extract_java,
    ".groovy": extract_groovy,
    ".gradle": extract_groovy,
    ".c": extract_c,
    ".h": extract_c,
    ".cpp": extract_cpp,
    ".cc": extract_cpp,
    ".cxx": extract_cpp,
    ".hpp": extract_cpp,
    ".cu": extract_cpp,
    ".cuh": extract_cpp,
    ".metal": extract_cpp,
    ".rb": extract_ruby, ".rake": extract_ruby,
    ".cs": extract_csharp,
    ".kt": extract_kotlin,
    ".kts": extract_kotlin,
    ".scala": extract_scala,
    ".php": extract_php,
    ".swift": extract_swift,
    ".lua": extract_lua,
    ".luau": extract_lua,
    ".toc": extract_lua,
    ".zig": extract_zig,
    ".ps1": extract_powershell,
    ".psm1": extract_powershell,
    ".psd1": extract_powershell_manifest,
    ".ex": extract_elixir,
    ".exs": extract_elixir,
    ".m": extract_objc,
    ".mm": extract_objc,
    ".jl": extract_julia,
    ".f": extract_fortran,
    ".F": extract_fortran,
    ".f90": extract_fortran,
    ".F90": extract_fortran,
    ".f95": extract_fortran,
    ".F95": extract_fortran,
    ".f03": extract_fortran,
    ".F03": extract_fortran,
    ".f08": extract_fortran,
    ".F08": extract_fortran,
    ".vue": extract_vue,
    ".svelte": extract_svelte,
    ".astro": extract_astro,
    ".dart": extract_dart,
    ".ml": extract_ocaml,
    ".mli": extract_ocaml,
    ".lisp": extract_commonlisp,
    ".cl": extract_commonlisp,
    ".lsp": extract_commonlisp,
    ".asd": extract_commonlisp,
    ".v": extract_verilog,
    ".sv": extract_verilog,
    ".svh": extract_verilog,
    ".sql": extract_sql,
    ".md": extract_markdown,
    ".mdx": extract_markdown,
    ".qmd": extract_markdown,
    ".skill": extract_markdown,
    ".pas": extract_pascal,
    ".pp": extract_pascal,
    ".dpr": extract_pascal,
    ".dpk": extract_pascal,
    ".lpr": extract_pascal,
    ".inc": extract_pascal,
    ".dfm": extract_delphi_form,
    ".lfm": extract_lazarus_form,
    ".lpk": extract_lazarus_package,
    ".sh": extract_bash,
    ".bash": extract_bash,
    ".json": extract_json,
    ".tf": extract_terraform,
    ".tfvars": extract_terraform,
    ".hcl": extract_terraform,
    ".dm": extract_dm,
    ".dme": extract_dm,
    ".dmi": extract_dmi,
    ".dmm": extract_dmm,
    ".dmf": extract_dmf,
    ".sln": extract_sln,
    ".slnx": extract_slnx,
    ".csproj": extract_csproj,
    ".fsproj": extract_csproj,
    ".vbproj": extract_csproj,
    ".xaml": extract_xaml,
    ".razor": extract_razor,
    ".cshtml": extract_razor,
    ".cls": extract_apex,
    ".trigger": extract_apex,
}


# Extensões cujo extrator depende de um extra de dependência opcional
# extract() para informar ao usuário qual extra restaura o idioma.
_EXTRA_FOR_EXTENSION = {
    ".sql": "sql",
    ".tf": "terraform",
    ".tfvars": "terraform",
    ".hcl": "terraform",
    ".dm": "dm",
    ".dme": "dm",
    ".ml": "ocaml",
    ".mli": "ocaml",
    ".lisp": "commonlisp",
    ".cl": "commonlisp",
    ".lsp": "commonlisp",
    ".asd": "commonlisp",
}

_DEP_MISSING_MARKER = "not installed"
_DEP_LOAD_FAILED_MARKER = "failed to load"


# Executáveis ​​sem extensão (pontos de entrada CLI como `devctl` ou `manage`) carregam
# sua língua no shebang, não no sufixo. detect.classify_file já
# honram o mesmo sinal ou esses arquivos são classificados como código e então silenciosamente
# descartado por extração. Somente intérpretes com um extrator real são mapeados —
# o conjunto mais amplo do detect (perl, fish, tcsh, Rscript) permanece não mapeado e ignorado.
_SHEBANG_DISPATCH: dict[str, Any] = {
    "python": extract_python,
    "python2": extract_python,
    "python3": extract_python,
    "bash": extract_bash,
    "sh": extract_bash,
    "dash": extract_bash,
    "zsh": extract_bash,
    "ksh": extract_bash,
    "node": extract_js,
    "nodejs": extract_js,
    "ruby": extract_ruby,
    "lua": extract_lua,
    "php": extract_php,
    "julia": extract_julia,
}


# Diretivas somente ObjC. Eles são ilegais em C e C++, portanto, encontrar um em `.h`
# arquivo é um sinal falso-positivo quase zero de que o cabeçalho é Objective-C (e assim
# pertence a extract_objc, não a extract_c). `@property` é deliberadamente excluído:
# funciona como um comando de comentário Doxygen e as propriedades ObjC só vivem dentro de um
# @interface/@protocol de qualquer maneira, então as diretivas mais fortes já os cobrem.
# `#import` está incluído porque um cabeçalho ObjC *bridging* geralmente nada mais é do que
# `#import` como um `preproc_call` (não `preproc_include`), então cada aresta de importação é
# descartado e o cabeçalho é isolado. `#import` é uma diretiva somente ObjC (ilegal
# em C e C++), portanto, isso não sequestrará cabeçalhos C/C++ genuínos e extract_objc
_OBJC_HEADER_MARKERS = (b"@interface", b"@protocol", b"@implementation", b"@import", b"#import")


def _is_objc_header(path: Path) -> bool:
    """Whether a `.h` file is Objective-C rather than C/C++ (#1475).

    `.h` is shared by C, C++, and ObjC; the suffix map routes it to extract_c,
    which silently drops every @interface/@protocol/@property/method (1 node, 0
    edges). Sniffing for an ObjC-only directive reroutes genuine ObjC headers to
    extract_objc while leaving every C/C++ header on its existing extractor.
    """
    try:
        head = path.read_bytes()[:256 * 1024]
    except OSError:
        return False
    return any(marker in head for marker in _OBJC_HEADER_MARKERS)


# Sinais somente C++. Nenhum deles é válido em um cabeçalho C simples, portanto, encontrar um
# em um `.h` é um sinal de alta confiança, o cabeçalho é C++. A gramática C
# não tem class_specifier, então um cabeçalho `class Foo { ... };` roteado para extract_c
# perde a classe e seus protótipos de método (um nó lixo `foo_foo` + um nó sem fonte
# esboço de `classe`); o roteamento para extract_cpp recupera o tipo real. Mantido CONSERVADOR:
# um cabeçalho C simples sem nenhum desses permanece em extract_c. A detecção do ObjC continua
# prioridade (um cabeçalho ObjC pode conter legitimamente `::`/`class` dentro de um inline
_CPP_HEADER_MARKERS = (
    b"class ", b"namespace ", b"template", b"::",
    b"public:", b"private:", b"protected:",
)


def _is_objc_source(path: Path) -> bool:
    """Whether a `.m` file is Objective-C rather than MATLAB/Octave (#1702).

    `.m` is shared by Objective-C implementation files and MATLAB (also Octave).
    The suffix map routes `.m` to extract_objc unconditionally, which force-parses
    MATLAB through the Objective-C tree-sitter grammar and emits garbage nodes/edges
    (worse than skipping). A genuine ObjC `.m` always carries an ObjC directive
    (@implementation/@interface/@import/#import); MATLAB has none of them. Reuses
    the same marker set as the `.h` sniff. `.mm` is unambiguously Objective-C++ and
    is not sniffed.
    """
    return _is_objc_header(path)


def _is_cpp_header(path: Path) -> bool:
    """Whether a `.h` file is C++ rather than plain C (#1547).

    Mirrors `_is_objc_header`: sniffs for a C++-only token. Used only to reroute
    a `.h` from extract_c to extract_cpp when no ObjC marker is present (ObjC has
    priority). Conservative by construction — a plain C header matches nothing
    here and keeps its existing extract_c routing.
    """
    try:
        head = path.read_bytes()[:256 * 1024]
    except OSError:
        return False
    return any(marker in head for marker in _CPP_HEADER_MARKERS)


def _get_extractor(path: Path) -> Any | None:
    """Return the correct extractor function for a file, or None if unsupported."""
    if path.name.lower().endswith(".blade.php"):
        return extract_blade
    # Os arquivos de configuração do MCP (.mcp.json, claude_desktop_config.json, ...) são roteados
    # por nome de arquivo antes do envio .json genérico para que eles obtenham nós com reconhecimento de MCP
    if is_mcp_config_path(path):
        return extract_mcp_config
    # Manifestos do pacote (apm.yml, pyproject.toml, go.mod, pom.xml) → um canônico
    # nó do pacote + arestas depends_on, por nome de arquivo antes do envio do sufixo genérico
    #. Caso contrário, apm.yml seria um documento.yml manipulado pelo LLM.
    if is_package_manifest_path(path):
        return extract_package_manifest
    # `.h` é C/C++/ObjC ambíguo; encaminhar cabeçalhos Objective-C para extract_objc
    # (o mapa de sufixo envia `.h` para extract_c, que não pode ler @interface etc.).
    # A detecção de ObjC tem prioridade sobre a detecção de C++: um cabeçalho Objective-C++ pode
    # contém `@interface` e C++ embutido (`::`) e deve ser analisado como ObjC.
    suffix = path.suffix
    if suffix not in _DISPATCH and suffix.lower() in _DISPATCH:
        suffix = suffix.lower()
    if suffix == ".h":
        if _is_objc_header(path):
            return extract_objc
        # Um cabeçalho de classe C++ roteado para extract_c perde totalmente a classe (o cabeçalho C++
        # gramática não tem class_specifier). Redirecione para extract_cpp.
        if _is_cpp_header(path):
            return extract_cpp
    # `.m` é Objective-C OU MATLAB. extract_objc incondicionalmente forçaria a análise
    # MATLAB através da gramática ObjC no lixo. Rota para extract_objc
    # somente quando o arquivo realmente se parece com Objective-C; caso contrário, deixe-o sem
    # mal analisado. `.mm` é inequivocamente Objective-C++ e permanece em extract_objc.
    if suffix == ".m" and not _is_objc_source(path):
        return None
    # Arquivos sem extensão: resolvidos por shebang, espelhando detect.classify_file.
    # Sem isso, detecte rótulos, por ex. `#!/usr/bin/env bash` CLIs como código, mas
    # a extração não retorna nenhum extrator e o arquivo silenciosamente não contribui com nada.
    if not suffix:
        from omnigraph.detect import _shebang_interpreter
        interp = _shebang_interpreter(path)
        if interp is not None:
            return _SHEBANG_DISPATCH.get(interp)
    return _DISPATCH.get(suffix)


def _safe_extract_with_xaml_root(extractor, path: Path, root: Path) -> dict:
    global _XAML_ACTIVE_EXTRACT_ROOT
    previous_root = _XAML_ACTIVE_EXTRACT_ROOT
    _XAML_ACTIVE_EXTRACT_ROOT = root.resolve()
    try:
        return _safe_extract(extractor, path)
    finally:
        _XAML_ACTIVE_EXTRACT_ROOT = previous_root


def _extract_single_file(args: tuple) -> tuple[int, dict]:
    """Worker function for parallel extraction. Runs in a subprocess.

    Must be at module level (not a closure) so it can be pickled by
    ProcessPoolExecutor.

    Args:
        args: (index, path_str, root_str, cache_location_str) tuple. ``root``
            anchors hash keys / node ids / the XAML boundary; ``cache_location``
            is where the cache dir is written, decoupled per #1774. A legacy
            3-tuple (no cache_location) is still accepted for back-compat.

    Returns:
        (index, result_dict) so results can be placed back in order.
    """
    if len(args) == 4:
        idx, path_str, root_str, cache_location_str = args
    else:
        idx, path_str, root_str = args
        cache_location_str = root_str
    path = Path(path_str)
    root = Path(root_str)
    cache_location = Path(cache_location_str)
    _raise_recursion_limit()
    bypass_cache = path.suffix in _JS_CACHE_BYPASS_SUFFIXES

    if not bypass_cache:
        cached = load_cached(path, root, cache_root=cache_location)
        if cached is not None:
            return idx, cached

    extractor = _get_extractor(path)
    if extractor is None:
        return idx, {"nodes": [], "edges": []}

    result = _safe_extract_with_xaml_root(extractor, path, root)
    # Nunca armazene em cache um resultado de nó zero para um arquivo extraível. Cada suporte
    # source produz pelo menos um nó de arquivo, portanto, uma lista de nós vazia é anômala
    # (por exemplo, um soluço temporário em lote/paralelo). Armazenar em cache torna o vazio
    # estável em bytes em execuções e cega silenciosamente afetado/explicado e
    # através do arquivo; pular a gravação permite uma nova execução da autocura.
    if not bypass_cache and "error" not in result and result.get("nodes"):
        save_cached(path, result, root, cache_root=cache_location)
    return idx, result


def _extract_parallel(
    uncached_work: list[tuple[int, Path]],
    per_file: list[dict | None],
    root: Path,
    max_workers: int | None,
    total_files: int,
    cache_location: Path | None = None,
) -> bool:
    """Extract uncached files in parallel using ProcessPoolExecutor.

    Returns True if the pool ran to completion. Returns False if the pool
    failed in a recoverable way (typically Windows-spawn without an
    ``if __name__ == "__main__"`` guard in the calling script, which causes
    BrokenProcessPool); the caller should fall back to sequential extraction.
    """
    import concurrent.futures

    if max_workers is None:
        # Honrar a substituição de ambiente OMNIGRAPH_MAX_WORKERS; caso contrário, dimensione para o
        # CPU completa. O limite histórico `, 8)` era um limite de segurança para laptops
        # em 2023 – em uma estação de trabalho de 32 threads, custa uma desaceleração de 4x
        # (edição). O limite em len(uncached_work) mantém pequenos trabalhos
        # de gerar trabalhadores ociosos inúteis.
        env_raw = os.environ.get("OMNIGRAPH_MAX_WORKERS", "").strip()
        env_cap = None
        if env_raw:
            try:
                v = int(env_raw)
                if v > 0:
                    env_cap = v
            except ValueError:
                pass
        cpu_cap = env_cap if env_cap is not None else (os.cpu_count() or 4)
        max_workers = min(cpu_cap, len(uncached_work))

    # O Windows ProcessPoolExecutor tem limite máximo de 61 trabalhadores (limitação do CPython
    # OMNIGRAPH_MAX_WORKERS e --max-workers — permanecem válidos em caixas com mais de 61 núcleos
    # (edição). Proteja-se contra 0 de uma lista de trabalho vazia.
    if sys.platform == "win32":
        max_workers = min(max_workers, 61)
    max_workers = max(max_workers, 1)

    if max_workers == 1:
        return False

    # chaves hash de âncoras raiz/ids de nó/limite XAML; cache_location é onde
    # o diretório de cache é gravado (o padrão é root quando não desacoplado).
    root_str = str(root)
    cache_loc_str = str(cache_location if cache_location is not None else root)
    work_items = [(idx, str(path), root_str, cache_loc_str) for idx, path in uncached_work]

    done_count = 0
    failed: list[int] = []
    _PROGRESS_INTERVAL = 100
    try:
        with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_extract_single_file, item): pos
                for pos, item in enumerate(work_items)
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    idx, result = future.result()
                    per_file[idx] = result
                except concurrent.futures.process.BrokenProcessPool:
                    raise
                except Exception as exc:
                    pos = futures[future]
                    print(
                        f"  warning: worker failed for {work_items[pos][1]}: {exc}",
                        file=sys.stderr, flush=True,
                    )
                    failed.append(pos)
                done_count += 1
                if (
                    total_files >= _PROGRESS_INTERVAL
                    and done_count % _PROGRESS_INTERVAL == 0
                ):
                    print(
                        f"  AST extraction: {done_count}/{len(uncached_work)} uncached files "
                        f"({done_count * 100 // len(uncached_work)}%) [{max_workers} workers]",
                        flush=True,
                    )
    except concurrent.futures.process.BrokenProcessPool:
        # No Windows (método spawn start), os subprocessos de trabalho reimportam o
        # __main__ do chamador. Invocações embutidas como `python -c "..."` não têm
        # __main__ guarda, então o bootstrap do trabalhador aumenta e o pool morre antes
        # qualquer trabalho é concluído. Volte para a extração sequencial em processo -
        print(
            "  warning: parallel extraction failed (BrokenProcessPool); "
            "falling back to sequential. On Windows this usually means the "
            'caller is missing an `if __name__ == "__main__":` guard. Pass '
            "parallel=False to extract() to skip the pool entirely.",
            flush=True,
        )
        return False
    if failed:
        _extract_sequential(
            [uncached_work[pos] for pos in failed],
            per_file, root, total_files, cache_location,
        )
    if total_files >= _PROGRESS_INTERVAL:
        # Informa o mesmo denominador das linhas intermediárias usadas (arquivos sem cache
        # realmente processou esta execução), não total_files - mudando para o completo
        # corpus fez a contagem subir no final (ocorrências em cache + arquivos sem
        _done = len(uncached_work)
        print(
            f"  AST extraction: {_done}/{_done} uncached files (100%) [{max_workers} workers]",
            flush=True,
        )
    return True


def _extract_sequential(
    uncached_work: list[tuple[int, Path]],
    per_file: list[dict | None],
    root: Path,
    total_files: int,
    cache_location: Path | None = None,
) -> None:
    """Extract uncached files sequentially (fallback for small batches)."""
    _PROGRESS_INTERVAL = 100
    for work_idx, (idx, path) in enumerate(uncached_work):
        if (
            total_files >= _PROGRESS_INTERVAL
            and work_idx % _PROGRESS_INTERVAL == 0
            and work_idx > 0
        ):
            print(
                f"  AST extraction: {work_idx}/{len(uncached_work)} uncached files ({work_idx * 100 // len(uncached_work)}%)",
                flush=True,
            )
        extractor = _get_extractor(path)
        if extractor is None:
            per_file[idx] = {"nodes": [], "edges": []}
            continue
        bypass_cache = path.suffix in _JS_CACHE_BYPASS_SUFFIXES
        # O limite XAML ancora na `raiz` (o corpus), não no local do cache.
        result = _safe_extract_with_xaml_root(extractor, path, root)
        # Consulte _extract_single_file: não armazene em cache um resultado anômalo de nó zero.
        if not bypass_cache and "error" not in result and result.get("nodes"):
            save_cached(path, result, root, cache_root=cache_location)
        per_file[idx] = result
    if total_files >= _PROGRESS_INTERVAL:
        # Denominador consistente com as linhas intermediárias.
        _done = len(uncached_work)
        print(f"  AST extraction: {_done}/{_done} uncached files (100%)", flush=True)


_PARALLEL_THRESHOLD = 20


def extract(
    paths: list[Path],
    cache_root: Path | None = None,
    *,
    root: Path | None = None,
    parallel: bool = True,
    max_workers: int | None = None,
    resolution_context_nodes: list[dict] | None = None,
    resolution_context_edges: list[dict] | None = None,
) -> dict:
    """Extract AST nodes and edges from a list of code files.

    Two-pass process:
    1. Per-file structural extraction (classes, functions, imports)
    2. Cross-file import resolution: turns file-level imports into
       class-level INFERRED edges (DigestAuth --uses--> Response)

    Args:
        paths: files to extract from
        root: explicit anchor for source_file relativization, node ids, and
            symbol resolution. Pass the SCAN root whenever the cache lives
            somewhere else (`--out`); without it the anchor falls back to
            cache_root and every scanned file reads as out-of-root (#1941).
        cache_root: explicit root for omnigraph-out/cache/ (overrides the
            inferred common path prefix). Pass Path('.') when running on a
            subdirectory so the cache stays at ./omnigraph-out/cache/.
            Anchors ids/source_file only as a fallback when `root` is unset.
        parallel: if True and there are >= _PARALLEL_THRESHOLD uncached files,
            use ProcessPoolExecutor for multi-core extraction.
        max_workers: max subprocess count. Defaults to cpu_count (or the
            value of OMNIGRAPH_MAX_WORKERS if set), bounded by len(uncached_work).
        resolution_context_nodes: read-only AST nodes from files that are NOT
            being extracted this run (an incremental rebuild's unchanged
            corpus, #2406). They extend the cross-file resolution indexes —
            the shared direct-call pass's label/file indexes, the
            indirect_call callable guard (via the persisted `_callable` /
            `_callable_class` markers, #2438), and the member-call resolvers
            run by `run_language_resolvers` (#2437) — so a changed caller can
            still bind `foo()`, `obj.method()`, or `submit(handler)` to an
            unchanged callee. They are never parsed, mutated, or returned;
            raw_calls come only from `paths`, so only edges sourced by the
            re-extracted files are emitted.
        resolution_context_edges: the `contains`/`method` edges of the same
            unchanged corpus (#2437). The member-call resolvers walk these to
            map a receiver type to the single class owning the called method;
            without them an unchanged callee's class never passes the
            single-definition guard. Read-only, same contract as
            resolution_context_nodes: they widen the resolvers' view but only
            fresh results are appended to the returned nodes/edges.
    """
    paths = [Path(p) for p in paths]
    anchor_root = Path(root) if root is not None else None
    _check_tree_sitter_version()
    _raise_recursion_limit()
    # Os manifestos/globs do pacote do espaço de trabalho podem mudar durante a observação ou extração repetida.
    _WORKSPACE_PACKAGE_CACHE.clear()
    _XAML_CSHARP_CLASS_CACHE.clear()

    # Inferir uma raiz comum para chaves de cache (use o primeiro segmento divergente, não a soma de todas as correspondências)
    try:
        if not paths:
            root = Path(".")
        elif len(paths) == 1:
            root = paths[0].parent
        else:
            min_parts = min(len(p.parts) for p in paths)
            common_len = 0
            for i in range(min_parts):
                if len({p.parts[i] for p in paths}) == 1:
                    common_len += 1
                else:
                    break
            root = Path(*paths[0].parts[:common_len]) if common_len else Path(".")
    except Exception:
        root = Path(".")
    if anchor_root is not None:
        root = anchor_root
    elif cache_root is not None:
        root = cache_root
    root = root.resolve()

    # o cache é um OUTPUT, então quando nenhum cache_root explícito é fornecido, ele é
    # escrito no diretório de trabalho atual - nunca `root` (o inferido
    # corpus somente leitura ou estrangeiro. `root` ainda ancora as chaves hash de conteúdo,
    # ids de nó, resolução de símbolo e limite de verificação de projeto XAML; apenas o
    # a localização do diretório de cache diverge dele.
    cache_location = (cache_root if cache_root is not None else Path(".")).resolve()
    total = len(paths)

    # Fase 1: separar ocorrências armazenadas em cache de trabalhos não armazenados em cache
    per_file: list[dict | None] = [None] * total
    uncached_work: list[tuple[int, Path]] = []

    for i, path in enumerate(paths):
        if _get_extractor(path) is None:
            per_file[i] = {"nodes": [], "edges": []}
            continue
        bypass_cache = path.suffix in _JS_CACHE_BYPASS_SUFFIXES
        if not bypass_cache:
            cached = load_cached(path, root, cache_root=cache_location)
            if cached is not None:
                per_file[i] = cached
                continue
        uncached_work.append((i, path))

    # Fase 2: extrair arquivos não armazenados em cache (paralelo ou sequencial)
    if uncached_work:
        ran_parallel = False
        if parallel and len(uncached_work) >= _PARALLEL_THRESHOLD:
            ran_parallel = _extract_parallel(
                uncached_work, per_file, root, max_workers, total, cache_location
            )
        if not ran_parallel:
            _extract_sequential(
                [(i, p) for (i, p) in uncached_work if per_file[i] is None],
                per_file, root, total, cache_location,
            )

    for i in range(total):
        if per_file[i] is None:
            per_file[i] = {
                "nodes": [], "edges": [],
                "error": "internal: no extraction result produced",
            }

    # traz à tona qualquer arquivo de origem que um extrator aceitou, mas que produziu zero
    # nós (nem mesmo um nó de arquivo). Esse arquivo está silenciosamente ausente do grafo,
    # tão afetados/explicados são cegos e passam por ele sem nenhum outro sinal.
    _empty_sources: list[str] = []
    for i, _p in enumerate(paths):
        _res = per_file[i] or {}
        if _res.get("nodes") or _res.get("error"):
            continue
        if _get_extractor(_p) is not None:
            _empty_sources.append(str(_p))
    if _empty_sources:
        _shown = ", ".join(Path(x).name for x in _empty_sources[:5])
        _more = f" (+{len(_empty_sources) - 5} more)" if len(_empty_sources) > 5 else ""
        print(
            f"  warning: {len(_empty_sources)} source file(s) produced zero nodes and "
            f"are absent from the graph: {_shown}{_more}. A re-run will retry them "
            f"(empties are no longer cached); if it persists, please report the "
            f"file(s) (#1666).",
            file=sys.stderr, flush=True,
        )

    _failed_sources: list[str] = []
    _failed_seen: set[str] = set()
    for i, _p in enumerate(paths):
        _res = per_file[i] or {}
        _key = str(_p)
        if _res.get("error"):
            if _key not in _failed_seen:
                _failed_sources.append(_key)
                _failed_seen.add(_key)
            continue
        if (not _res.get("nodes")) and _get_extractor(_p) is not None:
            if _key not in _failed_seen:
                _failed_sources.append(_key)
                _failed_seen.add(_key)

    # um arquivo contado como código (extensão em CODE_EXTENSIONS) mas sem AST
    # extrator conectado (por exemplo, .r/.R - não há despacho tree-sitter-r) silenciosamente
    # contribui com zero nós. O aviso acima ignora deliberadamente estes (é
    # só é acionado quando existe um extrator), então coloque-os explicitamente, agrupados por
    # extensão, em vez de relatar o sucesso como se a linguagem estivesse mapeada.
    from omnigraph.detect import CODE_EXTENSIONS as _CODE_EXTS
    _no_extractor: dict[str, int] = {}
    for _p in paths:
        _ext = _p.suffix.lower()
        if _ext in _CODE_EXTS and _get_extractor(_p) is None:
            _no_extractor[_ext] = _no_extractor.get(_ext, 0) + 1
    if _no_extractor:
        _by_count = ", ".join(
            f"{ext} ({n})" for ext, n in sorted(_no_extractor.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        _tot = sum(_no_extractor.values())
        print(
            f"  warning: {_tot} file(s) are classified as code but omnigraph has no AST "
            f"extractor for their language, so they contributed nothing to the graph: "
            f"{_by_count}. Please open an issue to request support for these (#1689).",
            file=sys.stderr, flush=True,
        )

    # um extrator ESTÁ conectado para esses arquivos, mas foi resgatado porque
    # falta dependência (por exemplo, .sql precisa de tree-sitter-sql do [sql]
    # erro, # 1689 cobre apenas arquivos sem extrator - então o grafo é construído
    # "com sucesso" enquanto cada arquivo silenciosamente não contribui com nada.
    # Mostre-os agrupados por extensão, nomeando o extra que fornece o
    # dependência quando existe uma.
    _missing_dep_count: dict[str, int] = {}
    _missing_dep_error: dict[str, str] = {}
    for i, _p in enumerate(paths):
        _err = (per_file[i] or {}).get("error") or ""
        if _DEP_MISSING_MARKER in _err or _DEP_LOAD_FAILED_MARKER in _err:
            _ext = _p.suffix.lower()
            _missing_dep_count[_ext] = _missing_dep_count.get(_ext, 0) + 1
            _missing_dep_error.setdefault(_ext, _err)
    for _ext, _n in sorted(_missing_dep_count.items(), key=lambda kv: (-kv[1], kv[0])):
        _extra = _EXTRA_FOR_EXTENSION.get(_ext)
        _err_text = _missing_dep_error[_ext]
        if _extra and _DEP_MISSING_MARKER in _err_text:
            _reason = _err_text.split(". ")[0]
            _hint = f' Install it with: pip install "omnigraph[{_extra}]"'
            _cause = "a dependency is missing"
        else:
            _reason = _err_text
            _hint = ""
            _cause = ("a dependency is missing" if _DEP_MISSING_MARKER in _err_text
                      else "a dependency failed to load")
        print(
            f"  warning: {_n} {_ext} file(s) contributed nothing to the graph "
            f"because {_cause}: {_reason}.{_hint} (#1745)",
            file=sys.stderr, flush=True,
        )

    _syntax_error_files: list[tuple[str, int | None]] = []
    for i, _p in enumerate(paths):
        _res = per_file[i] or {}
        _pe = _res.get("parse_errors")
        if not _pe:
            continue
        if len(_res.get("nodes", [])) <= 1 or _pe.get("multiline_error"):
            _rel = os.path.relpath(str(_p), str(root)).replace("\\", "/")
            _syntax_error_files.append((_rel, _pe.get("first_error_line")))
    if _syntax_error_files:
        _shown = ", ".join(
            f"{x} (first error at line {ln})" if ln else x
            for x, ln in _syntax_error_files[:5]
        )
        _more = (
            f" (+{len(_syntax_error_files) - 5} more)"
            if len(_syntax_error_files) > 5 else ""
        )
        print(
            f"  warning: {len(_syntax_error_files)} file(s) had syntax errors and "
            f"may be partially extracted: {_shown}{_more} (#2551)",
            file=sys.stderr, flush=True,
        )

    all_nodes: list[dict] = []
    all_edges: list[dict] = []
    all_raw_calls: list[dict] = []
    for result in per_file:
        all_nodes.extend(result.get("nodes", []))
        all_edges.extend(result.get("edges", []))
        all_raw_calls.extend(result.get("raw_calls", []))
    # Função/método/classe def ids para chamada indireta de arquivo cruzado que pode ser chamada
    # guarda. Construído a partir do marcador de nó `_callable` APÓS o id-remap/desambiguação
    # passa abaixo (que reescreve os IDs dos nós), para que nunca fique obsoleto - veja o
    callable_nids: set[str] = set()

    _augment_symbol_resolution_edges(paths, all_nodes, all_edges, root)

    # Mesclar uma classe declarada no cabeçalho (e seus métodos) com seu irmão-impl
    # definição em UM nó (C/C++/ObjC/). Executa ANTES do id-remap
    # abaixo: um símbolo de cabeçalho e sua contraparte impl compartilham um ID apenas enquanto ambos
    # ainda carrega o prefixo do arquivo bruto; o remapeamento do prefixo por arquivo então diverge
    # eles (foo_h vs foo_cpp), então o colapso deve acontecer primeiro. Desabando aqui
    # também significa que a desambiguação vê um source_file por ID e não os divide.
    _merge_decl_def_classes(all_nodes, all_edges)

    # Remapear IDs de nó de arquivo de derivado de caminho absoluto para canônico
    # Formulário de especificação {parent_dir}_{stem} para que (a) os endpoints de aresta graph.json sejam estáveis
    # entre máquinas (# 502) e (b) os nós do arquivo AST correspondem à semântica dos IDs
    # a forma relativa ainda está ancorada na raiz (resolvida).
    id_remap: dict[str, str] = {}
    def _portable_out_of_root_sf(p: Path) -> str:
        try:
            rel = os.path.relpath(str(p), str(root)).replace("\\", "/")
        except ValueError:
            return p.name  # unidade diferente do Windows: não existe caminho relativo
        updepth = 0
        for seg in rel.split("/"):
            if seg == "..":
                updepth += 1
            else:
                break
        # corpus; seus diretórios ancestrais incorporariam estrangeiros (possivelmente com nome de usuário)
        # segmentos, então recolha o nome base.
        return p.name if updepth > 3 else rel

    # IDs de nó de símbolo incorporam a raiz do arquivo como um prefixo (_file_node_id do caminho
    # a serra extratora). Para um arquivo de nível raiz que stem pega o absoluto
    # nome do diretório pai, então um símbolo se torna <rootdir>_main_run enquanto o
    # o nó do arquivo é relativizado corretamente para main e a especificação skill.md deseja
    # main_run - dividindo o símbolo em fantasmas AST/semânticos. Relativizar
    # o prefixo do símbolo da mesma maneira, bloqueado por source_file para que dois arquivos compartilhem um
    # prefixo não pode contaminar cruzadamente. Digitado por caminho resolvido -> (old_pref, new_pref).
    # Cada arquivo mapeia de até DOIS prefixos antigos — o prefixo da forma de entrada
    # através de .resolve(), para que seus alvos de aresta sejam desviados do formato ABSOLUTO;
    # quando as entradas são relativas, as duas formas diferem e os alvos derivados absolutos
    # caso contrário, seria órfão. Armazenado como uma lista para que o remapeamento do prefixo do símbolo
    # abaixo pode tentar ambos (formas idênticas são reduzidas a uma - um ambiente autônomo).
    prefix_remap: dict[Path, list[tuple[str, str]]] = {}
    stem_forms: dict[Path, tuple[str, list[str]]] = {}
    remap_paths: list[Path] = list(paths)
    _remap_seen: set[Path] = set()
    for _p in paths:
        try:
            _remap_seen.add(_p.resolve())
        except (OSError, RuntimeError):
            pass
    for _e in all_edges:
        _tf = _e.get("target_file")
        if not _tf:
            continue
        _raw_tp = Path(_tf)
        try:
            _tp = _raw_tp.resolve()
        except (OSError, RuntimeError):
            continue
        if _tp in _remap_seen:
            continue
        _remap_seen.add(_tp)
        try:
            _tp.relative_to(root)
        except ValueError:
            try:
                if _tp.is_file():
                    ext_new_id = _make_id("ext", _portable_out_of_root_sf(_tp))
                    id_remap[_make_id(str(_tp))] = ext_new_id
                    if _raw_tp != _tp:
                        id_remap[_make_id(str(_raw_tp))] = ext_new_id
                    id_remap.setdefault(
                        _make_id(str(_tp)) + "__entry", ext_new_id + "__entry")
                    if _raw_tp != _tp:
                        id_remap.setdefault(
                            _make_id(str(_raw_tp)) + "__entry",
                            ext_new_id + "__entry")
            except OSError:
                pass
            continue
        try:
            if not _tp.is_file():
                continue
        except OSError:
            continue
        remap_paths.append(_tp)
        if _raw_tp != _tp:
            _remap_seen.add(_raw_tp)
            remap_paths.append(_raw_tp)
    for path in remap_paths:
        old_id = _make_id(str(path))
        try:
            rel = path.relative_to(root)
        except ValueError:
            try:
                rel = path.resolve().relative_to(root)
            except ValueError:
                continue
        new_id = _file_node_id(rel)
        if old_id != new_id:
            id_remap[old_id] = new_id
        # Registre também a forma absolutamente resolvida do ID em nível de arquivo para
        # destinos de importação de alias/espaço de trabalho (resolvidos via .resolve()) remapeados para
        # canônico em vez de órfão.
        old_id_abs = _make_id(str(path.resolve()))
        if old_id_abs != new_id:
            id_remap[old_id_abs] = new_id
        old_prefs: list[tuple[str, str]] = []
        old_pref = _file_node_id(path)
        if old_pref != new_id:
            old_prefs.append((old_pref, new_id))
        old_pref_abs = _file_node_id(path.resolve())
        if old_pref_abs != new_id and old_pref_abs != old_pref:
            old_prefs.append((old_pref_abs, new_id))
        for _old, _pref in ((old_id, old_pref), (old_id_abs, old_pref_abs)):
            if not _old.startswith(_pref):
                continue
            _entry_new = new_id + _old[len(_pref):] + "__entry"
            _entry_old = _old + "__entry"
            if _entry_old != _entry_new:
                id_remap.setdefault(_entry_old, _entry_new)
        if old_prefs:
            prefix_remap[path.resolve()] = old_prefs
        stem_forms[path.resolve()] = (
            new_id, [old_pref_abs, old_pref, new_id]
        )
    if id_remap:
        for n in all_nodes:
            if n.get("id") in id_remap:
                n["id"] = id_remap[n["id"]]
        for e in all_edges:
            if e.get("source") in id_remap:
                e["source"] = id_remap[e["source"]]
            if e.get("target") in id_remap:
                e["target"] = id_remap[e["target"]]
        for rc in all_raw_calls:
            cn = rc.get("caller_nid")
            if cn in id_remap:
                rc["caller_nid"] = id_remap[cn]
        for result in per_file:
            for ext in result.get("swift_extensions", []) or []:
                en = ext.get("nid")
                if en in id_remap:
                    ext["nid"] = id_remap[en]
    if prefix_remap:
        sym_remap: dict[str, str] = {}
        edge_alias_candidates: dict[str, set[str]] = {}
        for n in all_nodes:
            sf = n.get("source_file")
            if not sf:
                continue
            # Os nós do pacote carregam um ID canônico com chave de nome (pkg_<nome>) que deve
            # permanecem idênticos em todos os manifestos que fazem referência ao pacote, então
            # eles estão isentos do remapeamento do prefixo da haste do arquivo, como o
            if n.get("type") == "package":
                continue
            try:
                entry = prefix_remap.get(Path(sf).resolve())
            except Exception:
                continue
            if entry is None:
                continue
            nid = n.get("id", "")
            # contaminação, então o primeiro prefixo correspondente vence.
            canonical_nid: str | None = None
            for old_pref, new_pref in entry:
                if nid.startswith(old_pref + "_"):
                    canonical_nid = new_pref + nid[len(old_pref):]
                    if canonical_nid != nid:
                        sym_remap[nid] = canonical_nid
                    break
                if nid.startswith(new_pref + "_"):
                    canonical_nid = nid
                    break
            if canonical_nid is None:
                continue
            for old_pref, new_pref in entry:
                if not canonical_nid.startswith(new_pref + "_"):
                    continue
                old_nid = old_pref + canonical_nid[len(new_pref):]
                if old_nid != canonical_nid:
                    edge_alias_candidates.setdefault(old_nid, set()).add(canonical_nid)
        if sym_remap:
            for n in all_nodes:
                if n.get("id") in sym_remap:
                    n["id"] = sym_remap[n["id"]]
            for e in all_edges:
                if e.get("source") in sym_remap:
                    e["source"] = sym_remap[e["source"]]
                if e.get("target") in sym_remap:
                    e["target"] = sym_remap[e["target"]]
            # raw_calls carregam caller_nid (um id de símbolo) consumido pelo arquivo cruzado
            # chame o passe abaixo, após este remapeamento - reescreva também ou essas arestas
            for rc in all_raw_calls:
                cn = rc.get("caller_nid")
                if cn in sym_remap:
                    rc["caller_nid"] = sym_remap[cn]
            for result in per_file:
                for ext in result.get("swift_extensions", []) or []:
                    en = ext.get("nid")
                    if en in sym_remap:
                        ext["nid"] = sym_remap[en]
        if edge_alias_candidates:
            def _edge_key(edge: dict) -> str:
                return json.dumps(
                    {k: v for k, v in edge.items() if k != "target_file"},
                    sort_keys=True, separators=(",", ":"), default=str,
                )
            edge_key_counts = Counter(_edge_key(edge) for edge in all_edges)
            owned_node_ids = {node.get("id") for node in all_nodes}
            deduped_edges: list[dict] = []
            for edge in all_edges:
                if edge.get("relation") == "re_exports":
                    candidates = edge_alias_candidates.get(edge.get("target", ""), set())
                    if len(candidates) == 1 and edge.get("target") not in owned_node_ids:
                        edge["target"] = next(iter(candidates))
                    deduped_edges.append(edge)
                    continue
                candidates = (
                    edge_alias_candidates.get(edge.get("target", ""), set())
                    if edge.get("relation") == "imports"
                    else set()
                )
                if len(candidates) == 1:
                    candidate = next(iter(candidates))
                    twin_key = _edge_key({**edge, "target": candidate})
                    if edge_key_counts[twin_key]:
                        if edge.get("target") in owned_node_ids:
                            edge_key_counts[twin_key] -= 1
                        continue
                deduped_edges.append(edge)
            all_edges[:] = deduped_edges

    if stem_forms:
        owned_ids = {n.get("id") for n in all_nodes}

        def _decompose(target: str, tf: str) -> "tuple[str, str] | None":
            try:
                forms = stem_forms.get(Path(tf).resolve())
            except (OSError, RuntimeError):
                return None
            if not forms:
                return None
            canonical, prefixes = forms
            for pref in prefixes:
                if pref and target.startswith(pref + "_"):
                    return canonical, target[len(pref) + 1:]
            return None

        chain: dict[tuple[str, str], set] = {}

        def _resolve1(key) -> "str | None":
            targets = chain.get(key)
            return next(iter(targets)) if targets and len(targets) == 1 else None

        def _learn(e: dict) -> None:
            tf = e.get("target_file")
            if not tf or e.get("target") not in owned_ids:
                return
            dec = _decompose(e.get("target", ""), tf)
            if dec is not None:
                chain.setdefault((e.get("source"), dec[1]), set()).add(e["target"])

        for e in all_edges:
            if e.get("relation") == "re_exports":
                _learn(e)

        pending = [
            e for e in all_edges
            if e.get("relation") in ("re_exports", "imports")
            and e.get("target_file")
            and e.get("target") not in owned_ids
        ]
        for _ in range(8):
            progressed = False
            still: list[dict] = []
            for e in pending:
                dec = _decompose(e.get("target", ""), e["target_file"])
                resolved_target = _resolve1((dec[0], dec[1])) if dec else None
                if resolved_target is None:
                    still.append(e)
                    continue
                e["target"] = resolved_target
                if e.get("relation") == "re_exports":
                    chain.setdefault((e.get("source"), dec[1]), set()).add(resolved_target)
                progressed = True
            pending = still
            if not progressed:
                break
        for e in pending:
            dec = _decompose(e.get("target", ""), e["target_file"])
            if dec is not None:
                e["target"] = f"{dec[0]}_{dec[1]}"

    _repoint_python_package_imports(paths, all_nodes, all_edges, root)
    _merge_swift_extensions(per_file, all_nodes, all_edges)
    _merge_csharp_partial_class_nodes(per_file, all_nodes, all_edges, paths, root)
    _disambiguate_colliding_node_ids(all_nodes, all_edges, all_raw_calls, root)
    _canonicalize_csharp_namespace_nodes(all_nodes, all_edges)
    # A desambiguação de namespace/uso do PHP deve ser executada ANTES da religação do stub exclusivo:
    # a falsa mesclagem acontece dentro da religação quando um stub de nome simples
    _php_exts = {".php", ".phtml", ".php3", ".php4", ".php5", ".php7", ".phps"}
    _php_sel = [
        (r, p) for r, p in zip(per_file, paths)
        if p.suffix.lower() in _php_exts and not p.name.lower().endswith(".blade.php")
    ]
    if _php_sel:
        try:
            _resolve_php_type_references(
                [r for r, _ in _php_sel], [p for _, p in _php_sel], all_nodes, all_edges
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("PHP type-reference resolution failed, skipping: %s", exc)
    _java_sel = [(r, p) for r, p in zip(per_file, paths) if p.suffix == ".java"]
    if _java_sel:
        try:
            _resolve_java_type_references(
                [r for r, _ in _java_sel], [p for _, p in _java_sel], all_nodes, all_edges
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Java type-reference resolution failed, skipping: %s", exc)
    _go_sel = [(r, p) for r, p in zip(per_file, paths) if p.suffix == ".go"]
    if _go_sel:
        try:
            _resolve_go_type_references(
                [r for r, _ in _go_sel], [p for _, p in _go_sel],
                all_nodes, all_edges, root,
                resolution_context_nodes, resolution_context_edges,
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "Go type-reference resolution failed, skipping: %s", exc
            )
    _rewire_unique_stub_nodes(all_nodes, all_edges)

    # Adicione arestas de nível de classe entre arquivos (somente Python - usa o analisador Python internamente)
    py_paths = [p for p in paths if p.suffix == ".py"]
    if py_paths:
        py_results = [r for r, p in zip(per_file, paths) if p.suffix == ".py"]
        try:
            cross_file_edges = _resolve_cross_file_imports(py_results, py_paths)
            all_edges.extend(cross_file_edges)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Cross-file import resolution failed, skipping: %s", exc)

    java_paths = [p for p in paths if p.suffix == ".java"]
    if java_paths:
        java_results = [r for r, p in zip(per_file, paths) if p.suffix == ".java"]
        try:
            all_edges.extend(_resolve_cross_file_java_imports(java_results, java_paths))
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Java cross-file import resolution failed, skipping: %s", exc)

    # faz referência às arestas deixadas nos stubs de sombra, desambiguando tipos com o mesmo nome pelo
    cs_paths = [p for p in paths if p.suffix == ".cs"]
    if cs_paths:
        cs_results = [r for r, p in zip(per_file, paths) if p.suffix == ".cs"]
        try:
            _resolve_csharp_type_references(cs_results, cs_paths, all_nodes, all_edges)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("C# type-reference resolution failed, skipping: %s", exc)
        try:
            _resolve_cross_file_csharp_imports(cs_results, cs_paths, all_nodes, all_edges)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("C# cross-file import resolution failed, skipping: %s", exc)

    def _looks_like_bash(result: object) -> bool:
        if not isinstance(result, dict):
            return False
        nodes = result.get("nodes")
        if not isinstance(nodes, list):
            return False
        for n in nodes:
            if not isinstance(n, dict):
                continue
            md = n.get("metadata")
            if isinstance(md, dict) and md.get("language") == "bash":
                return True
        return False

    sh_pairs = [
        (r, p) for r, p in zip(per_file, paths)
        if p.suffix in (".sh", ".bash") or _looks_like_bash(r)
    ]
    if sh_pairs:
        sh_results = [r for r, _ in sh_pairs]
        sh_paths = [p for _, p in sh_pairs]
        try:
            all_edges.extend(
                resolve_bash_source_edges(sh_results, sh_paths, root, existing_edges=all_edges)
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Bash cross-file call resolution failed, skipping: %s", exc)

    # Resolução de chamadas entre arquivos para todos os idiomas
    # Cada extrator salvou chamadas não resolvidas em raw_calls. Agora que temos todos
    # nós de todos os arquivos, resolva qualquer receptor que exista em outro arquivo.
    # Nome da compilação → TODOS os IDs de nó correspondentes para que possamos pular nomes comuns ambíguos
    # (por exemplo, "log", "execute", "find") que aparecem em vários arquivos - resolvendo
    # isso aumenta a classificação god_nodes com arestas falsas de arquivos cruzados.
    # Rótulo de construção -> índice node_id para resolução de chamadas entre arquivos.
    # Ignorar nós de lógica (seus rótulos são textos docstring, não podem ser chamados
    # identificadores, e eram correspondências poluentes para nomes curtos —).
    global_label_to_nids: dict[str, list[str]] = {}
    global_label_to_nids_ci: dict[str, list[str]] = {}
    resolution_nodes = all_nodes
    if resolution_context_nodes:
        _fresh_ids = {n["id"] for n in all_nodes}
        resolution_nodes = all_nodes + [
            n for n in resolution_context_nodes
            if n.get("id") and n["id"] not in _fresh_ids
        ]
    for n in resolution_nodes:
        if n.get("file_type") == "rationale" or n.get("type") == "namespace":
            continue
        raw = n.get("label", "")
        normalised = raw.strip("()").lstrip(".")
        if normalised:
            # O caso é semântico na maioria dos idiomas, portanto, indexe (e corresponda, abaixo) por exato
            # variável de shell única, o god node nº 1 (# 1581). Somente sem distinção entre maiúsculas e minúsculas
            # linguagens (PHP/SQL/Nim) também recebem uma chave dobrada para correspondência de dobra legítima.
            global_label_to_nids.setdefault(normalised, []).append(n["id"])
            if _lang_is_case_insensitive(n.get("source_file")):
                global_label_to_nids_ci.setdefault(normalised.lower(), []).append(n["id"])

    # marcador nos nós FINAL (pós-remapeamento) - portanto, um retorno de chamada resolve apenas para um real
    # função/método/classe, nunca um símbolo de dados com o mesmo nome, e o guarda nunca vai
    callable_nids = {n["id"] for n in resolution_nodes if n.get("_callable")}
    class_nids = {n["id"] for n in resolution_nodes if n.get("_callable_class")}

    run_language_resolvers(
        paths, per_file, all_nodes, all_edges,
        resolvers=[_KOTLIN_IMPORT_TARGET_RESOLVER],
    )

    # Crie um índice de evidências a partir de arestas de importação para que chamadas entre arquivos sejam apoiadas por um
    # a instrução de importação explícita pode ser promovida de INFERRED para EXTRACTED.
    # As importações diretas de símbolos (`import { foo }` / `const { foo } = require()`) são
    # a evidência mais forte - o file_id do chamador tem uma vantagem de `importação` diretamente para
    # o ID do símbolo do chamado. As importações de módulos (`imports_from`) são mais fracas, mas ainda assim
    file_to_symbol_imports: dict[str, set[str]] = {}
    file_to_module_imports: dict[str, set[str]] = {}
    for e in all_edges:
        if e.get("relation") == "imports":
            file_to_symbol_imports.setdefault(e["source"], set()).add(e["target"])
        elif e.get("relation") == "imports_from":
            file_to_module_imports.setdefault(e["source"], set()).add(e["target"])

    # Mapeie cada nó de volta ao ID do nó do arquivo que o contém para que possamos perguntar
    # Um nó e seu nó de arquivo compartilham exatamente a mesma string ``source_file``, e um
    # nó de arquivo é aquele cujo rótulo é o nome base (``add_node(file_nid,
    # caminho.nome)``). Resolver a associação de arquivos por essa string compartilhada é robusto
    # contra a incompatibilidade de resolução de caminho/link simbólico que faz
    # ``relative_to(root.resolve())`` lança e retorna para um valor não correspondente
    # ID derivado de absoluto - o que falsificaria falsamente na evidência de importação e (com
    # o portão JS/TS abaixo) descarta uma chamada importada legitimamente.
    sf_to_file_nid: dict[str, str] = {}
    for n in resolution_nodes:
        sf = n.get("source_file")
        if sf and n.get("label") == Path(str(sf)).name:
            sf_to_file_nid.setdefault(str(sf), n["id"])
    nid_to_file_nid: dict[str, str] = {}
    # nid -> string source_file bruta, para os desempates de nomes ambíguos abaixo
    # (classificação teste/não teste + proximidade do caminho). Mantido separado do
    nid_to_source_file: dict[str, str] = {}
    for n in resolution_nodes:
        sf = n.get("source_file")
        if not sf:
            continue
        nid_to_source_file[n["id"]] = str(sf)
        fnid = sf_to_file_nid.get(str(sf))
        if fnid is not None:
            nid_to_file_nid[n["id"]] = fnid
            continue
        # Fallback (nenhum nó de arquivo encontrado para este source_file): deriva-o do antigo
        sf_path = Path(sf)
        try:
            sf_rel = sf_path.relative_to(root) if sf_path.is_absolute() else sf_path
        except ValueError:
            sf_rel = sf_path
        nid_to_file_nid[n["id"]] = _file_node_id(sf_rel)

    existing_pairs = {(e["source"], e["target"]) for e in all_edges}
    # Apenas pares semelhantes a chamadas, para a desduplicação indirect_call: uma aresta `importa` de um
    # arquivo para o símbolo que ele importa é ESPERADO e não deve suprimir um
    # indirect_call para o mesmo símbolo (importações nomeadas JS/TS criam essa vantagem).
    call_like_pairs = {
        (e["source"], e["target"]) for e in all_edges
        if e.get("relation") in ("calls", "indirect_call")
    }
    # Os módulos JS/TS/JSX não têm escopo implícito entre módulos: uma chamada para outro
    # o arquivo é real SOMENTE se o chamador o importou. Então, uma chamada entre arquivos de um
    # desses arquivos sem nenhuma evidência de importação está listado abaixo.
    _JS_TS_CALL_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
    _go_module_cache: dict[Path, str | None] = {}
    for rc in all_raw_calls:
        callee = rc.get("callee", "")
        if not callee:
            continue
        if callee in _LANGUAGE_BUILTIN_GLOBALS:
            continue
        # Ignorar chamadores de chamada de membro: obj.log() → "log" não tem evidência de importação
        # e colide com qualquer função de nível superior chamada "log" no corpus.
        if rc.get("is_member_call"):
            continue
        # Skip Ruby inclui/estende/precede marcadores mixin: eles carregam um módulo
        # nome como `callee` mas não são chamadas — o resolvedor Ruby os transforma em
        # ambos rotulam incorretamente a relação e bloqueiam a emissão mixes_in como um dup.
        if rc.get("is_mixin"):
            continue
        if rc.get("language") == "bash":
            continue
        if rc.get("language") == "go" and callee in _GO_PREDECLARED_FUNCS:
            continue
        # Correspondência de caso exato primeiro (o caso é semântico). Desista apenas quando o CALLING
        # o idioma do arquivo não diferencia maiúsculas de minúsculas e apenas em relação ao índice dobrado de
        # definições de linguagem que não diferenciam maiúsculas de minúsculas - portanto, uma chamada `Path()` do Python nunca pode
        # resolver para um nó shell `PATH`.
        candidates = global_label_to_nids.get(callee, [])
        if not candidates and _lang_is_case_insensitive(rc.get("source_file")):
            candidates = global_label_to_nids_ci.get(callee.lower(), [])
        if not candidates:
            continue
        # Proteção entre idiomas: nunca vincule uma chamada a uma definição em um idioma diferente
        # família linguística. A correspondência somente de nome estava resolvendo um retorno de chamada TSX passado
        # por nome para um método Kotlin de mesmo nome na metade Android do repositório
        # (e uma chamada Python para uma diversão Kotlin) - arestas fantasmas da especificação de extração
        # proíbe explicitamente. Candidatos cuja família é desconhecida (sem source_file,
        # nós não-código) são mantidos, preservando o comportamento permissivo anterior;
        # pares de interoperabilidade reais (Kotlin↔Java, C↔C++↔ObjC, JS↔TS) compartilham uma família e
        caller_family = _lang_family(rc.get("source_file"))
        if caller_family is not None:
            candidates = [
                c for c in candidates
                if (candidate_family := _lang_family(nid_to_source_file.get(c))) is None
                or candidate_family == caller_family
            ]
            if not candidates:
                continue
        go_exact_import = False
        if rc.get("language") == "go" and rc.get("import_path"):
            import_path = str(rc["import_path"])
            candidates = [
                candidate for candidate in candidates
                if _go_import_path_for_file(
                    nid_to_source_file.get(candidate, ""), root, _go_module_cache
                ) == import_path
            ]
            if not candidates:
                continue
            go_exact_import = True
        caller = rc["caller_nid"]
        # Resolva o arquivo do chamador por meio da string source_file do próprio raw_call,
        # que é estável independentemente de qualquer remapeamento caller_nid. Uma indireta
        # caller_nid do retorno de chamada é o nó do arquivo, cujo id pode ter sido
        # relativizado após o raw_call ter sido gravado, então uma pesquisa caller_nid pode
        # miss e (com o portão) descarta um retorno de chamada importado legitimamente.
        caller_file_nid = (
            sf_to_file_nid.get(str(rc.get("source_file", "")))
            or nid_to_file_nid.get(caller)
        )
        imported_symbols = file_to_symbol_imports.get(caller_file_nid, set())
        imported_modules = file_to_module_imports.get(caller_file_nid, set())

        def _has_import_evidence(candidate_id: str) -> bool:
            # A importação direta de símbolos (`import { foo }`) é a evidência mais forte:
            # o arquivo do chamador tem uma aresta `imports` diretamente para este símbolo.
            # Uma importação de módulo (`import './helper.js'`) confirma o chamador extraído
            candidate_file_nid = nid_to_file_nid.get(candidate_id)
            return (
                candidate_id in imported_symbols
                or (candidate_file_nid is not None and candidate_file_nid in imported_modules)
            )

        if len(candidates) == 1:
            tgt = candidates[0]
            has_import_evidence = go_exact_import or _has_import_evidence(tgt)
        else:
            # Nome ambíguo (definido em mais de 2 arquivos). Não desista imediatamente:
            # se o chamador tiver evidências de importação explícitas apontando exatamente para um
            # dos candidatos, essa importação nomeada desambigua inequivocamente.
            # Prefira correspondências diretas de importação de símbolos; volte para importação de módulo
            # corresponde apenas quando eles também se reduzem a um único alvo. Sem um
            # escolha única baseada em evidências que ignoramos, preservando a guarda # 543
            symbol_matches = [c for c in candidates if c in imported_symbols]
            if len(symbol_matches) == 1:
                tgt = symbol_matches[0]
                has_import_evidence = True
            else:
                module_matches = [
                    c for c in candidates
                    if (cf := nid_to_file_nid.get(c)) is not None and cf in imported_modules
                ]
                if len(module_matches) == 1:
                    tgt = module_matches[0]
                    has_import_evidence = True
                else:
                    # Nenhuma evidência de importação única. Em vez de deixar cair a aresta
                    # de uma vez (o que permite que uma única simulação de teste com o mesmo nome apague o
                    # desempates (preferência sem teste, depois proximidade do caminho).
                    tgt = disambiguate_ambiguous_candidates(
                        candidates,
                        {c: nid_to_source_file.get(c, "") for c in candidates},
                        rc.get("source_file", ""),
                    )
                    if tgt is None:
                        continue
                    has_import_evidence = False
        if rc.get("indirect"):
            # mesa). Resolvido através da mesma definição única/evidência de importação
            # lógica candidata como uma chamada direta, mas emitida como um INFERRED distinto
            # `indirect_call` e SOMENTE quando o alvo é um def real que pode ser chamado -
            # nunca um símbolo de dados com o mesmo nome. Permanece INFERIDO mesmo com importação
            # evidência: o nome é referenciado como um valor aqui, não invocado. Deduplicação
            # é ciente de chamadas (uma aresta de `chamadas` diretas existentes o antecipa; um benigno
            # `importa` aresta para o mesmo símbolo NÃO a suprime).
            if tgt != caller and (caller, tgt) not in call_like_pairs and tgt in callable_nids and tgt not in class_nids:
                call_like_pairs.add((caller, tgt))
                all_edges.append({
                    "source": caller,
                    "target": tgt,
                    "relation": "indirect_call",
                    "context": rc.get("context", "argument"),
                    "confidence": "INFERRED",
                    "confidence_score": 0.8,
                    "source_file": rc.get("source_file", ""),
                    "source_location": rc.get("source_location"),
                    "weight": 1.0,
                })
            continue
        # uma chamada JS/TS DIRECT sem evidência de importação é quase sempre uma
        # exportação não relacionada com o mesmo nome em um pacote que nunca foi importado - um
        # símbolos de nomes genéricos exportados). Módulos JS/TS não têm implícito
        # escopo de módulo cruzado, então deixe-o sem solução em vez de vincular por nome
        # sozinho. Outros idiomas mantêm a resolução de candidato único nº 1553:
        # Cabeçalhos C/C++, carregamento automático Ruby e escopo implícito do mesmo pacote
        # chamar legitimamente entre arquivos sem uma importação explícita. Escopo para
        # chamadas diretas: o caminho indirect_call acima já é conservador
        # (INFERRED, callable-target-gated) e independente de evidências de importação.
        if not has_import_evidence and str(rc.get("source_file", "")).endswith(_JS_TS_CALL_SUFFIXES):
            continue
        if tgt != caller and (caller, tgt) not in existing_pairs:
            existing_pairs.add((caller, tgt))
            # Promova para EXTRACTED quando houver uma aresta de importação direta do
            # arquivo do chamador apontando para o próprio símbolo do chamador ou para o
            if has_import_evidence:
                confidence = "EXTRACTED"
                confidence_score = 1.0
            else:
                confidence = "INFERRED"
                confidence_score = 0.8
            all_edges.append({
                "source": caller,
                "target": tgt,
                "relation": "calls",
                "context": "call",
                "confidence": confidence,
                "confidence_score": confidence_score,
                "source_file": rc.get("source_file", ""),
                "source_location": rc.get("source_location"),
                "weight": 1.0,
            })

    # Resolução de chamada de membro entre arquivos e idioma específico. Corre atrás do compartilhado
    # passe de chamada para que os ids/caller_nids dos nós sejam finais; cada passagem é aditiva (somente a
    # chamadas digitadas/qualificadas pelo receptor, o passe compartilhado foi ignorado) com seu próprio
    # guarda de god node de definição única. Registrado em omnigraph.resolver_registry então
    # uma nova linguagem é conectada sem editar este corpo (Swift Python).
    if resolution_context_nodes or resolution_context_edges:
        _rl_nodes = list(resolution_nodes)
        _rl_edges = all_edges + list(resolution_context_edges or [])
        _n0, _e0 = len(_rl_nodes), len(_rl_edges)
        run_language_resolvers(paths, per_file, _rl_nodes, _rl_edges)
        all_nodes.extend(_rl_nodes[_n0:])
        all_edges.extend(_rl_edges[_e0:])
    else:
        run_language_resolvers(paths, per_file, all_nodes, all_edges)

    # Relativize os campos source_file para que os caminhos sejam portáveis ​​entre máquinas.
    # ID portátil e reescrever os pontos de extremidade que o referenciam.
    ext_id_remap: dict[str, str] = {}
    owned_ids = {n.get("id") for n in all_nodes}
    _sf_forms: dict[str, tuple[str, str, tuple[str, ...]]] = {}

    def _sf_entry(sf: str, sf_path: Path) -> tuple[str, str, tuple[str, ...]]:
        cached = _sf_forms.get(sf)
        if cached is not None:
            return cached
        try:
            rel = sf_path.relative_to(root)
        except ValueError:
            portable = _portable_out_of_root_sf(sf_path)
            canonical_id = _make_id("ext", portable)
            new_sf = portable
        else:
            canonical_id = _file_node_id(rel)
            new_sf = rel.as_posix()
        try:
            sf_resolved = sf_path.resolve()
        except (OSError, RuntimeError):
            sf_resolved = sf_path
        keys = tuple({
            _make_id(str(sf_path)),
            _make_id(str(sf_resolved)),
            _make_id(_file_stem(sf_path)),
            _make_id(_file_stem(sf_resolved)),
        })
        entry = (new_sf, canonical_id, keys)
        _sf_forms[sf] = entry
        return entry

    for item in all_nodes + all_edges:
        sf = item.get("source_file")
        if not sf:
            continue
        sf_path = Path(sf)
        if not sf_path.is_absolute():
            continue
        new_sf, canonical_id, keys = _sf_entry(str(sf), sf_path)
        if "id" in item:
            for key in keys:
                if key == canonical_id or key in ext_id_remap:
                    continue
                if key in owned_ids and item.get("id") != key:
                    continue
                ext_id_remap[key] = canonical_id
        item["source_file"] = new_sf

    if ext_id_remap:
        _ENTRY = "__entry"

        def _canon(nid: str) -> str:
            if nid in ext_id_remap:
                return ext_id_remap[nid]
            if nid.endswith(_ENTRY) and nid[: -len(_ENTRY)] in ext_id_remap:
                return ext_id_remap[nid[: -len(_ENTRY)]] + _ENTRY
            if nid not in owned_ids:
                idx = nid.rfind("_")
                while idx > 0:
                    canonical = ext_id_remap.get(nid[:idx])
                    if canonical is not None:
                        return canonical + nid[idx:]
                    idx = nid.rfind("_", 0, idx)
            return nid

        for n in all_nodes:
            if n.get("id"):
                n["id"] = _canon(n["id"])
        for e in all_edges:
            if e.get("source"):
                e["source"] = _canon(e["source"])
            if e.get("target"):
                e["target"] = _canon(e["target"])

    # origin_file é uma dica de desambiguação interna: a passagem do ID de colisão
    # acima lê para manter os stubs de arquivos cruzados com o mesmo nome distintos, após o que nada
    # consome. Elimine-o dos nós retornados para que nunca seja enviado para graph.json como
    # um caminho absoluto específico da máquina - o mesmo "sem caminhos absolutos na saída"
    # cache mantém sua própria cópia, que é o que a passagem de ID de colisão lê em uma ocorrência de cache.
    for n in all_nodes:
        n.pop("origin_file", None)

    for e in all_edges:
        e.pop("local_alias", None)

    # Marque a proveniência do AST para que a reconstrução incremental do relógio possa distinguir
    # Nós extraídos por AST de nós semânticos/LLM. Em uma reextração completa
    # o observador elimina qualquer nó marcado com AST ausente na nova saída
    # substitui suas arestas AST sem expulsar as arestas semânticas que o AST
    for n in all_nodes:
        n["_origin"] = "ast"
    for e in all_edges:
        e["_origin"] = "ast"

    for _item in (*all_nodes, *all_edges):
        _sf = _item.get("source_file")
        if _sf and "\\" in str(_sf):
            _item["source_file"] = PurePath(_sf).as_posix()

    return {
        "nodes": all_nodes,
        "edges": all_edges,
        "input_tokens": 0,
        "output_tokens": 0,
        "failed_sources": _failed_sources,
    }


def collect_files(target: Path, *, follow_symlinks: bool = False, root: Path | None = None) -> list[Path]:
    containment_root = root if root is not None else target
    from omnigraph.detect import _resolves_under_root
    if target.is_file():
        return [target] if _resolves_under_root(target, containment_root) else []
    _EXTENSIONS = set(_DISPATCH.keys())
    from omnigraph.detect import _is_ignored, _is_noise_dir, _load_omnigraphignore
    ignore_root = root if root is not None else target
    patterns = _load_omnigraphignore(ignore_root)
    # Compartilhado entre todas as chamadas _is_ignored nesta varredura, portanto, diretório ancestral
    # os resultados são memorizados em vez de reavaliados por arquivo.
    ignore_cache: dict[Path, bool] = {}

    def _ignored(p: Path) -> bool:
        return bool(patterns and _is_ignored(p, ignore_root, patterns, _cache=ignore_cache))

    if not follow_symlinks:
        # O antigo filtro rglob rejeitava caminhos com componente de ruído em qualquer lugar,
        # incluindo componentes do próprio alvo – preserve isso.
        if any(_is_noise_dir(part) for part in target.parts):
            return []
        # Quando existirem padrões de negação (!), ignore a remoção de ignorar no nível do diretório
        # então arquivos negados dentro de diretórios ignorados ainda podem ser alcançados (o mesmo
        # conservadorismo como caminhada de varredura da detecção).
        has_negation = any(pat.startswith("!") for _, pat in patterns)
        results: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(target):
            dp = Path(dirpath)
            dirnames[:] = [
                d for d in dirnames
                if not _is_noise_dir(d, dp)
                and (has_negation or not _ignored(dp / d))
            ]
            for fname in filenames:
                p = dp / fname
                suffix = p.suffix
                if (suffix in _EXTENSIONS or suffix.lower() in _EXTENSIONS) and not _ignored(p) and _resolves_under_root(p, containment_root):
                    results.append(p)
        return sorted(results)
    # Caminhe com link simbólico seguindo + detecção de ciclo
    results = []
    for dirpath, dirnames, filenames in os.walk(target, followlinks=True):
        if os.path.islink(dirpath):
            real = os.path.realpath(dirpath)
            parent_real = os.path.realpath(os.path.dirname(dirpath))
            if parent_real == real or parent_real.startswith(real + os.sep):
                dirnames.clear()
                continue
        dp = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames
            if not _is_noise_dir(d, dp)
            and (not (dp / d).is_symlink() or _resolves_under_root(dp / d, containment_root))
        ]
        for fname in filenames:
            p = dp / fname
            suffix = p.suffix
            if (suffix in _EXTENSIONS or suffix.lower() in _EXTENSIONS) and not _ignored(p) and _resolves_under_root(p, containment_root):
                results.append(p)
    return sorted(results)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m omnigraph.extract <file_or_dir> ...", file=sys.stderr)
        sys.exit(1)

    paths: list[Path] = []
    for arg in sys.argv[1:]:
        paths.extend(collect_files(Path(arg)))

    result = extract(paths)
    print(json.dumps(result, indent=2))
