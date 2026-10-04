"""
Structure: the control flow graph and the call graph.

The trace says what the program DID. These say what it COULD do. Shown
together they answer the question a plain debugger never answers: which
branches never ran, and which functions were never called.

So every node and edge here carries a hit count from the trace, and the
report draws the zeroes in grey.
"""

import ast

from . import schema


# --------------------------------------------------------------------------
# Control flow graph
# --------------------------------------------------------------------------

class CFG:
    def __init__(self, name, first_line):
        self.name = name
        self.first_line = first_line
        self.nodes = []        # {id, line, label, kind}
        self.edges = []        # {src, dst, label}

    def node(self, line, label, kind="stmt"):
        nid = len(self.nodes)
        self.nodes.append({"id": nid, "line": line,
                           "label": label, "kind": kind})
        return nid

    def edge(self, src, dst, label=""):
        if src is None or dst is None:
            return
        self.edges.append({"src": src, "dst": dst, "label": label})

    def compact(self):
        """Merge straight-line runs into basic blocks.

        One node per statement is unreadable -- a 17-line module became a
        1,500-pixel column. A real CFG groups statements that always run
        together, so a block absorbs its successor when the pair has no
        choice about it: exactly one edge out, exactly one edge in, no
        branch label, and neither end is special.
        """
        for n in self.nodes:
            n.setdefault("lines", [(n["line"], n["label"])])

        changed = True
        while changed:
            changed = False
            outs, ins = {}, {}
            for e in self.edges:
                outs.setdefault(e["src"], []).append(e)
                ins.setdefault(e["dst"], []).append(e)
            for node in self.nodes:
                if node.get("gone") or node["kind"] != "stmt":
                    continue
                oe = outs.get(node["id"], [])
                if len(oe) != 1 or oe[0]["label"]:
                    continue
                nxt = next((m for m in self.nodes
                            if m["id"] == oe[0]["dst"]), None)
                if (nxt is None or nxt.get("gone")
                        or nxt["kind"] != "stmt"
                        or len(ins.get(nxt["id"], [])) != 1):
                    continue
                node["lines"].extend(nxt["lines"])
                nxt["gone"] = True
                self.edges = [e for e in self.edges if e is not oe[0]]
                for e in self.edges:
                    if e["src"] == nxt["id"]:
                        e["src"] = node["id"]
                    if e["dst"] == nxt["id"]:
                        e["dst"] = node["id"]
                changed = True
                break
        self.nodes = [n for n in self.nodes if not n.get("gone")]
        for n in self.nodes:
            n["label"] = n["lines"][0][1]
        return self


def _label(node, source_lines):
    """The source text of a statement, trimmed to fit a graph node."""
    try:
        text = source_lines[node.lineno - 1].strip()
    except IndexError:
        return type(node).__name__
    for suffix in (":",):
        if text.endswith(suffix):
            text = text[:-1]
    return text if len(text) <= 44 else text[:44] + "…"


