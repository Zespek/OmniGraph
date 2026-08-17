"""OCaml extractor (own module, optional tree-sitter-ocaml dependency).

Handles implementation files (.ml, `language_ocaml`) and interface files
(.mli, `language_ocaml_interface`). Interfaces have no expression bodies, so
they contribute definitions and `open` imports but no `calls` edges.
"""
from __future__ import annotations


from pathlib import Path
from omnigraph.extractors.base import _file_stem, _make_id, _read_text


def extract_ocaml(path: Path) -> dict:
    """Extract modules, values, functions, types, variant constructors, `open`
    imports, and (for .ml) function calls from an OCaml source file."""
    try:
        import tree_sitter_ocaml as tsocaml
        from tree_sitter import Language, Parser
    except ImportError:
        return {"nodes": [], "edges": [], "error": "tree-sitter-ocaml not installed"}

    try:
        if path.suffix == ".mli":
            language = Language(tsocaml.language_ocaml_interface())
        else:
            language = Language(tsocaml.language_ocaml())
        parser = Parser(language)
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()

    local_defs: dict[str, str] = {}
    ambiguous: set[str] = set()
    local_modules: set[str] = set()
    call_sites: list[tuple[str, str, str | None, str, int]] = []

    def add_node(nid: str, label: str, line: int) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": label,
                "file_type": "code",
                "source_file": str_path,
                "source_location": f"L{line}",
            })

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0) -> None:
        edges.append({
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": confidence,
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": weight,
        })

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    def ref_stub(name: str) -> str:
        """Mint a SOURCELESS stub for a cross-file reference target (an `open`ed
        module or a call to a name not defined in this file). The corpus-level
        rewire collapses it onto the unique real definition of that name;
        external names (Stdlib, Core, ...) dangle and are pruned. A *sourced*
        stub here would bake this file's path into the id and block the rewire
        (the #1402 phantom-duplicate bug)."""
        nid = _make_id(name)
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": name,
                "file_type": "code",
                "source_file": "",
                "source_location": "",
                "origin_file": str_path,
            })
        return nid

    def line_of(node) -> int:
        return node.start_point[0] + 1

    def named_child_text(node, child_type: str) -> str | None:
        for child in node.children:
            if child.type == child_type:
                return _read_text(child, source)
        return None

    def last_name(path_node) -> str | None:
        """Final component of a *_path node: `Geo.area` -> `area`,
        `Stdlib.List` -> `List`. The qualifier is nested under a child
        `module_path`, so the final name is always a *direct* child of the path
        node -- a deep search would wrongly return the module qualifier."""
        found: str | None = None
        for n in path_node.children:
            if n.type in ("value_name", "module_name", "constructor_name"):
                found = _read_text(n, source)
        return found

    def path_root_module(path_node) -> str | None:
        """Leftmost (outermost) module segment qualifying a *_path node:
        `Reg_spec.create` -> `Reg_spec`, `Stdlib.List.map` -> `Stdlib`. Returns
        None when the path is unqualified (`create`), which has no child
        module_path."""
        mp = next((c for c in path_node.children if c.type == "module_path"), None)
        if mp is None:
            return None
        node = mp
        while True:
            inner = next((c for c in node.children if c.type == "module_path"), None)
            if inner is None:
                break
            node = inner
        mn = next((c for c in node.children if c.type == "module_name"), None)
        return _read_text(mn, source) if mn is not None else None

    def register_def(name: str, nid: str) -> None:
        if name in ambiguous:
            return
        if name in local_defs and local_defs[name] != nid:
            ambiguous.add(name)
            local_defs.pop(name, None)
            return
        local_defs[name] = nid

    def emit_type(binding, container_nid: str) -> None:
        type_name = named_child_text(binding, "type_constructor")
        if not type_name:
            return
        line = line_of(binding)
        nid = _make_id(stem, type_name)
        add_node(nid, type_name, line)
        add_edge(container_nid, nid, "defines" if container_nid == file_nid else "contains", line)
        register_def(type_name, nid)
        for vd in binding.children:
            if vd.type != "variant_declaration":
                continue
            for cd in vd.children:
                if cd.type != "constructor_declaration":
                    continue
                cname = named_child_text(cd, "constructor_name")
                if not cname:
                    continue
                cnid = _make_id(stem, type_name, cname)
                add_node(cnid, cname, line_of(cd))
                add_edge(nid, cnid, "contains", line_of(cd))

    def walk(node, container_nid: str, enclosing_value: str) -> None:
        t = node.type

        if t == "open_module":
            mp = next((c for c in node.children if c.type == "module_path"), None)
            name = last_name(mp) if mp is not None else None
            if name:
                add_edge(container_nid, ref_stub(name), "imports_from", line_of(node),
                         confidence="INFERRED")
            return

        if t == "module_definition":
            binding = next((c for c in node.children if c.type == "module_binding"), None)
            if binding is not None:
                mname = named_child_text(binding, "module_name")
                if mname:
                    line = line_of(node)
                    mnid = _make_id(stem, mname)
                    add_node(mnid, mname, line)
                    add_edge(container_nid, mnid,
                             "defines" if container_nid == file_nid else "contains", line)
                    register_def(mname, mnid)
                    local_modules.add(mname)
                    for child in binding.children:
                        walk(child, mnid, enclosing_value)
                    return

        if t == "module_type_definition":
            mname = named_child_text(node, "module_type_name")
            if mname:
                line = line_of(node)
                mnid = _make_id(stem, mname)
                add_node(mnid, mname, line)
                add_edge(container_nid, mnid,
                         "defines" if container_nid == file_nid else "contains", line)
                register_def(mname, mnid)
                local_modules.add(mname)
                for child in node.children:
                    walk(child, mnid, enclosing_value)
                return

        if t == "value_definition":
            is_toplevel = node.parent is not None and node.parent.type in (
                "compilation_unit", "structure")
            for lb in node.children:
                if lb.type != "let_binding":
                    continue
                vname = named_child_text(lb, "value_name")
                new_scope = enclosing_value
                if vname and is_toplevel:
                    line = line_of(lb)
                    nid = _make_id(stem, vname)
                    add_node(nid, vname, line)
                    add_edge(container_nid, nid,
                             "defines" if container_nid == file_nid else "contains", line)
                    register_def(vname, nid)
                    new_scope = nid
                for child in lb.children:
                    walk(child, container_nid, new_scope)
            return

        if t == "value_specification":
            vname = named_child_text(node, "value_name")
            if vname:
                line = line_of(node)
                nid = _make_id(stem, vname)
                add_node(nid, vname, line)
                add_edge(container_nid, nid,
                         "defines" if container_nid == file_nid else "contains", line)
                register_def(vname, nid)
            return

        if t == "type_definition":
            for binding in node.children:
                if binding.type == "type_binding":
                    emit_type(binding, container_nid)
            return

        if t == "application_expression":
            fn = node.named_children[0] if node.named_children else None
            if fn is not None and fn.type == "value_path":
                callee = last_name(fn)
                if callee:
                    caller = enclosing_value if enclosing_value else file_nid
                    call_sites.append((caller, callee, path_root_module(fn),
                                       _read_text(fn, source), line_of(node)))

        for child in node.children:
            walk(child, container_nid, enclosing_value)

    walk(root, file_nid, "")

    for caller, callee, qualifier, full_path, line in call_sites:
        if qualifier is not None and qualifier not in local_modules:
            if callee in local_defs:
                add_edge(caller, ref_stub(full_path), "calls", line, confidence="INFERRED")
            else:
                add_edge(caller, ref_stub(callee), "calls", line, confidence="INFERRED")
        elif callee in local_defs:
            add_edge(caller, local_defs[callee], "calls", line)
        else:
            add_edge(caller, ref_stub(callee), "calls", line, confidence="INFERRED")

    return {"nodes": nodes, "edges": edges}
