"""engine — moved verbatim from omnigraph/extract.py."""
from __future__ import annotations

import hashlib
import importlib
import json
from omnigraph.extractors.base import _LANGUAGE_BUILTIN_GLOBALS, _file_stem, _make_id, _read_text
from omnigraph.ids import normalize_id
from omnigraph.extractors.models import LanguageConfig
from omnigraph.extractors.resolution import _resolve_js_import_target
from omnigraph.security import sanitize_metadata
from pathlib import Path


def _csharp_namespace_id(dotted_name: str) -> str:
    digest = hashlib.sha1(dotted_name.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    return f"csharp_namespace:{digest}"

REFERENCE_CONTEXTS = frozenset({
    "field", "parameter_type", "return_type", "generic_arg", "attribute", "value", "type",
})

def _source_location(line: int | str | None) -> str | None:
    if line is None:
        return None
    if isinstance(line, str):
        return line if line.startswith("L") else f"L{line}"
    return f"L{line}"

def _semantic_reference_edge(
    source: str,
    target: str,
    context: str,
    source_file: str,
    line: int | str | None,
) -> dict:
    if context not in REFERENCE_CONTEXTS:
        raise ValueError(f"unknown reference context: {context}")
    return {
        "source": source,
        "target": target,
        "relation": "references",
        "context": context,
        "confidence": "EXTRACTED",
        "source_file": source_file,
        "source_location": _source_location(line),
        "weight": 1.0,
    }

_PYTHON_TYPE_CONTAINERS = frozenset({
    "list", "dict", "set", "tuple", "frozenset", "type",
    "List", "Dict", "Set", "Tuple", "FrozenSet", "Type",
    "Optional", "Union", "Sequence", "Iterable", "Mapping", "MutableMapping",
    "Iterator", "Callable", "Awaitable", "AsyncIterable", "AsyncIterator", "Coroutine",
    "Generator", "AsyncGenerator", "ContextManager", "AsyncContextManager",
    "Annotated", "ClassVar", "Final", "Literal", "Concatenate", "ParamSpec", "TypeVar",
    "None", "Ellipsis",
})

_PYTHON_ANNOTATION_NOISE = frozenset({
    # scalar builtins
    "str", "int", "float", "bool", "bytes", "bytearray", "complex", "object",
    "True", "False",
    # unittest.mock
    "MagicMock", "Mock", "AsyncMock", "NonCallableMock",
    "NonCallableMagicMock", "PropertyMock", "patch", "sentinel",
})

# Builtin/stdlib decorators (@property, @dataclass, @functools.wraps, …) are
# ambient vocabulary, not corpus symbols: emitting decorator edges for them
# fabricates sourceless stub nodes on nearly every class-heavy file, and the
# unique-function rewire can collapse them onto an unrelated local definition
# (a corpus defining its own `def wraps(...)` gets a false decorator edge).
# Same name-based tradeoff as `patch`/`Mock` in _PYTHON_ANNOTATION_NOISE.
_PYTHON_DECORATOR_NOISE = frozenset({
    "property", "staticmethod", "classmethod", "abstractmethod",
    "abstractproperty", "cached_property", "wraps", "lru_cache", "cache",
    "singledispatch", "singledispatchmethod", "total_ordering",
    "contextmanager", "asynccontextmanager", "overload", "override",
    "final", "no_type_check", "runtime_checkable", "dataclass",
})

def _python_collect_type_refs(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    """Walk a Python type annotation; append (name, role) where role is 'type' or 'generic_arg'.

    Builtin/typing containers (list, dict, Optional, Union, …) are not emitted as refs themselves,
    but their nested type arguments still count as generic_arg.
    """
    if node is None:
        return
    t = node.type
    if t == "type":
        for c in node.children:
            if c.is_named:
                _python_collect_type_refs(c, source, generic, out)
        return
    if t == "identifier":
        name = _read_text(node, source)
        if name and name not in _PYTHON_TYPE_CONTAINERS and name not in _PYTHON_ANNOTATION_NOISE:
            out.append((name, "generic_arg" if generic else "type"))
        return
    if t == "attribute":
        tail = _read_text(node, source).rsplit(".", 1)[-1]
        if tail and tail not in _PYTHON_TYPE_CONTAINERS and tail not in _PYTHON_ANNOTATION_NOISE:
            out.append((tail, "generic_arg" if generic else "type"))
        return
    if t == "generic_type":
        for c in node.children:
            if c.type == "identifier":
                container = _read_text(c, source)
                if container and container not in _PYTHON_TYPE_CONTAINERS and container not in _PYTHON_ANNOTATION_NOISE:
                    out.append((container, "generic_arg" if generic else "type"))
            elif c.type == "type_parameter":
                for sub in c.children:
                    if sub.is_named:
                        _python_collect_type_refs(sub, source, True, out)
        return
    if t == "subscript":
        value = node.child_by_field_name("value")
        if value is not None:
            _python_collect_type_refs(value, source, generic, out)
        for c in node.children:
            if c is value or not c.is_named:
                continue
            _python_collect_type_refs(c, source, True, out)
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _python_collect_type_refs(c, source, generic, out)

def _csharp_pre_scan_interfaces(root_node, source: bytes) -> set[str]:
    """Return names declared as `interface` in this C# compilation unit."""
    out: set[str] = set()
    stack = [root_node]
    while stack:
        n = stack.pop()
        if n.type == "interface_declaration":
            name_node = n.child_by_field_name("name")
            if name_node is not None:
                text = _read_text(name_node, source)
                if text:
                    out.add(text)
        stack.extend(n.children)
    return out

def _csharp_classify_base(name: str, interface_names: set[str]) -> str:
    """`implements` if the base name is an interface (declared or by I-prefix convention), else `inherits`."""
    if name in interface_names:
        return "implements"
    if len(name) >= 2 and name[0] == "I" and name[1].isupper():
        return "implements"
    return "inherits"

_CSHARP_TYPE_PARAMETER_SCOPE_DECLARATIONS = frozenset({
    "class_declaration",
    "interface_declaration",
    "record_declaration",
    "struct_declaration",
    "method_declaration",
})

def _csharp_type_parameters_in_scope(node, source: bytes) -> frozenset[str]:
    """Return C# type-parameter names visible from ``node``."""
    names: set[str] = set()
    scope = node
    while scope is not None:
        if scope.type in _CSHARP_TYPE_PARAMETER_SCOPE_DECLARATIONS:
            for child in scope.children:
                if child.type != "type_parameter_list":
                    continue
                for param in child.children:
                    if param.type == "type_parameter":
                        name_node = next(
                            (sub for sub in param.children if sub.type == "identifier"),
                            None,
                        )
                        if name_node is not None:
                            name = _read_text(name_node, source)
                            if name:
                                names.add(name)
                    elif param.type == "identifier":
                        name = _read_text(param, source)
                        if name:
                            names.add(name)
        scope = scope.parent
    return frozenset(names)

def _csharp_collect_type_refs(
    node,
    source: bytes,
    generic: bool,
    out: list[tuple[str, str, bool, str]],
    skip: frozenset[str] | None = None,
) -> None:
    """Walk a C# type expression; append (name, role, qualified, qualifier) tuples."""
    if node is None:
        return
    if skip is None:
        skip = _csharp_type_parameters_in_scope(node, source)
    t = node.type
    if t == "predefined_type":
        return
    if t == "identifier":
        name = _read_text(node, source)
        if name and name not in skip:
            out.append((name, "generic_arg" if generic else "type", False, ""))
        return
    if t == "qualified_name":
        prefix, _, text = _read_text(node, source).rpartition(".")
        text = text.split("<", 1)[0]
        if text and text not in skip:
            out.append((text, "generic_arg" if generic else "type", True, prefix))
        return
    if t == "generic_name":
        name_child = node.child_by_field_name("name")
        if name_child is None:
            for sub in node.children:
                if sub.type == "identifier":
                    name_child = sub
                    break
        if name_child is not None:
            qualified = name_child.type == "qualified_name"
            prefix, _, name = _read_text(name_child, source).rpartition(".")
            if name and name not in skip:
                out.append((name, "generic_arg" if generic else "type", qualified, prefix if qualified else ""))
        for sub in node.children:
            if sub.type == "type_argument_list":
                for arg in sub.children:
                    if arg.is_named:
                        _csharp_collect_type_refs(arg, source, True, out, skip)
        return
    if t in ("nullable_type", "array_type", "pointer_type", "ref_type"):
        for c in node.children:
            if c.is_named:
                _csharp_collect_type_refs(c, source, generic, out, skip)
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _csharp_collect_type_refs(c, source, generic, out, skip)

def _csharp_attribute_names(method_node, source: bytes) -> list[tuple[str, bool, str]]:
    """Collect attribute names from a C# method/declaration's attribute_list children."""
    names: list[tuple[str, bool, str]] = []
    skip = _csharp_type_parameters_in_scope(method_node, source)
    for child in method_node.children:
        if child.type != "attribute_list":
            continue
        for attr in child.children:
            if attr.type != "attribute":
                continue
            name_node = attr.child_by_field_name("name")
            if name_node is None:
                for sub in attr.children:
                    if sub.type in ("identifier", "qualified_name"):
                        name_node = sub
                        break
            if name_node is not None:
                qualified = name_node.type == "qualified_name"
                prefix, _, text = _read_text(name_node, source).rpartition(".")
                if text and text not in skip:
                    names.append((text, qualified, prefix if qualified else ""))
    return names

_JAVA_TYPE_PARAMETER_SCOPE_DECLARATIONS = frozenset({
    "class_declaration",
    "interface_declaration",
    "record_declaration",
    "method_declaration",
    "constructor_declaration",
})

def _java_type_parameters_in_scope(node, source: bytes) -> frozenset[str]:
    """Return Java type-parameter names visible from ``node``."""
    names: set[str] = set()
    scope = node
    while scope is not None:
        if scope.type in _JAVA_TYPE_PARAMETER_SCOPE_DECLARATIONS:
            params = scope.child_by_field_name("type_parameters")
            if params is not None:
                for param in params.children:
                    if param.type != "type_parameter":
                        continue
                    name_node = next(
                        (child for child in param.children if child.type == "type_identifier"),
                        None,
                    )
                    if name_node is not None:
                        names.add(_read_text(name_node, source))
        scope = scope.parent
    return frozenset(names)

_JAVA_BUILTIN_TYPES = frozenset({
    # java.lang — core
    "Object", "String", "CharSequence", "StringBuilder", "StringBuffer",
    "Number", "Byte", "Short", "Integer", "Long", "Float", "Double",
    "Boolean", "Character", "Void", "Class", "Enum", "Record", "Math",
    "System", "Thread", "Runnable", "Comparable", "Iterable", "Cloneable",
    "AutoCloseable", "Appendable", "Readable", "Process", "ProcessBuilder",
    "Runtime", "Package", "ThreadLocal", "InheritableThreadLocal",
    # java.lang — throwables
    "Throwable", "Exception", "RuntimeException", "Error",
    "IllegalArgumentException", "IllegalStateException", "NullPointerException",
    "IndexOutOfBoundsException", "ArrayIndexOutOfBoundsException",
    "ClassCastException", "NumberFormatException", "ArithmeticException",
    "UnsupportedOperationException", "InterruptedException",
    "CloneNotSupportedException", "SecurityException", "StackOverflowError",
    "OutOfMemoryError", "AssertionError",
    # java.util — collections & core
    "Collection", "List", "ArrayList", "LinkedList", "Vector", "Stack",
    "Set", "HashSet", "LinkedHashSet", "TreeSet", "SortedSet", "NavigableSet",
    "EnumSet", "Map", "HashMap", "LinkedHashMap", "TreeMap", "SortedMap",
    "NavigableMap", "Hashtable", "EnumMap", "Properties", "Queue", "Deque",
    "ArrayDeque", "PriorityQueue", "Iterator", "ListIterator", "Comparator",
    "Optional", "OptionalInt", "OptionalLong", "OptionalDouble", "Collections",
    "Arrays", "Objects", "Date", "Calendar", "Random", "UUID", "Scanner",
    "StringJoiner", "StringTokenizer", "BitSet", "Spliterator", "Locale",
    "NoSuchElementException", "ConcurrentModificationException",
    # java.util.stream
    "Stream", "IntStream", "LongStream", "DoubleStream", "Collector",
    "Collectors",
    # java.util.function
    "Function", "BiFunction", "Consumer", "BiConsumer", "Supplier",
    "Predicate", "BiPredicate", "UnaryOperator", "BinaryOperator",
    "IntFunction", "ToIntFunction", "ToLongFunction", "ToDoubleFunction",
    # java.util.concurrent
    "Callable", "Future", "CompletableFuture", "CompletionStage", "Executor",
    "ExecutorService", "Executors", "ScheduledExecutorService", "TimeUnit",
    "ConcurrentHashMap", "ConcurrentMap", "CopyOnWriteArrayList",
    "BlockingQueue", "CountDownLatch", "Semaphore", "CyclicBarrier",
    "AtomicInteger", "AtomicLong", "AtomicBoolean", "AtomicReference",
    # java.time
    "Instant", "Duration", "Period", "LocalDate", "LocalTime", "LocalDateTime",
    "ZonedDateTime", "OffsetDateTime", "ZoneId", "ZoneOffset", "DayOfWeek",
    "Month", "Year", "Clock", "DateTimeFormatter",
    # java.io / java.nio.file
    "IOException", "UncheckedIOException", "FileNotFoundException", "File",
    "InputStream", "OutputStream", "Reader", "Writer", "BufferedReader",
    "BufferedWriter", "InputStreamReader", "OutputStreamWriter", "FileReader",
    "FileWriter", "PrintStream", "PrintWriter", "ByteArrayInputStream",
    "ByteArrayOutputStream", "Serializable", "Closeable", "Path", "Paths",
    "Files",
    # java.math
    "BigDecimal", "BigInteger",
})

def _java_collect_type_refs(
    node,
    source: bytes,
    generic: bool,
    out: list[tuple[str, str]],
    skip: frozenset[str] | None = None,
    preserve_qualified: bool = False,
) -> None:
    """Walk a Java type expression; append (name, role) tuples."""
    if node is None:
        return
    if skip is None:
        skip = _java_type_parameters_in_scope(node, source)
    t = node.type
    if t in ("integral_type", "floating_point_type", "boolean_type", "void_type"):
        return
    if t == "type_identifier":
        name = _read_text(node, source)
        if name and name not in skip and name not in _JAVA_BUILTIN_TYPES:
            out.append((name, "generic_arg" if generic else "type"))
        return
    if t == "scoped_type_identifier":
        raw = _read_text(node, source)
        simple = raw.rsplit(".", 1)[-1]
        text = raw if preserve_qualified else raw.rsplit(".", 1)[-1]
        if text and simple not in _JAVA_BUILTIN_TYPES:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t == "generic_type":
        for c in node.children:
            if c.type in ("type_identifier", "scoped_type_identifier"):
                raw = _read_text(c, source)
                simple = raw.rsplit(".", 1)[-1]
                text = (
                    raw
                    if preserve_qualified and c.type == "scoped_type_identifier"
                    else simple
                )
                if (
                    text
                    and simple not in _JAVA_BUILTIN_TYPES
                    and (c.type == "scoped_type_identifier" or simple not in skip)
                ):
                    out.append((text, "generic_arg" if generic else "type"))
                break
        for c in node.children:
            if c.type == "type_arguments":
                for arg in c.children:
                    if arg.is_named:
                        _java_collect_type_refs(
                            arg, source, True, out, skip, preserve_qualified
                        )
        return
    if t == "array_type":
        for c in node.children:
            if c.is_named:
                _java_collect_type_refs(
                    c, source, generic, out, skip, preserve_qualified
                )
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _java_collect_type_refs(
                    c, source, generic, out, skip, preserve_qualified
                )


def _java_receiver_type_name(type_node, source: bytes) -> str | None:
    """Return the concrete declared type usable for Java receiver resolution."""
    if type_node is None:
        return None
    t = type_node.type
    if t == "type_identifier":
        name = _read_text(type_node, source)
    elif t == "scoped_type_identifier":
        name = _read_text(type_node, source).rsplit(".", 1)[-1]
    elif t == "generic_type":
        base = next(
            (
                child
                for child in type_node.children
                if child.type in ("type_identifier", "scoped_type_identifier")
            ),
            None,
        )
        return _java_receiver_type_name(base, source)
    else:
        return None
    if (
        not name
        or name in _JAVA_BUILTIN_TYPES
        or name in _java_type_parameters_in_scope(type_node, source)
    ):
        return None
    return name


def _java_declarator_names(declaration_node, source: bytes) -> list[str]:
    names: list[str] = []
    for child in declaration_node.children:
        if child.type != "variable_declarator":
            continue
        name_node = child.child_by_field_name("name")
        if name_node is not None:
            name = _read_text(name_node, source)
            if name:
                names.append(name)
    return names


def _java_lambda_parameters(
    lambda_node,
    source: bytes,
) -> list[tuple[str, str | None]]:
    parameters = lambda_node.child_by_field_name("parameters")
    if parameters is None:
        return []
    if parameters.type == "identifier":
        return [(_read_text(parameters, source), None)]
    if parameters.type == "inferred_parameters":
        return [
            (_read_text(child, source), None)
            for child in parameters.children
            if child.type == "identifier"
        ]
    bindings: list[tuple[str, str | None]] = []
    for parameter in parameters.children:
        if parameter.type not in ("formal_parameter", "spread_parameter"):
            continue
        name_node = parameter.child_by_field_name("name")
        if name_node is not None:
            bindings.append((
                _read_text(name_node, source),
                _java_receiver_type_name(
                    parameter.child_by_field_name("type"), source
                ),
            ))
    return bindings


def _java_method_receiver_types(
    method_node,
    source: bytes,
    field_types: dict[str, str],
) -> dict[str, str]:
    """Build the receiver type table visible to one Java method.

    Current-class fields are the base scope, and parameters shadow them for the
    full method. Conflicting local declarations are omitted because raw call
    facts do not retain lexical scope.
    """
    method_types: dict[str, str] = {}
    ambiguous: set[str] = set()

    def bind(name: str, type_name: str | None) -> None:
        if not name or not type_name or name in ambiguous:
            return
        previous = method_types.get(name)
        if previous is not None and previous != type_name:
            method_types.pop(name, None)
            ambiguous.add(name)
        else:
            method_types[name] = type_name

    params = method_node.child_by_field_name("parameters")
    if params is not None:
        for param in params.children:
            if param.type not in ("formal_parameter", "spread_parameter"):
                continue
            type_name = _java_receiver_type_name(
                param.child_by_field_name("type"), source
            )
            name_node = param.child_by_field_name("name")
            if name_node is not None:
                bind(_read_text(name_node, source), type_name)

    body = method_node.child_by_field_name("body")
    stack = list(body.children) if body is not None else []
    while stack:
        node = stack.pop()
        if node.type in (
            "class_declaration",
            "class_body",
            "interface_declaration",
            "record_declaration",
            "enum_declaration",
            "annotation_type_declaration",
        ):
            continue
        if node.type == "lambda_expression":
            # As chamadas brutas têm escopo de método, portanto, uma ligação local lambda não pode ser
            # distinto de uma encadernação anexa com o mesmo nome.
            for name, type_name in _java_lambda_parameters(node, source):
                if type_name is None or field_types.get(name) not in (None, type_name):
                    method_types.pop(name, None)
                    ambiguous.add(name)
                else:
                    bind(name, type_name)
        if node.type == "local_variable_declaration":
            type_name = _java_receiver_type_name(
                node.child_by_field_name("type"), source
            )
            for name in _java_declarator_names(node, source):
                if field_types.get(name) not in (None, type_name):
                    method_types.pop(name, None)
                    ambiguous.add(name)
                else:
                    bind(name, type_name)
        stack.extend(node.children)

    table = dict(field_types)
    table.update(method_types)
    for name in ambiguous:
        table.pop(name, None)
    table.update({f"this.{name}": type_name for name, type_name in field_types.items()})
    return table


def _java_annotation_nodes(declaration_node) -> list:
    """Return annotations from a Java declaration's `modifiers` child."""
    modifiers = None
    for child in declaration_node.children:
        if child.type == "modifiers":
            modifiers = child
            break
    if modifiers is None:
        return []
    return [
        child
        for child in modifiers.children
        if child.type in ("marker_annotation", "annotation")
    ]


def _java_annotation_names(declaration_node, source: bytes) -> list[tuple[str, str]]:
    """Collect ``(simple, raw)`` annotation names from a Java declaration's
    `modifiers` child. ``raw`` keeps the dotted qualifier of an inline-qualified
    annotation (``@org.pkg.Foo``); it equals ``simple`` when unqualified."""
    names: list[tuple[str, str]] = []
    for anno in _java_annotation_nodes(declaration_node):
        name_node = anno.child_by_field_name("name")
        if name_node is None:
            for sub in anno.children:
                if sub.type in ("identifier", "scoped_identifier", "type_identifier"):
                    name_node = sub
                    break
        if name_node is not None:
            raw = _read_text(name_node, source)
            text = raw.rsplit(".", 1)[-1]
            if text:
                names.append((text, raw))
    return names


def _java_annotation_class_literal_refs(
    declaration_node,
    source: bytes,
) -> list[str]:
    """Collect Java type names used as class literals in annotation arguments."""
    names: list[str] = []
    for anno in _java_annotation_nodes(declaration_node):
        arguments = anno.child_by_field_name("arguments")
        if arguments is None:
            continue
        stack = [arguments]
        while stack:
            current = stack.pop()
            if current.type == "class_literal":
                type_node = next(
                    (child for child in current.children if child.is_named),
                    None,
                )
                refs: list[tuple[str, str]] = []
                _java_collect_type_refs(
                    type_node, source, False, refs, preserve_qualified=True
                )
                names.extend(name for name, _role in refs)
                continue
            stack.extend(child for child in current.children if child.is_named)
    return names


def _php_name_text(node, source: bytes) -> str | None:
    """Return the unqualified name text from a PHP `name`/`qualified_name` node."""
    if node is None:
        return None
    return _read_text(node, source).rsplit("\\", 1)[-1] or None

def _php_collect_type_refs(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    """Walk a PHP type expression; append (name, role) tuples."""
    if node is None:
        return
    t = node.type
    if t == "primitive_type":
        return
    if t == "named_type":
        for c in node.children:
            if c.type in ("name", "qualified_name"):
                text = _php_name_text(c, source)
                if text:
                    out.append((text, "generic_arg" if generic else "type"))
                return
        return
    if t in ("name", "qualified_name"):
        text = _php_name_text(node, source)
        if text:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t in ("nullable_type", "union_type", "intersection_type", "optional_type"):
        for c in node.children:
            if c.is_named:
                _php_collect_type_refs(c, source, generic, out)
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _php_collect_type_refs(c, source, generic, out)

def _php_method_return_type_node(method_node):
    """Return the named_type/primitive_type node sitting after formal_parameters."""
    saw_params = False
    for c in method_node.children:
        if c.type == "formal_parameters":
            saw_params = True
            continue
        if saw_params and c.is_named and c.type not in ("compound_statement",):
            if c.type in ("named_type", "primitive_type", "nullable_type",
                          "union_type", "intersection_type", "optional_type"):
                return c
    return None

# Tipos escalares/coleção/núcleo Kotlin stdlib que aparecem constantemente como tipo
# anotações, mas não carregam nenhum significado semântico útil como nós de grafo (espelhos
# _JAVA_BUILTIN_TYPES / _PYTHON_ANNOTATION_NOISE / _GO_PREDECLARED_TYPES).
# Kotlin compila para a JVM e também faz referência livremente aos tipos java.*, então isso
# é combinado com _JAVA_BUILTIN_TYPES no site de chamada em vez de duplicado.
_KOTLIN_BUILTIN_TYPES = frozenset({
    # kotlin — scalars & core
    "Any", "Unit", "Nothing", "Boolean", "Byte", "Short", "Int", "Long",
    "Float", "Double", "Char", "String", "CharSequence", "Number",
    "Comparable", "Enum", "Annotation", "Pair", "Triple", "Lazy",
    "Function",
    # kotlin — throwables
    "Throwable", "Exception", "RuntimeException", "Error",
    "IllegalArgumentException", "IllegalStateException", "NullPointerException",
    "IndexOutOfBoundsException", "ClassCastException", "NumberFormatException",
    "ArithmeticException", "UnsupportedOperationException",
    "NoSuchElementException", "ConcurrentModificationException",
    "StackOverflowError", "OutOfMemoryError", "AssertionError",
    "InterruptedException",
    # kotlin.collections
    "Array", "List", "MutableList", "ArrayList", "Set", "MutableSet",
    "HashSet", "LinkedHashSet", "Map", "MutableMap", "HashMap",
    "LinkedHashMap", "Collection", "MutableCollection", "Iterable",
    "MutableIterable", "Iterator", "MutableIterator", "ListIterator",
    "MutableListIterator", "Sequence", "Comparator",
    # kotlin.text
    "Regex", "MatchResult", "StringBuilder",
})

def _kotlin_user_type_name(user_type_node, source: bytes) -> str | None:
    """Return the tail identifier text from a Kotlin user_type node (without generics).

    A qualified supertype like `com.example.Base` lists its segments as flat
    `identifier` children (`com`, `example`, `Base`) separated by `.` tokens, so
    the real type is the LAST segment, not the first — returning the head yielded
    the package root (`com`). Type arguments live in a separate `type_arguments`
    child, so scanning direct children and keeping the last identifier/
    type_identifier segment ignores generics correctly (mirrors the C++ qualified
    base handling, which uses the unqualified tail)."""
    if user_type_node is None:
        return None
    name: str | None = None
    for c in user_type_node.children:
        if c.type in ("type_identifier", "identifier"):
            text = _read_text(c, source)
            if text:
                name = text
        elif c.type == "simple_user_type":
            for sub in c.children:
                if sub.type in ("identifier", "type_identifier"):
                    text = _read_text(sub, source)
                    if text:
                        name = text
                    break
    return name

def _kotlin_collect_type_refs(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    """Walk a Kotlin type expression; append (name, role) tuples."""
    if node is None:
        return
    t = node.type
    if t in ("integral_literal", "boolean_literal"):
        return
    if t == "user_type":
        for c in node.children:
            if c.type in ("identifier", "type_identifier"):
                text = _read_text(c, source)
                if text and text not in _KOTLIN_BUILTIN_TYPES and text not in _JAVA_BUILTIN_TYPES:
                    out.append((text, "generic_arg" if generic else "type"))
                break
            if c.type == "simple_user_type":
                for sub in c.children:
                    if sub.type in ("identifier", "type_identifier"):
                        text = _read_text(sub, source)
                        if text and text not in _KOTLIN_BUILTIN_TYPES and text not in _JAVA_BUILTIN_TYPES:
                            out.append((text, "generic_arg" if generic else "type"))
                        break
                break
        for c in node.children:
            if c.type == "type_arguments":
                for arg in c.children:
                    if arg.type == "type_projection":
                        for sub in arg.children:
                            if sub.is_named:
                                _kotlin_collect_type_refs(sub, source, True, out)
                    elif arg.is_named:
                        _kotlin_collect_type_refs(arg, source, True, out)
        return
    if t in ("identifier", "type_identifier"):
        text = _read_text(node, source)
        if text and text not in _KOTLIN_BUILTIN_TYPES and text not in _JAVA_BUILTIN_TYPES:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t in ("nullable_type", "parenthesized_type", "type_reference"):
        for c in node.children:
            if c.is_named:
                _kotlin_collect_type_refs(c, source, generic, out)
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _kotlin_collect_type_refs(c, source, generic, out)

def _kotlin_property_type_node(property_node):
    """Find the user_type node within a Kotlin property_declaration."""
    for c in property_node.children:
        if c.type == "variable_declaration":
            for sub in c.children:
                if sub.type in ("user_type", "nullable_type", "type_reference"):
                    return sub
        if c.type in ("user_type", "nullable_type", "type_reference"):
            return c
    return None

def _kotlin_function_return_type_node(func_node):
    """Find the return-type node of a Kotlin function_declaration (the type after `: ` post-params)."""
    saw_params = False
    saw_colon = False
    for c in func_node.children:
        if c.type == "function_value_parameters":
            saw_params = True
            continue
        if saw_params and c.type == ":":
            saw_colon = True
            continue
        if saw_colon:
            if c.is_named:
                return c
    return None

def _swift_declaration_keyword(node) -> str | None:
    """Return the leading kind token for a Swift class_declaration: class/struct/enum/extension/actor."""
    for c in node.children:
        if not c.is_named and c.type in ("class", "struct", "enum", "extension", "actor"):
            return c.type
    return None

def _python_pre_scan_underscore_collisions(root_node, source: bytes, stem: str) -> dict[str, set[str]]:
    """Pre-scan a Python module for name-only differences that collapse to one node id.

    ``ids.py:make_id`` strips leading/trailing underscores from every part before
    normalizing, so ``_get_connection`` and ``get_connection`` mint the SAME id.
    ``add_node`` then silently drops whichever declaration is walked second — no
    warning, exit 0 — so a public method can be entirely absent from the graph
    while its private-by-convention sibling (or a dunder, which strips the same
    way: ``__x``/``__x__``/``x`` all collapse too) occupies the public name (#3302).

    Returns ``{plain_nid: {raw names that would collide on it}}`` for every
    module-level function and every direct method of a module-level class — the
    two cases the id-minting code actually distinguishes via ``parent_class_nid``.
    Deliberately does not recurse into nested classes or nested functions: this
    keeps the scope key trivially exact (matching ``_make_id(stem, class_name)``,
    which only holds because Python never populates ``namespace_stack``) rather
    than replicating the corpus-wide id-computation rules for every nesting shape.
    A collision entirely inside an unhandled nested scope is simply not caught
    here — a strict miss, never a false positive, since the map is only ever
    consulted for a nid this same scan actually computed.
    """
    groups: dict[str, set[str]] = {}

    def _record(plain_nid: str, name: str) -> None:
        groups.setdefault(plain_nid, set()).add(name)

    for child in root_node.children:
        if child.type == "function_definition":
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                name = _read_text(name_node, source)
                if name:
                    _record(_make_id(stem, name), name)
        elif child.type == "class_definition":
            class_name_node = child.child_by_field_name("name")
            body = child.child_by_field_name("body")
            if class_name_node is None or body is None:
                continue
            class_name = _read_text(class_name_node, source)
            if not class_name:
                continue
            class_nid = _make_id(stem, class_name)
            for member in body.children:
                if member.type != "function_definition":
                    continue
                name_node = member.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _read_text(name_node, source)
                if name:
                    _record(_make_id(class_nid, name), name)

    return {nid: names for nid, names in groups.items() if len(names) >= 2}


def _python_underscore_salted_nid(plain_nid: str, name: str, groups: dict[str, set[str]]) -> str:
    """Resolve a Python function/method's real node id against the collision map.

    A name with no leading underscore at all is "public" per PEP 8 convention.
    When a collision group has exactly one public member, that member keeps the
    plain id — cross-file/documentation references overwhelmingly target the
    public name, and keeping it stable means an incremental rebuild that adds or
    removes a private-by-convention sibling re-points nothing. Every other
    member of the group (including the public one when it is NOT unique, e.g.
    `_x`/`__x` colliding with no public member at all) is salted, so the outcome
    never depends on declaration order — mirrors the exported/unexported rule
    the Go extractor uses for its own case-only collision (#2779).
    """
    names = groups.get(plain_nid)
    if not names or len(names) < 2:
        return plain_nid
    public = [n for n in names if not n.startswith("_")]
    if len(public) == 1 and name == public[0]:
        return plain_nid
    salt = hashlib.sha1(name.encode("utf-8"), usedforsecurity=False).hexdigest()[:6]
    return _make_id(plain_nid, salt)


def _swift_pre_scan(root_node, source: bytes) -> tuple[set[str], set[str]]:
    """Pre-scan a Swift compilation unit and return (protocol_names, class_like_names)."""
    protocols: set[str] = set()
    classes: set[str] = set()
    stack = [root_node]
    while stack:
        n = stack.pop()
        if n.type == "protocol_declaration":
            name_node = n.child_by_field_name("name")
            if name_node is None:
                for c in n.children:
                    if c.type == "type_identifier":
                        name_node = c
                        break
            if name_node is not None:
                text = _read_text(name_node, source)
                if text:
                    protocols.add(text)
        elif n.type == "class_declaration":
            kw = _swift_declaration_keyword(n)
            if kw in ("class", "struct", "enum", "actor"):
                name_node = n.child_by_field_name("name")
                if name_node is not None:
                    text = _read_text(name_node, source)
                    if text:
                        classes.add(text)
        stack.extend(n.children)
    return protocols, classes

def _swift_classify_base(name: str, kind: str | None, is_first: bool,
                          protocols: set[str], classes: set[str]) -> str:
    """Classify a Swift inheritance_specifier entry as `inherits` or `implements`."""
    if name in protocols:
        return "implements"
    if name in classes:
        return "inherits"
    # struct/enum/extension/actor não pode herdar uma classe — todas as conformidades são protocolos.
    if kind in ("struct", "enum", "extension", "actor"):
        return "implements"
    # `class`: a primeira entrada é convencionalmente a classe base; a seguir estão os protocolos.
    return "inherits" if is_first else "implements"

def _swift_user_type_name(user_type_node, source: bytes) -> str | None:
    """Return the head type_identifier text from a Swift user_type node (without generics)."""
    if user_type_node is None:
        return None
    for c in user_type_node.children:
        if c.type == "type_identifier":
            text = _read_text(c, source)
            return text or None
    return None

def _swift_collect_type_refs(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    """Walk a Swift type expression; append (name, role) tuples (role 'type' or 'generic_arg')."""
    if node is None:
        return
    t = node.type
    if t == "type_annotation":
        for c in node.children:
            if c.is_named:
                _swift_collect_type_refs(c, source, generic, out)
        return
    if t == "user_type":
        for c in node.children:
            if c.type == "type_identifier":
                text = _read_text(c, source)
                if text:
                    out.append((text, "generic_arg" if generic else "type"))
                break
        for c in node.children:
            if c.type == "type_arguments":
                for arg in c.children:
                    if arg.is_named:
                        _swift_collect_type_refs(arg, source, True, out)
        return
    if t == "type_identifier":
        text = _read_text(node, source)
        if text:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t in ("optional_type", "implicitly_unwrapped_optional_type", "array_type",
             "dictionary_type", "tuple_type"):
        for c in node.children:
            if c.is_named:
                _swift_collect_type_refs(c, source, generic, out)
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _swift_collect_type_refs(c, source, generic, out)

def _swift_property_type_node(property_node):
    """Return the type_annotation child of a Swift property_declaration, if any."""
    for c in property_node.children:
        if c.type == "type_annotation":
            return c
    return None

def _swift_attribute_type_name(property_node, source: bytes) -> str | None:
    """Return the type named by an ``@Environment(Type.self)`` attribute argument.

    Structural, whitelist-gated (#2561): only the ``Environment`` wrapper names
    the property's OWN type in its argument — ``@Query(Item.self)`` properties
    hold a *collection* of the argument type, so typing them as the element type
    fabricates member-call edges (measured false edge in the report). The
    argument must be a navigation_expression of exactly
    ``[simple_identifier (uppercase), navigation_suffix ".self"]``; the keypath
    form (``@Environment(\\.dismiss)``, key_path_expression head) and the
    module-dotted form (``@Environment(MyModule.Store.self)``, nested
    navigation_expression head) are skipped — a missed edge, never a wrong one.
    """
    for c in property_node.children:
        if c.type != "modifiers":
            continue
        for attr in c.children:
            if attr.type != "attribute":
                continue
            head = next((a for a in attr.children if a.type == "user_type"), None)
            if head is None or _read_text(head, source) != "Environment":
                continue
            arg = next((a for a in attr.children
                        if a.type == "navigation_expression"), None)
            if arg is None:
                continue
            named = [a for a in arg.children if a.is_named]
            if len(named) != 2:
                continue
            ident, suffix = named
            if ident.type != "simple_identifier" or suffix.type != "navigation_suffix":
                continue
            if _read_text(suffix, source) != ".self":
                continue
            name = _read_text(ident, source)
            if name and name[:1].isupper():
                return name
    return None

def _swift_factory_call(call_node, source: bytes) -> tuple[str, str] | None:
    """If a Swift call expression is a static factory call (``Factory.make()``),
    return ``(factory_type, method_name)``; else None (#2561).

    Only the exact depth-1 shape is accepted: a navigation_expression of
    ``[simple_identifier (uppercase), navigation_suffix]``. Deeper chains
    (``A.B.make()``, ``Singleton.shared.make()``) stay untyped — the resolver
    would have to guess the intermediate hop.
    """
    first = call_node.children[0] if call_node.children else None
    if first is None or first.type != "navigation_expression":
        return None
    named = [c for c in first.children if c.is_named]
    if len(named) != 2:
        return None
    head, suffix = named
    if head.type != "simple_identifier" or suffix.type != "navigation_suffix":
        return None
    htext = _read_text(head, source)
    if not htext or not htext[:1].isupper():
        return None
    mname = next((_read_text(sc, source) for sc in suffix.children
                  if sc.type == "simple_identifier"), None)
    if not mname:
        return None
    return htext, mname

def _swift_property_name(property_node, source: bytes) -> str | None:
    """Return the bound name of a Swift property (``let x``/``var x = ...``)."""
    for c in property_node.children:
        if c.type == "pattern":
            for sc in c.children:
                if sc.type == "simple_identifier":
                    return _read_text(sc, source)
        if c.type == "simple_identifier":
            return _read_text(c, source)
    return None

def _swift_constructor_type(call_node, source: bytes) -> str | None:
    """If a Swift call expression is a constructor (``Foo()``), return the type name.

    Only upper-cased callees are treated as types so a free-function call like
    ``configure()`` in an initializer is not mistaken for a constructor.
    """
    first = call_node.children[0] if call_node.children else None
    if first is not None and first.type == "simple_identifier":
        text = _read_text(first, source)
        if text and text[:1].isupper():
            return text
    return None

def _swift_receiver_name(recv_node, source: bytes) -> str | None:
    """Return the depth-1 receiver name of a Swift member call (``recv.method()``).

    ``vm.update()`` -> ``vm``; ``Type.staticMethod()`` -> ``Type``;
    ``Singleton.shared.method()`` -> ``Singleton`` (head of the chain);
    ``self.svc.fetch()`` -> ``svc`` (the property the call is reached through).
    Returns None for anything deeper, so resolution stays depth-1.
    """
    if recv_node is None:
        return None
    if recv_node.type == "simple_identifier":
        return _read_text(recv_node, source)
    if recv_node.type == "navigation_expression":
        head = recv_node.children[0] if recv_node.children else None
        if head is not None and head.type == "simple_identifier":
            return _read_text(head, source)
        if head is not None and head.type == "self_expression":
            for child in recv_node.children:
                if child.type == "navigation_suffix":
                    for sc in child.children:
                        if sc.type == "simple_identifier":
                            return _read_text(sc, source)
    return None

_C_PRIMITIVE_TYPE_NODES = frozenset({
    "primitive_type", "sized_type_specifier", "auto", "placeholder_type_specifier",
})

def _c_collect_type_refs(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    """Walk a C type expression; append (name, role) tuples for user-defined types.
    Skips primitive types and qualifiers; recognises type_identifier."""
    if node is None or node.type in _C_PRIMITIVE_TYPE_NODES:
        return
    t = node.type
    if t == "type_identifier":
        text = _read_text(node, source)
        if text:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t in ("pointer_declarator", "reference_declarator", "array_declarator",
             "type_qualifier", "type_descriptor", "abstract_pointer_declarator",
             "abstract_reference_declarator", "abstract_array_declarator"):
        for c in node.children:
            if c.is_named:
                _c_collect_type_refs(c, source, generic, out)

def _cpp_collect_type_refs(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    """Walk a C++ type expression; append (name, role) tuples.
    Resolves qualified_identifier tails (std::string → string) and template_type
    base + arguments (std::vector<HttpClient> → vector + HttpClient as generic_arg)."""
    if node is None or node.type in _C_PRIMITIVE_TYPE_NODES:
        return
    t = node.type
    if t == "type_identifier":
        text = _read_text(node, source)
        if text:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t == "qualified_identifier":
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            _cpp_collect_type_refs(name_node, source, generic, out)
        return
    if t == "template_type":
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            text = _read_text(name_node, source)
            if text:
                out.append((text, "generic_arg" if generic else "type"))
        args_node = node.child_by_field_name("arguments")
        if args_node is not None:
            for c in args_node.children:
                if c.is_named:
                    _cpp_collect_type_refs(c, source, True, out)
        return
    if t in ("type_descriptor", "pointer_declarator", "reference_declarator",
             "array_declarator", "type_qualifier", "abstract_pointer_declarator",
             "abstract_reference_declarator", "abstract_array_declarator"):
        for c in node.children:
            if c.is_named:
                _cpp_collect_type_refs(c, source, generic, out)

def _scala_collect_type_refs(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    """Walk a Scala type expression; append (name, role) tuples.
    Handles type_identifier, generic_type (List[T]), and common type wrappers."""
    if node is None:
        return
    t = node.type
    if t == "type_identifier":
        text = _read_text(node, source)
        if text:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t == "generic_type":
        base = node.child_by_field_name("type")
        if base is None:
            for c in node.children:
                if c.type == "type_identifier":
                    base = c
                    break
        if base is not None and base.type == "type_identifier":
            text = _read_text(base, source)
            if text:
                out.append((text, "generic_arg" if generic else "type"))
        for c in node.children:
            if c.type == "type_arguments":
                for arg in c.children:
                    if arg.is_named:
                        _scala_collect_type_refs(arg, source, True, out)
        return
    if t in ("compound_type", "infix_type", "function_type", "tuple_type",
             "annotated_type", "projected_type"):
        for c in node.children:
            if c.is_named:
                _scala_collect_type_refs(c, source, generic, out)

def _python_collect_param_refs(params_node, source: bytes) -> list[tuple[str, str]]:
    """Collect type refs from each typed parameter under a `parameters` node."""
    out: list[tuple[str, str]] = []
    if params_node is None:
        return out
    for child in params_node.children:
        if child.type in ("typed_parameter", "typed_default_parameter"):
            type_node = child.child_by_field_name("type")
            _python_collect_type_refs(type_node, source, False, out)
    return out

def _python_param_names(params_node, source: bytes) -> set[str]:
    """Plain parameter identifiers declared on a Python `parameters` node.

    Covers positional/keyword params plus `*args` / `**kwargs` and typed or
    default forms — anything that binds a local name the function body can shadow
    a module-level definition with.
    """
    out: set[str] = set()
    if params_node is None:
        return out
    for child in params_node.children:
        if child.type == "identifier":
            out.add(_read_text(child, source))
        elif child.type in (
            "typed_parameter",
            "default_parameter",
            "typed_default_parameter",
            "list_splat_pattern",
            "dictionary_splat_pattern",
        ):
            # O nome vinculado é o primeiro identificador filho (o restante é tipo/padrão).
            name_n = child.child_by_field_name("name")
            if name_n is None:
                name_n = next(
                    (c for c in child.children if c.type == "identifier"), None
                )
            if name_n is not None:
                out.add(_read_text(name_n, source))
    return out

def _python_collect_assignment_targets(node, source: bytes, out: set[str]) -> None:
    """Identifiers bound as `pattern` targets under a Python AST subtree.

    Recurses through `pattern_list` / `tuple_pattern` / `list_pattern` so tuple
    unpacking (`a, b = ...`, `for a, b in ...`) contributes every bound name.
    """
    if node is None:
        return
    if node.type == "identifier":
        out.add(_read_text(node, source))
        return
    if node.type in ("pattern_list", "tuple_pattern", "list_pattern"):
        for c in node.children:
            _python_collect_assignment_targets(c, source, out)

def _python_local_bound_names(func_def_node, source: bytes) -> set[str]:
    """Names bound LOCALLY inside a Python function: parameters plus assignment,
    `for`, `with ... as`, and comprehension targets.

    Used by the indirect-dispatch guard to reject a call-argument identifier that
    is a parameter or a local binding — it names a local value, not the module-
    level function/class that happens to share the name. Nested `function_definition`
    and `class_definition` subtrees are NOT descended into: their bindings belong
    to a different scope.
    """
    bound: set[str] = set()
    bound |= _python_param_names(func_def_node.child_by_field_name("parameters"), source)

    def walk(n) -> None:
        for child in n.children:
            t = child.type
            if t in ("function_definition", "class_definition", "lambda"):
                continue  # escopo interno — suas ligações não são os locais desta função
            if t == "assignment":
                _python_collect_assignment_targets(
                    child.child_by_field_name("left"), source, bound
                )
            elif t in ("for_statement", "for_in_clause"):
                _python_collect_assignment_targets(
                    child.child_by_field_name("left"), source, bound
                )
            elif t == "with_statement":
                for item in child.children:
                    if item.type == "with_clause":
                        for wi in item.children:
                            if wi.type == "with_item":
                                alias = wi.child_by_field_name("alias")
                                _python_collect_assignment_targets(alias, source, bound)
            elif t == "named_expression":  # walrus :=
                _python_collect_assignment_targets(
                    child.child_by_field_name("name"), source, bound
                )
            walk(child)

    body = func_def_node.child_by_field_name("body")
    if body is not None:
        walk(body)
    return bound

def _python_module_bound_names(root, source: bytes) -> set[str]:
    """Names rebound by assignment at MODULE scope (top-level `x = ...`, `for`, walrus).

    The module-scope analogue of the per-function shadow set: a dispatch-table value
    whose name is reassigned to data at module level (`handler = build()`) names that
    value, not a same-named function, so it must not manufacture an indirect edge.
    Function and class bodies are not descended into — their bindings are local.
    """
    bound: set[str] = set()

    def walk(n) -> None:
        for child in n.children:
            t = child.type
            if t in ("function_definition", "class_definition", "lambda"):
                continue  # escopo interno - não uma ligação em nível de módulo
            if t == "assignment":
                _python_collect_assignment_targets(
                    child.child_by_field_name("left"), source, bound
                )
            elif t in ("for_statement", "for_in_clause"):
                _python_collect_assignment_targets(
                    child.child_by_field_name("left"), source, bound
                )
            elif t == "named_expression":  # walrus :=
                _python_collect_assignment_targets(
                    child.child_by_field_name("name"), source, bound
                )
            walk(child)

    walk(root)
    return bound

_JS_SCOPE_BOUNDARY = frozenset({
    "function_declaration", "function_expression", "function", "arrow_function",
    "method_definition", "class_declaration", "class", "generator_function",
    "generator_function_declaration",
})

def _js_collect_pattern_idents(node, source: bytes, bound: set) -> None:
    """Collect binding identifier names from a JS/TS pattern (a parameter, or a
    declarator LHS). Recurses through destructuring (object/array patterns, rest)
    but never into the default-value side of `x = default` or a type annotation,
    so only names actually bound by the pattern are collected."""
    t = node.type
    if t in ("identifier", "shorthand_property_identifier_pattern"):
        bound.add(_read_text(node, source))
        return
    if t == "type_annotation":
        return  # `(h: Handler)` — Handler é um tipo, não um nome vinculado
    if t == "assignment_pattern":  # `x = default` — apenas x está vinculado
        left = node.child_by_field_name("left")
        if left is not None:
            _js_collect_pattern_idents(left, source, bound)
        return
    if t == "pair_pattern":  # `{ a: localName }` — localName está vinculado
        val = node.child_by_field_name("value")
        if val is not None:
            _js_collect_pattern_idents(val, source, bound)
        return
    for c in node.children:
        if c.is_named:
            _js_collect_pattern_idents(c, source, bound)

def _js_local_bound_names(func_node, source: bytes) -> set[str]:
    """Names bound locally inside a JS/TS function: parameters plus `const`/`let`/
    `var` declarator targets. Mirrors `_python_local_bound_names`: an argument that
    is a parameter or local binding names a local value, not a same-named module
    function, so it must not manufacture an indirect_call edge. Nested function and
    class scopes are not descended into."""
    bound: set[str] = set()
    params = func_node.child_by_field_name("parameters")
    if params is not None:
        _js_collect_pattern_idents(params, source, bound)
    # An arrow with ONE unparenthesised parameter exposes it as `parameter`
    # (singular) — there is no `parameters` list node — so `x => f(x)` bound
    # nothing at all and `x` read as a by-name reference to any same-named
    # callable in the corpus. Same singular/plural trap as `catch_clause`.
    solo = func_node.child_by_field_name("parameter")
    if solo is not None:
        _js_collect_pattern_idents(solo, source, bound)

    def walk(n) -> None:
        for c in n.children:
            if c.type in _JS_SCOPE_BOUNDARY:
                continue  # escopo interno — suas ligações não são os locais desta função
            if c.type == "variable_declarator":
                name = c.child_by_field_name("name")
                if name is not None:
                    _js_collect_pattern_idents(name, source, bound)
            elif c.type == "for_in_statement":
                # `for (const entry of xs)` / `for (const {k} of xs)`: the loop
                # binding is the `left` pattern, NOT wrapped in a
                # variable_declarator, so the branch above misses it and `entry`
                # read as a by-name reference to any same-named module callable
                #. C-style `for (let i = 0;...)` uses a lexical_declaration
                # with real declarators, already covered by the recursion below.
                left = c.child_by_field_name("left")
                if left is not None:
                    _js_collect_pattern_idents(left, source, bound)
            walk(c)

    body = func_node.child_by_field_name("body")
    if body is not None:
        walk(body)
    return bound

def _js_module_bound_names(root, source: bytes) -> set[str]:
    """Module-scope names rebound to NON-function data (`const X = {...}`, `let y = 5`).

    The JS/TS module-scope shadow set. Unlike the per-function set, a declarator
    whose value is itself a function (`const cb = () => {}`) is EXCLUDED: that name
    IS a callable we want dispatch tables to resolve to, not a data shadow.
    """
    bound: set[str] = set()

    def walk(n) -> None:
        for c in n.children:
            if c.type in _JS_SCOPE_BOUNDARY:
                continue
            if c.type == "variable_declarator":
                value = c.child_by_field_name("value")
                if value is None or value.type not in _JS_FUNCTION_VALUE_TYPES:
                    name = c.child_by_field_name("name")
                    if name is not None:
                        _js_collect_pattern_idents(name, source, bound)
            walk(c)

    walk(root)
    return bound

def _js_import_binds_external(raw: str, str_path: str) -> bool:
    """True when a JS/TS import specifier names a module outside the scanned corpus.

    Reuses `_resolve_js_import_target`, so this is omnigraph's own verdict rather
    than a second opinion: a specifier it cannot resolve is an external package
    (the `ref`-namespaced branch). The extra `node_modules` test covers the case
    where resolution *succeeds* but lands in a dependency tree — a `tsconfig`
    `paths` entry mapping a package to its own installed copy
    (`"lucide-react": ["./node_modules/lucide-react"]`) is common, and
    `node_modules` is pruned from every scan, so the target is never a node.
    """
    resolved = _resolve_js_import_target(raw, str_path)
    if resolved is None:
        return False  # empty specifier — binds nothing
    _target_nid, resolved_path = resolved
    if resolved_path is None:
        return True  # unresolved after relative / alias / workspace lookup
    return "node_modules" in resolved_path.parts


def _js_external_import_names(root, source: bytes, str_path: str) -> set[str]:
    """Names an `import` binds to a module OUTSIDE the corpus.

    An imported name is a module-scoped binding: within this file it denotes the
    imported symbol and nothing else. Neither shadow set collects it —
    `_js_local_bound_names` reads parameters and `variable_declarator`s and
    `_js_module_bound_names` only the latter — so the name reaches
    `_emit_indirect_ref` as an unresolved by-name reference, gets resolved against
    the corpus-wide label index, and fabricates an `indirect_call` (INFERRED, 0.8)
    to any unique same-named callable elsewhere in the corpus. That is the symptom
    already fixed for `catch` bindings, single-parameter arrows and untracked
    closures; an import binding is the same class of shadow, and a UI kit makes it
    land constantly because icon names (`Palette`, `Search`, `Filter`) collide with
    ordinary component names.

    Only imports the corpus cannot contain are collected. A relative specifier
    resolves to a real file and that edge is the graph's whole point, so those
    names stay resolvable.
    """
    bound: set[str] = set()

    def _clause_names(clause) -> None:
        for c in clause.children:
            if c.type == "identifier":            # import Default from "pkg"
                bound.add(_read_text(c, source))
            elif c.type == "namespace_import":    # import * as NS from "pkg"
                for ident in c.children:
                    if ident.type == "identifier":
                        bound.add(_read_text(ident, source))
            elif c.type == "named_imports":       # import { A, B as C } from "pkg"
                for spec in c.children:
                    if spec.type != "import_specifier":
                        continue
                    idents = [g for g in spec.children if g.type == "identifier"]
                    # `B as C` exposes both names; only the LAST one is bound here.
                    if idents:
                        bound.add(_read_text(idents[-1], source))

    def walk(n) -> None:
        for c in n.children:
            if c.type == "import_statement":
                src_node = c.child_by_field_name("source")
                if src_node is not None:
                    raw = _read_text(src_node, source).strip("\"'`")
                    if _js_import_binds_external(raw, str_path):
                        for child in c.children:
                            if child.type == "import_clause":
                                _clause_names(child)
                continue
            walk(c)

    walk(root)
    return bound


def _js_dispatch_value_idents(coll_node):
    """Yield identifier value-nodes of a JS/TS object/array literal that are
    function-reference candidates: object property VALUES and shorthand properties
    (`{ handler }`), and array elements. Keys and inline methods are not references."""
    if coll_node.type == "object":
        for c in coll_node.children:
            if c.type == "pair":
                val = c.child_by_field_name("value")
                if val is not None and val.type == "identifier":
                    yield val
            elif c.type == "shorthand_property_identifier":
                yield c
    else:  # array
        for el in coll_node.children:
            if el.type == "identifier":
                yield el

def _find_body(node, config: LanguageConfig):
    """Find the body node using config.body_field, falling back to child types."""
    b = node.child_by_field_name(config.body_field)
    if b:
        return b
    for child in node.children:
        if child.type in config.body_fallback_child_types:
            return child
    return None

def _dynamic_import_js(node, source: bytes, caller_nid: str, str_path: str, edges: list,
                       seen_dyn_pairs: set) -> bool:
    """Detect dynamic import() calls in JS/TS and emit imports_from edges.

    Handles patterns like:
      await import('./foo.js')
      import('./foo.js').then(...)
      const m = await import(`./foo`)

    Returns True if the node was a dynamic import (caller should skip normal call handling).
    """
    # A importação dinâmica é uma expressão_de_chamada cuja função filha é a palavra-chave "importar".
    # tree-sitter-typescript analisa `import('...')` como call_expression com o primeiro filho
    # sendo um token de "importação" (type="import").
    func_node = node.child_by_field_name("function")
    if func_node is None:
        # Fallback: verifique o primeiro filho diretamente (algumas versões TS)
        if node.children and _read_text(node.children[0], source) == "import":
            func_node = node.children[0]
        else:
            return False
    if _read_text(func_node, source) != "import":
        return False

    # Extraia o caminho do módulo dos argumentos
    args = node.child_by_field_name("arguments")
    if args is None:
        return True  # É um import() mas sem argumentos - pule
    for arg in args.children:
        if arg.type == "template_string":
            # Ignorar literais de modelo dinâmico — o caminho não pode ser resolvido estaticamente
            if any(c.type == "template_substitution" for c in arg.children):
                break
            raw = _read_text(arg, source).strip("`")
        elif arg.type == "string":
            raw = _read_text(arg, source).strip("'\" ")
        else:
            continue
        if not raw:
            break
        # Resolva o caminho usando a mesma lógica das importações estáticas.
        resolved = _resolve_js_import_target(raw, str_path)
        if resolved is None:
            break
        tgt_nid, resolved_path = resolved
        pair = (caller_nid, tgt_nid)
        if pair not in seen_dyn_pairs:
            seen_dyn_pairs.add(pair)
            edge = {
                "source": caller_nid,
                "target": tgt_nid,
                # Uma `import(...)` diferida é uma dependência real, então mantenha-a como uma
                # aresta `imports_from` (visível no grafo), mas marque-a como `deferred`
                # então find_import_cycles não o trata como uma importação estática e
                # relatar um ciclo de arquivo fantasma.
                "relation": "imports_from",
                "context": "import",
                "deferred": True,
                "confidence": "EXTRACTED",
                "source_file": str_path,
                "source_location": f"L{node.start_point[0] + 1}",
                "weight": 1.0,
            }
            # Key the target salt by the resolved target file so a same-basename
            # cross-extension sibling isn't mis-salted onto the importer.
            if resolved_path is not None:
                edge["target_file"] = str(resolved_path)
            edges.append(edge)
        break
    return True

def _get_cpp_func_name(node, source: bytes) -> str | None:
    """Recursively unwrap declarator to find the innermost identifier (C++)."""
    if node.type == "identifier":
        return _read_text(node, source)
    if node.type in ("field_identifier", "destructor_name", "operator_name"):
        return _read_text(node, source)
    if node.type == "qualified_identifier":
        # Uma DEFINIÇÃO fora da classe (`void Foo::bar() {}`) carrega um
        # declarador qualified_identifier. Manter o qualificador `Foo::` faz
        # _make_id(stem, "Foo::bar") normaliza para o mesmo id da classe
        # membro _make_id(class_nid, "bar"), então o decl em Foo.h e o def em
        # Foo.cpp resolve para UM nó de método em vez de dois. O completo
        # texto qualificado também lida com escopos aninhados (`A::B::bar`). Funções gratuitas
        # nunca tem um qualified_identifier aqui, então seus IDs de nome simples são
        # inalterado; apenas definições qualificadas passam para sua classe proprietária.
        return _read_text(node, source)
    decl = node.child_by_field_name("declarator")
    if decl:
        return _get_cpp_func_name(decl, source)
    for child in node.children:
        if child.type == "identifier":
            return _read_text(child, source)
    return None

def _cpp_declarator_name(node, source: bytes) -> str | None:
    """Return the bare variable name from a C++ declaration declarator, unwrapping
    pointer/reference/init wrappers (``*f``, ``&r``, ``f = Foo()``). Returns None
    for anything that isn't a plain named local (arrays, function pointers,
    structured bindings) so the type table never records a guessed receiver."""
    t = node.type
    if t == "identifier":
        return _read_text(node, source)
    if t in ("pointer_declarator", "reference_declarator", "init_declarator"):
        inner = node.child_by_field_name("declarator")
        if inner is None:
            for c in node.children:
                if c.type in ("identifier", "pointer_declarator",
                              "reference_declarator"):
                    inner = c
                    break
        if inner is not None:
            return _cpp_declarator_name(inner, source)
    return None

def _cpp_local_var_types(body_node, source: bytes, table: dict[str, str]) -> None:
    """Collect ``var -> ClassName`` from local variable declarations in a C++
    function body, for receiver-type inference in the cross-file member-call pass
    (#1547). Handles ``Foo f;``, ``Foo* f;``, ``Foo *f = ...;``, ``Foo f = Foo();``.

    Only a class-like (``type_identifier``/``qualified_identifier``) type with a
    single named declarator is recorded — PRECISION over recall: a built-in type
    (``int x``), an ambiguous multi-declarator line, or an un-nameable declarator
    contributes nothing rather than a guess. A qualified type ``ns::Foo`` records
    its simple tail ``Foo`` so it keys to the type's definition node label.
    """
    stack = [body_node]
    while stack:
        n = stack.pop()
        if n.type in ("function_definition", "lambda_expression"):
            # Não desça para uma função/lambda aninhada: seus locais têm escopo definido
            # longe e poluiria a mesa deste corpo.
            if n is not body_node:
                continue
        if n.type == "declaration":
            type_node = n.child_by_field_name("type")
            if type_node is not None and type_node.type in (
                "type_identifier", "qualified_identifier"
            ):
                type_name = _read_text(type_node, source).split("::")[-1].strip()
                declarators = [
                    c for c in n.children
                    if c.type in ("identifier", "pointer_declarator",
                                  "reference_declarator", "init_declarator")
                ]
                # Apenas um único declarador: `Foo a, b;` é ambíguo para atribuir
                # para um nome de receptor de forma limpa, então pule as linhas de vários declaradores.
                if type_name and type_name[:1].isupper() and len(declarators) == 1:
                    var = _cpp_declarator_name(declarators[0], source)
                    if var and var not in table:
                        table[var] = type_name
        for c in n.children:
            stack.append(c)

def _swift_local_var_types(body_node, source: bytes, table: dict[str, str],
                           factory: dict[str, tuple[str, str]] | None = None) -> None:
    """Collect ``var -> Type`` from local ``let``/``var`` bindings in a Swift
    function body, so a member call on the local (``x.method()``) resolves to Type
    in the cross-file member-call pass (#1604).

    Two initializer shapes are recorded, PRECISION over recall:
      - a constructor call ``let x = Type()`` (``_swift_constructor_type``);
      - a static-member access ``let x = Type.shared`` (a navigation_expression
        with an upper-cased head) — the singleton-cached-into-a-local idiom, one
        of the most common Swift call patterns and previously resolved to nothing.
    A factory call (``let x = Factory.make()``) has no in-file type; when
    ``factory`` is given, the pending ``name -> (Factory, method)`` binding is
    stashed there (label-only) for corpus-side resolution against the factory
    method's plain return type (#2561).
    Nested function declarations are not descended into (their locals are scoped
    away); the first binding for a name wins, so a class property of the same name
    already in the table is not overwritten.
    """
    stack = [body_node]
    while stack:
        n = stack.pop()
        if n.type == "function_declaration" and n is not body_node:
            continue
        if n.type == "property_declaration":
            prop_type: str | None = None
            factory_bind: tuple[str, str] | None = None
            for child in n.children:
                if child.type == "call_expression":
                    prop_type = _swift_constructor_type(child, source)
                    if prop_type is None:
                        factory_bind = _swift_factory_call(child, source)
                    break
                if child.type == "navigation_expression":
                    head = child.children[0] if child.children else None
                    if head is not None and head.type == "simple_identifier":
                        htext = _read_text(head, source)
                        if htext and htext[:1].isupper():
                            prop_type = htext
                    break
            name = _swift_property_name(n, source)
            if name and prop_type and name not in table:
                table[name] = prop_type
            elif (name and factory_bind is not None and factory is not None
                  and name not in table and name not in factory):
                factory[name] = factory_bind
        for c in n.children:
            stack.append(c)

def _csharp_receiver_type_name(type_node, source: bytes) -> str | None:
    """Resolve a C# declared type to a receiver-typable class name, or None.

    A genuine C# class name is Pascal-cased; predefined primitives
    (int/bool/string) and ``dynamic`` never own a resolvable method definition
    here, and ``var`` (``implicit_type``) carries no name at all.
    """
    info = _read_csharp_type_name(type_node, source)
    if not info:
        return None
    name = info[0]
    return name if name and name[:1].isupper() else None


def _csharp_method_receiver_types(
    method_node,
    source: bytes,
    field_types: dict[str, str],
) -> tuple[dict[str, list[tuple[int, int, str | None]]], dict[str, str]]:
    """Build the SCOPED receiver bindings visible to one C# method (#2299, #2472).

    The C# twin of ``_java_method_receiver_types``, but positional: instead of
    a flat name -> type map, the first element maps each name to a list of
    ``(scope_start_byte, scope_end_byte, type_name)`` bindings and the second
    is the class field/property base scope; ``_csharp_scoped_receiver_type``
    resolves a call site against them by byte offset. C# scoping is per-method,
    so a name rebound in a DIFFERENT method never affects this one (#2299) —
    and, since #2472, an untypable binding (``out var x``) in one lexical scope
    no longer wipes a same-named typed binding in a sibling or nested scope
    (a ``static`` local-function parameter, a declaration in the other branch
    of an ``if``), the regression the #2346 declaration-expression harvest
    exposed under the old method-wide poison rule.

    A receiver_type is stamped iff exactly one binding is lexically visible at
    the call site (innermost scope wins) and it is typed; an untypable or tied
    binding at the call site yields no edge (never a guess). Scope ranges are
    deliberately conservative — a pattern binding (``is T x``, ``case T x:``)
    spans its whole enclosing block, which is over-wide, but over-wide only
    ever produces ties (drop), never a wrong bind. The class-field conflict
    rule is unchanged: a local binding disagreeing with a same-named
    field/property's type drops the name entirely. Residual limitation:
    ``out var x`` itself stays untyped — resolving it from the callee's
    ``out`` parameter signature is a separate, pre-existing gap.
    """
    bindings: dict[str, list[tuple[int, int, str | None]]] = {}
    field_poisoned: set[str] = set()

    def bind(name: str | None, type_name: str | None, scope_node) -> None:
        if not name or scope_node is None:
            return
        if field_types.get(name) not in (None, type_name):
            field_poisoned.add(name)
        bindings.setdefault(name, []).append(
            (scope_node.start_byte, scope_node.end_byte, type_name)
        )

    def bind_parameter(param, scope_node) -> None:
        name_node = param.child_by_field_name("name")
        if name_node is not None:
            bind(
                _read_text(name_node, source),
                _csharp_receiver_type_name(param.child_by_field_name("type"), source),
                scope_node,
            )

    body = method_node.child_by_field_name("body")
    # Parameters scope to the BODY range: a parameter and an (illegal)
    # same-named top-level local share one C# declaration space, and equal
    # ranges tie at the call site — drop, never a guess.
    param_scope = body if body is not None else method_node
    params = method_node.child_by_field_name("parameters")
    if params is not None:
        for param in params.children:
            if param.type == "parameter":
                bind_parameter(param, param_scope)

    stack = (
        [(child, param_scope) for child in body.children]
        if body is not None
        else []
    )
    while stack:
        node, scope = stack.pop()
        if node.type in (
            "class_declaration",
            "struct_declaration",
            "interface_declaration",
            "record_declaration",
            "enum_declaration",
        ):
            continue
        if node.type == "lambda_expression":
            # A lambda parameter is visible exactly inside the lambda: a typed
            # one binds its type there, an untyped one (`x => ...`,
            # `(z) => ...`) binds None so calls on it inside the lambda stay
            # unstamped — without wiping a same-named outer binding.
            lam_params = node.child_by_field_name("parameters")
            if lam_params is not None:
                if lam_params.type == "implicit_parameter":
                    bind(_read_text(lam_params, source), None, node)
                else:
                    for param in lam_params.children:
                        if param.type == "parameter":
                            bind_parameter(param, node)
                        elif param.type == "implicit_parameter":
                            bind(_read_text(param, source), None, node)
        elif node.type == "local_function_statement":
            lf_params = node.child_by_field_name("parameters")
            if lf_params is not None:
                for param in lf_params.children:
                    if param.type == "parameter":
                        bind_parameter(param, node)
        elif node.type == "local_declaration_statement":
            vd = next(
                (c for c in node.children if c.type == "variable_declaration"), None
            )
            if vd is not None:
                declared = _csharp_receiver_type_name(
                    vd.child_by_field_name("type"), source
                )
                for declarator in vd.children:
                    if declarator.type != "variable_declarator":
                        continue
                    name_node = declarator.child_by_field_name("name") or next(
                        (g for g in declarator.children if g.type == "identifier"),
                        None,
                    )
                    if name_node is None:
                        continue
                    type_name = declared
                    if type_name is None:
                        # `var v = new T()` — recover T from the object-creation.
                        for g in declarator.children:
                            if g.type == "object_creation_expression":
                                type_name = _csharp_receiver_type_name(
                                    g.child_by_field_name("type"), source
                                )
                                break
                    bind(_read_text(name_node, source), type_name, scope)
        elif node.type in ("declaration_expression", "declaration_pattern"):
            # inline-declared receivers. `out Sect s` is a
            # declaration_expression; `is Leaf lf`, `is not Node nd`,
            # `case Twig tw:` and a switch-arm `Stem st =>` are
            # declaration_patterns — all carry `type` + `name` fields and
            # bind the name for the enclosing block. `out var v`
            # (implicit_type) yields None from _csharp_receiver_type_name
            # and stays untypable inside that block only — no guess at its
            # own call sites, no method-wide wipe of other scopes.
            name_node = node.child_by_field_name("name")
            if name_node is not None and name_node.type == "identifier":
                bind(
                    _read_text(name_node, source),
                    _csharp_receiver_type_name(
                        node.child_by_field_name("type"), source
                    ),
                    scope,
                )
        child_scope = (
            node
            if node.type in (
                "block", "lambda_expression", "local_function_statement"
            )
            else scope
        )
        stack.extend((child, child_scope) for child in node.children)

    base = {
        name: type_name
        for name, type_name in field_types.items()
        if name not in field_poisoned
    }
    for name in field_poisoned:
        bindings.pop(name, None)
    return bindings, base


def _csharp_scoped_receiver_type(
    table: tuple[dict[str, list[tuple[int, int, str | None]]], dict[str, str]] | None,
    name: str | None,
    call_byte: int,
) -> str | None:
    """Resolve a C# receiver name to its type at a specific call offset (#2472).

    ``table`` is the (scoped bindings, field base) pair built by
    ``_csharp_method_receiver_types``. Bindings whose scope contains the call
    offset are candidates and the innermost (smallest-range) one wins; no
    candidate at all falls back to the class field/property base scope. A tie
    at the innermost range (an illegal same-declaration-space clash, e.g. a
    parameter redeclared as a top-level local, or two sibling pattern bindings
    of the same name) or an untypable winner yields None — no edge, never a
    guess.
    """
    if not table or not name:
        return None
    bindings, base = table
    candidates = [
        b for b in bindings.get(name, ())
        if b[0] <= call_byte < b[1]
    ]
    if not candidates:
        return base.get(name)
    innermost = min(end - start for start, end, _ in candidates)
    inner = [b for b in candidates if b[1] - b[0] == innermost]
    if len(inner) == 1:
        return inner[0][2]
    return None

def _ts_receiver_type_table(root, source: bytes, table: dict[str, str]) -> None:
    """Add TS/JS receiver bindings to ``table`` (name -> TypeName), for member-call
    resolution beyond the constructor-injected `this.field` case (#1630):

      * local ``const/let/var x = new Foo()`` -> ``x: Foo`` (Pattern A);
      * a type-annotated parameter ``(svc: Svc)`` -> ``svc: Svc`` (Pattern B), so a
        call on the param — including inside a returned closure — resolves.

    File-scoped, first-binding-wins (merged into the constructor-injection table,
    which is populated first and therefore wins on a name clash). Only a bare
    ``type_identifier`` (a single class/interface name) is recorded — an array,
    union, generic, qualified, or predefined type is skipped (precision over
    recall, matching the receiver-typed resolvers for Swift/C#/C++)."""
    def _bare_type_ident(type_annotation):
        # anotação_tipo -> ":T"; aceite apenas um único filho type_identifier.
        idents = [c for c in type_annotation.children if c.type == "type_identifier"]
        others = [c for c in type_annotation.children
                  if c.is_named and c.type not in ("type_identifier",)]
        if len(idents) == 1 and not others:
            return _read_text(idents[0], source)
        return None

    stack = [root]
    while stack:
        n = stack.pop()
        t = n.type
        if t == "variable_declarator":
            name_n = n.child_by_field_name("name")
            value = n.child_by_field_name("value")
            if (name_n is not None and name_n.type == "identifier"
                    and value is not None and value.type == "new_expression"):
                ctor = value.child_by_field_name("constructor")
                if ctor is not None and ctor.type in ("identifier", "type_identifier"):
                    name = _read_text(name_n, source)
                    tname = _read_text(ctor, source)
                    if name and tname and name not in table:
                        table[name] = tname
        elif t == "required_parameter" or t == "optional_parameter":
            pat = n.child_by_field_name("pattern")
            ann = n.child_by_field_name("type")
            if pat is not None and pat.type == "identifier" and ann is not None:
                tname = _bare_type_ident(ann)
                name = _read_text(pat, source)
                if name and tname and name not in table:
                    table[name] = tname
        for c in n.children:
            stack.append(c)

def _find_require_call(value_node):
    """Return the call_expression node if `value_node` is a `require(...)` call
    or `require(...).x` member access. Otherwise None."""
    if value_node is None:
        return None
    if value_node.type == "call_expression":
        fn = value_node.child_by_field_name("function")
        if fn is not None and fn.type == "identifier":
            return value_node
    if value_node.type == "member_expression":
        obj = value_node.child_by_field_name("object")
        return _find_require_call(obj)
    return None

def _require_imports_js(node, source: bytes, importer_nid: str, stem: str, edges: list, str_path: str) -> bool:
    """Detect CommonJS require imports inside lexical_declaration / variable_declaration.

    Handles three patterns:
      const { foo, bar } = require('./mod')   → file → mod (imports_from), file → foo, file → bar
      const mod         = require('./mod')   → file → mod (imports_from)
      const x           = require('./mod').y → file → mod (imports_from), file → y

    Returns True if any require import was found.
    """
    if node.type not in ("lexical_declaration", "variable_declaration"):
        return False
    found = False
    for child in node.children:
        if child.type != "variable_declarator":
            continue
        value = child.child_by_field_name("value")
        call = _find_require_call(value)
        if call is None:
            continue
        fn = call.child_by_field_name("function")
        if fn is None or _read_text(fn, source) != "require":
            continue
        args = call.child_by_field_name("arguments")
        if args is None:
            continue
        raw = None
        for arg in args.children:
            if arg.type == "string":
                raw = _read_text(arg, source).strip("'\"` ")
                break
        if not raw:
            continue
        resolved = _resolve_js_import_target(raw, str_path)
        if resolved is None:
            continue
        tgt_nid, resolved_path = resolved
        line = node.start_point[0] + 1
        edge = {
            "source": importer_nid,
            "target": tgt_nid,
            "relation": "imports_from",
            "context": "import",
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        }
        # Key the target salt by the resolved target file so a same-basename
        # cross-extension sibling isn't mis-salted onto the importer.
        if resolved_path is not None:
            edge["target_file"] = str(resolved_path)
        edges.append(edge)
        found = True

        # Arestas em nível de símbolo para fichários desestruturados/acessadores.
        target_stem = _file_stem(resolved_path) if resolved_path is not None else None
        name_node = child.child_by_field_name("name")
        sym_names: list[str] = []
        if name_node is not None and name_node.type == "object_pattern":
            # `const { a, b: alias } = require('./m')` — emite arestas para cada chave de propriedade
            for prop in name_node.children:
                if prop.type == "shorthand_property_identifier_pattern":
                    sym_names.append(_read_text(prop, source))
                elif prop.type == "pair_pattern":
                    key = prop.child_by_field_name("key")
                    if key is not None:
                        sym_names.append(_read_text(key, source))
        elif value is not None and value.type == "member_expression":
            # `const x = require('./m').y` — símbolo é a propriedade acessada
            prop = value.child_by_field_name("property")
            if prop is not None:
                sym_names.append(_read_text(prop, source))
        if target_stem is not None:
            for sym in sym_names:
                edges.append({
                    "source": importer_nid,
                    "target": _make_id(target_stem, sym),
                    "relation": "imports",
                    "context": "import",
                    "confidence": "EXTRACTED",
                    "source_file": str_path,
                    "source_location": f"L{line}",
                    "weight": 1.0,
                })
    return found

_JS_FUNCTION_VALUE_TYPES = frozenset({"arrow_function", "function_expression", "function", "generator_function"})


def _scan_js_nested_function_declarations(
    container_node, parent_nid: str, *, source: bytes, config,
    add_node, add_edge, callable_def_nids: set | None,
    local_bound_names: dict | None, function_bodies: list,
) -> None:
    """Emit a node + `contains` edge for every named `function`/generator
    declaration lexically nested inside *container_node*, scoped under
    *parent_nid*, and track its body so calls made from inside it resolve
    instead of dangling (#2653).

    Recurses through non-function children AND through the bodies of nested
    arrow / function expressions, so a `function` declared inside an arrow
    callback (`useEffect(() => { function h(){} })`) or inside an arrow-defined
    component (`const Panel = () => { function handleClick(){} }`, the React
    idiom that motivated #2653) is captured too. Anonymous closures themselves
    are not noded — they are attributed to the nearest enclosing named scope,
    which is *parent_nid*.
    """
    if container_node is None:
        return
    for child in container_node.children:
        if child.type in ("function_declaration", "generator_function_declaration"):
            name_node = child.child_by_field_name(config.name_field)
            if name_node is None:
                for c in child.children:
                    if c.type in config.name_fallback_child_types:
                        name_node = c
                        break
            func_name = _read_text(name_node, source) if name_node else None
            # A name that normalizes to nothing (e.g. minified `$`) would collapse
            # the nested id onto parent_nid and leak the scan path; skip it.
            if func_name and normalize_id(func_name):
                line = child.start_point[0] + 1
                nested_nid = _make_id(parent_nid, func_name)
                add_node(nested_nid, f"{func_name}()", line)
                add_edge(parent_nid, nested_nid, "contains", line)
                if callable_def_nids is not None:
                    callable_def_nids.add(nested_nid)
                if local_bound_names is not None:
                    local_bound_names[nested_nid] = _js_local_bound_names(child, source)
                nested_body = _find_body(child, config)
                if nested_body:
                    function_bodies.append((nested_nid, nested_body))
                    _scan_js_nested_function_declarations(
                        nested_body, nested_nid, source=source, config=config,
                        add_node=add_node, add_edge=add_edge,
                        callable_def_nids=callable_def_nids,
                        local_bound_names=local_bound_names,
                        function_bodies=function_bodies,
                    )
        elif child.type in _JS_FUNCTION_VALUE_TYPES:
            # An anonymous arrow/function expression is not itself a node, but a
            # `function` declared inside its body still belongs to the enclosing
            # named scope — descend into the body keeping the SAME parent_nid.
            _scan_js_nested_function_declarations(
                _find_body(child, config), parent_nid, source=source, config=config,
                add_node=add_node, add_edge=add_edge,
                callable_def_nids=callable_def_nids,
                local_bound_names=local_bound_names,
                function_bodies=function_bodies,
            )
        else:
            _scan_js_nested_function_declarations(
                child, parent_nid, source=source, config=config,
                add_node=add_node, add_edge=add_edge,
                callable_def_nids=callable_def_nids,
                local_bound_names=local_bound_names,
                function_bodies=function_bodies,
            )


def _js_topmost_closures(node, out: list) -> None:
    """Collect the TOPMOST closure nodes (arrow / function expressions) under
    ``node``, without descending into a found closure — its nested closures
    belong to it and are reached by the walk_calls closure descend (#1630)."""
    for c in node.children:
        if c.type in _JS_FUNCTION_VALUE_TYPES:
            out.append(c)
        else:
            _js_topmost_closures(c, out)

def _js_member_assignment_target(left, source: bytes):
    """Classify the symbol an `assignment_expression` LHS defines when its RHS
    is a function. Returns (kind, owner_name, member_name) or None.

      this.foo = fn            → ("this",      None,  "foo")
      exports.foo = fn         → ("exports",   None,  "foo")
      module.exports.foo = fn  → ("exports",   None,  "foo")
      Foo.prototype.bar = fn   → ("prototype", "Foo", "bar")

    An arbitrary identifier receiver is returned as ``("object", name, member)``.
    It is only materialized after the caller proves that the identifier is a
    direct object-literal binding in the enclosing function. Keeping that scope
    check at the caller avoids the bare-named / phantom-god-node failure mode
    that the module-level guard (#1077) prevents.
    """
    if left is None or left.type != "member_expression":
        return None
    prop = left.child_by_field_name("property")
    if prop is None:
        return None
    member_name = _read_text(prop, source)
    if not member_name:
        return None
    obj = left.child_by_field_name("object")
    if obj is None:
        return None
    if obj.type == "this":
        return ("this", None, member_name)
    if obj.type == "identifier":
        if _read_text(obj, source) == "exports":
            return ("exports", None, member_name)
        return ("object", _read_text(obj, source), member_name)
    if obj.type == "member_expression":
        # module.exports.X ou Foo.prototype.X
        inner_obj = obj.child_by_field_name("object")
        inner_prop = obj.child_by_field_name("property")
        if inner_obj is None or inner_prop is None:
            return None
        inner_prop_name = _read_text(inner_prop, source)
        if inner_obj.type == "identifier":
            inner_obj_name = _read_text(inner_obj, source)
            if inner_obj_name == "module" and inner_prop_name == "exports":
                return ("exports", None, member_name)
            if inner_prop_name == "prototype":
                return ("prototype", inner_obj_name, member_name)
    return None

def _js_extra_walk(node, source: bytes, file_nid: str, stem: str, str_path: str,
                   nodes: list, edges: list, seen_ids: set, function_bodies: list,
                   parent_class_nid: str | None, add_node_fn, add_edge_fn,
                   callable_def_nids: set | None = None,
                   local_bound_names: dict | None = None,
                   closure_locals_by_body: dict | None = None,
                   config=None) -> bool:
    """Handle lexical_declaration (arrow functions, CJS requires, module-level const literals) for JS/TS. Returns True if handled."""
    # Atribuições de membro CommonJS/protótipo cujo valor é uma função:
    #   exports.X = () => {}     → file-contained function  X()
    #   module.exports.X = fn    → file-contained function  X()
    #   Foo.prototype.bar = fn → método bar() de propriedade de Foo
    # (`this.X = fn` reside dentro de um corpo de função, que não é recorrente aqui;
    #  ele é capturado na função envolvente — consulte o ramo da função.)
    if node.type == "expression_statement":
        assign = next((c for c in node.children
                       if c.type == "assignment_expression"), None)
        if assign is not None:
            value = assign.child_by_field_name("right")
            if value is not None:
                target = _js_member_assignment_target(
                    assign.child_by_field_name("left"), source)
                if target is not None:
                    kind, owner_name, member_name = target
                    line = node.start_point[0] + 1
                    if value.type in _JS_FUNCTION_VALUE_TYPES:
                        handled = False
                        if kind == "exports":
                            nid = _make_id(stem, member_name)
                            add_node_fn(nid, f"{member_name}()", line)
                            add_edge_fn(file_nid, nid, "contains", line)
                            handled = True
                        elif kind == "prototype":
                            owner_nid = _make_id(stem, owner_name)
                            nid = _make_id(owner_nid, member_name)
                            add_node_fn(nid, f".{member_name}()", line)
                            add_edge_fn(owner_nid, nid, "method", line)
                            handled = True
                        if handled:
                            if callable_def_nids is not None:
                                callable_def_nids.add(nid)  # CJS/protótipo fn pode ser chamado
                            if local_bound_names is not None:
                                local_bound_names[nid] = _js_local_bound_names(value, source)
                            body = value.child_by_field_name("body")
                            if body:
                                function_bodies.append((nid, body))
                            return True
                    elif kind == "exports":
                        # `exports.handler = wrapper(async (req) => …)` or `module.exports.handler = wrapper(…)`
                        inner = value
                        while inner is not None and inner.type in (
                                "as_expression", "satisfies_expression"):
                            inner = (inner.named_children[0]
                                     if inner.named_children else None)
                        if inner is not None and inner.type in (
                                "call_expression", "new_expression"):
                            closures: list = []
                            _js_topmost_closures(inner, closures)
                            if closures:
                                nid = _make_id(stem, member_name)
                                add_node_fn(nid, f"{member_name}()", line)
                                add_edge_fn(file_nid, nid, "contains", line)
                                if callable_def_nids is not None:
                                    callable_def_nids.add(nid)  # exported HOF is callable
                                for closure in closures:
                                    body = closure.child_by_field_name("body")
                                    if body:
                                        if closure_locals_by_body is not None:
                                            closure_locals_by_body[id(body)] = (
                                                _js_local_bound_names(closure, source))
                                        function_bodies.append((nid, body))
                                return True

    # Campos de classe cujo valor é uma função:
    #   classe C { handler = () => {} } → método handler() de propriedade de C
    # Chega aqui com parent_class_nid definido porque os corpos das classes são recursivos
    # com a classe nid como pai.
    if parent_class_nid and node.type in ("field_definition", "public_field_definition"):
        prop = node.child_by_field_name("property") or node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if (prop is not None and value is not None
                and value.type in _JS_FUNCTION_VALUE_TYPES):
            field_name = _read_text(prop, source)
            if field_name:
                line = node.start_point[0] + 1
                nid = _make_id(parent_class_nid, field_name)
                add_node_fn(nid, f".{field_name}()", line)
                add_edge_fn(parent_class_nid, nid, "method", line)
                if callable_def_nids is not None:
                    callable_def_nids.add(nid)  # arrow class-field pode ser chamado
                if local_bound_names is not None:
                    local_bound_names[nid] = _js_local_bound_names(value, source)
                body = value.child_by_field_name("body")
                if body:
                    function_bodies.append((nid, body))
                return True

    if node.type in ("lexical_declaration", "variable_declaration"):
        # CJS exige importações - emite arestas, não bloqueia outra manipulação de lexical_declaration
        require_found = _require_imports_js(node, source, file_nid, stem, edges, str_path)

        # Guarda de escopo: emite apenas nós para declarações em nível de módulo.
        # Sem isso, `const x = ...` dentro de um retorno de chamada de seta (por exemplo, dentro
        # `describe(() => { const set = new Set(...) })`) emite um nome simples
        # nó, e o mesmo nome colide com arquivos não relacionados, produzindo
        # nós de deuses fantasmas. Os corpos das funções das setas são percorridos separadamente
        # via function_bodies, então nunca precisaremos emitir nós para locais aqui.
        parent = node.parent
        is_exported = parent is not None and parent.type == "export_statement"
        is_module_level = parent is not None and (
            parent.type == "program"
            or (is_exported
                and parent.parent is not None
                and parent.parent.type == "program")
        )

        # Declarações de função de seta e literais const em nível de módulo (somente lexical_declaration)
        arrow_found = False
        const_found = False
        if node.type == "lexical_declaration" and is_module_level:
            for child in node.children:
                if child.type == "variable_declarator":
                    value = child.child_by_field_name("value")
                    name_node = child.child_by_field_name("name")
                    is_exported_scalar_binding = (
                        is_exported
                        and name_node is not None
                        and name_node.type == "identifier"
                        and bool(normalize_id(_read_text(name_node, source)))
                    )
                    if value and value.type in _JS_FUNCTION_VALUE_TYPES:
                        # `const f = () => {}` e `const f = function(){}`
                        if name_node:
                            func_name = _read_text(name_node, source)
                            line = child.start_point[0] + 1
                            # Um nome que normaliza para nada (por exemplo, `$` minificado)
                            # recolheria o id para o radical do arquivo absoluto e
                            # vazar o caminho da varredura; pule (sem sinal grafo).
                            if not normalize_id(func_name):
                                continue
                            func_nid = _make_id(stem, func_name)
                            add_node_fn(func_nid, f"{func_name}()", line)
                            add_edge_fn(file_nid, func_nid, "contains", line)
                            if callable_def_nids is not None:
                                callable_def_nids.add(func_nid)  # `const f = () =>` pode ser chamado
                            if local_bound_names is not None:
                                local_bound_names[func_nid] = _js_local_bound_names(value, source)
                            body = value.child_by_field_name("body")
                            if body:
                                function_bodies.append((func_nid, body))
                                # a `function` declared inside an arrow-defined
                                # component (`const Panel = () => { function h(){} }`)
                                # is otherwise never seen — the main walk does not
                                # recurse into arrow bodies. Scan it here so the
                                # nested declaration is noded and its calls resolve.
                                _scan_js_nested_function_declarations(
                                    body, func_nid, source=source, config=config,
                                    add_node=add_node_fn, add_edge=add_edge_fn,
                                    callable_def_nids=callable_def_nids,
                                    local_bound_names=local_bound_names,
                                    function_bodies=function_bodies,
                                )
                            arrow_found = True
                    elif value and (
                        is_exported_scalar_binding
                        or value.type in (
                            "object", "array", "as_expression",
                            "satisfies_expression", "call_expression",
                            "new_expression",
                        )
                    ):
                        # Simple exported identifiers are part of the module API
                        # regardless of initializer shape. Keep other scalar noise suppressed.
                        if name_node:
                            const_name = _read_text(name_node, source)
                            line = child.start_point[0] + 1
                            const_nid = _make_id(stem, const_name)
                            add_node_fn(const_nid, const_name, line)
                            add_edge_fn(file_nid, const_nid, "contains", line)
                            const_found = True
                            # `const handler = wrapper(async (req) => …)`
                            # created the const node above but, unlike the arrow
                            # branch, never tracked the callback's body — so
                            # walk_calls never descended into it and its calls
                            # were dropped. Track each TOPMOST closure in the
                            # initializer under the const's nid; nested closures
                            # are reached by the closure descend with the
                            # same caller, so appending them too would
                            # double-walk. `_tracked_body_ids` picks these up,
                            # so the descend skips them (no double-count).
                            inner = value
                            while inner is not None and inner.type in (
                                    "as_expression", "satisfies_expression"):
                                inner = (inner.named_children[0]
                                         if inner.named_children else None)
                            if inner is not None and inner.type in (
                                    "call_expression", "new_expression"):
                                closures: list = []
                                _js_topmost_closures(inner, closures)
                                for closure in closures:
                                    # keep each sibling closure's
                                    # params/locals scoped to its OWN body
                                    # (keyed by id(body), fed to walk_calls as
                                    # extra_locals) instead of unioning them
                                    # under const_nid — the union let closure
                                    # A's param suppress a real indirect_call
                                    # to the same name in sibling closure B.
                                    body = closure.child_by_field_name("body")
                                    if body:
                                        if closure_locals_by_body is not None:
                                            closure_locals_by_body[id(body)] = (
                                                _js_local_bound_names(closure, source))
                                        function_bodies.append((const_nid, body))
        if arrow_found:
            return True
        if const_found:
            return True
        if require_found:
            return True
    return False

def _ts_extra_walk(node, source: bytes, file_nid: str, stem: str, str_path: str,
                   nodes: list, edges: list, seen_ids: set, function_bodies: list,
                   parent_class_nid: str | None, add_node_fn, add_edge_fn,
                   walk_fn) -> bool:
    """Emit enum member nodes, and a container node for a TS `namespace`/`module`.

    `namespace Foo {}` parses as `internal_module` (with `name`/`body` fields);
    `module Bar {}` and ambient `declare module "pkg" {}` parse as a named
    `module` node that exposes no fields, so its name and body are found
    positionally. Without this the container was never a node — its members were
    still reached by the default recurse but lost their namespace context. The
    members stay file-contained (parity with C#'s `_csharp_extra_walk`); the
    namespace becomes a sibling marker node so it is queryable. Returns True if
    handled.

    The guard requires `is_named` because the anonymous `module` keyword token
    shares the `module` type string and would otherwise match here.
    """
    if (parent_class_nid
            and node.parent is not None
            and node.parent.type == "enum_body"
            and node.type in ("property_identifier", "enum_assignment")):
        # `enum_declaration` is in TS's class_types "parity with Java/C#", so the
        # enum type is a node while its members were not, leaving the type a leaf.
        # Java emits a node per `enum_constant` with a `case_of` edge,
        # Kotlin per `enum_entry`, Swift the same; this is that shape.
        #
        # Two member spellings: a bare `Red` is a `property_identifier`, while
        # `Green = 5` is an `enum_assignment` whose `name` is either a
        # `property_identifier` or, for a quoted member, a `string`. The parent
        # check is what keeps this off the `property_identifier` nodes that
        # appear all over a TS file.
        name_node = node if node.type == "property_identifier" else node.child_by_field_name("name")
        member_name = ""
        if name_node is not None:
            member_name = _read_text(name_node, source)
            if name_node.type == "string":
                # `"Odd Name" = 7`: the label is the member name, not the quoted
                # literal. Unquote the whole text the way the namespace handler
                # below does rather than reading a `string_fragment`, because an
                # escape splits the string into several fragments and the first
                # one alone truncates the name (`"A\tB"` would become `A`).
                member_name = member_name.strip("'\"`")
        if member_name:
            line = node.start_point[0] + 1
            member_nid = _make_id(parent_class_nid, member_name)
            # TS is case-sensitive while the id recipe casefolds, so `enum E {
            # Value, value }` puts two legal members on one id. The first
            # declaration keeps the node rather than a second edge on it.
            if member_nid not in seen_ids:
                add_node_fn(member_nid, member_name, line)
                add_edge_fn(parent_class_nid, member_nid, "case_of", line)
        if node.type == "enum_assignment":
            # Claiming the member must not swallow its initializer. An enum value
            # can hold a whole expression, and `A = class Inner { m() {} }.name`
            # loses Inner's method node if the walk stops here. Descend into the
            # `value` only: the `name` is already read above, and walking it
            # again would put the member through the default recurse as well.
            value_node = node.child_by_field_name("value")
            if value_node is not None:
                walk_fn(value_node, parent_class_nid)
        return True
    if node.is_named and node.type in ("internal_module", "module"):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            for child in node.children:
                if child.is_named and child.type in (
                        "identifier", "nested_identifier", "string"):
                    name_node = child
                    break
        body = node.child_by_field_name("body")
        if body is None:
            for child in node.children:
                if child.type == "statement_block":
                    body = child
                    break
        if name_node is not None:
            ns_name = _read_text(name_node, source)
            if name_node.type == "string":
                ns_name = ns_name.strip("'\"`")
            if ns_name:
                ns_nid = _make_id(stem, ns_name)
                line = node.start_point[0] + 1
                add_node_fn(ns_nid, ns_name, line)
                add_edge_fn(file_nid, ns_nid, "contains", line)
        if body is not None:
            for child in body.children:
                walk_fn(child, parent_class_nid)
        return True
    return False

def _csharp_namespace_name(node, source: bytes) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return _read_text(name_node, source).strip()
    for child in node.children:
        if child.type in ("identifier", "qualified_name"):
            return _read_text(child, source).strip()
    return ""

def _csharp_extra_walk(node, source: bytes, file_nid: str, stem: str, str_path: str,
                       nodes: list, edges: list, seen_ids: set, function_bodies: list,
                       parent_class_nid: str | None, add_node_fn, add_edge_fn,
                       walk_fn, namespace_stack: list[str], scope_stack: list[str]) -> bool:
    """Handle C# namespaces, enum members, and transparent class-member wrappers."""
    if node.type == "enum_member_declaration" and parent_class_nid:
        # `enum_declaration` is in C#'s class_types, so the enum type is a node
        # but its members were not, leaving the type a leaf: "which value does
        # this consumer branch on" had no answer. Java has emitted a node per
        # `enum_constant` with a `case_of` edge since, Kotlin since,
        # and Swift does the same for `enum_entry`; C# reaches the members
        # through the same walk, so this is the Java shape applied here.
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return True
        member_name = _read_text(name_node, source)
        if not member_name:
            return True
        line = node.start_point[0] + 1
        member_nid = _make_id(parent_class_nid, member_name)
        # C# is case-sensitive, so `enum E { Value, value }` is legal, but the id
        # recipe casefolds — both members normalize to one id. Emitting the
        # second would hang a duplicate edge on the first member's node, so the
        # first declaration keeps it (same guard as the property nodes in).
        if member_nid not in seen_ids:
            add_node_fn(member_nid, member_name, line)
            add_edge_fn(parent_class_nid, member_nid, "case_of", line)
        return True
    if node.type == "namespace_declaration":
        ns_name = _csharp_namespace_name(node, source)
        pushed = False
        if ns_name:
            namespace_stack.append(ns_name)
            scope_stack.append(f"s{node.start_byte}")
            pushed = True
            ns_label = ".".join(namespace_stack)
            ns_nid = _csharp_namespace_id(ns_label)
            line = node.start_point[0] + 1
            add_node_fn(ns_nid, ns_label, line, node_type="namespace", metadata={"kind": "csharp_namespace"})
            add_edge_fn(file_nid, ns_nid, "contains", line)
        body = node.child_by_field_name("body")
        if body:
            try:
                for child in body.children:
                    walk_fn(child, parent_class_nid)
            finally:
                if pushed:
                    namespace_stack.pop()
                    scope_stack.pop()
        elif pushed:
            namespace_stack.pop()
            scope_stack.pop()
        return True
    if node.type == "file_scoped_namespace_declaration":
        ns_name = _csharp_namespace_name(node, source)
        if ns_name:
            namespace_stack.append(ns_name)
            scope_stack.append(f"s{node.start_byte}")
            ns_label = ".".join(namespace_stack)
            ns_nid = _csharp_namespace_id(ns_label)
            line = node.start_point[0] + 1
            add_node_fn(ns_nid, ns_label, line, node_type="namespace", metadata={"kind": "csharp_namespace"})
            add_edge_fn(file_nid, ns_nid, "contains", line)
        return True
    if parent_class_nid and node.type.startswith("preproc_"):
        # tree-sitter wraps members in #if/#else/#elif directives in preproc_*
        # nodes. They are conditional containers, not ownership scopes: dropping
        # parent_class_nid here makes guarded methods look file-level.
        for child in node.children:
            walk_fn(child, parent_class_nid)
        return True
    return False

def _swift_extra_walk(node, source: bytes, file_nid: str, stem: str, str_path: str,
                      nodes: list, edges: list, seen_ids: set, function_bodies: list,
                      parent_class_nid: str | None, add_node_fn, add_edge_fn,
                      ensure_named_node_fn) -> bool:
    """Handle enum_entry for Swift. Returns True if handled."""
    if node.type == "enum_entry" and parent_class_nid:
        line = node.start_point[0] + 1
        for child in node.children:
            if child.type == "simple_identifier":
                case_name = _read_text(child, source)
                case_nid = _make_id(parent_class_nid, case_name)
                add_node_fn(case_nid, case_name, line)
                add_edge_fn(parent_class_nid, case_nid, "case_of", line)
        # Os tipos de valores associados são aninhados como `enum_type_parameters -> user_type ->
        # type_identifier` (um irmão do nome do caso simple_identifier). O
        # O loop de nome de caso acima nunca desce para eles, então `case start(Session)`
        # usado para eliminar totalmente a referência Evento -> Sessão. Espelhe o Swift
        # estilo de emissão de propriedade/parâmetro: colete as referências do tipo e emita um
        # aresta `references` do nó ENUM para cada tipo coletado.
        for child in node.children:
            if child.type != "enum_type_parameters":
                continue
            for grand in child.children:
                if not grand.is_named:
                    continue
                refs: list[tuple[str, str]] = []
                _swift_collect_type_refs(grand, source, False, refs)
                for ref_name, role in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "type"
                    target_nid = ensure_named_node_fn(ref_name, line)
                    if target_nid != parent_class_nid:
                        add_edge_fn(parent_class_nid, target_nid, "references",
                                    line, context=ctx)
        return True
    return False

def _java_extra_walk(node, source: bytes, file_nid: str, stem: str, str_path: str,
                     nodes: list, edges: list, seen_ids: set, function_bodies: list,
                     parent_class_nid: str | None, add_node_fn, add_edge_fn,
                     walk_fn) -> bool:
    """Handle enum_constant for Java. Returns True if handled."""
    if node.type == "enum_constant" and parent_class_nid:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return True
        const_name = _read_text(name_node, source)
        line = node.start_point[0] + 1
        const_nid = _make_id(parent_class_nid, const_name)
        add_node_fn(const_nid, const_name, line)
        add_edge_fn(parent_class_nid, const_nid, "case_of", line)
        # Constantes de corpo anônimo (`MONDAY { void greet(){} }`): desce para que o
        # os métodos do corpo não são abandonados; const_nid os anexa à constante.
        for child in node.children:
            if child.type == "class_body":
                for member in child.children:
                    walk_fn(member, parent_class_nid=const_nid)
        return True
    return False


def _kotlin_extra_walk(node, source: bytes, file_nid: str, stem: str, str_path: str,
                       nodes: list, edges: list, seen_ids: set, function_bodies: list,
                       parent_class_nid: str | None, add_node_fn, add_edge_fn,
                       walk_fn) -> bool:
    """Handle enum_entry for Kotlin. Returns True if handled (#1700 Kotlin half)."""
    if node.type == "enum_entry" and parent_class_nid:
        name_node = None
        for child in node.children:
            if child.type in ("simple_identifier", "identifier"):
                name_node = child
                break
        if name_node is None:
            return True
        const_name = _read_text(name_node, source)
        line = node.start_point[0] + 1
        const_nid = _make_id(parent_class_nid, const_name)
        add_node_fn(const_nid, const_name, line)
        add_edge_fn(parent_class_nid, const_nid, "case_of", line)
        for child in node.children:
            if child.type == "class_body":
                for member in child.children:
                    walk_fn(member, parent_class_nid=const_nid)
        return True
    return False


def _kotlin_package_name(root, source: bytes) -> str | None:
    """Dotted package FQN from the file's ``package_header``, or None.

    Grammar 1.1.0 puts the path in a ``qualified_identifier`` child; older
    forks use an ``identifier`` that spans the whole dotted text. Either way
    the node's text IS the FQN.
    """
    for child in root.children:
        if child.type != "package_header":
            continue
        for c in child.children:
            if c.type in ("qualified_identifier", "identifier"):
                pkg = _read_text(c, source).strip()
                return pkg or None
        return None
    return None


def _kotlin_nav_identifier_segments(nav, source: bytes) -> list[str] | None:
    """Flatten a Kotlin ``navigation_expression`` chain into its dotted
    identifier segments (``com.example.Foo.bar`` -> [com, example, Foo, bar]).

    Returns None when any segment is not a plain identifier — a receiver that
    is an expression, a call, ``this``, a string literal, etc. must never read
    as a qualified name (#2550). Older grammars with a different navigation
    shape also bail here, preserving their current behavior.
    """
    segments: list[str] = []
    node = nav
    while node is not None and node.type == "navigation_expression":
        named = [c for c in node.children if c.is_named]
        # Grammar 1.1.0 shape: <receiver> "." <identifier> (the dot is unnamed).
        if len(named) != 2:
            return None
        head, tail = named
        if tail.type not in ("simple_identifier", "identifier"):
            return None
        segments.append(_read_text(tail, source))
        node = head
    if node is None or node.type not in ("simple_identifier", "identifier"):
        return None
    segments.append(_read_text(node, source))
    segments.reverse()
    return segments


def _first_parse_error_line(root) -> int:
    """1-based line of the first ERROR/MISSING node under ``root`` (#2551).

    Descends the first erroring child at each level (document order), so it
    lands on the earliest error region. Some recoveries set ``has_error``
    without materializing an ERROR/MISSING child (zero-width recovery); the
    deepest still-erroring node's line is reported for those.
    """
    node = root
    while True:
        if node.type == "ERROR" or node.is_missing:
            return node.start_point[0] + 1
        child = next((c for c in node.children if c.has_error), None)
        if child is None:
            return node.start_point[0] + 1
        node = child


def _has_multiline_error(root) -> bool:
    """True if any materialized ERROR node spans more than one line (a
    recovery region large enough to plausibly drop symbols, vs a tiny
    single-line recovery that extracts completely)."""
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "ERROR" and n.end_point[0] > n.start_point[0]:
            return True
        stack.extend(c for c in n.children if c.has_error)
    return False


def _read_csharp_type_name(node, source: bytes) -> tuple[str, bool, str] | None:
    """Resolve a C# type name, whether it was qualified, and its qualifier prefix."""
    if node is None:
        return None
    if node.type in ("identifier", "predefined_type"):
        return (_read_text(node, source), False, "")
    if node.type == "qualified_name":
        prefix, _, tail = _read_text(node, source).rpartition(".")
        tail = tail.split("<", 1)[0]
        return (tail, True, prefix)
    if node.type == "generic_name":
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            qualified = name_node.type == "qualified_name"
            prefix, _, tail = _read_text(name_node, source).rpartition(".")
            return (tail, qualified, prefix if qualified else "")
    for child in node.children:
        if not child.is_named:
            continue
        result = _read_csharp_type_name(child, source)
        if result:
            return result
    return None

def _ruby_new_class_name(node, source: bytes) -> str | None:
    """Return ``ClassName`` if ``node`` is a ``ClassName.new(...)`` call, else None.

    Only a bare capitalized constant receiver counts (``Processor.new``);
    namespaced (``A::B.new``) and dynamic receivers are intentionally ignored so
    the binding stays unambiguous.
    """
    if node is None or node.type != "call":
        return None
    recv = node.child_by_field_name("receiver")
    meth = node.child_by_field_name("method")
    if recv is None or meth is None:
        return None
    if recv.type != "constant" or _read_text(meth, source) != "new":
        return None
    return _read_text(recv, source)

def _ruby_local_class_bindings(body_node, source: bytes) -> dict[str, str | None]:
    """Map ``local_var -> ClassName`` for ``var = ClassName.new`` within one Ruby
    method body, not descending into nested method definitions.

    100%-confidence contract: a variable assigned more than once, or to anything
    other than a single ``Constant.new``, maps to ``None`` (ambiguous) so callers
    never resolve it. Only the certain single-binding case carries a type.
    """
    bindings: dict[str, str | None] = {}
    boundary = {"method", "singleton_method"}

    def visit(n) -> None:
        for child in n.children:
            if child.type in boundary:
                continue  # método aninhado tem seu próprio escopo
            if child.type == "assignment":
                left = child.child_by_field_name("left")
                right = child.child_by_field_name("right")
                if left is not None and left.type == "identifier":
                    var = _read_text(left, source)
                    cls = _ruby_new_class_name(right, source) if right is not None else None
                    if cls is None:
                        # atribuído a algo que não podemos digitar: veneno se foi digitado
                        if var in bindings:
                            bindings[var] = None
                    elif var in bindings:
                        if bindings[var] != cls:
                            bindings[var] = None  # transferido para uma classe diferente
                    else:
                        bindings[var] = cls
            visit(child)

    visit(body_node)
    return bindings

def _ruby_const_last_name(node, source: bytes) -> str:
    """Last constant of a ``constant`` or ``scope_resolution`` (``A::B::C`` -> ``C``)."""
    if node is None:
        return ""
    if node.type == "constant":
        return _read_text(node, source)
    if node.type == "scope_resolution":
        consts = [c for c in node.children if c.type == "constant"]
        if consts:
            return _read_text(consts[-1], source)
    return ""

def _ruby_const_full_name(node, source: bytes) -> str:
    """Full constant path of a ``constant`` or ``scope_resolution`` (``A::B::C`` kept whole)."""
    if node is None or node.type not in ("constant", "scope_resolution"):
        return ""
    return _read_text(node, source).strip()

_RUBY_CLASS_FACTORIES = frozenset({("Struct", "new"), ("Class", "new"), ("Data", "define")})

def _ruby_extra_walk(node, source: bytes, file_nid: str, stem: str, str_path: str,
                     nodes: list, edges: list, seen_ids: set, function_bodies: list,
                     parent_class_nid: str | None, add_node, add_edge, walk,
                     callable_def_nids: set, callable_class_nids: set,
                     ruby_namespace: list) -> bool:
    """Ruby: a constant assignment whose RHS is ``Struct.new(...)``,
    ``Class.new(Super)`` or ``Data.define(...)`` defines a class named after the
    constant (#1640). Synthesize the class node, attach block-defined methods via
    ``method`` (by recursing the block with the new node as parent), and emit an
    ``inherits`` edge for ``Class.new(Super)``. Returns True if handled.
    """
    if node.type != "assignment":
        return False
    left = node.child_by_field_name("left")
    right = node.child_by_field_name("right")
    if left is None or right is None or left.type != "constant" or right.type != "call":
        return False
    recv = right.child_by_field_name("receiver")
    meth = right.child_by_field_name("method")
    if recv is None or meth is None or recv.type != "constant":
        return False
    if (_read_text(recv, source), _read_text(meth, source)) not in _RUBY_CLASS_FACTORIES:
        return False

    const_name = _read_text(left, source)
    if not const_name:
        return False
    # Qualify the factory-defined const against the enclosing scope, mirroring
    # the generic class branch: `module Billing; Invoice = Struct.new`
    # labels `Billing::Invoice`.
    const_segments = const_name.split("::")
    const_name = "::".join(ruby_namespace + const_segments)
    line = node.start_point[0] + 1
    class_nid = _make_id(stem, const_name)
    add_node(class_nid, const_name, line)
    callable_def_nids.add(class_nid)  # uma classe pode ser chamada (seu construtor)
    callable_class_nids.add(class_nid)  #...but only via its constructor
    # Espelhe a ramificação da classe genérica: a contenção sempre fica suspensa no nó do arquivo.
    add_edge(file_nid, class_nid, "contains", line)

    # `Class.new(Super)` — o primeiro argumento constante posicional é a superclasse.
    if _read_text(recv, source) == "Class":
        args = next((c for c in right.children if c.type == "argument_list"), None)
        if args is not None:
            for arg in args.children:
                if arg.type in ("constant", "scope_resolution"):
                    base = _ruby_const_last_name(arg, source)
                    if base:
                        base_nid = _make_id(stem, base)
                        if base_nid not in seen_ids:
                            base_nid = _make_id(base)
                            if base_nid not in seen_ids:
                                # origin_file lets _disambiguate_colliding_node_ids
                                # diferencie a referência não resolvida deste arquivo de
                                # outro arquivo com o mesmo nome, em vez de todos
                                # file's stub collapsing onto one shared bare id
                                # (veja ensure_named_node(), que define o mesmo
                                # campo exatamente por esse motivo).
                                nodes.append({
                                    "id": base_nid, "label": base,
                                    "file_type": "code", "source_file": "",
                                    "source_location": "", "origin_file": str_path,
                                })
                                seen_ids.add(base_nid)
                        add_edge(class_nid, base_nid, "inherits", line)
                    break

    # Recurse o bloco do/brace para que os métodos definidos pelo bloco sejam anexados à classe.
    # O bloco envolve suas instruções em um `body_statement` (como o corpo de uma classe);
    # desça até ele para que o manipulador do método veja parent_class_nid - caso contrário, o
    # recurse padrão redefine o pai para None e o método trava no arquivo
    # com um rótulo sem pontos.
    block = next((c for c in right.children if c.type in ("do_block", "block")), None)
    if block is not None:
        body = next((c for c in block.children if c.type == "body_statement"), block)
        ruby_namespace.extend(const_segments)
        try:
            for child in body.children:
                walk(child, parent_class_nid=class_nid)
        finally:
            del ruby_namespace[-len(const_segments):]
    return True

def _extract_generic(
    path: Path, config: LanguageConfig, *, source_override: bytes | None = None
) -> dict:
    """Generic AST extractor driven by LanguageConfig.

    ``source_override`` parses the given bytes instead of reading ``path``, while
    still keying nodes/edges off ``path``. Lets container formats (e.g. Vue SFCs)
    mask the wrapper and parse just the embedded ``<script>``.
    """
    try:
        mod = importlib.import_module(config.ts_module)
        from tree_sitter import Language, Parser
        lang_fn = getattr(mod, config.ts_language_fn, None)
        if lang_fn is None:
            # Fallback para PHP: tente "language_php" e depois "idioma"
            lang_fn = getattr(mod, "language", None)
        if lang_fn is None:
            return {"nodes": [], "edges": [], "error": f"No language function in {config.ts_module}"}
        language = Language(lang_fn())
    except ImportError:
        return {"nodes": [], "edges": [], "error": f"{config.ts_module} not installed"}
    except TypeError as e:
        # tree-sitter version mismatch: old Language() expects (lib_path),
        # new Language() espera (linguagem_cápsula, nome). Dê uma dica
        # para que os usuários vejam o caminho de atualização em vez de um TypeError simples.
        hint = (
            f"tree-sitter version mismatch for {config.ts_module}: {e}. "
            "Try: pip install --upgrade tree-sitter tree-sitter-languages"
        )
        return {"nodes": [], "edges": [], "error": hint}
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    try:
        parser = Parser(language)
        source = path.read_bytes() if source_override is None else source_override
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = _file_stem(path)
    str_path = str(path)
    # Names bound by an import of a module outside the corpus. Module-scoped, so it
    # is computed once per file and consulted from every scope — see
    # `_js_external_import_names`.
    js_external_imports: set[str] = (
        _js_external_import_names(root, source, str_path)
        if config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript")
        else set()
    )
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    namespace_stack: list[str] = []
    # Ruby only: enclosing module/class segments, so `module Foo::Bar` (compact)
    # and `module Foo; module Bar` (nested) label the same node `Foo::Bar` and
    # `include Foo::Bar` resolves for both spellings. Kept separate from
    # namespace_stack so Ruby method ids/labels are unchanged.
    ruby_namespace: list[str] = []
    scope_stack: list[str] = []
    function_bodies: list[tuple[str, object]] = []
    # nids de definições de função/método/classe neste arquivo. O indireto-
    # dispatch guard (Python) resolve um identificador de argumento de chamada apenas para uma aresta
    # quando nomeia um desses defs chamáveis ​​- nunca um arbitrário com o mesmo nome
    # node - então `process(config)` não pode fabricar uma vantagem para um não-chamável.
    callable_def_nids: set[str] = set()
    # Subset of callable_def_nids that are CLASS defs (callable only via their
    # constructor). Classes are frequently passed as descriptive values, not for
    # invocation (`select(Model)`, exception tuples), so the cross-file indirect_call
    # guard excludes them to avoid false edges.
    callable_class_nids: set[str] = set()
    # Apenas Python: conjunto por função de nomes vinculados localmente (params + local
    # atribuição / para / com-como / metas de compreensão). O despacho indireto
    # guard ignora qualquer identificador de argumento de chamada no conjunto da função envolvente,
    # portanto, um param/local que sombreia o nome de uma função de módulo não produz nenhuma vantagem.
    local_bound_names: dict[str, set[str]] = {}
    # JS/TS only: per-BODY locals for sibling closures tracked under a
    # single const nid by the branch (`const h = wrapper(cb1, cb2)`).
    # Keyed by id(body) — like receiver_types_by_body — and fed to the per-body
    # walk_calls as extra_locals, so each closure sees only its own
    # params/locals instead of a shared union that over-suppresses siblings.
    closure_locals_by_body: dict[int, set[str]] = {}
    pending_listen_edges: list[tuple[str, str, int]] = []
    # tree-sitter-swift analisa `class Foo` e `extension Foo` como
    # `class_declaration`. Pares do mesmo arquivo se fundem por seen_ids, mas entre arquivos
    # extensões não (a haste do arquivo faz parte do ID), então elas são coletadas aqui
    # para uma mesclagem em nível de corpus após cada arquivo ter sido analisado.
    swift_extensions: list[dict] = []
    # expressões de chamada em inicializadores de propriedade/campo (por exemplo
    # `deixe vm = VM()`) viver fora dos corpos da função, então o call-walk nunca
    # os alcança. Colete (owner_nid, call_node) aqui e acompanhe-os também.
    initializer_nodes: list[tuple[str, object]] = []
    # Ruby inclui/estende/precede mixins coletados durante a caminhada do nó,
    # mesclado em raw_calls depois que o call-walk o preenche (raw_calls não
    # ainda existe enquanto walk() é executado). Arquivo cruzado resolvido pelo resolvedor Ruby.
    _ruby_mixin_calls: list[dict] = []
    # mapa por arquivo do nome local -> tipo declarado (propriedades + parâmetros),
    # encadeado como `swift_type_table` para que chamadas de membros (`vm.update()`) possam ser
    # resolvido para a definição real do receptor em _resolve_swift_member_calls.
    type_table: dict[str, str] = {}
    # pending factory bindings (`let x = Factory.make()`), name ->
    # (FactoryType, method). Label-only (no nids, so the per-file AST cache
    # stays valid); resolved corpus-side in _resolve_swift_member_calls against
    # the factory method's marked plain return type.
    swift_factory_bindings: dict[str, tuple[str, str]] = {}
    # A digitação do receptor Java tem escopo de método: os campos da classe atual são compartilhados,
    # enquanto parâmetros e locais pertencem apenas ao seu método de declaração.
    java_field_types: dict[str, dict[str, str]] = {}
    java_method_scopes: dict[int, tuple[object, str]] = {}
    # C# receiver typing is method-scoped too: class fields/properties
    # are shared, parameters and locals belong only to their declaring method —
    # the old file-wide table let one method's untypable rebinding poison a
    # same-named, explicitly typed receiver in a different method.
    csharp_field_types: dict[str, dict[str, str]] = {}
    csharp_method_scopes: dict[int, tuple[object, str]] = {}

    csharp_interface_names: set[str] = set()
    if config.ts_module == "tree_sitter_c_sharp":
        csharp_interface_names = _csharp_pre_scan_interfaces(root, source)

    swift_protocol_names: set[str] = set()
    swift_class_names: set[str] = set()
    if config.ts_module == "tree_sitter_swift":
        swift_protocol_names, swift_class_names = _swift_pre_scan(root, source)

    python_underscore_groups: dict[str, set[str]] = {}
    if config.ts_module == "tree_sitter_python":
        python_underscore_groups = _python_pre_scan_underscore_collisions(root, source, stem)

    def add_node(nid: str, label: str, line: int, *, node_type: str | None = None,
                 metadata: dict | None = None) -> None:
        if nid in seen_ids:
            return
        seen_ids.add(nid)
        merged = dict(metadata or {})
        if namespace_stack:
            merged.setdefault("namespace", ".".join(namespace_stack))
        if scope_stack and node_type != "namespace":
            merged.setdefault("scope_chain", list(scope_stack))
        node = {
            "id": nid,
            "label": label,
            "file_type": "code",
            "source_file": str_path,
            "source_location": f"L{line}",
        }
        if node_type:
            node["type"] = node_type
        if merged:
            node["metadata"] = sanitize_metadata(merged)
        nodes.append(node)

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0,
                 context: str | None = None,
                 metadata: dict | None = None) -> None:
        edge = {
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": confidence,
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": weight,
        }
        if context:
            edge["context"] = context
        if metadata:
            edge["metadata"] = sanitize_metadata(metadata)
        edges.append(edge)

    def ensure_named_node(name: str, line: int) -> str:
        nid = _make_id(stem, ".".join(namespace_stack), name)
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

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    def walk(node, parent_class_nid: str | None = None) -> None:
        t = node.type

        # Import types
        if t in config.import_types:
            if config.import_handler:
                imported_modules = config.import_handler(node, source, file_nid, stem, edges, str_path, scope_stack)
                # Manipuladores de importação em nível de módulo (Swift) nomeiam um módulo, não um arquivo
                # caminho, portanto não há nenhum nó pré-existente para ancorar a aresta.
                # Eles retornam pares (id, rótulo) para os quais materializamos um
                # nó `type=módulo`; caso contrário, build_from_json remove todos esses
                # importe a aresta como uma referência pendente/externa. O mesmo módulo
                # importado de N arquivos compartilha um id (file_type=code mantém
                # validação build.py feliz; `type=module` isenta-o de
                # id-desambiguation) para que ele seja recolhido em um nó compartilhado.
                if imported_modules:
                    line = node.start_point[0] + 1
                    for mod_nid, mod_label in imported_modules:
                        if mod_nid not in seen_ids:
                            seen_ids.add(mod_nid)
                            nodes.append({
                                "id": mod_nid,
                                "label": mod_label,
                                "file_type": "code",
                                "type": "module",
                                "source_file": str_path,
                                "source_location": f"L{line}",
                            })
            # Para export_statement: retornar (ignorar filhos) apenas se for uma reexportação
            # (tem uma fonte `from`). Caso contrário, deixe de passear com crianças, o que pode
            # contain function_declaration, class_declaration, etc.
            if t == "export_statement":
                has_source = any(c.type == "string" for c in node.children)
                if not has_source:
                    for child in node.children:
                        walk(child, parent_class_nid)
            return

        # Class types
        if t in config.class_types:
            # Resolve class name
            name_node = node.child_by_field_name(config.name_field)
            if name_node is None:
                for child in node.children:
                    if child.type in config.name_fallback_child_types:
                        name_node = child
                        break
            if not name_node:
                return
            class_name = _read_text(name_node, source)
            # Ruby: fully qualify the module/class label with its enclosing
            # scope, splitting compact `Foo::Bar` names into segments so both
            # declaration styles converge on one `Foo::Bar` label.
            ruby_segments: list[str] = []
            if config.ts_module == "tree_sitter_ruby":
                ruby_segments = class_name.split("::")
                class_name = "::".join(ruby_namespace + ruby_segments)
            class_nid = _make_id(stem, ".".join(namespace_stack), class_name)
            line = node.start_point[0] + 1
            metadata = None
            if config.ts_module == "tree_sitter_c_sharp":
                if parent_class_nid:
                    metadata = {"is_nested_type": True}
                # `partial class Foo` split across files mints one node
                # per file (the id carries the file stem). Stamp the halves so
                # the corpus-level _merge_csharp_partial_class_nodes pass can
                # collapse them onto one canonical node. Grammar: `partial` is
                # a `modifier` direct child of the type declaration.
                if t in (
                    "class_declaration",
                    "struct_declaration",
                    "interface_declaration",
                    "record_declaration",
                ) and any(
                    c.type == "modifier" and _read_text(c, source) == "partial"
                    for c in node.children
                ):
                    metadata = dict(metadata or {})
                    metadata["is_partial"] = True
            add_node(class_nid, class_name, line, metadata=metadata)
            callable_def_nids.add(class_nid)  # uma classe pode ser chamada (construtor)
            callable_class_nids.add(class_nid)  #...but only via its constructor
            # A nested class/object/trait is contained by its ENCLOSING type, not
            # the file. parent_class_nid is threaded down the walk for
            # every language and is always a real class-like node (never a
            # namespace — namespace handlers pass it through unchanged), so it is
            # a valid edge source. The `!= class_nid` guard avoids a self-loop
            # when same-name nesting (`class Foo: class Foo`) collides ids, since
            # class ids omit the enclosing type name. Top-level types (parent
            # None) still source from the file, keeping the containment tree
            # connected: file -> Outer -> Inner.
            if parent_class_nid and parent_class_nid != class_nid:
                add_edge(parent_class_nid, class_nid, "contains", line)
            else:
                add_edge(file_nid, class_nid, "contains", line)

            # Decoradores TS/JS na classe e seus membros (@Component, @Injectable,
            # @Input, @Inject, @Entity,…). Os decoradores vivem apenas em subárvores de classe.
            if config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
                _ts_emit_decorator_edges(node, class_nid, stem, source,
                                         ensure_named_node, add_edge)

            if config.ts_module == "tree_sitter_swift" and any(
                c.type == "extension" for c in node.children
            ):
                swift_extensions.append({"nid": class_nid, "label": class_name})

            # Python-specific: inheritance
            if config.ts_module == "tree_sitter_python":
                args = node.child_by_field_name("superclasses")
                if args:
                    for arg in args.children:
                        if arg.type == "identifier":
                            base = _read_text(arg, source)
                            base_nid = ensure_named_node(base, line)
                            add_edge(class_nid, base_nid, "inherits", line)

            # Swift-specific: conformance / inheritance
            if config.ts_module == "tree_sitter_swift":
                swift_kind = _swift_declaration_keyword(node) if t == "class_declaration" else "protocol"
                seen_swift_base = False
                for child in node.children:
                    if child.type != "inheritance_specifier":
                        continue
                    base_name: str | None = None
                    user_type_node = None
                    for sub in child.children:
                        if sub.type == "user_type":
                            user_type_node = sub
                            base_name = _swift_user_type_name(sub, source)
                            break
                        if sub.type == "type_identifier":
                            base_name = _read_text(sub, source) or None
                            break
                    if not base_name:
                        continue
                    base_nid = _make_id(stem, base_name)
                    if base_nid not in seen_ids:
                        base_nid = _make_id(base_name)
                        if base_nid not in seen_ids:
                            nodes.append({
                                "id": base_nid,
                                "label": base_name,
                                "file_type": "code",
                                "source_file": "",
                                "source_location": "",
                            })
                            seen_ids.add(base_nid)
                    if t == "protocol_declaration":
                        relation = "inherits"
                    else:
                        relation = _swift_classify_base(
                            base_name, swift_kind, not seen_swift_base,
                            swift_protocol_names, swift_class_names,
                        )
                    seen_swift_base = True
                    add_edge(class_nid, base_nid, relation, line)
                    if user_type_node is not None:
                        for arg_child in user_type_node.children:
                            if arg_child.type != "type_arguments":
                                continue
                            for arg in arg_child.children:
                                if not arg.is_named:
                                    continue
                                refs: list[tuple[str, str]] = []
                                _swift_collect_type_refs(arg, source, True, refs)
                                for ref_name, _role in refs:
                                    target = ensure_named_node(ref_name, line)
                                    add_edge(class_nid, target, "references", line,
                                             context="generic_arg")

            # Específico do PHP: estende → herda, implementa → implementa, usa → mixes_in
            if config.ts_module == "tree_sitter_php":
                def _php_emit_base(base_name: str, rel: str, at_line: int) -> None:
                    if not base_name:
                        return
                    base_nid = _make_id(stem, base_name)
                    if base_nid not in seen_ids:
                        base_nid = _make_id(base_name)
                        if base_nid not in seen_ids:
                            nodes.append({
                                "id": base_nid,
                                "label": base_name,
                                "file_type": "code",
                                "source_file": "",
                                "source_location": "",
                            })
                            seen_ids.add(base_nid)
                    add_edge(class_nid, base_nid, rel, at_line)

                for child in node.children:
                    if child.type == "base_clause":
                        for sub in child.children:
                            if sub.type in ("name", "qualified_name"):
                                _php_emit_base(_php_name_text(sub, source) or "",
                                                "inherits", child.start_point[0] + 1)
                    elif child.type == "class_interface_clause":
                        for sub in child.children:
                            if sub.type in ("name", "qualified_name"):
                                _php_emit_base(_php_name_text(sub, source) or "",
                                                "implements", child.start_point[0] + 1)
                body = node.child_by_field_name("body")
                if body is None:
                    for c in node.children:
                        if c.type == "declaration_list":
                            body = c
                            break
                if body is not None:
                    for member in body.children:
                        if member.type != "use_declaration":
                            continue
                        for sub in member.children:
                            if sub.type in ("name", "qualified_name"):
                                _php_emit_base(_php_name_text(sub, source) or "",
                                                "mixes_in", member.start_point[0] + 1)

            # Kotlin-specific: delegation_specifiers → inherits (constructor_invocation) / implements (user_type)
            if config.ts_module == "tree_sitter_kotlin":
                for child in node.children:
                    if child.type != "delegation_specifiers":
                        continue
                    for spec in child.children:
                        if spec.type != "delegation_specifier":
                            continue
                        relation = "implements"
                        user_type_node = None
                        for sub in spec.children:
                            if sub.type == "constructor_invocation":
                                relation = "inherits"
                                for inner in sub.children:
                                    if inner.type == "user_type":
                                        user_type_node = inner
                                        break
                                break
                            if sub.type == "user_type":
                                user_type_node = sub
                                break
                            # `class Foo : Bar by baz` envolve o delegado
                            # interface `Bar` em uma `delegação_explícita`
                            # nó; pegue seu primeiro descendente `user_type` então
                            # a aresta dos implementos (e recuperação de argumentos genéricos)
                            # still fire.
                            if sub.type == "explicit_delegation":
                                for inner in sub.children:
                                    if inner.type == "user_type":
                                        user_type_node = inner
                                        break
                                break
                        if user_type_node is None:
                            continue
                        base = _kotlin_user_type_name(user_type_node, source)
                        if not base:
                            continue
                        base_nid = ensure_named_node(base, line)
                        add_edge(class_nid, base_nid, relation, line)
                        for arg_child in user_type_node.children:
                            if arg_child.type != "type_arguments":
                                continue
                            for arg in arg_child.children:
                                if arg.type == "type_projection":
                                    for inner in arg.children:
                                        if not inner.is_named:
                                            continue
                                        refs: list[tuple[str, str]] = []
                                        _kotlin_collect_type_refs(inner, source, True, refs)
                                        for ref_name, _role in refs:
                                            target = ensure_named_node(ref_name, line)
                                            add_edge(class_nid, target, "references", line,
                                                     context="generic_arg")

            # Ruby: `class Dog < Animal` coloca a classe base na `superclass`
            # campo (um token `<` seguido por uma constante ou scope_resolution).
            # Não havia branch Ruby, então cada aresta herdada do Ruby foi descartada.
            if config.ts_module == "tree_sitter_ruby":
                sup = node.child_by_field_name("superclass")
                if sup is not None:
                    base = ""
                    for sub in sup.children:
                        if sub.type == "constant":
                            base = _read_text(sub, source)
                            break
                        if sub.type == "scope_resolution":
                            consts = [c for c in sub.children if c.type == "constant"]
                            if consts:
                                base = _read_text(consts[-1], source)
                            break
                    if base:
                        base_nid = ensure_named_node(base, line)
                        add_edge(class_nid, base_nid, "inherits", line)

                # `include`/`extend`/`prepend <Const>` no corpo da classe/módulo ->
                # uma aresta `mixes_in` para o módulo. O módulo geralmente vive
                # em outro arquivo, então adie a resolução para o arquivo cruzado Ruby
                # resolvedor (reutilizando a lógica candidata e o módulo
                # nós como alvos). Somente argumentos constantes vazios/com namespace contam;
                # `extend self`, `include some_var`, etc. são ignorados.
                _rb_body = _find_body(node, config)
                if _rb_body is not None:
                    for _stmt in _rb_body.children:
                        if _stmt.type != "call" or _stmt.child_by_field_name("receiver") is not None:
                            continue
                        _m = _stmt.child_by_field_name("method")
                        if _m is None or _read_text(_m, source) not in ("include", "extend", "prepend"):
                            continue
                        _args = _stmt.child_by_field_name("arguments")
                        if _args is None:
                            continue
                        for _arg in _args.children:
                            if _arg.type not in ("constant", "scope_resolution"):
                                continue
                            # Full path, not last segment: `include Foo::Bar`
                            # must reference `Foo::Bar`, and truncating
                            # `ActiveSupport::Concern` to `Concern` fabricated
                            # edges to any local `Concern` module.
                            _mod = _ruby_const_full_name(_arg, source)
                            if _mod:
                                _ruby_mixin_calls.append({
                                    "caller_nid": class_nid,
                                    "callee": _mod,
                                    "is_mixin": True,
                                    "source_file": str_path,
                                    "source_location": f"L{_stmt.start_point[0] + 1}",
                                })

            # C#-specific: inheritance / interface implementation via base_list
            if config.ts_module == "tree_sitter_c_sharp":
                csharp_type_params = _csharp_type_parameters_in_scope(node, source)
                for child in node.children:
                    if child.type != "base_list":
                        continue
                    for sub in child.children:
                        if sub.type not in ("identifier", "generic_name", "qualified_name"):
                            continue
                        base_info = _read_csharp_type_name(sub, source)
                        if base_info is None:
                            continue
                        base, qualified, qualifier = base_info
                        if not base or base in csharp_type_params:
                            continue
                        base_nid = _make_id(stem, ".".join(namespace_stack), base)
                        if base_nid not in seen_ids:
                            base_nid = _make_id(base)
                            if base_nid not in seen_ids:
                                nodes.append({
                                    "id": base_nid,
                                    "label": base,
                                    "file_type": "code",
                                    "source_file": "",
                                    "source_location": "",
                                })
                                seen_ids.add(base_nid)
                        # An `interface`'s base_list holds base interfaces, so every
                        # entry is interface inheritance (`inherits`) -- the same way the
                        # Java extractor treats `extends_interfaces`. Only class/struct/
                        # record declarations use the name-based class-vs-interface split.
                        if t == "interface_declaration":
                            relation = "inherits"
                        else:
                            relation = _csharp_classify_base(base, csharp_interface_names)
                        metadata = {"ref_token": base}
                        if qualified:
                            metadata["qualified"] = True
                        if qualifier:
                            metadata["ref_qualifier"] = qualifier
                        add_edge(class_nid, base_nid, relation, line, metadata=metadata)
                        if sub.type == "generic_name":
                            for tal in sub.children:
                                if tal.type != "type_argument_list":
                                    continue
                                for arg in tal.children:
                                    if not arg.is_named:
                                        continue
                                    refs: list[tuple[str, str, bool, str]] = []
                                    _csharp_collect_type_refs(
                                        arg, source, True, refs, csharp_type_params
                                    )
                                    for ref_name, _role, ref_qualified, ref_qualifier in refs:
                                        target = ensure_named_node(ref_name, line)
                                        metadata = {"ref_token": ref_name}
                                        if ref_qualified:
                                            metadata["qualified"] = True
                                        if ref_qualifier:
                                            metadata["ref_qualifier"] = ref_qualifier
                                        add_edge(class_nid, target, "references", line,
                                                 context="generic_arg", metadata=metadata)

            # Java-specific: extends (superclass) / implements (interfaces) / interface-extends
            if config.ts_module in ("tree_sitter_java", "tree_sitter_groovy"):
                def _emit_java_parent(base_name: str, rel: str, at_line: int) -> None:
                    if not base_name:
                        return
                    base_nid = _make_id(stem, base_name)
                    if base_nid not in seen_ids:
                        base_nid = _make_id(base_name)
                        if base_nid not in seen_ids:
                            nodes.append({
                                "id": base_nid,
                                "label": base_name,
                                "file_type": "code",
                                "source_file": "",
                                "source_location": "",
                            })
                            seen_ids.add(base_nid)
                    add_edge(class_nid, base_nid, rel, at_line)

                def _emit_java_parent_type(type_node, rel: str, at_line: int) -> None:
                    refs: list[tuple[str, str]] = []
                    _java_collect_type_refs(type_node, source, False, refs)
                    parent_emitted = False
                    for ref_name, role in refs:
                        if role == "type" and not parent_emitted:
                            _emit_java_parent(ref_name, rel, at_line)
                            parent_emitted = True
                        elif role == "generic_arg":
                            target_nid = ensure_named_node(ref_name, at_line)
                            if target_nid != class_nid:
                                add_edge(class_nid, target_nid, "references", at_line,
                                         context="generic_arg")

                sup = node.child_by_field_name("superclass")
                if sup is not None:
                    for sub in sup.children:
                        if sub.is_named:
                            _emit_java_parent_type(sub, "inherits", line)
                            break

                ifs = node.child_by_field_name("interfaces")
                if ifs is not None:
                    for sub in ifs.children:
                        if sub.type == "type_list":
                            for tid in sub.children:
                                if tid.is_named:
                                    _emit_java_parent_type(tid, "implements", line)

                if t == "interface_declaration":
                    for child in node.children:
                        if child.type == "extends_interfaces":
                            for sub in child.children:
                                if sub.type == "type_list":
                                    for tid in sub.children:
                                        if tid.is_named:
                                            _emit_java_parent_type(tid, "inherits", line)

                annotation_targets: set[str] = set()
                for anno_name, anno_raw in _java_annotation_names(node, source):
                    # An inline-qualified annotation (`@org.pkg.Foo`) keeps its
                    # full dotted name so a bare same-named local class can't
                    # absorb it; _resolve_java_type_references maps internal
                    # FQNs back to their real nodes. Groovy has no such
                    # resolver pass, so it keeps the legacy bare-name stub.
                    if "." in anno_raw and config.ts_module == "tree_sitter_java":
                        anno_name = anno_raw
                    target_nid = ensure_named_node(anno_name, line)
                    if target_nid != class_nid and target_nid not in annotation_targets:
                        add_edge(class_nid, target_nid, "references", line,
                                 context="attribute")
                        annotation_targets.add(target_nid)
                for ref_name in _java_annotation_class_literal_refs(node, source):
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != class_nid and target_nid not in annotation_targets:
                        add_edge(class_nid, target_nid, "references", line,
                                 context="attribute")
                        annotation_targets.add(target_nid)

                if t == "record_declaration":
                    components = node.child_by_field_name("parameters")
                    if components is not None:
                        for component in components.children:
                            if component.type == "formal_parameter":
                                type_node = component.child_by_field_name("type")
                            elif component.type == "spread_parameter":
                                type_node = next(
                                    (
                                        child
                                        for child in component.children
                                        if child.is_named
                                        and child.type not in ("modifiers", "variable_declarator")
                                    ),
                                    None,
                                )
                            else:
                                continue
                            refs: list[tuple[str, str]] = []
                            _java_collect_type_refs(type_node, source, False, refs)
                            component_line = component.start_point[0] + 1
                            for ref_name, role in refs:
                                ctx = "generic_arg" if role == "generic_arg" else "field"
                                target_nid = ensure_named_node(ref_name, component_line)
                                if target_nid != class_nid:
                                    add_edge(class_nid, target_nid, "references",
                                             component_line, context=ctx)

            # Scala: extends_clause carrega `estende Base com Trait1 com Trait2`.
            # A primeira base depois de `extends` é `inherits`; cada subseqüente
            # o tipo depois de `with` é `mixes_in`. Também ande class_parameters para
            # referências de tipo construtor como campo.
            if config.ts_module == "tree_sitter_scala":
                extend = node.child_by_field_name("extend")
                if extend is None:
                    for c in node.children:
                        if c.type == "extends_clause":
                            extend = c
                            break
                if extend is not None:
                    bases: list[tuple[str, int]] = []

                    def scala_base_name(type_node) -> str | None:
                        if type_node.type == "type_identifier":
                            return _read_text(type_node, source)
                        if type_node.type == "stable_type_identifier":
                            tail = next(
                                (
                                    child
                                    for child in reversed(type_node.children)
                                    if child.type in ("type_identifier", "identifier")
                                ),
                                None,
                            )
                            return _read_text(tail, source) if tail is not None else None
                        if type_node.type == "generic_type":
                            base = type_node.child_by_field_name("type")
                            if base is None:
                                base = next(
                                    (
                                        child
                                        for child in type_node.children
                                        if child.type
                                        in ("type_identifier", "stable_type_identifier")
                                    ),
                                    None,
                                )
                            return scala_base_name(base) if base is not None else None
                        return None

                    for c in extend.children:
                        base_name = scala_base_name(c)
                        if base_name is not None:
                            bases.append((base_name, c.start_point[0] + 1))
                    for idx, (base_name, base_line) in enumerate(bases):
                        rel = "inherits" if idx == 0 else "mixes_in"
                        base_nid = ensure_named_node(base_name, base_line)
                        if base_nid != class_nid:
                            add_edge(class_nid, base_nid, rel, base_line)
                for c in node.children:
                    if c.type != "class_parameters":
                        continue
                    for cp in c.children:
                        if cp.type != "class_parameter":
                            continue
                        ptype = cp.child_by_field_name("type")
                        if ptype is None:
                            continue
                        cp_line = cp.start_point[0] + 1
                        refs: list[tuple[str, str]] = []
                        _scala_collect_type_refs(ptype, source, False, refs)
                        for ref_name, role in refs:
                            ctx = "generic_arg" if role == "generic_arg" else "field"
                            target_nid = ensure_named_node(ref_name, cp_line)
                            if target_nid != class_nid:
                                add_edge(class_nid, target_nid, "references",
                                         cp_line, context=ctx)

            # C#: a primary constructor (`class Foo(IBar bar)`, C# 12+) declares
            # its dependencies on the type declaration itself rather than in a
            # field or property, so neither the field_declaration nor the
            # property_declaration handler ever sees them — the parameter type
            # got no references edge, and because the name was never registered
            # in csharp_field_types, _csharp_method_receiver_types could not type
            # the receiver either, so calls through it (`bar.Baz()`) lost their
            # calls edge as well. The Scala class_parameters branch directly
            # above is the analogue; Kotlin's is. Grammar note: the list is
            # an UNNAMED child of the declaration, so child_by_field_name(
            # "parameters") returns None and the children must be scanned.
            if config.ts_module == "tree_sitter_c_sharp" and t in (
                "class_declaration",
                "record_declaration",
                "struct_declaration",
            ):
                csharp_type_params = _csharp_type_parameters_in_scope(node, source)
                for c in node.children:
                    if c.type != "parameter_list":
                        continue
                    for param in c.children:
                        if param.type != "parameter":
                            continue
                        ptype = param.child_by_field_name("type")
                        if ptype is None:
                            continue
                        pname = param.child_by_field_name("name")
                        p_line = param.start_point[0] + 1
                        # Receiver binding mirrors the field_declaration rule:
                        # Pascal-case only (a primitive owns no resolvable
                        # method) and never a bare type parameter (`T item`).
                        recv = _csharp_receiver_type_name(ptype, source)
                        if (pname is not None and recv and recv[:1].isupper()
                                and recv not in csharp_type_params):
                            csharp_field_types.setdefault(class_nid, {})[
                                _read_text(pname, source)
                            ] = recv
                        refs = []
                        _csharp_collect_type_refs(
                            ptype, source, False, refs, csharp_type_params
                        )
                        for ref_name, role, qualified, qualifier in refs:
                            ctx = "generic_arg" if role == "generic_arg" else "field"
                            target_nid = ensure_named_node(ref_name, p_line)
                            if target_nid != class_nid:
                                metadata = {"ref_token": ref_name}
                                if qualified:
                                    metadata["qualified"] = True
                                if qualifier:
                                    metadata["ref_qualifier"] = qualifier
                                add_edge(class_nid, target_nid, "references",
                                         p_line, context=ctx, metadata=metadata)

            # Específico de C++: herança via base_class_clause (classe e struct).
            # tree-sitter-cpp shape:
            #   class_specifier / struct_specifier
            #     base_class_clause
            #       access_specifier? ("public"/"protected"/"private")  -- skip
            #       "virtual"?                                          -- skip
            #       type_identifier                                     -- "Base"
            #       qualified_identifier                                -- "ns::Base"
            #       template_type                                       -- "Vec<int>"
            # Bases múltiplas são irmãs separadas por tokens ','.
            if config.ts_module == "tree_sitter_cpp":
                for child in node.children:
                    if child.type != "base_class_clause":
                        continue
                    for sub in child.children:
                        base = ""
                        template_args_node = None
                        if sub.type == "type_identifier":
                            base = _read_text(sub, source)
                        elif sub.type == "qualified_identifier":
                            # Use a cauda não qualificada para que "std::vector" corresponda
                            # um ID de nó "vetorial", se existir no grafo;
                            # caso contrário, retorne ao texto completo e qualificado.
                            tail = sub.child_by_field_name("name")
                            base = _read_text(tail, source) if tail else _read_text(sub, source)
                        elif sub.type == "template_type":
                            tname = sub.child_by_field_name("name")
                            base = _read_text(tname, source) if tname else _read_text(sub, source)
                            # O template_argument_list da base carrega genérico
                            # argumentos de tipo (classe Car: public Base<Dep>). O
                            # O manipulador Java (_emit_java_parent_type) emite-os como
                            # referências generic_arg; C++ os abandonou porque nós
                            # emitiu apenas a aresta `herda` no nome base.
                            template_args_node = sub.child_by_field_name("arguments")
                        else:
                            continue
                        if not base:
                            continue
                        base_nid = ensure_named_node(base, line)
                        add_edge(class_nid, base_nid, "inherits", line)
                        # Emita uma referência generic_arg para cada argumento de tipo no
                        # base (Base<Dep> -> Car references Dep). _cpp_collect_type_refs
                        # handles nested/qualified args (Base<std::vector<Dep>>) too.
                        if template_args_node is not None:
                            arg_refs: list[tuple[str, str]] = []
                            for arg in template_args_node.children:
                                if arg.is_named:
                                    _cpp_collect_type_refs(arg, source, True, arg_refs)
                            for ref_name, _role in arg_refs:
                                target_nid = ensure_named_node(ref_name, line)
                                if target_nid != class_nid:
                                    add_edge(class_nid, target_nid, "references",
                                             line, context="generic_arg")

            # Find body and recurse. Ruby pushes its scope segments so nested
            # declarations qualify against the enclosing module/class;
            # ruby_segments is empty for every other language.
            body = _find_body(node, config)
            if body:
                ruby_namespace.extend(ruby_segments)
                try:
                    for child in body.children:
                        walk(child, parent_class_nid=class_nid)
                finally:
                    if ruby_segments:
                        del ruby_namespace[-len(ruby_segments):]
            return

        # Event listener property arrays: $listen = [Event::class => [Listener::class]]
        if (t == "property_declaration"
                and parent_class_nid
                and config.event_listener_properties):
            handled_event_listener = False
            for element in node.children:
                if element.type != "property_element":
                    continue
                prop_name: str | None = None
                array_node = None
                for c in element.children:
                    if c.type == "variable_name":
                        for sc in c.children:
                            if sc.type == "name":
                                prop_name = _read_text(sc, source)
                                break
                    elif c.type == "array_creation_expression":
                        array_node = c
                if (prop_name is None
                        or prop_name not in config.event_listener_properties
                        or array_node is None):
                    continue
                handled_event_listener = True
                for entry in array_node.children:
                    if entry.type != "array_element_initializer":
                        continue
                    event_cls: str | None = None
                    listener_arr = None
                    for sub in entry.children:
                        if sub.type == "class_constant_access_expression" and event_cls is None:
                            for sc in sub.children:
                                if sc.is_named and sc.type in ("name", "qualified_name"):
                                    event_cls = _read_text(sc, source)
                                    break
                        elif sub.type == "array_creation_expression":
                            listener_arr = sub
                    if not event_cls or listener_arr is None:
                        continue
                    for listener_entry in listener_arr.children:
                        if listener_entry.type != "array_element_initializer":
                            continue
                        for item in listener_entry.children:
                            if item.type != "class_constant_access_expression":
                                continue
                            for sc in item.children:
                                if sc.is_named and sc.type in ("name", "qualified_name"):
                                    listener_cls = _read_text(sc, source)
                                    line_no = item.start_point[0] + 1
                                    pending_listen_edges.append((event_cls, listener_cls, line_no))
                                    break
                            break
            if handled_event_listener:
                return

        if (config.ts_module == "tree_sitter_c_sharp"
                and t == "field_declaration"
                and parent_class_nid):
            type_node = node.child_by_field_name("type")
            if type_node is None:
                for child in node.children:
                    if child.type == "variable_declaration":
                        type_node = child.child_by_field_name("type")
                        if type_node is not None:
                            break
            type_info = _read_csharp_type_name(type_node, source)
            if type_info:
                type_name, qualified, qualifier = type_info
                csharp_type_params = _csharp_type_parameters_in_scope(
                    type_node if type_node is not None else node, source
                )
                if not type_name or type_name in csharp_type_params:
                    return
                # Record the field's declared type for the method-scoped
                # receiver tables — the C# twin of java_field_types.
                # Pascal-case only: primitives never own a resolvable method.
                if type_name[:1].isupper():
                    fields = csharp_field_types.setdefault(parent_class_nid, {})
                    for child in node.children:
                        if child.type != "variable_declaration":
                            continue
                        for declarator in child.children:
                            if declarator.type != "variable_declarator":
                                continue
                            name_node = declarator.child_by_field_name("name") or next(
                                (g for g in declarator.children
                                 if g.type == "identifier"),
                                None,
                            )
                            if name_node is not None:
                                fields[_read_text(name_node, source)] = type_name
                line = node.start_point[0] + 1
                # Walk the whole type expression rather than only its outer name, so
                # `Box<Widget>` yields the Box field ref AND the Widget generic_arg ref.
                # Reading just the outer name left every generic argument in field
                # position unlinked -- `IDbContextFactory<SomeContext>` lost SomeContext,
                # and `Mock<IThing>` lost IThing across entire test suites. The C#
                # property_declaration handler below and the tree_sitter_java
                # field_declaration handler beside it already do exactly this; C# fields
                # were the odd one out.
                refs: list[tuple[str, str, bool, str]] = []
                _csharp_collect_type_refs(
                    type_node, source, False, refs, csharp_type_params
                )
                for ref_name, role, ref_qualified, ref_qualifier in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "field"
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != parent_class_nid:
                        metadata = {"ref_token": ref_name}
                        if ref_qualified:
                            metadata["qualified"] = True
                        if ref_qualifier:
                            metadata["ref_qualifier"] = ref_qualifier
                        add_edge(parent_class_nid, target_nid, "references",
                                 line, context=ctx, metadata=metadata)
            return

        if (config.ts_module == "tree_sitter_c_sharp"
                and t == "property_declaration"
                and parent_class_nid):
            # Propriedades automáticas C# (`public Widget Main { get; set; }`) são as
            # maneira idiomática de declarar estado, mas apenas field_declaration foi
            # manipulado - portanto, os tipos de propriedade não produziram arestas de referência. Ao contrário de um
            # campo, uma propriedade expõe seu tipo diretamente no nó (sem
            # wrapper de declaração_variável), então leia-o diretamente do `type`
            # campo. Use _csharp_collect_type_refs (como Java/PHP/Kotlin
            # irmãos) então `List<Widget>` produz tanto o campo List ref quanto o
            # Widget generic_arg ref.
            # A property becomes a node, the way a C++ data member does. Fields
            # stay out: the id recipe casefolds and strips leading underscores, so
            # `_count` and `Count` normalize to the same id, and emitting both
            # would hand the node to whichever the parser reached first — in
            # practice the private backing field, hiding the public member behind
            # it. See for the follow-up.
            prop_node_name = node.child_by_field_name("name")
            if prop_node_name is not None:
                property_name = _read_text(prop_node_name, source)
                if property_name:
                    property_line = node.start_point[0] + 1
                    property_nid = _make_id(parent_class_nid, property_name)
                    if property_nid not in seen_ids:
                        add_node(property_nid, property_name, property_line)
                        add_edge(parent_class_nid, property_nid, "defines",
                                 property_line, context="field")
            type_node = node.child_by_field_name("type")
            if type_node is not None:
                # Record the property's declared type for the method-scoped
                # receiver tables, like a field: `Main.Render()` on a
                # `public Widget Main { get; set; }` types Main as Widget.
                prop_name_node = node.child_by_field_name("name")
                prop_type = _csharp_receiver_type_name(type_node, source)
                if prop_name_node is not None and prop_type:
                    csharp_field_types.setdefault(parent_class_nid, {})[
                        _read_text(prop_name_node, source)
                    ] = prop_type
                line = node.start_point[0] + 1
                refs: list[tuple[str, str, bool, str]] = []
                _csharp_collect_type_refs(type_node, source, False, refs)
                for ref_name, role, qualified, qualifier in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "field"
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != parent_class_nid:
                        metadata = {"ref_token": ref_name}
                        if qualified:
                            metadata["qualified"] = True
                        if qualifier:
                            metadata["ref_qualifier"] = qualifier
                        add_edge(parent_class_nid, target_nid, "references",
                                 line, context=ctx, metadata=metadata)
            return

        if (config.ts_module == "tree_sitter_java"
                and t == "field_declaration"
                and parent_class_nid):
            type_node = node.child_by_field_name("type")
            if type_node is not None:
                receiver_type = _java_receiver_type_name(type_node, source)
                if receiver_type:
                    fields = java_field_types.setdefault(parent_class_nid, {})
                    for field_name in _java_declarator_names(node, source):
                        fields[field_name] = receiver_type
                line = node.start_point[0] + 1
                refs: list[tuple[str, str]] = []
                _java_collect_type_refs(type_node, source, False, refs)
                for ref_name, role in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "field"
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != parent_class_nid:
                        add_edge(parent_class_nid, target_nid, "references",
                                 line, context=ctx)
            return

        if (config.ts_module == "tree_sitter_java"
                and t == "annotation_type_element_declaration"
                and parent_class_nid):
            type_node = node.child_by_field_name("type")
            line = node.start_point[0] + 1
            refs: list[tuple[str, str]] = []
            _java_collect_type_refs(
                type_node, source, False, refs, preserve_qualified=True
            )
            for ref_name, role in refs:
                ctx = "generic_arg" if role == "generic_arg" else "return_type"
                target_nid = ensure_named_node(ref_name, line)
                if target_nid != parent_class_nid:
                    add_edge(parent_class_nid, target_nid, "references",
                             line, context=ctx)
            return

        if (config.ts_module == "tree_sitter_php"
                and t == "property_declaration"
                and parent_class_nid):
            for c in node.children:
                if c.type not in ("named_type", "primitive_type", "nullable_type",
                                   "union_type", "intersection_type", "optional_type"):
                    continue
                line = node.start_point[0] + 1
                refs: list[tuple[str, str]] = []
                _php_collect_type_refs(c, source, False, refs)
                for ref_name, role in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "field"
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != parent_class_nid:
                        add_edge(parent_class_nid, target_nid, "references", line, context=ctx)
                break
            return

        if (config.ts_module == "tree_sitter_kotlin"
                and t == "property_declaration"):
            # Field-type references stay class-gated: top-level properties keep
            # their pre- (no-references) behavior unchanged.
            if parent_class_nid:
                type_node = _kotlin_property_type_node(node)
                if type_node is not None:
                    line = node.start_point[0] + 1
                    refs: list[tuple[str, str]] = []
                    _kotlin_collect_type_refs(type_node, source, False, refs)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "field"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != parent_class_nid:
                            add_edge(parent_class_nid, target_nid, "references", line, context=ctx)
            # seed the initializer into initializer_nodes so walk_calls
            # collects its calls (`val repo = createRepo()`), which previously
            # died at the `return` below. Seeding the WHOLE expression (not just
            # call_types) lets walk_calls recurse into nested argument calls
            # (`HttpClient(base())`) and lambda bodies; a literal initializer
            # (`val plain = 5`) contains no call and yields nothing. The
            # explicit type, if any, lives inside variable_declaration BEFORE
            # the `=`, so post-`=` named children are only the initializer.
            # Top-level properties attribute to the file node.
            owner_nid = parent_class_nid or file_nid
            seen_eq = False
            for child in node.children:
                if not child.is_named:
                    seen_eq = seen_eq or child.type == "="
                    continue
                if seen_eq:                              # `= expr` initializer
                    initializer_nodes.append((owner_nid, child))
                elif child.type == "property_delegate":  # `by lazy { ... }` / any delegate
                    for sub in child.children:
                        if sub.is_named:
                            initializer_nodes.append((owner_nid, sub))
            return

        if (config.ts_module == "tree_sitter_swift"
                and t == "property_declaration"
                and parent_class_nid):
            line = node.start_point[0] + 1
            prop_type: str | None = None
            type_anno = _swift_property_type_node(node)
            if type_anno is not None:
                refs: list[tuple[str, str]] = []
                _swift_collect_type_refs(type_anno, source, False, refs)
                for ref_name, role in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "field"
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != parent_class_nid:
                        add_edge(parent_class_nid, target_nid, "references", line, context=ctx)
                    if prop_type is None and role == "type":
                        prop_type = ref_name
            # Estágio 1: percorre o inicializador para uma chamada do construtor
            # (`let vm = VM()`) produz uma aresta de chamadas. Estágio 2a: quando o
            # propriedade não tem anotação de tipo, inferir seu tipo a partir do
            # construtor para que `vm.update()` posteriormente resolva para VM.
            pending_factory: tuple[str, str] | None = None
            for child in node.children:
                if child.type in config.call_types:
                    initializer_nodes.append((parent_class_nid, child))
                    if prop_type is None:
                        ctor = _swift_constructor_type(child, source)
                        if ctor is not None:
                            prop_type = ctor
                        else:
                            # `let x = Factory.make()` — no in-file type;
                            # stash the label-only binding for corpus-side
                            # resolution against make's plain return type.
                            pending_factory = _swift_factory_call(child, source)
                # Estágio 2b: `let x = Type.shared` (ou qualquer `Type.staticProp`)
                # liga x ao Type por meio de um acesso de membro estático, que é um
                # navigation_expression, não uma chamada de construtor. Inferir o tipo de x de
                # o cabeçalho maiúsculo para que mais tarde chamadas `x.method()` resolvam para Type. Esse
                # é o idioma singleton (`Type.shared`) armazenado em cache em uma var local e
                # chamado em uma linha subsequente — extremamente comum em Swift.
                elif child.type == "navigation_expression" and prop_type is None:
                    head = child.children[0] if child.children else None
                    if head is not None and head.type == "simple_identifier":
                        htext = _read_text(head, source)
                        if htext and htext[:1].isupper():
                            prop_type = htext
            # `@Environment(Store.self) var store` names the property's
            # type only inside the attribute argument (modifiers > attribute),
            # which the direct-children scan above never reaches. Last resort:
            # annotation and constructor inference keep priority.
            if prop_type is None:
                prop_type = _swift_attribute_type_name(node, source)
            prop_name = _swift_property_name(node, source)
            if prop_name and prop_type:
                type_table[prop_name] = prop_type
            elif (prop_name and pending_factory is not None
                  and prop_name not in swift_factory_bindings):
                swift_factory_bindings[prop_name] = pending_factory
            # a computed property (`var body: some View { … }`) or an
            # observed one (`willSet`/`didSet`) carries a body that the branches
            # above never emitted — so the property node AND every call inside it
            # were dropped. For SwiftUI this erases the whole view layer, since
            # `body` is a computed property. Emit a function-like member node and
            # defer its body to the call-walk via function_bodies (mirroring how
            # methods register their bodies). Stored properties have no such body
            # child, so their behaviour is unchanged (no regression).
            comp_bodies = [c for c in node.children
                           if c.type in ("computed_property", "willset_didset_block")]
            if comp_bodies and prop_name:
                prop_nid = _make_id(parent_class_nid, prop_name)
                add_node(prop_nid, f".{prop_name}", line)
                add_edge(parent_class_nid, prop_nid, "method", line)
                for body_block in comp_bodies:
                    function_bodies.append((prop_nid, body_block))
            return

        if (config.ts_module == "tree_sitter_scala"
                and t in ("val_definition", "var_definition")
                and parent_class_nid):
            type_node = node.child_by_field_name("type")
            if type_node is not None:
                line = node.start_point[0] + 1
                refs: list[tuple[str, str]] = []
                _scala_collect_type_refs(type_node, source, False, refs)
                for ref_name, role in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "field"
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != parent_class_nid:
                        add_edge(parent_class_nid, target_nid, "references",
                                 line, context=ctx)
            # falha para que qualquer expressão de chamada no inicializador seja percorrida

        # Scala: `self: Logging with Database =>` (or `this: T =>`) declares a
        # structural precondition on the enclosing type, not a mixin/reference.
        # self_type carries no field names, so the type node is found
        # positionally: the binder identifier is named[0], the type (when
        # present) is named[1]. `self =>` binds a name with no type at all, so
        # len(named) < 2 correctly yields no type node rather than misreading
        # the binder as a type. _scala_collect_type_refs already handles every
        # shape a self-type's type position can take (type_identifier,
        # compound_type for `with`, refinement bodies via compound_type) --
        # reused unchanged.
        if (config.ts_module == "tree_sitter_scala"
                and t == "self_type"
                and parent_class_nid):
            named = [c for c in node.children if c.is_named]
            type_node = named[1] if len(named) >= 2 else None
            if type_node is not None:
                line = node.start_point[0] + 1
                refs: list[tuple[str, str]] = []
                _scala_collect_type_refs(type_node, source, False, refs)
                for ref_name, role in refs:
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != parent_class_nid:
                        add_edge(parent_class_nid, target_nid, "requires", line)
            return

        if (config.ts_module == "tree_sitter_cpp"
                and t == "field_declaration"
                and parent_class_nid):
            # Ignorar protótipos de método (field_declaration com um function_declarator
            # é uma declaração de função-membro, não um membro de dados).
            decls = list(node.children_by_field_name("declarator"))
            is_method = any(
                d.type == "function_declarator"
                or (d.type in ("pointer_declarator", "reference_declarator")
                    and any(c.type == "function_declarator" for c in d.children))
                for d in decls
            )
            type_node = node.child_by_field_name("type")
            # A nested type (`class Inner { … };` inside a class body) is a
            # field_declaration whose `type` field IS the class_specifier, so
            # returning from this branch used to drop Inner and everything it
            # declares — silently, with no parse error. Walk it as a
            # class instead: the engine's existing nested-type handling gives
            # it a `contains` edge from the enclosing type. The declarator loop
            # below still runs, since `class Inner { } inst;` declares a member
            # alongside the type.
            # Only class/struct nested types are recovered here: `enum_specifier`
            # is deliberately not in C++'s `class_types`, so a nested `enum` and
            # its enumerators are still not emitted. That is outside's scope
            # (which is about nested class/struct and C++/CLI) and is left as a
            # known gap rather than widened here.
            is_nested_type = (
                type_node is not None
                and type_node.type in config.class_types
                and type_node.child_by_field_name("body") is not None
            )
            if is_nested_type:
                walk(type_node, parent_class_nid)
            if not is_method and not is_nested_type:
                if type_node is not None:
                    line = node.start_point[0] + 1
                    refs: list[tuple[str, str]] = []
                    _cpp_collect_type_refs(type_node, source, False, refs)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "field"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != parent_class_nid:
                            add_edge(parent_class_nid, target_nid, "references",
                                     line, context=ctx)
            # Emita um nó para cada membro de dados. Use children_by_field_name para que possamos
            # visite apenas os filhos do declarador, não o nó de tipo (o que daria
            # nos forneça o nome do tipo, não o nome do campo). Lida com int x, y; através de
            # vários campos do declarador e static const int MAX = 100; através do
            # init_declarator → recursão do field_identifier em _get_cpp_func_name.
            for decl in decls:
                name = _get_cpp_func_name(decl, source)
                if name:
                    line = decl.start_point[0] + 1
                    field_nid = _make_id(parent_class_nid, name)
                    add_node(field_nid, name, line)
                    add_edge(parent_class_nid, field_nid, "defines", line, context="field")
            return

        # Function types
        if t in config.function_types:
            # Swift deinit/subscript não tem campo de nome - resolva antes do fallback genérico
            if t == "deinit_declaration":
                func_name: str | None = "deinit"
            elif t == "subscript_declaration":
                func_name = "subscript"
            elif config.resolve_function_name_fn is not None:
                # Estilo C/C++: use o declarador
                declarator = node.child_by_field_name("declarator")
                func_name = None
                if declarator:
                    func_name = config.resolve_function_name_fn(declarator, source)
            else:
                name_node = node.child_by_field_name(config.name_field)
                if name_node is None:
                    for child in node.children:
                        if child.type in config.name_fallback_child_types:
                            name_node = child
                            break
                func_name = _read_text(name_node, source) if name_node else None

            if not func_name:
                return
            sanitized_name = (
                config.sanitize_symbol_name_fn(func_name)
                if config.sanitize_symbol_name_fn is not None
                else func_name
            )
            # Um nome que normaliza para nada recolhe `_make_id(prefix, name)`
            # no prefixo (derivado do caminho absoluto), vazando o caminho de varredura e
            # colidindo com o nó de arquivo/classe. Nenhum sinal grafo; pular.
            if not normalize_id(sanitized_name):
                return

            line = node.start_point[0] + 1
            if parent_class_nid:
                func_nid = _make_id(parent_class_nid, sanitized_name)
                if config.ts_module == "tree_sitter_python":
                    func_nid = _python_underscore_salted_nid(
                        func_nid, sanitized_name, python_underscore_groups
                    )
                add_node(func_nid, f".{func_name}()", line)
                add_edge(parent_class_nid, func_nid, "method", line)
            else:
                func_nid = _make_id(stem, sanitized_name)
                if config.ts_module == "tree_sitter_python":
                    func_nid = _python_underscore_salted_nid(
                        func_nid, sanitized_name, python_underscore_groups
                    )
                add_node(func_nid, f"{func_name}()", line)
                add_edge(file_nid, func_nid, "contains", line)
            callable_def_nids.add(func_nid)  # função/método def pode ser chamado
            if config.ts_module == "tree_sitter_python":
                local_bound_names[func_nid] = _python_local_bound_names(node, source)
            elif config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
                local_bound_names[func_nid] = _js_local_bound_names(node, source)

            if config.ts_module == "tree_sitter_python":
                params_node = node.child_by_field_name("parameters")
                for ref_name, role in _python_collect_param_refs(params_node, source):
                    ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != func_nid:
                        edges.append(
                            _semantic_reference_edge(func_nid, target_nid, ctx, str_path, line)
                        )
                return_type_node = node.child_by_field_name("return_type")
                if return_type_node is not None:
                    return_refs: list[tuple[str, str]] = []
                    _python_collect_type_refs(return_type_node, source, False, return_refs)
                    for ref_name, role in return_refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            edges.append(
                                _semantic_reference_edge(func_nid, target_nid, ctx, str_path, line)
                            )

            if config.ts_module == "tree_sitter_c_sharp":
                csharp_type_params = _csharp_type_parameters_in_scope(node, source)
                params_node = node.child_by_field_name("parameters")
                if params_node is not None:
                    for p in params_node.children:
                        if p.type != "parameter":
                            continue
                        type_node = p.child_by_field_name("type")
                        refs: list[tuple[str, str, bool, str]] = []
                        _csharp_collect_type_refs(
                            type_node, source, False, refs, csharp_type_params
                        )
                        for ref_name, role, qualified, qualifier in refs:
                            ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                            target_nid = ensure_named_node(ref_name, line)
                            if target_nid != func_nid:
                                metadata = {"ref_token": ref_name}
                                if qualified:
                                    metadata["qualified"] = True
                                if qualifier:
                                    metadata["ref_qualifier"] = qualifier
                                add_edge(func_nid, target_nid, "references", line,
                                         context=ctx, metadata=metadata)
                return_node = node.child_by_field_name("returns")
                if return_node is not None:
                    refs: list[tuple[str, str, bool, str]] = []
                    _csharp_collect_type_refs(
                        return_node, source, False, refs, csharp_type_params
                    )
                    for ref_name, role, qualified, qualifier in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            metadata = {"ref_token": ref_name}
                            if qualified:
                                metadata["qualified"] = True
                            if qualifier:
                                metadata["ref_qualifier"] = qualifier
                            add_edge(func_nid, target_nid, "references", line,
                                     context=ctx, metadata=metadata)
                for attr_name, qualified, qualifier in _csharp_attribute_names(node, source):
                    target_nid = ensure_named_node(attr_name, line)
                    if target_nid != func_nid:
                        metadata = {"ref_token": attr_name}
                        if qualified:
                            metadata["qualified"] = True
                        if qualifier:
                            metadata["ref_qualifier"] = qualifier
                        add_edge(func_nid, target_nid, "references", line,
                                 context="attribute", metadata=metadata)

            if config.ts_module == "tree_sitter_java":
                params_node = node.child_by_field_name("parameters")
                if params_node is not None:
                    for p in params_node.children:
                        if p.type != "formal_parameter":
                            continue
                        type_node = p.child_by_field_name("type")
                        refs = []
                        _java_collect_type_refs(type_node, source, False, refs)
                        for ref_name, role in refs:
                            ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                            target_nid = ensure_named_node(ref_name, line)
                            if target_nid != func_nid:
                                add_edge(func_nid, target_nid, "references", line, context=ctx)
                return_node = node.child_by_field_name("type")
                if return_node is not None:
                    refs = []
                    _java_collect_type_refs(return_node, source, False, refs)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            add_edge(func_nid, target_nid, "references", line, context=ctx)
                annotation_targets: set[str] = set()
                for anno_name, anno_raw in _java_annotation_names(node, source):
                    # Inline-qualified: keep the dotted name; see the
                    # class-level annotation handling above.
                    target_nid = ensure_named_node(
                        anno_raw if "." in anno_raw else anno_name, line)
                    if target_nid != func_nid and target_nid not in annotation_targets:
                        add_edge(func_nid, target_nid, "references", line, context="attribute")
                        annotation_targets.add(target_nid)
                for ref_name in _java_annotation_class_literal_refs(node, source):
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != func_nid and target_nid not in annotation_targets:
                        add_edge(func_nid, target_nid, "references", line,
                                 context="attribute")
                        annotation_targets.add(target_nid)

            if config.ts_module == "tree_sitter_php":
                params_container = None
                for c in node.children:
                    if c.type == "formal_parameters":
                        params_container = c
                        break
                if params_container is not None:
                    for p in params_container.children:
                        # PHP 8 constructor property promotion (`__construct(private
                        # Repo $repo)`) analisa o parâmetro promovido como
                        # property_promotion_parameter, não simple_parameter. Isso é
                        # tipo fica na mesma forma filho nomeada direta, então aceite
                        # ambos aqui; um parâmetro promovido também é um campo de classe.
                        if p.type not in ("simple_parameter", "property_promotion_parameter"):
                            continue
                        is_promoted = p.type == "property_promotion_parameter"
                        type_node = None
                        for sub in p.children:
                            if sub.type in ("named_type", "primitive_type", "nullable_type",
                                             "union_type", "intersection_type", "optional_type"):
                                type_node = sub
                                break
                        refs: list[tuple[str, str]] = []
                        _php_collect_type_refs(type_node, source, False, refs)
                        for ref_name, role in refs:
                            ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                            target_nid = ensure_named_node(ref_name, line)
                            if target_nid != func_nid:
                                add_edge(func_nid, target_nid, "references", line, context=ctx)
                            # Um parâmetro promovido declara um campo de classe real; espelho
                            # a aresta do contexto do campo property_declaration para que o
                            # type também pode ser descoberto como um campo de classe.
                            if is_promoted and parent_class_nid and target_nid != parent_class_nid:
                                fctx = "generic_arg" if role == "generic_arg" else "field"
                                add_edge(parent_class_nid, target_nid, "references",
                                         line, context=fctx)
                return_node = _php_method_return_type_node(node)
                if return_node is not None:
                    refs = []
                    _php_collect_type_refs(return_node, source, False, refs)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            add_edge(func_nid, target_nid, "references", line, context=ctx)

            if config.ts_module == "tree_sitter_kotlin":
                params_container = None
                for c in node.children:
                    if c.type == "function_value_parameters":
                        params_container = c
                        break
                if params_container is not None:
                    for p in params_container.children:
                        if p.type != "parameter":
                            continue
                        param_type_node = None
                        for sub in p.children:
                            if sub.type in ("user_type", "nullable_type", "type_reference"):
                                param_type_node = sub
                                break
                        refs: list[tuple[str, str]] = []
                        _kotlin_collect_type_refs(param_type_node, source, False, refs)
                        for ref_name, role in refs:
                            ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                            target_nid = ensure_named_node(ref_name, line)
                            if target_nid != func_nid:
                                add_edge(func_nid, target_nid, "references", line, context=ctx)
                return_type_node = _kotlin_function_return_type_node(node)
                if return_type_node is not None:
                    refs = []
                    _kotlin_collect_type_refs(return_type_node, source, False, refs)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            add_edge(func_nid, target_nid, "references", line, context=ctx)

            if config.ts_module == "tree_sitter_swift":
                for p in node.children:
                    if p.type != "parameter":
                        continue
                    type_node = p.child_by_field_name("type")
                    refs: list[tuple[str, str]] = []
                    _swift_collect_type_refs(type_node, source, False, refs)
                    param_type: str | None = None
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            add_edge(func_nid, target_nid, "references", line, context=ctx)
                        if param_type is None and role == "type":
                            param_type = ref_name
                    # Stage 2a: record param name -> type (flat per-file
                    # mesa; parâmetros posteriores com o mesmo nome vencem, o que é bom
                    # para a resolução de chamada de membro de profundidade 1 que fazemos).
                    if param_type:
                        name_node = p.child_by_field_name("name")
                        pname = _read_text(name_node, source) if name_node else None
                        if pname:
                            type_table[pname] = param_type
                return_node = node.child_by_field_name("return_type")
                if return_node is not None:
                    refs = []
                    _swift_collect_type_refs(return_node, source, False, refs)
                    # a plain concrete return (`-> Type`, node type
                    # user_type — NOT `some P`/`[T]`/`T?`, which parse as
                    # opaque_type/array_type/optional_type) with exactly one
                    # role=="type" ref is marked so the factory-receiver pass
                    # can read the method's return label corpus-side.
                    plain_return = (return_node.type == "user_type"
                                    and sum(1 for _, r in refs if r == "type") == 1)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            add_edge(func_nid, target_nid, "references", line,
                                     context=ctx,
                                     metadata={"swift_plain_return": True}
                                     if plain_return and role == "type" else None)

            if (config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript")
                    and func_name == "constructor"):
                params_node = node.child_by_field_name("parameters")
                if params_node is not None:
                    for p in params_node.children:
                        if p.type != "required_parameter":
                            continue
                        has_modifier = any(
                            c.type in ("accessibility_modifier", "readonly")
                            for c in p.children
                        )
                        if not has_modifier:
                            continue
                        name_n = p.child_by_field_name("pattern")
                        type_n = p.child_by_field_name("type")
                        if name_n is None or type_n is None:
                            continue
                        pname = _read_text(name_n, source)
                        for tc in type_n.children:
                            if tc.type == "type_identifier":
                                ptype = _read_text(tc, source)
                                if pname and ptype:
                                    type_table[pname] = ptype
                                break

            if config.ts_module in ("tree_sitter_c", "tree_sitter_cpp"):
                collect = (_cpp_collect_type_refs if config.ts_module == "tree_sitter_cpp"
                           else _c_collect_type_refs)
                return_node = node.child_by_field_name("type")
                if return_node is not None:
                    refs: list[tuple[str, str]] = []
                    collect(return_node, source, False, refs)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            add_edge(func_nid, target_nid, "references", line, context=ctx)
                # function_declarator pode ser encapsulado em declaradores de ponteiro/referência
                decl = node.child_by_field_name("declarator")
                while decl is not None and decl.type in (
                        "pointer_declarator", "reference_declarator"):
                    decl = decl.child_by_field_name("declarator")
                if decl is not None and decl.type == "function_declarator":
                    params_node = decl.child_by_field_name("parameters")
                    if params_node is not None:
                        for p in params_node.children:
                            if p.type != "parameter_declaration":
                                continue
                            ptype = p.child_by_field_name("type")
                            if ptype is None:
                                continue
                            refs = []
                            collect(ptype, source, False, refs)
                            for ref_name, role in refs:
                                ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                                target_nid = ensure_named_node(ref_name, line)
                                if target_nid != func_nid:
                                    add_edge(func_nid, target_nid, "references",
                                             line, context=ctx)

            if config.ts_module == "tree_sitter_scala":
                params_node = None
                for c in node.children:
                    if c.type == "parameters":
                        params_node = c
                        break
                if params_node is not None:
                    for p in params_node.children:
                        if p.type != "parameter":
                            continue
                        ptype = p.child_by_field_name("type")
                        if ptype is None:
                            continue
                        refs: list[tuple[str, str]] = []
                        _scala_collect_type_refs(ptype, source, False, refs)
                        for ref_name, role in refs:
                            ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                            target_nid = ensure_named_node(ref_name, line)
                            if target_nid != func_nid:
                                add_edge(func_nid, target_nid, "references",
                                         line, context=ctx)
                return_node = node.child_by_field_name("return_type")
                if return_node is not None:
                    refs = []
                    _scala_collect_type_refs(return_node, source, False, refs)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            add_edge(func_nid, target_nid, "references",
                                     line, context=ctx)

            body = _find_body(node, config)
            # JS/TS: capture callable members assigned directly in a function
            # body. Besides constructor-style `this.X = fn`, factories commonly
            # create an object literal and assign its public surface with
            # `api.X = fn`. These statements otherwise live only in a body that
            # is walked for calls, so their symbols vanish from the graph.
            if body is not None and config.ts_module in (
                "tree_sitter_javascript", "tree_sitter_typescript"
            ):
                function_owner_nid = parent_class_nid if parent_class_nid else func_nid
                object_bindings: dict[str, object] = {}
                for stmt in body.children:
                    if stmt.type not in ("lexical_declaration", "variable_declaration"):
                        continue
                    for declarator in stmt.children:
                        if declarator.type != "variable_declarator":
                            continue
                        name = declarator.child_by_field_name("name")
                        value = declarator.child_by_field_name("value")
                        if name is not None and name.type == "identifier" \
                                and value is not None and value.type == "object":
                            object_bindings[_read_text(name, source)] = declarator
                # A factory object gets one owner node and one `contains` edge no
                # matter how many methods hang off it. add_node dedups on id, but
                # add_edge does not, so without this guard N assigned methods would
                # emit N identical `contains` edges (the flood warns against).
                contained_owners: set[str] = set()
                for stmt in body.children:
                    if stmt.type != "expression_statement":
                        continue
                    assign = next((c for c in stmt.children
                                   if c.type == "assignment_expression"), None)
                    if assign is None:
                        continue
                    val = assign.child_by_field_name("right")
                    if val is None or val.type not in _JS_FUNCTION_VALUE_TYPES:
                        continue
                    tgt = _js_member_assignment_target(
                        assign.child_by_field_name("left"), source)
                    if tgt is None:
                        continue
                    if tgt[0] == "this":
                        owner_nid = function_owner_nid
                    elif tgt[0] == "object" and tgt[1] in object_bindings:
                        object_name = tgt[1]
                        owner_nid = _make_id(function_owner_nid, object_name)
                        owner_line = object_bindings[object_name].start_point[0] + 1
                        add_node(owner_nid, object_name, owner_line)
                        if owner_nid not in contained_owners:
                            contained_owners.add(owner_nid)
                            add_edge(function_owner_nid, owner_nid, "contains", owner_line)
                    else:
                        continue
                    m_name = tgt[2]
                    m_line = stmt.start_point[0] + 1
                    m_nid = _make_id(owner_nid, m_name)
                    add_node(m_nid, f".{m_name}()", m_line)
                    add_edge(owner_nid, m_nid, "method", m_line)
                    m_body = val.child_by_field_name("body")
                    if m_body:
                        function_bodies.append((m_nid, m_body))
            if body:
                if config.ts_module == "tree_sitter_java" and parent_class_nid:
                    java_method_scopes[id(body)] = (node, parent_class_nid)
                if config.ts_module == "tree_sitter_c_sharp" and parent_class_nid:
                    csharp_method_scopes[id(body)] = (node, parent_class_nid)
                function_bodies.append((func_nid, body))
                if config.ts_module in (
                    "tree_sitter_javascript", "tree_sitter_typescript"
                ):
                    _scan_js_nested_function_declarations(
                        body, func_nid, source=source, config=config,
                        add_node=add_node, add_edge=add_edge,
                        callable_def_nids=callable_def_nids,
                        local_bound_names=local_bound_names,
                        function_bodies=function_bodies,
                    )
                if config.ts_module == "tree_sitter_kotlin":
                    # Kotlin anonymous objects (`object: Foo { … }`,
                    # node type `object_literal`). The function branch never
                    # recurses into bodies and object_literal is not a
                    # class_type, so the literal's members (and every call
                    # inside them) got no nodes at all. Scan this body for
                    # object_literal descendants — without crossing a nested
                    # function_declaration boundary (a local fun's literals
                    # are not this function's) and without descending into a
                    # found literal — then emit an owner node per literal and
                    # walk its class_body exactly like the class branch, so
                    # members and their calls flow through the normal
                    # machinery (walk_calls' function_boundary_types already
                    # keep the enclosing function from absorbing them).
                    _kt_literals = []
                    _kt_stack = list(body.children)
                    while _kt_stack:
                        _kt_node = _kt_stack.pop()
                        if _kt_node.type == "function_declaration":
                            continue
                        if _kt_node.type == "object_literal":
                            _kt_literals.append(_kt_node)
                            continue
                        _kt_stack.extend(_kt_node.children)
                    _kt_literals.sort(key=lambda n: n.start_byte)
                    for lit in _kt_literals:
                        lit_line = lit.start_point[0] + 1
                        # Supertypes from the literal's delegation_specifiers,
                        # shaped like the Kotlin class-branch handling:
                        # constructor_invocation -> inherits, bare user_type
                        # (or explicit_delegation) -> implements.
                        lit_bases: list[tuple[str, str]] = []
                        for dchild in lit.children:
                            if dchild.type != "delegation_specifiers":
                                continue
                            for spec in dchild.children:
                                if spec.type != "delegation_specifier":
                                    continue
                                relation = "implements"
                                user_type_node = None
                                for sub in spec.children:
                                    if sub.type == "constructor_invocation":
                                        relation = "inherits"
                                        for inner in sub.children:
                                            if inner.type == "user_type":
                                                user_type_node = inner
                                                break
                                        break
                                    if sub.type == "user_type":
                                        user_type_node = sub
                                        break
                                    if sub.type == "explicit_delegation":
                                        for inner in sub.children:
                                            if inner.type == "user_type":
                                                user_type_node = inner
                                                break
                                        break
                                base = _kotlin_user_type_name(
                                    user_type_node, source
                                )
                                if base:
                                    lit_bases.append((base, relation))
                        obj_label = (
                            lit_bases[0][0] if lit_bases
                            else f"object@L{lit_line}"
                        )
                        obj_nid = _make_id(
                            func_nid, f"object:{obj_label}", f"L{lit_line}"
                        )
                        add_node(obj_nid, obj_label, lit_line)
                        add_edge(func_nid, obj_nid, "contains", lit_line)
                        callable_def_nids.add(obj_nid)
                        callable_class_nids.add(obj_nid)
                        for base, relation in lit_bases:
                            base_nid = ensure_named_node(base, lit_line)
                            if base_nid != obj_nid:
                                add_edge(obj_nid, base_nid, relation, lit_line)
                        lit_body = next(
                            (c for c in lit.children if c.type == "class_body"),
                            None,
                        )
                        if lit_body is not None:
                            for child in lit_body.children:
                                walk(child, parent_class_nid=obj_nid)
            return

        # Funções de seta JS/TS e namespaces C# — manipulação extra específica da linguagem
        if config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
            if _js_extra_walk(node, source, file_nid, stem, str_path,
                              nodes, edges, seen_ids, function_bodies,
                              parent_class_nid, add_node, add_edge,
                              callable_def_nids, local_bound_names,
                              closure_locals_by_body, config=config):
                return

        # TS enum members, and namespace / module containers
        if config.ts_module == "tree_sitter_typescript":
            if _ts_extra_walk(node, source, file_nid, stem, str_path,
                              nodes, edges, seen_ids, function_bodies,
                              parent_class_nid, add_node, add_edge, walk):
                return

        if config.ts_module == "tree_sitter_c_sharp":
            if _csharp_extra_walk(node, source, file_nid, stem, str_path,
                                   nodes, edges, seen_ids, function_bodies,
                                   parent_class_nid, add_node, add_edge, walk,
                                   namespace_stack, scope_stack):
                return

        if config.ts_module == "tree_sitter_swift":
            if _swift_extra_walk(node, source, file_nid, stem, str_path,
                                  nodes, edges, seen_ids, function_bodies,
                                  parent_class_nid, add_node, add_edge,
                                  ensure_named_node):
                return

        if config.ts_module == "tree_sitter_java":
            if _java_extra_walk(node, source, file_nid, stem, str_path,
                                nodes, edges, seen_ids, function_bodies,
                                parent_class_nid, add_node, add_edge, walk):
                return

        if config.ts_module == "tree_sitter_kotlin":
            if _kotlin_extra_walk(node, source, file_nid, stem, str_path,
                                  nodes, edges, seen_ids, function_bodies,
                                  parent_class_nid, add_node, add_edge, walk):
                return

        if config.ts_module == "tree_sitter_ruby":
            if _ruby_extra_walk(node, source, file_nid, stem, str_path,
                                nodes, edges, seen_ids, function_bodies,
                                parent_class_nid, add_node, add_edge, walk,
                                callable_def_nids, callable_class_nids,
                                ruby_namespace):
                return

        # `@property` / `@staticmethod` / `@classmethod` do Python envolve o
        # function_definition interna em um nó `decorated_definition`. O
        # a recursão padrão abaixo limpa parent_class_nid, o que faria com que o
        # método interno a ser emitido com um ID de nó não qualificado de classe (por exemplo
        # `file_baz` em vez de `file_bar_baz`). Isso diverge do
        # id qualificado pela classe que o racional walker usa para o mesmo método
        # docstring, deixando a lógica pendente e o docstring
        # nó órfão. Trate a definição_decorada como transparente
        # wrapper para que parent_class_nid se propague para o nó de função real.
        if t == "decorated_definition":
            # Applying a decorator emitted no edge to the decorator symbol, so
            # `affected <decorator>` reported nothing for the functions it wraps
            #. Emit the same shape TS/JS already emits in
            # `_ts_emit_decorator_edges`: a `references` edge (context=
            # "decorator") from the decorated function/class to each decorator,
            # resolved via ensure_named_node so an imported decorator becomes a
            # sourceless stub the corpus rewire collapses onto its definition.
            # The owner ids mirror the definition branches below/above verbatim,
            # so the edge lands on the node the walk is about to create.
            if config.ts_module == "tree_sitter_python":
                inner = node.child_by_field_name("definition")
                inner_name = None
                if inner is not None:
                    name_node = inner.child_by_field_name("name")
                    inner_name = _read_text(name_node, source) if name_node else None
                # A name that normalizes to nothing is skipped by the definition
                # branches, so an edge to it would dangle.
                if inner_name and normalize_id(inner_name):
                    if inner.type in config.class_types:
                        owner_nid = _make_id(stem, ".".join(namespace_stack), inner_name)
                    elif parent_class_nid:
                        owner_nid = _make_id(parent_class_nid, inner_name)
                    else:
                        owner_nid = _make_id(stem, inner_name)
                    for child in node.children:
                        if child.type != "decorator":
                            continue
                        deco_name = _python_decorator_name(child, source)
                        # Builtin/stdlib decorators are noise: no stub nodes,
                        # no false rewires onto same-named local definitions.
                        if not deco_name or deco_name in _PYTHON_DECORATOR_NOISE:
                            continue
                        deco_line = child.start_point[0] + 1
                        target = ensure_named_node(deco_name, deco_line)
                        if target != owner_nid:
                            add_edge(owner_nid, target, "references", deco_line,
                                     context="decorator")
            for child in node.children:
                walk(child, parent_class_nid=parent_class_nid)
            return

        # a `companion object` is not an attribution scope of its own —
        # its members belong to the enclosing class in Kotlin. The default
        # recurse below would strip parent_class_nid, orphaning companion
        # property initializers (and leaving companion `fun`s file-level).
        # Recurse transparently, entering the class_body's children directly
        # since a bare class_body would itself default-recurse and drop the
        # parent link. Companion `fun`s thereby become class-attributed methods.
        if config.ts_module == "tree_sitter_kotlin" and t == "companion_object":
            for child in node.children:
                if child.type == "class_body":
                    for member in child.children:
                        walk(member, parent_class_nid=parent_class_nid)
                else:
                    walk(child, parent_class_nid=parent_class_nid)
            return

        # tree-sitter ERROR recovery can wrap declarations that plainly
        # sit inside a class body (e.g. the Kotlin grammar choking on a one-line
        # sibling member). The default recurse below deliberately drops
        # parent_class_nid (an unknown wrapper usually IS a scope boundary), but
        # an ERROR node is a parse artifact, not a scope — keep the enclosing
        # class linkage for whatever declarations were recovered inside it.
        if t == "ERROR":
            for child in node.children:
                walk(child, parent_class_nid=parent_class_nid)
            return

        # Default: recurse
        for child in node.children:
            walk(child, parent_class_nid=None)

    walk(root)

    # ── Call-graph pass ───────────────────────────────────────────────────────
    label_to_nid: dict[str, str] = {}     # case-sensitive (Ruby, C#, Java, Kotlin, etc.)
    label_to_nid_ci: dict[str, str] = {}  # case-insensitive (PHP functions/classes)
    # nid -> source_file, para que o guarda de despacho indireto possa informar um local genuíno
    # não exigível (rejeitar) de um símbolo estrangeiro resolvido por importação cuja definição
    # reside em outro arquivo (adie para o resolvedor de arquivos cruzados). Importações nomeadas JS/TS
    # coloque o nó REAL do símbolo importado no mapa de rótulos deste arquivo.
    nid_to_sf: dict[str, str] = {}
    for n in nodes:
        nid_to_sf[n["id"]] = str(n.get("source_file") or "")
        if n.get("type") == "namespace":
            continue
        raw = n["label"]
        normalised = raw.strip("()").lstrip(".")
        label_to_nid[normalised] = n["id"]
        label_to_nid_ci[normalised.lower()] = n["id"]

    seen_call_pairs: set[tuple[str, str]] = set()
    seen_indirect_pairs: set[tuple[str, str]] = set()  # Python indirect_call dedup
    seen_dyn_import_pairs: set[tuple[str, str]] = set()
    seen_static_ref_pairs: set[tuple[str, str, str]] = set()
    seen_helper_ref_pairs: set[tuple[str, str, str]] = set()
    seen_bind_pairs: set[tuple[str, str, str]] = set()
    raw_calls: list[dict] = []  # chamadas não resolvidas para resolução de arquivos cruzados em extract()
    # Ruby: tabela `var -> ClassName` por método de ligações `var = Const.new`,
    # preenchido antes da execução de walk_calls. Permite que raw_calls de chamada de membro carreguem um
    # receiver_type para que a passagem entre arquivos resolva `var.method` por tipo (#ruby).
    ruby_var_types: dict[str, dict[str, str | None]] = {}
    # Fields declared on a SUPERCLASS type receivers in a subclass too:
    # fold each class's table with its ancestors', nearest declaration winning.
    # Local `inherits` edges only - the cross-file half lives in the corpus
    # member-call resolvers, which see the whole graph.
    _local_bases: dict[str, list[str]] = {}
    for _e in edges:
        if _e.get("relation") == "inherits":
            _local_bases.setdefault(_e["source"], []).append(_e["target"])

    def _fields_up_chain(tables: dict, class_nid) -> dict:
        if not class_nid:
            return {}
        merged: dict = {}
        seen: set = set()
        queue = [class_nid]
        while queue:
            cls = queue.pop(0)
            if cls in seen:
                continue
            seen.add(cls)
            for _name, _tname in tables.get(cls, {}).items():
                merged.setdefault(_name, _tname)
            queue.extend(_local_bases.get(cls, []))
        return merged

    java_receiver_types = {
        body_id: _java_method_receiver_types(
            method_node,
            source,
            _fields_up_chain(java_field_types, class_nid),
        )
        for body_id, (method_node, class_nid) in java_method_scopes.items()
    }
    csharp_receiver_types = {
        body_id: _csharp_method_receiver_types(
            method_node,
            source,
            _fields_up_chain(csharp_field_types, class_nid),
        )
        for body_id, (method_node, class_nid) in csharp_method_scopes.items()
    }

    def _emit_indirect_by_name(ident_name: str, loc_node, scope_nid: str,
                               context: str) -> None:
        """Resolve a name that is referenced AS A VALUE to a real callable def and emit
        one INFERRED ``indirect_call`` edge — deferring an unknown / foreign name to the
        cross-file resolver, which applies the single-definition god-node guard and the
        GLOBAL callable-target check. The name is already extracted; scope filtering is
        the CALLER's job: an identifier reference must reject param/local shadows (a bare
        name IS a binding — see ``_emit_indirect_ref``), whereas a ``getattr(obj, "x")``
        string names an ATTRIBUTE and is never shadowed by a local, so that path passes
        the name straight through. ``loc_node`` supplies the source line.
        """
        ref_nid = label_to_nid.get(ident_name)
        # Adie para o resolvedor de arquivos cruzados quando o nome não estiver definido neste arquivo
        # (`from .h import fn`), ou resolve para um símbolo FOREIGN com superfície de importação cujo
        # definição (e capacidade de chamada) reside em outro arquivo (mapa de importações chamado JS/TS
        # o nó real no mapa de rótulos deste arquivo). A passagem entre arquivos aplica o
        # guarda de god node de definição única mais a verificação de alvo chamável GLOBAL, então um
        # estrangeiro não exigível (um const de dados importado) ainda não produz vantagem.
        if ref_nid is None or (
            ref_nid not in callable_def_nids and nid_to_sf.get(ref_nid, "") != str_path
        ):
            raw_calls.append({
                "caller_nid": scope_nid,
                "callee": ident_name,
                "is_member_call": False,
                "indirect": True,
                "context": context,
                "source_file": str_path,
                "source_location": f"L{loc_node.start_point[0] + 1}",
            })
            return
        if ref_nid == scope_nid or ref_nid not in callable_def_nids:
            return  # auto-referência ou um nó de dados LOCAL não solicitável com o mesmo nome - sem aresta
        if ref_nid in callable_class_nids:
            # A class referenced as a value (`select(Model)`, `db.get(Model, id)`,
            # an exception tuple) is a descriptor, not an invocation — no edge.
            return
        if (scope_nid, ref_nid) in seen_call_pairs:
            return  # já é uma chamada direta para esse alvo
        if (scope_nid, ref_nid) in seen_indirect_pairs:
            return
        seen_indirect_pairs.add((scope_nid, ref_nid))
        edges.append({
            "source": scope_nid,
            "target": ref_nid,
            "relation": "indirect_call",
            "context": context,
            "confidence": "INFERRED",
            # 0.85 = "strong inference" on the extraction-spec rubric. The symbol
            # link is direct — the function is named right here — but that it is
            # ever INVOKED is the inference, which is why this is not the 0.95
            # tier. Previously no score was emitted at all and the edge inherited
            # the 0.5 default the rubric forbids.
            "confidence_score": 0.85,
            "source_file": str_path,
            "source_location": f"L{loc_node.start_point[0] + 1}",
            "weight": 1.0,
        })

    def _emit_indirect_ref(ident, scope_nid: str, enclosing_locals, context: str) -> None:
        """A function referenced BY NAME — passed as a call argument, or listed as a
        value in a dispatch table — is an indirect dependency of ``scope_nid``. Emit
        it as a distinct INFERRED ``indirect_call`` (kept out of the precise ``calls``
        relation) only when the name resolves to a real callable and is NOT shadowed
        by a parameter / local binding. A callback defined in another file is deferred
        to the cross-file resolver via an ``indirect`` raw_call carrying its context.
        Language-agnostic; shared by the call-argument and dispatch-table capture
        paths for Python and JS/TS (#1565, #1566).
        """
        if ident is None or ident.type not in ("identifier", "shorthand_property_identifier"):
            return
        ident_name = _read_text(ident, source)
        # sombreamento: uma ligação param/local nomeia um valor local, não o módulo fn
        if ident_name in enclosing_locals or ident_name in ("self", "cls"):
            return
        # An import from outside the corpus binds the name for the whole module, so
        # it shadows in every scope — no unique same-named definition elsewhere in
        # the corpus is what this identifier refers to.
        if ident_name in js_external_imports:
            return
        _emit_indirect_by_name(ident_name, ident, scope_nid, context)

    def _python_dispatch_value_idents(coll_node):
        """Yield the identifier value-nodes of a dict/list/set/tuple literal that are
        function-reference candidates: dict VALUES (never keys), and the elements of a
        list/set/tuple. Nested collections are reached by the caller's own recursion."""
        if coll_node.type == "dictionary":
            for pair in coll_node.children:
                if pair.type == "pair":
                    val = pair.child_by_field_name("value")
                    if val is not None and val.type == "identifier":
                        yield val
        else:  # list / set / tuple
            for el in coll_node.children:
                if el.type == "identifier":
                    yield el

    def _python_ref_value_idents(value_node):
        """Identifiers on the VALUE side of an assignment RHS or a return: a bare name
        (`cb = handler`, `return handler`) or the elements of a bare unpack
        (`a, b = f, g`). A collection LITERAL on the RHS (`cb = [f]`, `cb = (f, g)`) is a
        dispatch table reached by the normal recursion, so it is not handled here."""
        if value_node is None:
            return
        if value_node.type == "identifier":
            yield value_node
        elif value_node.type == "expression_list":
            for ch in value_node.children:
                if ch.type == "identifier":
                    yield ch

    def _getattr_ref_name(call_node):
        """If ``call_node`` is a builtin ``getattr(obj, "name"[, default])`` whose name
        argument is a PLAIN string literal, return ``(name, string_node)``: the string
        names an attribute looked up by that exact name, so it resolves to a callable
        def of the same label. A dynamic name — a variable, an f-string, a concatenation,
        any expression — is not statically resolvable and yields ``None`` (no edge is
        manufactured), as do the 1-arg form and ``obj.getattr(...)`` (a method, not the
        builtin). Unlike an identifier, a string is an attribute name and is never
        shadowed by a param/local, so callers resolve it without the shadow guard.
        """
        fn = call_node.child_by_field_name("function")
        if fn is None or fn.type != "identifier" or _read_text(fn, source) != "getattr":
            return None
        args = call_node.child_by_field_name("arguments")
        if args is None:
            return None
        positional = [c for c in args.children
                      if c.is_named and c.type not in ("keyword_argument", "comment")]
        if len(positional) < 2:
            return None
        name_node = positional[1]
        if name_node.type != "string" or any(
            ch.type == "interpolation" for ch in name_node.children
        ):
            return None  # variável, string F, concatenação ou expressão - dinâmica
        content = next(
            (ch for ch in name_node.children if ch.type == "string_content"), None)
        if content is None:
            return None  # string vazia "" — sem nome de atributo
        return _read_text(content, source), name_node

    def _php_class_const_scope(n) -> str | None:
        scope = n.child_by_field_name("scope")
        if scope is None:
            for c in n.children:
                if c.is_named and c.type in ("name", "qualified_name", "identifier"):
                    scope = c
                    break
        if scope is None:
            return None
        return _read_text(scope, source)

    _tracked_body_ids: set[object] = set()
    _JS_CLOSURE_TYPES = ("arrow_function", "function_expression")
    # nested NAMED functions get the same descent as closures. walk()
    # appends only the OUTER declaration's body to function_bodies and never
    # recurses into it, so `function outer(){ function inner(){ helper() } }`
    # hit this boundary and dropped every call (and dynamic import) inside
    # inner. Nested declarations are never in function_bodies, so the
    # _tracked_body_ids guard below still prevents double-walking the
    # top-level ones (those are entered via their own function_bodies entry).
    _JS_DESCEND_TYPES = _JS_CLOSURE_TYPES + (
        "function_declaration", "generator_function_declaration",
        "generator_function")

    def walk_calls(
        node,
        caller_nid: str,
        # Java: flat name -> type. C#: the (scoped bindings, field base) pair
        # from _csharp_method_receiver_types, resolved positionally.
        receiver_types: dict[str, str] | tuple | None = None,
        extra_locals: frozenset[str] = frozenset(),
    ) -> None:
        if node.type in config.function_boundary_types:
            # JS/TS: um fechamento in-line/retornado não rastreado separadamente em
            # caso contrário, function_bodies abandonaria suas chamadas neste limite.
            # Desça até ele com o chamador anexo, então `return () =>
            # svc.doThing()` vincula-se ao chamador. Fechamentos rastreados
            # (setas atribuídas const) são percorridas com seu próprio nid - pule para
            # avoid double-counting.
            if (config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript")
                    and node.type in _JS_DESCEND_TYPES):
                body = node.child_by_field_name("body")
                if body is not None and body not in _tracked_body_ids:
                    # This closure's own params/locals (`(r) => c.get(r)`) are
                    # scoped to it, not to the enclosing caller_nid — but its
                    # calls ARE attributed to caller_nid right here, so a bare
                    # reference to one of them (e.g. passed on as a call
                    # argument) must still be recognized as local, not resolved
                    # against an unrelated same-named definition elsewhere in
                    # the corpus. Fold this closure's own bindings into
                    # extra_locals for its subtree only; deeper untracked
                    # closures compound the same way on their own recursion.
                    closure_locals = extra_locals | _js_local_bound_names(node, source)
                    for child in node.children:
                        walk_calls(child, caller_nid, receiver_types, closure_locals)
            return

        # CommonJS imports are valid at any lexical depth.  The module-level
        # pass records top-level require() declarations; this pass owns function
        # bodies, so detect lazy/cycle-breaking requires here and attribute the
        # dependency to the enclosing callable rather than silently dropping it.
        if (config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript")
                and node.type in ("lexical_declaration", "variable_declaration")):
            _require_imports_js(node, source, caller_nid, stem, edges, str_path)

        if node.type in config.call_types:
            # JS/TS dynamic imports: await import('./foo.js')
            if config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
                if _dynamic_import_js(node, source, caller_nid, str_path,
                                      edges, seen_dyn_import_pairs):
                    # Ainda recorre a filhos (import().then(...) pode ter chamadas)
                    for child in node.children:
                        walk_calls(child, caller_nid, receiver_types, extra_locals)
                    return

            callee_name: str | None = None
            is_member_call: bool = False
            is_this_field_call: bool = False
            swift_receiver: str | None = None
            member_receiver: str | None = None
            kotlin_qualified_prefix: str | None = None
            csharp_qualified_prefix: str | None = None

            # Special handling per language
            if config.ts_module == "tree_sitter_swift":
                # Swift: o primeiro filho pode ser simple_identifier ou navigation_expression
                first = node.children[0] if node.children else None
                if first:
                    if first.type == "simple_identifier":
                        callee_name = _read_text(first, source)
                    elif first.type == "navigation_expression":
                        is_member_call = True
                        for child in first.children:
                            if child.type == "navigation_suffix":
                                for sc in child.children:
                                    if sc.type == "simple_identifier":
                                        callee_name = _read_text(sc, source)
                        # capture o receptor para que a passagem entre arquivos possa
                        # resolva-o através da tabela de tipos do arquivo.
                        recv_node = first.children[0] if first.children else None
                        swift_receiver = _swift_receiver_name(recv_node, source)
            elif config.ts_module == "tree_sitter_kotlin":
                # Kotlin: o primeiro filho pode ser simple_identifier/identifier ou
                # navigation_expression. PyPI's `tree_sitter_kotlin` produces
                # `identifier` para nós identificadores simples; gramática mais antiga
                # versões (incluindo a JVM `io.github.bonede:tree-sitter-kotlin`
                # binding) produce `simple_identifier`. Accept both.
                first = node.children[0] if node.children else None
                if first:
                    if first.type in ("simple_identifier", "identifier"):
                        callee_name = _read_text(first, source)
                    elif first.type == "navigation_expression":
                        is_member_call = True
                        for child in reversed(first.children):
                            if child.type in ("simple_identifier", "identifier"):
                                callee_name = _read_text(child, source)
                                break
                        # `com.example.Foo.bar()` is a NESTED
                        # navigation_expression chain; the last identifier alone
                        # (`bar`) rarely matches in-file, so the call was dropped
                        # (the shared cross-file pass skips member calls). When
                        # EVERY chain segment is a plain identifier and there are
                        # >= 3 (a real dotted FQN, not `recv.method()`), stamp the
                        # dotted prefix for _resolve_kotlin_qualified_calls.
                        # member_receiver is deliberately NOT set: an uppercase
                        # receiver would trip the capitalized-receiver deferral
                        # below and regress in-file `Foo.bar()` resolution.
                        segments = _kotlin_nav_identifier_segments(first, source)
                        if segments is not None and len(segments) >= 3:
                            kotlin_qualified_prefix = ".".join(segments[:-1])
            elif config.ts_module == "tree_sitter_scala":
                # Scala: first child
                first = node.children[0] if node.children else None
                if first:
                    if first.type == "identifier":
                        callee_name = _read_text(first, source)
                    elif first.type == "field_expression":
                        is_member_call = True
                        field = first.child_by_field_name("field")
                        if field:
                            callee_name = _read_text(field, source)
                        else:
                            for child in reversed(first.children):
                                if child.type == "identifier":
                                    callee_name = _read_text(child, source)
                                    break
            elif config.ts_module == "tree_sitter_php" and node.type == "object_creation_expression":
                # PHP `new Foo(...)` keeps the class in a bare name/qualified_name
                # child (no field), so the generic call path never names it and a
                # class a method only constructs stayed unlinked - the PHP twin of
                # Java's and C#'s. The types this loses are the ones
                # handed straight to something else (`$bus->dispatch(new Cmd(...))`).
                # A qualified `new \App\Bar()` names the last segment, matching how
                # the PHP namespace pass keys classes; `new $cls()` is dynamic and
                # `new self()`/`static`/`parent` name no other class - all stay
                # unnamed rather than minting junk edges.
                for _oce_child in node.children:
                    if _oce_child.type in ("name", "qualified_name"):
                        _oce_text = _read_text(_oce_child, source).rsplit("\\", 1)[-1]
                        if _oce_text.lower() not in ("self", "static", "parent"):
                            callee_name = _oce_text
                        break
            elif config.ts_module == "tree_sitter_c_sharp" and node.type == "object_creation_expression":
                # `new Foo(...)` keeps the constructed type in the `type` field, so
                # the invocation path below never sees it and a type a method only
                # constructs stays unlinked — the C# twin of the Java gap in.
                # Types reached solely through a method body are exactly the ones
                # this misses: message classes handed straight to a bus
                # (`Send(new OrderPlaced { ... })`) and locally built collaborators.
                # `_read_csharp_type_name` drops the generic arguments and the
                # namespace qualifier, so `new A.B.Cache<string>()` names `Cache`.
                # Target-typed `new()` parses as `implicit_object_creation_expression`
                # and stays out of `call_types`: naming it needs the declared type of
                # whatever it is being assigned to, which is a separate problem.
                # A qualifier written in source is kept for
                # `_resolve_csharp_qualified_calls`, so `new A.B.Cache()` can still
                # pick one of several `Cache` classes instead of hitting the
                # ambiguity guard on the bare name.
                type_info = _read_csharp_type_name(node.child_by_field_name("type"), source)
                if type_info and type_info[0]:
                    callee_name = type_info[0]
                    if type_info[1] and type_info[2]:
                        csharp_qualified_prefix = type_info[2]
            elif config.ts_module == "tree_sitter_c_sharp" and node.type == "invocation_expression":
                # C#: a função invocada é o campo `function`. Uma chamada de membro
                # `recv.Method(...)` é uma member_access_expression (receptor em seu
                # campo `expressão`, método em `nome`). Capture um identificador simples
                # ou `this` receiver + set is_member_call para que o receptor digite
                # resolvedor (_resolve_csharp_member_calls) pode vinculá-lo ao
                # tipo declarado do receptor. Sem isso, o nome do método simples correspondia
                # qualquer método com o mesmo nome no corpus, resolvendo silenciosamente
                # `_server.Save()` para um `Cache.Save()` não relacionado.
                fn_node = node.child_by_field_name("function")
                if fn_node is not None and fn_node.type == "member_access_expression":
                    mname = fn_node.child_by_field_name("name")
                    recv = fn_node.child_by_field_name("expression")
                    if mname is not None:
                        callee_name = _read_text(mname, source)
                        is_member_call = True
                        if recv is not None and recv.type == "identifier":
                            member_receiver = _read_text(recv, source)
                        elif recv is not None and recv.type in ("this", "this_expression"):
                            member_receiver = "this"
                        elif recv is not None and recv.type in ("base", "base_expression"):
                            # base.M(): resolved against the caller's single
                            # resolvable base class in the cross-file pass.
                            member_receiver = "base"
                        elif recv is not None and recv.type == "member_access_expression":
                            # this.field.M(): the explicit-`this` field access is
                            # typed exactly like a bare `field.M()` via the file
                            # table; any other chained receiver stays untyped
                            # (the resolver bails rather than guessing).
                            inner = recv.child_by_field_name("expression")
                            fname = recv.child_by_field_name("name")
                            if (
                                inner is not None
                                and inner.type in ("this", "this_expression")
                                and fname is not None
                                and fname.type == "identifier"
                            ):
                                member_receiver = _read_text(fname, source)
                elif fn_node is not None and fn_node.type == "identifier":
                    callee_name = _read_text(fn_node, source)
                else:
                    # Fallback: original name-field / first-named-child scan.
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        callee_name = _read_text(name_node, source)
                    else:
                        for child in node.children:
                            if child.is_named:
                                raw = _read_text(child, source)
                                if "." in raw:
                                    callee_name = raw.split(".")[-1]
                                    is_member_call = True
                                    parts = raw.split(".")
                                    if len(parts) == 2 and parts[0]:
                                        member_receiver = parts[0]
                                else:
                                    callee_name = raw
                                break
                # C#: emit a `references[generic_arg]` edge for every type
                # argument at the call site (`recv.Do<T>()`, the
                # `services.AddScoped<ISvc, Impl>()` DI shape, static
                # `Foo<IBar>()`). The property/return/parameter branches
                # already walk their declared type for the same reason; the
                # call-site branch didn't, so the type arguments never
                # became nodes and dependency edges were silently erased
                #. The C# class_declaration's field_declaration and
                # property_declaration branches above are the direct
                # analogue. The call-site function carries its type-arg list
                # either as a `type_argument_list` child on a `generic_name`
                # (static call) or as the same child on the
                # `member_access_expression`'s `name` `generic_name` (member
                # call); the fallback path uses raw text and never sees the
                # structured type-arg list. The class declaration's
                # field_declaration case is closed by the parallel fix in
                #; this branch covers what that PR deliberately left
                # out.
                if fn_node is not None:
                    call_tal = None
                    if fn_node.type == "member_access_expression":
                        ma_name = fn_node.child_by_field_name("name")
                        if ma_name is not None and ma_name.type == "generic_name":
                            for tal_child in ma_name.children:
                                if tal_child.type == "type_argument_list":
                                    call_tal = tal_child
                                    break
                    elif fn_node.type == "generic_name":
                        for tal_child in fn_node.children:
                            if tal_child.type == "type_argument_list":
                                call_tal = tal_child
                                break
                    if call_tal is not None:
                        call_type_params = _csharp_type_parameters_in_scope(node, source)
                        call_line = node.start_point[0] + 1
                        for call_arg in call_tal.children:
                            if not call_arg.is_named:
                                continue
                            call_refs: list[tuple[str, str, bool, str]] = []
                            _csharp_collect_type_refs(
                                call_arg, source, True, call_refs, call_type_params
                            )
                            for call_ref_name, _call_role, call_qualified, call_qualifier in call_refs:
                                call_target = ensure_named_node(call_ref_name, call_line)
                                if call_target == caller_nid:
                                    continue
                                call_meta = {"ref_token": call_ref_name}
                                if call_qualified:
                                    call_meta["qualified"] = True
                                if call_qualifier:
                                    call_meta["ref_qualifier"] = call_qualifier
                                add_edge(caller_nid, call_target, "references",
                                         call_line, context="generic_arg",
                                         metadata=call_meta)
            elif config.ts_module == "tree_sitter_php":
                # PHP: distinguish call expression subtypes
                if node.type == "function_call_expression":
                    func_node = node.child_by_field_name("function")
                    if func_node:
                        callee_name = _read_text(func_node, source)
                elif node.type == "scoped_call_expression":
                    # Static method call: Helper::format() → callee = "Helper"
                    scope_node = node.child_by_field_name("scope")
                    if scope_node:
                        callee_name = _read_text(scope_node, source)
                else:
                    # member_call_expression: $obj->method()
                    is_member_call = True
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        callee_name = _read_text(name_node, source)
            elif config.ts_module == "tree_sitter_cpp":
                # C++: campo de função, depois field_expression/qualified_identifier
                func_node = node.child_by_field_name(config.call_function_field) if config.call_function_field else None
                if func_node:
                    if func_node.type == "identifier":
                        callee_name = _read_text(func_node, source)
                    elif func_node.type == "field_expression":
                        # `f.bar()` / `f->bar()` / `this->bar()`: receptor é o
                        # Campo `argumento` (objeto), o receptor é o `campo`.
                        # Capture um receptor de identificador simples (ou `this`) para que o
                        # passagem entre arquivos pode resolvê-lo através do tipo do arquivo
                        # mesa; receptores encadeados (`abmethod()`) são deixados para fiança.
                        is_member_call = True
                        name = func_node.child_by_field_name("field")
                        if name:
                            callee_name = _read_text(name, source)
                        obj = func_node.child_by_field_name("argument")
                        if obj is not None and obj.type == "identifier":
                            member_receiver = _read_text(obj, source)
                        elif obj is not None and obj.type == "this":
                            member_receiver = "this"
                    elif func_node.type == "qualified_identifier":
                        # `Foo::bar()`: o escopo (`Foo`) é o tipo de receptor chamado
                        # explicitamente na fonte (EXTRACTED), o nome é o receptor.
                        is_member_call = True
                        name = func_node.child_by_field_name("name")
                        if name:
                            callee_name = _read_text(name, source)
                        scope = func_node.child_by_field_name("scope")
                        if scope is not None:
                            member_receiver = _read_text(scope, source)
            elif config.ts_module == "tree_sitter_java":
                if node.type == "object_creation_expression":
                    # `new Foo(...)` — o tipo construído está no campo `type`,
                    # não `name`, então o caminho genérico não o possui.
                    type_node = node.child_by_field_name("type")
                    if type_node is not None:
                        raw = _read_text(type_node, source).split("<", 1)[0].strip()
                        if raw:
                            callee_name = raw.rsplit(".", 1)[-1]
                elif node.type == "method_invocation":
                    name_node = node.child_by_field_name("name")
                    if name_node is not None:
                        callee_name = _read_text(name_node, source)
                    receiver = node.child_by_field_name("object")
                    if receiver is not None:
                        is_member_call = True
                        if receiver.type == "identifier":
                            member_receiver = _read_text(receiver, source)
                        elif receiver.type == "this":
                            member_receiver = "this"
                        elif receiver.type == "field_access":
                            owner = receiver.child_by_field_name("object")
                            field = receiver.child_by_field_name("field")
                            if owner is not None and owner.type == "this" and field is not None:
                                member_receiver = f"this.{_read_text(field, source)}"
                                is_this_field_call = True
            elif config.ts_module == "tree_sitter_ruby":
                # O nó `call` do Ruby carrega `receiver` e `method` como direto
                # campos (sem nó acessador intermediário), então o acessador genérico
                # modelo não se aplica. Leia-os diretamente e capture um simples
                # receptor (`p` em `p.run`, `Processor` em `Processor.new`) para que o
                # a passagem entre arquivos pode resolver chamadas de membros por tipo de receptor.
                meth = node.child_by_field_name("method")
                if meth is not None:
                    callee_name = _read_text(meth, source)
                recv = node.child_by_field_name("receiver")
                if recv is not None:
                    is_member_call = True
                    if recv.type in ("identifier", "constant"):
                        member_receiver = _read_text(recv, source)
                    elif recv.type == "scope_resolution":
                        # Namespaced receiver `Billing::Processor.call` — keep the whole
                        # constant path. Truncating to the last segment discarded the
                        # namespace, so `ActiveRecord::Base.transaction` bound to
                        # whatever single class named `Base` the corpus defined: the
                        # god-node guard only catches an ambiguous match, not a
                        # unique-but-wrong one.
                        member_receiver = _ruby_const_full_name(recv, source) or None
            else:
                # Generic: get callee from call_function_field (or constructor on new_expression)
                func_node = node.child_by_field_name(config.call_function_field) if config.call_function_field else None
                if func_node is None and node.type == "new_expression":
                    func_node = node.child_by_field_name("constructor")
                if func_node:
                    if func_node.type == "identifier":
                        callee_name = _read_text(func_node, source)
                    elif func_node.type in config.call_accessor_node_types:
                        is_member_call = True
                        if config.call_accessor_field:
                            attr = func_node.child_by_field_name(config.call_accessor_field)
                            if attr:
                                callee_name = _read_text(attr, source)
                        if config.call_accessor_object_field:
                            # Capture um receptor de identificador simples (por exemplo, `ClassName`
                            # em `ClassName.method()`) então chamada de membro entre arquivos
                            # resolution can resolve qualified class-method calls
                            #. Receptores encadeados (`abmethod()`) são ignorados
                            # A MENOS que a cadeia seja `this.field.method()`.
                            obj = func_node.child_by_field_name(config.call_accessor_object_field)
                            if obj is not None and obj.type == "identifier":
                                member_receiver = _read_text(obj, source)
                            elif (
                                config.ts_module == "tree_sitter_python"
                                and obj is not None
                                and obj.type == "call"
                            ):
                                # ``super().method()`` has a call node as its
                                # receiver. Preserve it as a known intra-class
                                # receiver instead of treating it as unresolved.
                                receiver_func = obj.child_by_field_name("function")
                                if (
                                    receiver_func is not None
                                    and receiver_func.type == "identifier"
                                    and _read_text(receiver_func, source) == "super"
                                ):
                                    member_receiver = "super"
                            elif (obj is not None
                                  and obj.type in config.call_accessor_node_types
                                  and config.call_accessor_object_field):
                                inner_obj = obj.child_by_field_name(config.call_accessor_object_field)
                                if inner_obj is not None and inner_obj.type == "this":
                                    inner_prop = obj.child_by_field_name(config.call_accessor_field)
                                    if inner_prop is not None:
                                        member_receiver = _read_text(inner_prop, source)
                                        is_this_field_call = True
                    else:
                        # Tente ler o nó diretamente (por exemplo, o campo de nome Java é o receptor)
                        callee_name = _read_text(func_node, source)

            if callee_name and callee_name not in _LANGUAGE_BUILTIN_GLOBALS:
                # Python member calls defer to receiver-based resolution unless the
                # receiver is known to stay in the current class. Falling back to a
                # bare method name for an unresolved/lowercase receiver (`d.get()` or
                # `self.store.get()`) can bind to an unrelated module function and
                # inflate it into a god node. Qualified class/module calls are
                # recovered later by _resolve_python_member_calls when the receiver
                # supplies enough evidence. Known recall trade:
                # a same-file `x = Thing(); x.method()` no longer gets an edge — it
                # came from the same evidence-free bare-name map and could bind wrong
                # under label collision; local-instantiation receiver typing is a
                # separate follow-up.
                # C#: QUALQUER chamada de membro com um receptor capturado é adiada para o
                # resolvedor digitado pelo receptor - uma correspondência simples de nome de método ignora o
                # tipo declarado do receptor e se liga incorretamente a um mesmo nome não relacionado
                # método. O receptor pode estar em letras minúsculas (`_server.Save()`),
                # portanto, isso é mais amplo do que a regra Python com letras maiúsculas/este campo.
                _csharp_defer = (
                    config.ts_module == "tree_sitter_c_sharp"
                    and is_member_call and member_receiver
                )
                _python_defer = (
                    config.ts_module == "tree_sitter_python"
                    and is_member_call
                    and member_receiver not in {"self", "cls", "super"}
                )
                _java_defer = (
                    config.ts_module == "tree_sitter_java" and is_member_call
                )
                if _python_defer or _java_defer or (
                    is_member_call
                    and member_receiver
                    and (
                        member_receiver[:1].isupper()
                        or is_this_field_call
                        or _csharp_defer
                    )
                ):
                    tgt_nid = None
                else:
                    tgt_nid = label_to_nid.get(callee_name)
                    # A qualified `new A.B.Foo()` whose bare name matches only a
                    # sourceless stub in this file would bind the call to the stub
                    # and never reach _resolve_csharp_qualified_calls, the one pass
                    # that can honour the namespace. Defer so it can.
                    if (
                        csharp_qualified_prefix
                        and tgt_nid
                        and not nid_to_sf.get(tgt_nid)
                    ):
                        tgt_nid = None
                if tgt_nid and tgt_nid != caller_nid:
                    pair = (caller_nid, tgt_nid)
                    if pair not in seen_call_pairs:
                        seen_call_pairs.add(pair)
                        line = node.start_point[0] + 1
                        edges.append({
                            "source": caller_nid,
                            "target": tgt_nid,
                            "relation": "calls",
                            "context": "call",
                            "confidence": "EXTRACTED",
                            "source_file": str_path,
                            "source_location": f"L{line}",
                            "weight": 1.0,
                        })
                elif callee_name and not tgt_nid:
                    # Callee não está neste arquivo - salve para resolução de arquivos cruzados em extract()
                    rc_entry = {
                        "caller_nid": caller_nid,
                        "callee": callee_name,
                        "is_member_call": is_member_call,
                        "source_file": str_path,
                        "source_location": f"L{node.start_point[0] + 1}",
                        "receiver": swift_receiver or member_receiver,
                    }
                    # Ruby: anexe o tipo inferido do receptor do método
                    # ligações locais `var = Const.new`, quando conhecidas inequivocamente.
                    if member_receiver and config.ts_module == "tree_sitter_ruby":
                        rc_entry["receiver_type"] = ruby_var_types.get(
                            caller_nid, {}
                        ).get(member_receiver)
                    # Marque a linguagem C++ raw_call para que o resolvedor C++ entre arquivos
                    # afirma isso inequivocamente: um arquivo `.h` é roteado para extract_cpp ou
                    # extract_objc por conteúdo, e ambos os resolvedores veem `.h` em seus
                    # conjuntos de sufixos, portanto, um sufixo source_file sozinho não pode separá-los.
                    if config.ts_module == "tree_sitter_cpp":
                        rc_entry["lang"] = "cpp"
                    # C#: tag the raw_call so _resolve_csharp_member_calls claims
                    # it, and stamp the receiver's type from the method's SCOPED
                    # bindings by the call's byte offset (, per-method since
                    #, position-aware since). `this.field.M()` is
                    # covered too: member_receiver is the bare field name, and
                    # class fields/properties are the base scope.
                    if config.ts_module == "tree_sitter_c_sharp":
                        rc_entry["lang"] = "csharp"
                        if csharp_qualified_prefix:
                            rc_entry["qualified_prefix"] = csharp_qualified_prefix
                        receiver_type = _csharp_scoped_receiver_type(
                            receiver_types, member_receiver, node.start_byte
                        )
                        if receiver_type:
                            rc_entry["receiver_type"] = receiver_type
                    if config.ts_module == "tree_sitter_java":
                        rc_entry["lang"] = "java"
                        receiver_type = (receiver_types or {}).get(member_receiver or "")
                        if receiver_type:
                            rc_entry["receiver_type"] = receiver_type
                    # Kotlin fully-qualified call: the dotted prefix +
                    # lang tag let _resolve_kotlin_qualified_calls claim it.
                    if kotlin_qualified_prefix:
                        rc_entry["lang"] = "kotlin"
                        rc_entry["qualified_prefix"] = kotlin_qualified_prefix
                    raw_calls.append(rc_entry)

            # Despacho indireto: uma função passada POR NOME como argumento de chamada
            # (executor.submit(fn), Thread(target=fn), map(fn, xs)) é uma dependência real
            # a varredura somente do receptor acima não pode ser vista. Emita-o como uma `indirect_call` distinta
            # relação tão rigorosa que consultas de `chamadas` permanecem precisas enquanto afetadas/raio de explosão
            # pega a aresta. Python apenas por enquanto; despacho via dict literais, getattr
            # ou decoradores mora em outros nós AST e é deixado para acompanhamento.
            #
            # A emissão é geral entre destinos de chamada (sem lista de permissões de envio/mapa/thread):
            # o valor está capturando um retorno de chamada passado para QUALQUER função. Dois guardas mantêm
            # parece - sem eles, um identificador meramente correspondente a um rótulo de nó produzido
            # arestas falsas para o caso sombra idiomático e para variáveis ​​de dados simples:
            #   1. SOMBRA — pula um argumento que é um parâmetro ou ligação local de
            #      a função envolvente; nomeia um valor local, não o módulo fn.
            #   2. CALLABLE TARGET — resolve apenas para uma função/método/classe def, então
            #      `process(config)` não pode apontar para um nó não chamável de mesmo nome.
            if config.ts_module == "tree_sitter_python":
                args_node = node.child_by_field_name("arguments")
                if args_node is not None:
                    enclosing_locals = local_bound_names.get(caller_nid, frozenset()) | extra_locals
                    for arg in args_node.children:
                        if arg.type == "identifier":
                            _emit_indirect_ref(arg, caller_nid, enclosing_locals, "argument")
                        elif arg.type == "keyword_argument":
                            _emit_indirect_ref(
                                arg.child_by_field_name("value"),
                                caller_nid, enclosing_locals, "argument")
                # Despacho reflexivo: getattr(obj, "handler") nomeia um chamável por
                # literal de string (fatia 3). A string é um nome de ATRIBUTO, não
                # uma ligação de identificador, portanto nunca é obscurecido por um param/local - é
                # resolve diretamente para o que pode ser chamado, ignorando a sombra do identificador
                # guarda. Um nome dinâmico (getattr(obj, name)) não pode ser resolvido → sem limite.
                getattr_ref = _getattr_ref_name(node)
                if getattr_ref is not None:
                    ref_name, loc = getattr_ref
                    _emit_indirect_by_name(ref_name, loc, caller_nid, "getattr")
            elif config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
                # JS/TS: um retorno de chamada passado por nome (`arr.map(fn)`, `setTimeout(fn)`,
                # `el.addEventListener("x",fn)`). Apenas argumentos de identificador posicional -
                # setas / expressões de função embutidas são definições diretas, não um
                # referência por nome. Nenhuma palavra-chave args em JS (args nomeados são objetos,
                # tratado pelo passe de coleta).
                args_node = node.child_by_field_name("arguments")
                if args_node is not None:
                    enclosing_locals = local_bound_names.get(caller_nid, frozenset()) | extra_locals
                    for arg in args_node.children:
                        if arg.type == "identifier":
                            _emit_indirect_ref(arg, caller_nid, enclosing_locals, "argument")

            # Chamadas de função auxiliar: config('foo.bar') → uses_config edge para "foo"
            if (callee_name and callee_name in config.helper_fn_names):
                args_node = node.child_by_field_name("arguments")
                first_key: str | None = None
                if args_node:
                    for arg in args_node.children:
                        if arg.type != "argument":
                            continue
                        for inner in arg.children:
                            if inner.type == "string":
                                for sc in inner.children:
                                    if sc.type == "string_content":
                                        first_key = _read_text(sc, source)
                                        break
                                break
                        if first_key:
                            break
                if first_key:
                    segment = first_key.split(".")[0]
                    tgt_nid = (label_to_nid_ci.get(segment.lower())
                               or label_to_nid_ci.get(f"{segment}.php".lower()))
                    if tgt_nid and tgt_nid != caller_nid:
                        relation = f"uses_{callee_name}"
                        pair3 = (caller_nid, tgt_nid, relation)
                        if pair3 not in seen_helper_ref_pairs:
                            seen_helper_ref_pairs.add(pair3)
                            line = node.start_point[0] + 1
                            edges.append({
                                "source": caller_nid,
                                "target": tgt_nid,
                                "relation": relation,
                                "confidence": "EXTRACTED",
                                "confidence_score": 1.0,
                                "source_file": str_path,
                                "source_location": f"L{line}",
                                "weight": 1.0,
                            })

            # Vinculações de contêiner de serviço: $this->app->bind(Foo::class, Bar::class)
            if (node.type == "member_call_expression"
                    and callee_name
                    and callee_name in config.container_bind_methods):
                args_node = node.child_by_field_name("arguments")
                class_args: list[str] = []
                if args_node:
                    for arg in args_node.children:
                        if arg.type != "argument":
                            continue
                        for inner in arg.children:
                            if inner.type == "class_constant_access_expression":
                                cls = _php_class_const_scope(inner)
                                if cls:
                                    class_args.append(cls)
                                break
                        if len(class_args) >= 2:
                            break
                if len(class_args) == 2:
                    contract_name, impl_name = class_args
                    contract_nid = label_to_nid_ci.get(contract_name.lower())
                    impl_nid = label_to_nid_ci.get(impl_name.lower())
                    if contract_nid and impl_nid and contract_nid != impl_nid:
                        pair3 = (contract_nid, impl_nid, "bound_to")
                        if pair3 not in seen_bind_pairs:
                            seen_bind_pairs.add(pair3)
                            line = node.start_point[0] + 1
                            edges.append({
                                "source": contract_nid,
                                "target": impl_nid,
                                "relation": "bound_to",
                                "confidence": "EXTRACTED",
                                "confidence_score": 1.0,
                                "source_file": str_path,
                                "source_location": f"L{line}",
                                "weight": 1.0,
                            })

        # Static property access: Foo::$bar → uses_static_prop edge
        if node.type in config.static_prop_types:
            scope_node = node.child_by_field_name("scope")
            if scope_node is None:
                for child in node.children:
                    if child.is_named and child.type in ("name", "qualified_name", "identifier"):
                        scope_node = child
                        break
            if scope_node is not None:
                class_name = _read_text(scope_node, source)
                tgt_nid = label_to_nid_ci.get(class_name.lower())
                if tgt_nid and tgt_nid != caller_nid:
                    pair3 = (caller_nid, tgt_nid, "uses_static_prop")
                    if pair3 not in seen_static_ref_pairs:
                        seen_static_ref_pairs.add(pair3)
                        line = node.start_point[0] + 1
                        edges.append({
                            "source": caller_nid,
                            "target": tgt_nid,
                            "relation": "uses_static_prop",
                            "confidence": "EXTRACTED",
                            "confidence_score": 1.0,
                            "source_file": str_path,
                            "source_location": f"L{line}",
                            "weight": 1.0,
                        })

        # PHP class constant access: Foo::BAR → references_constant edge
        if config.ts_module == "tree_sitter_php" and node.type == "class_constant_access_expression":
            class_name = _php_class_const_scope(node)
            if class_name:
                tgt_nid = label_to_nid_ci.get(class_name.lower())
                if tgt_nid and tgt_nid != caller_nid:
                    pair3 = (caller_nid, tgt_nid, "references_constant")
                    if pair3 not in seen_static_ref_pairs:
                        seen_static_ref_pairs.add(pair3)
                        line = node.start_point[0] + 1
                        edges.append({
                            "source": caller_nid,
                            "target": tgt_nid,
                            "relation": "references_constant",
                            "confidence": "EXTRACTED",
                            "confidence_score": 1.0,
                            "source_file": str_path,
                            "source_location": f"L{line}",
                            "weight": 1.0,
                        })

        # Tabelas de envio: uma função listada como um valor em um dict/list/set/
        # literal de tupla dentro deste corpo é uma dependência indireta do envolvente
        # função. Reutiliza a proteção compartilhada de resolução e emissão (somente alvo chamável,
        # não sombreado por um adiamento de parâmetro/local entre arquivos).
        if config.ts_module == "tree_sitter_python" and node.type in (
            "dictionary", "list", "set", "tuple"
        ):
            enclosing_locals = local_bound_names.get(caller_nid, frozenset()) | extra_locals
            for ident in _python_dispatch_value_idents(node):
                _emit_indirect_ref(ident, caller_nid, enclosing_locals, "collection")
        elif config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript") \
                and node.type in ("object", "array"):
            enclosing_locals = local_bound_names.get(caller_nid, frozenset()) | extra_locals
            for ident in _js_dispatch_value_idents(node):
                _emit_indirect_ref(ident, caller_nid, enclosing_locals, "collection")

        # Referências de atribuição/retorno (fatia 2): uma função vinculada a um nome
        # (cb = manipulador) ou retornado de uma fábrica (manipulador de retorno) é um processo indireto
        # dependência da função envolvente. Apenas o lado VALUE - a atribuição
        # TARGET é uma nova ligação local, não uma referência - portanto, o shadow guard compartilhado
        # ainda é válido (um parâmetro/local nomeado no RHS é o local, não o módulo fn).
        if config.ts_module == "tree_sitter_python" and node.type == "assignment":
            enclosing_locals = local_bound_names.get(caller_nid, frozenset()) | extra_locals
            for ident in _python_ref_value_idents(node.child_by_field_name("right")):
                _emit_indirect_ref(ident, caller_nid, enclosing_locals, "assignment")
        elif config.ts_module == "tree_sitter_python" and node.type == "return_statement":
            enclosing_locals = local_bound_names.get(caller_nid, frozenset()) | extra_locals
            value = next((c for c in node.children if c.is_named), None)
            for ident in _python_ref_value_idents(value):
                _emit_indirect_ref(ident, caller_nid, enclosing_locals, "return")

        # `catch (e)` binds through the clause's own `parameter` field, never a
        # variable_declarator, so `_js_local_bound_names` never sees it: a one-letter
        # binding passed on as a call argument in the handler read as a by-name
        # reference to a same-named callable elsewhere in the corpus (minified bundles
        # supply one for nearly every letter). The binding is scoped to the clause, so
        # fold it into extra_locals for that subtree only — same shape as the untracked
        # closure fold above — leaving references outside the block resolvable.
        if (
            config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript")
            and node.type == "catch_clause"
        ):
            param = node.child_by_field_name("parameter")  # absent for ES2019 `catch {}`
            if param is not None:
                caught: set[str] = set()
                _js_collect_pattern_idents(param, source, caught)
                extra_locals = extra_locals | frozenset(caught)

        for child in node.children:
            walk_calls(child, caller_nid, receiver_types, extra_locals)

    if config.ts_module == "tree_sitter_ruby":
        for caller_nid, body_node in function_bodies:
            ruby_var_types[caller_nid] = _ruby_local_class_bindings(body_node, source)

    # C++: construa a tabela `var -> ClassName` por arquivo a partir de declarações locais em
    # cada corpo da função para que a passagem de chamada de membro entre arquivos possa digitar um receptor
    #. Com escopo de arquivo (não por corpo): o `Foo f;` de um corpo posterior não atrapalha
    # uma ligação anterior (`var not in table`), mantendo a resolução conservadora.
    if config.ts_module == "tree_sitter_cpp":
        for _caller_nid, body_node in function_bodies:
            _cpp_local_var_types(body_node, source, type_table)

    # Swift: type local `let x = Type()` / `let x = Type.shared` bindings inside
    # corpos de métodos, então `x.method()` em uma linha posterior resolve - nível de classe
    # as propriedades são digitadas na caminhada, mas os locais do corpo do método não.
    if config.ts_module == "tree_sitter_swift":
        for _caller_nid, body_node in function_bodies:
            _swift_local_var_types(body_node, source, type_table,
                                   factory=swift_factory_bindings)

    # JS/TS: corpos já andaram com seu próprio caller_nid (const-atribuído
    # setas, métodos). Uma seta INLINE/retornada ou expressão de função que é
    # NÃO rastreado separadamente (por exemplo, `return () => svc.doThing()`) é caso contrário
    # pulou no limite da seta em walk_calls, perdendo suas chamadas - então vamos
    # walk_calls descem para tais fechamentos não rastreados com o chamador anexo
    # (Padrão B). A proteção no conjunto rastreado evita caminhada dupla.
    _tracked_body_ids.update(b for _, b in function_bodies)

    # Body ids are unique (one language per file), so the Java (flat) and C#
    # (scoped) per-method receiver tables merge without collision — the
    # stamp site branches on language to read the matching shape.
    receiver_types_by_body = {**java_receiver_types, **csharp_receiver_types}
    for caller_nid, body_node in function_bodies:
        walk_calls(
            body_node,
            caller_nid,
            receiver_types_by_body.get(id(body_node)),
            frozenset(closure_locals_by_body.get(id(body_node), ())),
        )

    # walk property/field initializers (collected above). walk_calls
    # autoprotege-se contra a reentrada em corpos funcionais e desduplicações via
    # seen_call_pairs, portanto, um fechamento dentro de um inicializador não é percorrido duas vezes.
    for owner_nid, init_node in initializer_nodes:
        walk_calls(init_node, owner_nid)

    # ── Event listener pass ───────────────────────────────────────────────────
    seen_listen_pairs: set[tuple[str, str]] = set()
    for event_name, listener_name, line in pending_listen_edges:
        event_nid = label_to_nid_ci.get(event_name.lower())
        listener_nid = label_to_nid_ci.get(listener_name.lower())
        if not event_nid or not listener_nid or event_nid == listener_nid:
            continue
        pair2 = (event_nid, listener_nid)
        if pair2 in seen_listen_pairs:
            continue
        seen_listen_pairs.add(pair2)
        edges.append({
            "source": event_nid,
            "target": listener_nid,
            "relation": "listened_by",
            "confidence": "EXTRACTED",
            "confidence_score": 1.0,
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        })

    # ── Module-level dispatch tables ──────────────────────────────────
    # Uma função listada como um valor em um literal de dicionário/lista/conjunto/tupla de NÍVEL SUPERIOR (um
    # registro de rota/manipulador) é uma dependência indireta do arquivo. Atribuído
    # para o nó do arquivo. Os corpos de funções e classes são abordados acima, portanto, esta verificação
    # para em seus limites — não deve reatribuir a tabela local de um método
    # ao arquivo, e as tabelas de atributos de classe são um refinamento posterior.
    if config.ts_module == "tree_sitter_python":
        module_bound = _python_module_bound_names(root, source)

        def _scan_module_dispatch(n) -> None:
            if n.type in ("function_definition", "class_definition"):
                return
            if n.type in ("dictionary", "list", "set", "tuple"):
                for ident in _python_dispatch_value_idents(n):
                    _emit_indirect_ref(ident, file_nid, module_bound, "collection")
            elif n.type == "assignment":
                # Module-level alias / re-export: CALLBACK = handler
                for ident in _python_ref_value_idents(n.child_by_field_name("right")):
                    _emit_indirect_ref(ident, file_nid, module_bound, "assignment")
            elif n.type == "call":
                # Module-level reflective dispatch: HANDLER = getattr(mod, "handler")
                # (fatia 3). Atribuído ao nó do arquivo, como uma tabela de módulo.
                getattr_ref = _getattr_ref_name(n)
                if getattr_ref is not None:
                    ref_name, loc = getattr_ref
                    _emit_indirect_by_name(ref_name, loc, file_nid, "getattr")
            for c in n.children:
                _scan_module_dispatch(c)

        _scan_module_dispatch(root)
    elif config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
        js_module_bound = _js_module_bound_names(root, source)

        def _scan_js_module_dispatch(n) -> None:
            if n.type in _JS_SCOPE_BOUNDARY:
                return  # corpos de função/classe são percorridos separadamente
            if n.type in ("object", "array"):
                for ident in _js_dispatch_value_idents(n):
                    _emit_indirect_ref(ident, file_nid, js_module_bound, "collection")
            elif n.type in ("call_expression", "new_expression"):
                # O registro de retorno de chamada em nível de módulo é idiomático em JS — Express
                # rotas (`app.get("/", manipulador)`), fiação de evento (`emitter.on("e",
                # manipulador)`), `setTimeout(fn)`. Capture argumentos de identificador como indiretos
                # refs do arquivo (as setas embutidas são definições diretas, não referências por nome).
                margs = n.child_by_field_name("arguments")
                if margs is not None:
                    for marg in margs.children:
                        if marg.type == "identifier":
                            _emit_indirect_ref(marg, file_nid, js_module_bound, "argument")
            for c in n.children:
                _scan_js_module_dispatch(c)

        _scan_js_module_dispatch(root)

    # ── Clean edges ───────────────────────────────────────────────────────────
    valid_ids = seen_ids
    clean_edges = []
    # Byte-identical duplicates collapse to one edge: a signature that
    # annotates the same type twice (``def f(a: Path, b: Path)``) is ONE
    # reference relationship at one location, but the per-occurrence emission
    # loops above append it once per annotation — in every language block, since
    # neither add_edge nor the raw appends de-duplicate. The copies carry zero
    # information (build's dedup drops them anyway) and their only observable
    # effect is tripping diagnose_extraction's exact_duplicate_edges health
    # warning. Only edges whose ENTIRE payload is identical collapse; any
    # differing field (source_location, context, metadata, …) keeps both.
    _seen_edge_payloads: set[str] = set()
    for edge in edges:
        src, tgt = edge["source"], edge["target"]
        if src in valid_ids and (tgt in valid_ids or edge["relation"] in ("imports", "imports_from", "re_exports")):
            payload = json.dumps(edge, sort_keys=True, default=str)
            if payload in _seen_edge_payloads:
                continue
            _seen_edge_payloads.add(payload)
            clean_edges.append(edge)

    # Os mixins Ruby foram coletados durante a caminhada do nó (antes da existência de raw_calls);
    # dobre-os para que o resolvedor de arquivos cruzados os veja.
    if _ruby_mixin_calls:
        raw_calls.extend(_ruby_mixin_calls)
    result = {"nodes": nodes, "edges": clean_edges, "raw_calls": raw_calls}
    # Export the per-file field->type tables for the corpus member-call
    # resolvers: a field declared on a superclass in ANOTHER file can
    # only be typed once the whole graph is visible. Keyed by class label +
    # source_file, never by node id - ids are rewritten by the remap
    # passes and the cache portability rewrite, which is exactly how the
    # id-keyed ObjC table went stale.
    _field_table_export = [
        {"lang": _lang, "class_label": _n.get("label"),
         "source_file": _n.get("source_file"), "fields": dict(_tbl)}
        for _lang, _tables in (("java", java_field_types), ("csharp", csharp_field_types))
        for _cls, _tbl in _tables.items()
        if _tbl
        for _n in (next((x for x in nodes if x["id"] == _cls), None),)
        if _n is not None and _n.get("label")
    ]
    if _field_table_export:
        result["member_field_tables"] = _field_table_export
    # the parser recovered from syntax errors, so extraction may be
    # partial (in the worst case, nothing but the file node). Record the first
    # error's line so extract() can warn instead of reporting silent success.
    # Rides on the result dict, so it survives the per-file AST cache.
    if root.has_error:
        result["parse_errors"] = {
            "first_error_line": _first_parse_error_line(root),
            "multiline_error": _has_multiline_error(root),
        }
    # Kotlin: the declared package qualifies every node in the
    # file; the import-target and qualified-call resolvers key their per-package
    # symbol indexes off it.
    if config.ts_module == "tree_sitter_kotlin":
        _pkg = _kotlin_package_name(root, source)
        if _pkg:
            result["kotlin_package"] = _pkg
    if callable_def_nids:
        # Marque função/método/classe defs com um atributo `_callable` para que o
        # passagem indirect_call entre arquivos pode resolver um retorno de chamada por nome apenas para um real
        # chamável (nunca um símbolo de dados com o mesmo nome). Um marcador passa pelo nó dict
        # e sobrevive às passagens de id-remap/desambiguação em extract(); um pré-remapeamento
        # id set ficaria obsoleto e eliminaria silenciosamente todas as arestas indiretas de arquivos cruzados quando
        # ids são relativizados (regressão). Removido antes da saída, como origin_file.
        for n in nodes:
            if n["id"] in callable_def_nids:
                n["_callable"] = True
                if n["id"] in callable_class_nids:
                    # Class def: callable only via constructor. The indirect_call
                    # guard excludes these to avoid false edges.
                    n["_callable_class"] = True
    if swift_extensions:
        result["swift_extensions"] = swift_extensions
    # TS/JS: aumente a tabela de tipo de injeção de construtor com `new` local
    # ligações e parâmetros anotados por tipo, então `const s = new Svc(); s.m()` e
    # uma chamada em um parâmetro digitado (incl. dentro de um fechamento) resolve. O
    # entradas de injeção de construtor são preenchidas durante a caminhada acima e vencem em
    # um conflito de nomes (a primeira ligação vence no auxiliar).
    if config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
        _ts_receiver_type_table(root, source, type_table)
    if config.ts_module == "tree_sitter_swift":
        if type_table or swift_factory_bindings:
            result["swift_type_table"] = {"path": str_path, "table": type_table}
            if swift_factory_bindings:
                # Lists, not tuples: the value must round-trip the JSON AST cache.
                result["swift_type_table"]["factory"] = {
                    k: list(v) for k, v in swift_factory_bindings.items()
                }
    elif type_table:
        if config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
            result["ts_type_table"] = {"path": str_path, "table": type_table}
        elif config.ts_module == "tree_sitter_cpp":
            result["cpp_type_table"] = {"path": str_path, "table": type_table}
    return result

def _python_decorator_name(deco_node, source: bytes) -> str | None:
    """Return the head symbol of a Python `decorator` node.

    The Python twin of `_ts_decorator_name`, differing only in grammar node
    names: `@traced` -> the identifier; `@retry(times=3)` -> the `function` of
    the `call`; `@app.route("/")` / `@mod.deco` -> the `attribute` (the symbol
    itself, not the module alias it is reached through).
    """
    for child in deco_node.children:
        if not child.is_named:
            continue
        target = child
        if target.type == "call":
            target = target.child_by_field_name("function") or target
        if target.type == "attribute":
            attr = target.child_by_field_name("attribute")
            return _read_text(attr, source) if attr else None
        if target.type == "identifier":
            return _read_text(target, source)
        return None
    return None

def _ts_decorator_name(deco_node, source: bytes) -> str | None:
    """Return the head symbol of a TS `decorator` node.

    `@Injectable` -> the identifier; `@Component({...})` / `@Input()` -> the
    `function` of the call_expression; `@ng.Component()` / `@core.Injectable` ->
    the `property` of the member_expression (the imported symbol, not the
    namespace alias).
    """
    for child in deco_node.children:
        if not child.is_named:
            continue
        target = child
        if target.type == "call_expression":
            target = target.child_by_field_name("function") or target
        if target.type == "member_expression":
            prop = target.child_by_field_name("property")
            return _read_text(prop, source) if prop else None
        if target.type == "identifier":
            return _read_text(target, source)
        return None
    return None

def _ts_method_name(method_node, source: bytes) -> str | None:
    """Name of a `method_definition`, matching the id the function-types branch
    builds (`_make_id(class_nid, name)`)."""
    name_node = method_node.child_by_field_name("name")
    return _read_text(name_node, source) if name_node else None

def _ts_descendant_decorators(node) -> list:
    """Collect `decorator` nodes under `node` (e.g. parameter decorators inside a
    method's formal_parameters, or a field's own decorator), without crossing into
    a nested class or a nested method, which own their own decorators."""
    out: list = []

    def rec(n, top: bool) -> None:
        for child in n.children:
            ct = child.type
            if ct == "decorator":
                out.append(child)
            elif ct in ("class_declaration", "abstract_class_declaration"):
                continue
            elif ct == "method_definition" and not top:
                continue
            else:
                rec(child, False)

    rec(node, True)
    return out

def _ts_emit_decorator_edges(class_node, class_nid: str, stem: str, source: bytes,
                             ensure_named_node, add_edge) -> None:
    """Emit `references` edges (context="decorator") from a class and its members
    to the symbols of the TS decorators applied to them.

    Decorators only occur on classes, class members, and parameters, so a single
    pass over the class declaration covers them. Members that are graph nodes
    (methods, incl. the constructor) own their decorators and their parameter
    decorators; members that are not nodes (fields, parameters) attribute to the
    enclosing class. Targets go through `ensure_named_node`, so a decorator
    imported from another module (the common case — `@Component` from
    `@angular/core`) becomes a sourceless stub the corpus rewire collapses onto
    the real definition.
    """
    def emit(deco_node, owner_nid: str) -> None:
        name = _ts_decorator_name(deco_node, source)
        if not name:
            return
        line = deco_node.start_point[0] + 1
        target = ensure_named_node(name, line)
        if target != owner_nid:
            add_edge(owner_nid, target, "references", line, context="decorator")

    # Decoradores de nível de classe: filhos diretos do nó de classe (`@Deco class C`),
    # plus - quando exportado (`@Deco export class C`) - os decoradores que ficam
    # o empacotamento export_statement, antes da classe.
    for child in class_node.children:
        if child.type == "decorator":
            emit(child, class_nid)
    parent = class_node.parent
    if parent is not None and parent.type == "export_statement":
        for child in parent.children:
            if child.type == "decorator":
                emit(child, class_nid)
            elif child.type in ("class_declaration", "abstract_class_declaration"):
                break

    # Decoradores de membros dentro do corpo da classe.
    body = next((c for c in class_node.children if c.type == "class_body"), None)
    if body is None:
        return
    for member in body.children:
        mt = member.type
        if mt == "decorator":
            # Um decorador de método é um irmão que precede o método; pule qualquer
            # decoradores empilhados para encontrá-lo.
            owner = class_nid
            sib = member.next_named_sibling
            while sib is not None and sib.type == "decorator":
                sib = sib.next_named_sibling
            if sib is not None and sib.type == "method_definition":
                mname = _ts_method_name(sib, source)
                if mname:
                    owner = _make_id(class_nid, mname)
            emit(member, owner)
        elif mt == "method_definition":
            mname = _ts_method_name(member, source)
            m_nid = _make_id(class_nid, mname) if mname else class_nid
            for deco in _ts_descendant_decorators(member):
                emit(deco, m_nid)
        else:
            # Campos/acessadores: o membro não é um nó, então atribua seu
            # decoradores (por exemplo, `@Input()`, `@Column()`) para a classe.
            for deco in _ts_descendant_decorators(member):
                emit(deco, class_nid)
