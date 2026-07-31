"""Type-aware cross-file resolution for Ruby member calls.

Ruby has no type annotations and reuses method names heavily, so resolving
``obj.method()`` by globally-unique name is both lossy (drops on collision) and
unsafe (can attach to the wrong same-named method). This resolver instead uses
the receiver's *type*, inferred at extraction time from local
``var = ClassName.new`` bindings and carried on each member-call raw_call as
``receiver_type``.

It resolves two shapes, both at EXTRACTED (1.0) confidence and only when the
target is certain (single owning class, single owned method) — bail otherwise:

  * ``Processor.new``          -> a ``calls`` edge to the ``Processor`` class
  * ``p.run`` where ``p`` is a ``Processor`` -> a ``calls`` edge to ``Processor#run``

Registered into omnigraph.resolver_registry and run by extract() after id
disambiguation, so node ids and raw_call caller_nids are final.
"""

from __future__ import annotations

import re
from typing import Any


def _key(label: str) -> str:
    """Normalize a class/method label to a comparison key (drop punctuation)."""
    return re.sub(r"[^a-zA-Z0-9]+", "", str(label)).lower()


# Um nó contêiner de classe/módulo Ruby é rotulado com uma constante simples
# (``Processador``, ``TaxCalculator``); os métodos terminam em ``()`` e os arquivos em ``.rb``.
# Vamos registrar contêineres sem método (um erro ``Class.new(StandardError)``
# class, um módulo vazio) que não possui nenhuma aresta de `método` para ser encontrada.
_BARE_CONST_RE = re.compile(r"^[A-Z][A-Za-z0-9_]*$")


def _ruby_raw_calls(per_file: list[dict]) -> list[dict]:
    calls: list[dict] = []
    for result in per_file:
        if not isinstance(result, dict):
            continue
        for rc in result.get("raw_calls", []):
            if not isinstance(rc, dict):
                continue
            sf = str(rc.get("source_file", ""))
            if sf.endswith((".rb", ".rake")):
                calls.append(rc)
    return calls


def resolve_ruby_member_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve Ruby ``Class.new`` and typed ``var.method`` calls by receiver type.

    Purely additive: only emits edges the shared (name-based) call pass skips
    because they are member calls. Each emission requires a single owning class
    (god-node guard) so an ambiguous class name resolves to nothing rather than a
    wrong edge.
    """
    node_by_id: dict[str, dict] = {n.get("id"): n for n in all_nodes}

    # class label key -> [class node ids]; (class_node_id, method_key) -> method id
    class_def_nids: dict[str, list[str]] = {}
    method_index: dict[tuple[str, str], str] = {}
    for e in all_edges:
        if e.get("relation") != "method":
            continue
        src, tgt = e.get("source"), e.get("target")
        cnode = node_by_id.get(src)
        if cnode is not None:
            class_def_nids.setdefault(_key(cnode.get("label", "")), []).append(str(src))
        tnode = node_by_id.get(tgt)
        if tnode is not None:
            method_index[(str(src), _key(tnode.get("label", "")))] = str(tgt)
    # Registre também nós de contêiner de classe/módulo que não possuem aresta de `método` - um
    # `Class.new(StandardError)` sem método ou um módulo vazio - portanto, uma constante
    # o receptor ainda resolve para um nó real. Stubs de base externos
    # carrega um source_file vazio, então o filtro `.rb` os mantém fora.
    for n in all_nodes:
        nid = n.get("id")
        sf = str(n.get("source_file", ""))
        if nid and sf.endswith((".rb", ".rake")) and _BARE_CONST_RE.match(str(n.get("label", ""))):
            class_def_nids.setdefault(_key(n.get("label", "")), []).append(str(nid))
    for k in list(class_def_nids):
        class_def_nids[k] = sorted(set(class_def_nids[k]))

    existing_pairs = {(e.get("source"), e.get("target")) for e in all_edges}

    def _unique_class(name: str) -> str | None:
        nids = class_def_nids.get(_key(name), [])
        return nids[0] if len(nids) == 1 else None

    def _emit(caller: str, target: str, rc: dict[str, Any],
              relation: str = "calls", context: str = "call") -> None:
        if not caller or not target or caller == target:
            return
        if (caller, target) in existing_pairs:
            return
        existing_pairs.add((caller, target))
        all_edges.append({
            "source": caller,
            "target": target,
            "relation": relation,
            "context": context,
            "confidence": "EXTRACTED",
            "confidence_score": 1.0,
            "source_file": rc.get("source_file", ""),
            "source_location": rc.get("source_location"),
            "weight": 1.0,
        })

    # `include`/`extend`/`prepend <Const>` mixins: resolva o módulo por
    # seu nome constante para o único módulo/nó de classe proprietário e emite um
    # aresta `mixes_in`, sob a mesma proteção de god node de definição única. Um
    # constante ambígua ou não resolvida não produz arestas.
    for rc in _ruby_raw_calls(per_file):
        if not rc.get("is_mixin"):
            continue
        caller = str(rc.get("caller_nid", ""))
        module_name = rc.get("callee")
        if not caller or not module_name:
            continue
        target = _unique_class(str(module_name))
        if target is not None:
            _emit(caller, target, rc, relation="mixes_in", context="mixin")

    for rc in _ruby_raw_calls(per_file):
        if not rc.get("is_member_call"):
            continue
        caller = str(rc.get("caller_nid", ""))
        callee = rc.get("callee")
        if not caller or not callee:
            continue

        # Receptor constante: `Processor.new` (instanciação) ou `Service.call` /
        # `Model.where` (método singleton/classe). O nome do método simples seria
        # colidem com métodos não relacionados com o mesmo nome, então resolvemos pelo
        # classe do receptor sob a guarda do god node de classe de propriedade única.
        receiver = rc.get("receiver")
        if receiver and str(receiver)[:1].isupper():
            class_nid = _unique_class(str(receiver))
            if class_nid is not None:
                if callee == "new":
                    _emit(caller, class_nid, rc)
                else:
                    # Emitir para o método singleton/instância que a classe possui
                    # (`def self.call`, que o extrator indexa); de outra forma
                    # para o próprio nó de classe, então métodos de classe herdados/dinâmicos
                    # como ActiveRecord `where`/`find_by` ainda dá correto
                    # raio de explosão. Um receptor ambíguo não dá em nada.
                    method_nid = method_index.get((class_nid, _key(str(callee))))
                    _emit(caller, method_nid or class_nid, rc)
            continue

        # `p.run` onde o tipo de p é conhecido -> aresta do método dessa classe.
        receiver_type = rc.get("receiver_type")
        if not receiver_type:
            continue
        class_nid = _unique_class(str(receiver_type))
        if class_nid is None:
            continue
        method_nid = method_index.get((class_nid, _key(str(callee))))
        if method_nid is None:
            continue
        _emit(caller, method_nid, rc)