def build_cfg(func_node, source_lines, name, first_line):
    """A statement-level CFG.

    Not basic blocks: one node per statement. Basic blocks are what a
    compiler wants; a person reading their own code wants to see the lines
    they wrote, so the nodes are statements.
    """
    g = CFG(name, first_line)
    entry = g.node(first_line, name, "entry")
    exit_id = g.node(0, "end", "exit")
    g.exit = exit_id

    def block(stmts, nxt, loop=None):
        """Wire up a list of statements; return the id of the first one.

        Walked in reverse, so each statement already knows what follows it.
        """
        follow = nxt
        first = nxt
        for stmt in reversed(stmts):
            first = stmt_node(stmt, follow, loop)
            follow = first
        return first

    def stmt_node(stmt, nxt, loop):
        if isinstance(stmt, ast.If):
            nid = g.node(stmt.lineno, _label(stmt, source_lines), "cond")
            g.edge(nid, block(stmt.body, nxt, loop), "True")
            g.edge(nid, block(stmt.orelse, nxt, loop) if stmt.orelse else nxt,
                   "False")
            return nid

        if isinstance(stmt, (ast.While, ast.For, ast.AsyncFor)):
            nid = g.node(stmt.lineno, _label(stmt, source_lines), "loop")
            after = block(stmt.orelse, nxt, loop) if stmt.orelse else nxt
            # The body's "next" is the loop header -- that back edge is the
            # loop. break jumps past it, continue jumps to it.
            g.edge(nid, block(stmt.body, nid, (nid, after)), "each")
            g.edge(nid, after, "done")
            return nid

        if isinstance(stmt, ast.Return):
            nid = g.node(stmt.lineno, _label(stmt, source_lines), "return")
            g.edge(nid, exit_id)
            return nid

        if isinstance(stmt, ast.Break) and loop:
            nid = g.node(stmt.lineno, "break", "jump")
            g.edge(nid, loop[1])
            return nid

        if isinstance(stmt, ast.Continue) and loop:
            nid = g.node(stmt.lineno, "continue", "jump")
            g.edge(nid, loop[0])
            return nid

        if isinstance(stmt, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            nid = g.node(stmt.lineno, "try", "cond")
            final = (block(stmt.finalbody, nxt, loop)
                     if stmt.finalbody else nxt)
            g.edge(nid, block(stmt.body, final, loop), "ok")
            for handler in stmt.handlers:
                label = "except %s" % (
                    _label(handler, source_lines).replace("except ", "")
                    or "…")
                g.edge(nid, block(handler.body, final, loop), label[:28])
            if stmt.orelse:
                block(stmt.orelse, final, loop)
            return nid

        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            nid = g.node(stmt.lineno, _label(stmt, source_lines), "stmt")
            g.edge(nid, block(stmt.body, nxt, loop))
            return nid

        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            # A def is one statement here; its body gets its own CFG.
            nid = g.node(stmt.lineno, "def %s" % stmt.name, "stmt")
            g.edge(nid, nxt)
            return nid

        nid = g.node(stmt.lineno, _label(stmt, source_lines), "stmt")
        g.edge(nid, nxt)
        return nid

    body = func_node.body if hasattr(func_node, "body") else []
    g.edge(entry, block(body, exit_id))
    return g


def cfgs_for_source(source, path):
    """One CFG per function, plus one for the module body."""
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        return []
    lines = source.splitlines()
    out = [build_cfg(tree, lines, "<module>", 1).compact()]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(build_cfg(node, lines, node.name,
                                 node.lineno).compact())
    return out


# --------------------------------------------------------------------------
# Call graph
# --------------------------------------------------------------------------

def call_graph(reader):
    """Who called whom, how often, and who was never called.

    Dynamic edges come from the trace and are ground truth. Static edges come
    from the AST and fill in the paths that did not run this time.
    """
    K_CALL = schema.KIND_CODE[schema.CALL]
    K_RET = schema.KIND_CODE[schema.RETURN]
    K_UNWIND = schema.KIND_CODE[schema.UNWIND]

    # Walk the log keeping a frame stack, so each CALL knows its caller.
    stack = []
    edges = {}
    calls = {}
    frame_func = {}
    for step in range(1, reader.n_steps + 1):
        _, k, fr, loc, _, _, _, _, _ = reader.raw(step)
        if k == K_CALL:
            info = reader.view(step)["extra"]
            fn = info.get("func")
            frame_func[fr] = fn
            caller = frame_func.get(stack[-1]) if stack else None
            edges[(caller, fn)] = edges.get((caller, fn), 0) + 1
            calls[fn] = calls.get(fn, 0) + 1
            stack.append(fr)
        elif k == K_RET or k == K_UNWIND:
            if stack and stack[-1] == fr:
                stack.pop()
            elif fr in stack:
                stack.remove(fr)

    nodes = {}
    for fid, fn in reader.funcs.items():
        nodes[fid] = {"id": fid, "name": fn["qualname"] or fn["name"],
                      "line": fn["first_line"], "calls": calls.get(fid, 0)}

    by_name = {}
    for fid, n in nodes.items():
        by_name.setdefault(n["name"].split(".")[-1], fid)

    # A function that never ran produced no CALL event, so the trace knows
    # nothing about it. Those are exactly the ones worth reporting, so add
    # them from the source with synthetic negative ids.
    next_synthetic = -1
    for path in set(reader.files.values()):
        # C and C++ sources are not Python; the dynamic half of the call
        # graph still works, only the static fill-in is skipped.
        try:
            tree = ast.parse(reader.source(path), filename=path)
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name not in by_name:
                nodes[next_synthetic] = {"id": next_synthetic,
                                         "name": node.name,
                                         "line": node.lineno, "calls": 0}
                by_name[node.name] = next_synthetic
                next_synthetic -= 1

    # Static edges: a call by name inside a function body, matched against
    # the functions we know about in the same file.
    static = set()
    for path in set(reader.files.values()):
        try:
            tree = ast.parse(reader.source(path), filename=path)
        except (SyntaxError, ValueError):
            continue
        owner = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    owner[id(child)] = node.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            callee = (fn.id if isinstance(fn, ast.Name)
                      else fn.attr if isinstance(fn, ast.Attribute) else None)
            if callee not in by_name:
                continue
            src = by_name.get(owner.get(id(node), "<module>"))
            static.add((src, by_name[callee]))

    out = []
    for (src, dst), n in edges.items():
        out.append({"src": src, "dst": dst, "n": n, "ran": True})
    for src, dst in static:
        if (src, dst) not in edges and src != dst:
            out.append({"src": src, "dst": dst, "n": 0, "ran": False})
    return nodes, out


def line_hits_per_node(reader, path, cfg):
    """{cfg node id: hits} by matching node line numbers to the heatmap."""
    stats = reader.line_stats()
    hits = {}
    for node in cfg.nodes:
        # A merged block ran if any line in it ran.
        best = 0
        for line, _text in node.get("lines", [(node["line"], "")]):
            got = stats.get((path, line))
            if got:
                best = max(best, got[0])
        hits[node["id"]] = best
    return hits
