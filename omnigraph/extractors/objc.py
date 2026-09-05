"""objc — moved verbatim from omnigraph/extract.py."""
from __future__ import annotations

from omnigraph.extractors.base import _file_stem, _make_id, _read_text
from omnigraph.extractors.engine import _cpp_declarator_name, _semantic_reference_edge
from omnigraph.extractors.resolution import _resolve_c_include_path
from pathlib import Path
from typing import Any
import re

# A category stem splits only when both halves look like plain ObjC identifiers, so
# `C++Bridge.h` and `Foo+.h` are left intact.
_OBJC_STEM_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _objc_category_base_stem(stem: str) -> str:
    """Strip an ObjC category/extension suffix from a file stem (``Foo+Cat`` -> ``Foo``).

    A category (``@interface Foo (Cat)``) or class extension (``@interface Foo ()``)
    declares members of an EXISTING class, so its class node must key to the same id
    as ``Foo.h``'s ``@interface Foo`` instead of minting a second class node (#1556).
    Only the final path segment is considered, and only a well-formed
    ``Name+Suffix`` pair is split.

    Mirrors ``_decldef_class_stem``, which already compares sibling header/impl files
    by the stem before ``+``.
    """
    head, sep, tail = stem.rpartition("/")
    base, plus, suffix = tail.partition("+")
    if not plus or not _OBJC_STEM_PART.fullmatch(base) or not _OBJC_STEM_PART.fullmatch(suffix):
        return stem
    return f"{head}{sep}{base}"


def _objc_is_category(node) -> bool:
    """True for ``@interface/@implementation Foo (Cat)`` and ``Foo ()``.

    The grammar emits the parentheses as anonymous children only for a category or
    class extension; a generic class (``@interface Box<T>``) uses
    ``parameterized_arguments`` instead, so it is not matched.
    """
    return any(c.type == "(" for c in node.children)


def _objc_local_var_types(body_node, source: bytes, table: dict[str, str]) -> None:
    """Collect ``var -> ClassName`` from ObjC local declarations (``Foo *f = ...;``)
    in a method body, for receiver typing in the cross-file message-send pass
    (#1556). Only a capitalized ``type_identifier`` with a single named declarator
    is recorded; a built-in/lower-cased type or an un-nameable declarator is skipped
    (precision over recall). Reuses the C++ declarator unwrapper (identical grammar).
    """
    stack = [body_node]
    while stack:
        n = stack.pop()
        if n.type == "method_definition" and n is not body_node:
            continue
        if n.type == "declaration":
            type_node = n.child_by_field_name("type")
            if type_node is None:
                for c in n.children:
                    if c.type == "type_identifier":
                        type_node = c
                        break
            if type_node is not None and type_node.type == "type_identifier":
                type_name = _read_text(type_node, source).strip()
                declarators = [
                    c for c in n.children
                    if c.type in ("identifier", "pointer_declarator", "init_declarator")
                ]
                if type_name and type_name[:1].isupper() and len(declarators) == 1:
                    var = _cpp_declarator_name(declarators[0], source)
                    if var and var not in table:
                        table[var] = type_name
        for c in n.children:
            stack.append(c)

