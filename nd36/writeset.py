"""
Static pre-pass: which names can each function bind?

Diffing every local on every line is the slowest thing you can write.
So before running, we parse the file once with `ast` (stdlib) and work out,
per function, the set of names it could ever assign.  Most functions bind
3-6 names, not 50, so this alone is a 5-20x saving on the hot path.

`ast.walk` yields every node in the tree, in no particular order.
`ast.iter_child_nodes` yields only the direct children.
"""

import ast


def _target_names(node, out):
    """Collect names from an assignment target (handles a, b = ... and *rest)."""
    if isinstance(node, ast.Name):
        out.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for el in node.elts:
            _target_names(el, out)
    elif isinstance(node, ast.Starred):
        _target_names(node.value, out)
    # Attribute (self.x) and Subscript (a[i]) do not create a NAME, they
    # mutate an existing object -- that is a MUTATE event, phase 2.


def names_bound_in(func_node):
    """Every name this function body can bind. Does not descend into
    nested functions, which have their own scope."""
    out = set()
    a = getattr(func_node, "args", None)
    if a is not None:
        for group in (a.posonlyargs, a.args, a.kwonlyargs):
            for arg in group:
                out.add(arg.arg)
        if a.vararg:
            out.add(a.vararg.arg)
        if a.kwarg:
            out.add(a.kwarg.arg)

    stack = list(ast.iter_child_nodes(func_node))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            out.add(node.name)          # the def itself binds a name...
            continue                     # ...but its body is another scope
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, ast.Assign):
            for t in node.targets:
                _target_names(t, out)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            _target_names(node.target, out)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _target_names(node.target, out)
        elif isinstance(node, ast.NamedExpr):           # the walrus :=
            _target_names(node.target, out)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    _target_names(item.optional_vars, out)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                out.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                out.add((alias.asname or alias.name).split(".")[0])
        stack.extend(ast.iter_child_nodes(node))
    return frozenset(out)


def _root_name(node):
    """`a[i].b[j]` -> "a".  The outermost name a mutation path hangs off."""
    while True:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, (ast.Attribute,)):
            node = node.value
        elif isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Starred):
            node = node.value
        else:
            return None


# Methods that change their receiver in place. `xs.append(v)` binds no name,
# so without this list the change is invisible.
MUTATING_METHODS = frozenset((
    "append", "extend", "insert", "remove", "pop", "clear", "sort",
    "reverse", "update", "add", "discard", "setdefault", "popitem",
    "appendleft", "extendleft", "rotate", "put", "push",
    "fillna", "dropna", "drop", "rename", "sort_values", "sort_index",
    "add_", "mul_", "zero_", "copy_", "clamp_", "normal_", "fill_",
))


def mutation_roots(path):
    """{line: frozenset(names)} -- names whose OBJECT this line may change.

    Phase 0 only noticed a name being rebound, so `a[i] = x`, `self.total = 1`
    and `xs.append(v)` were all invisible. Those are the commonest things a
    real program does, so Phase 2 looks for them.

    Finding them statically matters for speed: only on a line that could
    mutate do we pay to re-walk the object and compare. Everywhere else the
    cheap (id, len) check still decides.
    """
    try:
        with open(path, "r", encoding="utf8", errors="replace") as fh:
            tree = ast.parse(fh.read(), filename=path)
    except (OSError, SyntaxError):
        return {}

    table = {}

    def add(line, name):
        if name:
            table.setdefault(line, set()).add(name)

    for node in ast.walk(tree):
        # a[i] = v   /   obj.field = v   /   a[i].field = v
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, (ast.Subscript, ast.Attribute)):
                    add(t.lineno, _root_name(t))
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            if isinstance(node.target, (ast.Subscript, ast.Attribute)):
                add(node.target.lineno, _root_name(node.target))
        elif isinstance(node, ast.Delete):
            for t in node.targets:
                if isinstance(t, (ast.Subscript, ast.Attribute)):
                    add(t.lineno, _root_name(t))
        # xs.append(v), d.update(...), t.add_(...)
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr in MUTATING_METHODS:
                add(node.lineno, _root_name(fn.value))
            # A call can mutate anything passed to it by reference, so a
            # name handed to a function is worth re-checking afterwards.
            for arg in node.args:
                if isinstance(arg, ast.Name):
                    add(node.lineno, arg.id)
    return {line: frozenset(names) for line, names in table.items()}


def scan_file(path):
    """{(first_line, name): frozenset(names)} for every scope in a file.

    Keyed by line AND name, not line alone. A function defined on line 1
    collides with the module, whose own first line is also 1 -- which
    silently wiped out every module-level variable in files that open with
    a `def`. The code object gives us both halves at trace time
    (`co_firstlineno`, `co_name`).
    """
    try:
        with open(path, "r", encoding="utf8", errors="replace") as fh:
            tree = ast.parse(fh.read(), filename=path)
    except (OSError, SyntaxError):
        return {}

    table = {}
    table[("<module>", 0)] = names_bound_in(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = names_bound_in(node)
            table[(node.name, node.lineno)] = names
            # Depending on the CPython version, co_firstlineno may point at
            # the first decorator instead of the `def`. Register both.
            for dec in node.decorator_list:
                table[(node.name, dec.lineno)] = names
        elif isinstance(node, ast.ClassDef):
            table[(node.name, node.lineno)] = names_bound_in(node)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp,
                               ast.GeneratorExp)):
            # On some versions a comprehension is its own code object,
            # named <listcomp> / <genexpr>. Its loop target is worth seeing.
            targets = set()
            for gen in node.generators:
                _target_names(gen.target, targets)
            for kind in ("<listcomp>", "<setcomp>", "<dictcomp>",
                         "<genexpr>"):
                table[(kind, node.lineno)] = frozenset(targets)
    return table