def extract_objc(path: Path) -> dict:
    """Extract interfaces, implementations, protocols, methods, and imports from .m/.mm/.h files."""
    try:
        import tree_sitter_objc as tsobjc
        from tree_sitter import Language, Parser
    except ImportError:
        return {"nodes": [], "edges": [], "error": "tree_sitter_objc not installed"}

    try:
        language = Language(tsobjc.language())
        parser = Parser(language)
        source = path.read_bytes()
        # tree-sitter-objc não pode expandir essas macros de anotação sem argumentos (não
        # à direita de ';'), e sua presença antes de @interface faz com que o analisador falhe
        # emita um nó class_interface. Apague-os em espaços de comprimento igual, então byte
        # os deslocamentos/números de linha são preservados e a interface é analisada.
        _OBJC_BLANK_MACROS = (b"NS_ASSUME_NONNULL_BEGIN", b"NS_ASSUME_NONNULL_END")
        for _m in _OBJC_BLANK_MACROS:
            source = source.replace(_m, b" " * len(_m))
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    method_bodies: list[tuple[str, Any, str]] = []
    # envios de mensagens não resolvidas salvas para o resolvedor ObjC de arquivos cruzados, mais um
    # tabela `var -> ClassName` por arquivo de declarações locais `Foo *f = ...;`.
    raw_calls: list[dict] = []
    objc_type_table: dict[str, str] = {}
    # per-CLASS `field -> ClassName` tables from `@property Bar *bar;` and
    # ivar `Bar *_bar;` declarations, keyed by the class nid the .h/.m pair share
    # (preserved by _merge_decl_def_classes), so the cross-file resolver can type a
    # `[self.bar doIt]` / `[_bar doIt]` receiver. A conflicting redeclaration of the
    # same (class, field) tombstones the entry (None) — drop, don't guess.
    objc_field_types: dict[str, dict[str, str | None]] = {}

    def add_node(nid: str, label: str, line: int) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({"id": nid, "label": label, "file_type": "code",
                          "source_file": str_path, "source_location": f"L{line}"})

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0,
                 context: str | None = None) -> None:
        edge = {"source": src, "target": tgt, "relation": relation,
                "confidence": confidence, "source_file": str_path,
                "source_location": f"L{line}", "weight": weight}
        if context:
            edge["context"] = context
        edges.append(edge)

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    def _read(node) -> str:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    def _get_name(node, field: str) -> str | None:
        n = node.child_by_field_name(field)
        return _read(n) if n else None

    def _type_identifiers(node):
        """Yield every type_identifier under a property's type node, descending
        through generic_specifier/type_name so NSArray<Product *> yields both
        NSArray and the element type Product (the generic case was invisible
        because the type was wrapped in a generic_specifier, not a bare
        type_identifier child) (#1475)."""
        if node.type == "type_identifier":
            yield node
            return
        for c in node.children:
            yield from _type_identifiers(c)

    def ensure_named_node(name: str, line: int) -> str:
        nid = _make_id(stem, name)
        if nid in seen_ids:
            return nid
        nid = _make_id(name)
        if nid not in seen_ids:
            # O nome não está definido neste arquivo, então esta é uma referência entre arquivos
            # (por exemplo, uma anotação do tipo `Thing` importada de outro módulo). Emita um
            # Stub SOURCELESS - como o caminho base de herança abaixo - então o
            # a religação em nível de corpus pode reduzi-la à definição real. Uma fonte
            # stub aqui faz _disambiguate_colliding_node_ids preparar a referência
            # caminho do arquivo (com extensão) para o id e bloqueia a religação, que é
            # o bug do nó duplicado fantasma.
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

    def _field_decl_entry(sd) -> tuple[str, str] | None:
        """``(field, TypeName)`` from a property/ivar ``struct_declaration``, else None.

        Precision gates (#1556): the type must be a BARE capitalized
        ``type_identifier`` DIRECTLY under the struct_declaration — a
        ``generic_specifier`` (``NSArray<Bar *>``) or ``typedefed_specifier``
        (``id<P>``) wraps its type_identifier, so both are naturally excluded and
        never type a receiver — and there must be exactly one ``struct_declarator``
        whose name unwraps via the C++ declarator unwrapper (identical grammar).
        """
        type_ids = [s for s in sd.children if s.type == "type_identifier"]
        declarators = [s for s in sd.children if s.type == "struct_declarator"]
        if len(type_ids) != 1 or len(declarators) != 1:
            return None
        type_name = _read(type_ids[0]).strip()
        if not type_name or not type_name[:1].isupper():
            return None
        # struct_declarator wraps ONE pointer_declarator (`*bar`) or identifier
        # (`bar`); anything else (bitfields, arrays) has more children -> bail.
        inner = declarators[0].children
        if len(inner) != 1:
            return None
        field = _cpp_declarator_name(inner[0], source)
        if not field:
            return None
        return field, type_name

    def _record_field_type(cls_nid: str, field: str, type_name: str) -> None:
        table = objc_field_types.setdefault(cls_nid, {})
        if field in table:
            if table[field] != type_name:
                table[field] = None  # conflicting redeclaration -> drop, don't guess
        else:
            table[field] = type_name

    def _collect_instance_variables(ivars_node, cls_nid: str) -> None:
        """Record field types from an ``instance_variables`` block (``{ Bar *_b; }``)
        under an @interface or @implementation. No nodes/edges are emitted here;
        the table only feeds receiver typing in the cross-file resolver (#1556).
        """
        for iv in ivars_node.children:
            if iv.type != "instance_variable":
                continue
            for sd in iv.children:
                if sd.type == "struct_declaration":
                    entry = _field_decl_entry(sd)
                    if entry is not None:
                        _record_field_type(cls_nid, *entry)

    def walk(node, parent_nid: str | None = None) -> None:
        t = node.type
        line = node.start_point[0] + 1

        if t == "preproc_include":
            # #importar <Foundation/Foundation.h> ou #import "MyClass.h"
            for child in node.children:
                if child.type == "system_lib_string":
                    raw = _read(child).strip("<>")
                    module = raw.split("/")[-1].replace(".h", "")
                    if module:
                        tgt_nid = _make_id(module)
                        add_edge(file_nid, tgt_nid, "imports", line, context="import")
                elif child.type == "string_literal":
                    # recorrer a string_literal para encontrar string_content
                    for sub in child.children:
                        if sub.type == "string_content":
                            raw = _read(sub)
                            # Resolva a inclusão citada em um arquivo real para que o ID de destino
                            # corresponde ao id do nó (possivelmente sem ambiguidade) _make_id fornecido
                            # esse arquivo; o id da haste nua nunca sobrevive
                            # _disambiguate_colliding_node_ids quando existe um par .h/.m,
                            # então a aresta ficou pendurada e caiu.
                            resolved = _resolve_c_include_path(raw, str_path)
                            if resolved is not None:
                                add_edge(file_nid, _make_id(str(resolved)), "imports", line, context="import")
                            else:
                                module = raw.split("/")[-1].replace(".h", "")
                                if module:
                                    add_edge(file_nid, _make_id(module), "imports", line, context="import")
            return

        if t == "module_import":
            # @import Foundation;  /  @import Foundation.NSString;
            path_node = node.child_by_field_name("path")
            if path_node is not None:
                module = _read(path_node).split(".")[0].strip()
                if module:
                    add_edge(file_nid, _make_id(module), "imports", line, context="import")
            return

        if t == "class_interface":
            # @interface ClassName : SuperClass <Protocols>
            # children: @interface, identifier(name), ':', identifier(super), parameterized_arguments, ...
            identifiers = [c for c in node.children if c.type == "identifier"]
            if not identifiers:
                for child in node.children:
                    walk(child, parent_nid)
                return
            name = _read(identifiers[0])
            # A category / class extension extends an existing class, so key its
            # class node off the BASE stem (`Foo+Cat.h` -> `Foo`). Without this,
            # `Foo+Cat.h` minted a SECOND node labelled `Foo`, which made every
            # `[Foo ...]` receiver ambiguous and tripped the resolver's
            # single-definition god-node guard — destroying edges the same corpus
            # produced fine when the members lived in `Foo.h`.
            cls_stem = _objc_category_base_stem(stem) if _objc_is_category(node) else stem
            cls_nid = _make_id(cls_stem, name)
            add_node(cls_nid, name, line)
            add_edge(file_nid, cls_nid, "contains", line)
            # superclasse é o segundo identificador depois de ':'
            colon_seen = False
            for child in node.children:
                if child.type == ":":
                    colon_seen = True
                elif colon_seen and child.type == "identifier":
                    super_nid = ensure_named_node(_read(child), line)
                    add_edge(cls_nid, super_nid, "inherits", line)
                    colon_seen = False
                elif child.type == "parameterized_arguments":
                    # protocols adopted: @interface Foo : Bar <Proto1, Proto2>
                    for sub in child.children:
                        if sub.type == "type_name":
                            for s in sub.children:
                                if s.type == "type_identifier":
                                    proto_nid = ensure_named_node(_read(s), line)
                                    add_edge(cls_nid, proto_nid, "implements", line)
                elif child.type == "property_declaration":
                    prop_line = child.start_point[0] + 1
                    for sub in child.children:
                        if sub.type == "struct_declaration":
                            # O tipo é um type_identifier direto
                            # (NSString *x) ou encapsulado em um generic_specifier
                            # (NSArray<Produto *> *xs). Percorra cada nome de tipo no
                            # parte do tipo, ignorando o declarador (o *campo
                            # nome), então as coleções genéricas não são mais invisíveis.
                            seen_types: set[str] = set()
                            for s in sub.children:
                                if s.type in ("struct_declarator", ";"):
                                    continue
                                for ti in _type_identifiers(s):
                                    tname = _read(ti)
                                    if tname in seen_types:
                                        continue
                                    seen_types.add(tname)
                                    type_nid = ensure_named_node(tname, prop_line)
                                    edges.append(_semantic_reference_edge(
                                        cls_nid, type_nid, "field", str_path, prop_line))
                            # a bare capitalized property type also types the
                            # `[self.field msg]` receiver in the cross-file resolver.
                            entry = _field_decl_entry(sub)
                            if entry is not None:
                                _record_field_type(cls_nid, *entry)
                elif child.type == "instance_variables":
                    _collect_instance_variables(child, cls_nid)
                elif child.type == "method_declaration":
                    walk(child, cls_nid)
            return

        if t == "class_implementation":
            # @implementation ClassName
            name = None
            for child in node.children:
                if child.type == "identifier":
                    name = _read(child)
                    break
            if not name:
                for child in node.children:
                    walk(child, parent_nid)
                return
            impl_stem = _objc_category_base_stem(stem) if _objc_is_category(node) else stem
            impl_nid = _make_id(impl_stem, name)
            if impl_nid not in seen_ids:
                add_node(impl_nid, name, line)
                add_edge(file_nid, impl_nid, "contains", line)
            for child in node.children:
                if child.type == "instance_variables":
                    _collect_instance_variables(child, impl_nid)
                elif child.type == "implementation_definition":
                    for sub in child.children:
                        walk(sub, impl_nid)
            return

        if t == "protocol_declaration":
            name = None
            for child in node.children:
                if child.type == "identifier":
                    name = _read(child)
                    break
            if name:
                proto_nid = _make_id(stem, name)
                add_node(proto_nid, f"<{name}>", line)
                add_edge(file_nid, proto_nid, "contains", line)
                # Protocolos adotados: `@protocol Derived <Base, Other>`. Esses
                # aninhar sob um nó protocol_reference_list (distinto do nó
                # nó parameterized_arguments usado pela adoção de @interface), então
                # eles nunca foram emitidos. Emita uma aresta `implementos` para cada um,
                # combinando como a adoção do protocolo @interface é tratada.
                for child in node.children:
                    if child.type == "protocol_reference_list":
                        for sub in child.children:
                            if sub.type == "identifier":
                                base_nid = ensure_named_node(_read(sub), line)
                                if base_nid != proto_nid:
                                    add_edge(proto_nid, base_nid, "implements", line)
                for child in node.children:
                    walk(child, proto_nid)
            return

        if t in ("method_declaration", "method_definition"):
            container = parent_nid or file_nid
            # Os métodos de classe começam com '+', os métodos de instância com '-' (a gramática
            # emite o sigilo como o primeiro filho). O seletor é a concatenação
            # dos filhos do identificador direto: um para um seletor simples (-go),
            # vários para um composto (-tableView:numberOfRowsInSection: ->
            # "tableViewnumberOfRowsInSection"); method_parameter contém o argumento
            # tipos/nomes, não palavras-chave do seletor, portanto é ignorado corretamente.
            prefix = "-"
            for child in node.children:
                if child.type in ("+", "-"):
                    prefix = child.type
                    break
            parts = [_read(c) for c in node.children if c.type == "identifier"]
            method_name = "".join(parts) if parts else None
            if method_name:
                method_nid = _make_id(container, method_name)
                add_node(method_nid, f"{prefix}{method_name}", line)
                add_edge(container, method_nid, "method", line)
                if t == "method_definition":
                    method_bodies.append((method_nid, node, container))
            return

        for child in node.children:
            walk(child, parent_nid)

    walk(root)

    # Second pass: resolve calls inside method bodies
    all_method_nids = {n["id"] for n in nodes if n["id"] != file_nid}
    class_method_nids: dict[str, set[str]] = {}
    for m_nid, _, container_nid in method_bodies:
        class_method_nids.setdefault(container_nid, set()).add(m_nid)
    seen_calls: set[tuple[str, str]] = set()
    # tabela `var -> ClassName` por arquivo de declarações locais em cada
    # corpo do método, para que o resolvedor de arquivos cruzados possa digitar um receptor `[f doThing]`.
    for _m_nid, body_node, _container in method_bodies:
        _objc_local_var_types(body_node, source, objc_type_table)

    for caller_nid, body_node, container_nid in method_bodies:
        sibling_nids = class_method_nids.get(container_nid, set())

        def walk_calls(n) -> None:
            if n.type == "message_expression":
                # `[[Foo alloc] init]` é uma expressão_mensagem cujo método é o
                # identificador `alloc` e cujo receptor é o identificador de classe simples
                # `Foo`; resolva o nome da classe e emita uma aresta de `referências` para que o
                # alocando links de método para o tipo alocado. ensure_named_node
                # emite um stub sem fonte para nomes desconhecidos, que o corpus religa
                # entra em colapso SOMENTE quando existe exatamente uma classe real com esse nome, então um
                # classe desconhecida/ambígua não produz nenhuma aresta falsa resolvida.
                meth = n.child_by_field_name("method")
                recv = n.child_by_field_name("receiver")
                if (meth is not None and meth.type == "identifier" and _read(meth) == "alloc"
                        and recv is not None and recv.type == "identifier"):
                    tname = _read(recv)
                    ref_line = n.start_point[0] + 1
                    type_nid = ensure_named_node(tname, ref_line)
                    if type_nid != caller_nid:
                        edges.append(_semantic_reference_edge(
                            caller_nid, type_nid, "type", str_path, ref_line))
                # [receiver sel] e [receiver kw1:a kw2:b] ambos analisam para um
                # message_expression cujas partes do seletor carregam o nome do campo
                # "método" (um para um seletor simples, vários para um composto);
                # o receptor carrega o nome de campo "receptor". Reconstrua o
                # seletor de cada filho de "método" então self/super/ClassName
                # receptores nunca são confundidos com um seletor e envios compostos
                # resolver também (toda a segunda passagem era anteriormente um código morto para
                # ObjC porque a gramática os emite como `identificador`, não
                # `selector`/`keyword_argument_list`).
                sel_parts = [
                    _read(child)
                    for i, child in enumerate(n.children)
                    if n.field_name_for_child(i) == "method" and child.type == "identifier"
                ]
                method_name = "".join(sel_parts)
                if method_name:
                    needle = _make_id("", method_name).lstrip("_")
                    for candidate in all_method_nids:
                        if candidate.endswith(needle):
                            pair = (caller_nid, candidate)
                            if pair not in seen_calls and caller_nid != candidate:
                                seen_calls.add(pair)
                                add_edge(caller_nid, candidate, "calls", n.start_point[0] + 1,
                                         confidence="EXTRACTED", weight=1.0, context="call")
                    # também emite um raw_call para que o resolvedor de arquivos cruzados possa digitar
                    # o receptor e link para um método em OUTRO arquivo. Um nu
                    # receptor identificador (`f`, `self`, `Foo`) é capturado; um aninhado
                    # mensagem enviada (`[[Foo alloc] init]`) não tem nome de receptor simples
                    # para digitar, então é deixado para a aresta `references` de alloc/init acima.
                    if recv is not None and recv.type == "identifier":
                        raw_calls.append({
                            "caller_nid": caller_nid,
                            "callee": method_name,
                            "is_member_call": True,
                            "source_file": str_path,
                            "source_location": f"L{n.start_point[0] + 1}",
                            "receiver": _read(recv),
                            "lang": "objc",
                        })
                    elif recv is not None and recv.type == "field_expression":
                        # `[self.bar doIt]`: capture ONLY the exact `self.<field>`
                        # shape and stamp the BARE field name, typed via the class's
                        # property/ivar table. Anything else (`obj.prop`, chains,
                        # `Foo.shared`) stays dropped: passing the dotted text
                        # through would let a capitalized `Foo.shared` enter the
                        # explicit-class arm, where _key strips the dot and collides
                        # with a real class `FooShared` — a fabricated edge.
                        kids = recv.children
                        if (len(kids) == 3 and kids[0].type == "identifier"
                                and _read(kids[0]) == "self" and kids[1].type == "."
                                and kids[2].type == "field_identifier"):
                            raw_calls.append({
                                "caller_nid": caller_nid,
                                "callee": method_name,
                                "is_member_call": True,
                                "source_file": str_path,
                                "source_location": f"L{n.start_point[0] + 1}",
                                "receiver": _read(kids[2]),
                                "receiver_kind": "self_field",
                                "lang": "objc",
                            })
            elif n.type == "field_expression":
                # self.name / self.product.name — açúcar de sintaxe de ponto para [nome próprio].
                # Resolva para um método irmão da classe SAME, correspondido por EXACT
                # ID do nó (um ID de método é _make_id(container, name)). Um sufixo
                # a correspondência de substring resolveria incorretamente self.name -> -surname e seria
                # deixe um irmão que colide com substrings (-sobrenome) suprimir o real
                # -name edge, portanto deve ser uma correspondência exata.
                for child in n.children:
                    if child.type == "field_identifier":
                        field_name = _read(child)
                        target = _make_id(container_nid, field_name)
                        if target in sibling_nids and target != caller_nid:
                            pair = (caller_nid, target)
                            if pair not in seen_calls:
                                seen_calls.add(pair)
                                add_edge(caller_nid, target, "accesses",
                                         n.start_point[0] + 1,
                                         confidence="EXTRACTED", weight=1.0)
            elif n.type == "selector_expression":
                # @selector(doSomething:withParam:) — compile-time method ref.
                # Corresponda EXATAMENTE ao nome do seletor (um id de método é
                # _make_id(container, name)) em relação aos métodos de cada classe e emite
                # somente quando exatamente um método corresponde, para evitar distribuição ambígua.
                # A correspondência exata (não um sufixo) mantém -doThing distinto de
                # -reallyDoThing.
                sel_parts = [_read(c) for c in n.children if c.type == "identifier"]
                sel_name = "".join(sel_parts)
                if sel_name:
                    matches = sorted({
                        m for m, _, cont in method_bodies
                        if m == _make_id(cont, sel_name) and m != caller_nid
                    })
                    if len(matches) == 1:
                        pair = (caller_nid, matches[0])
                        if pair not in seen_calls:
                            seen_calls.add(pair)
                            add_edge(caller_nid, matches[0], "calls",
                                     n.start_point[0] + 1,
                                     confidence="EXTRACTED", weight=1.0,
                                     context="call")
            for child in n.children:
                walk_calls(child)
        walk_calls(body_node)

    result = {"nodes": nodes, "edges": edges, "raw_calls": raw_calls,
              "input_tokens": 0, "output_tokens": 0}
    if objc_type_table:
        result["objc_type_table"] = {"path": str_path, "table": objc_type_table}
    # Drop tombstoned (conflicting) entries and empty tables before export.
    field_tables = {
        cls: {f: t for f, t in tbl.items() if t}
        for cls, tbl in objc_field_types.items()
    }
    field_tables = {cls: tbl for cls, tbl in field_tables.items() if tbl}
    if field_tables:
        result["objc_field_types"] = {"path": str_path, "tables": field_tables}
    return result
