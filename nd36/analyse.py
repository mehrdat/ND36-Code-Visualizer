"""
One pass over the event log that answers every structural question.

`nd flow` and `nd heat` each ask the trace one question. The explorer asks
it fifty, interactively, so it cannot afford a scan per question. This
module does a single linear walk and comes out with:

    functions   calls, self/total steps and time, callers, callees
    variables   how often written, by which function, on which line
    matrix      function x variable -- who writes what, who reads what
    activity    a per-function density over time, for the timeline picture
    invocations call/return pairs, so one call's args and result line up

The result is cached as `analysis.json` inside the trace folder, keyed by
the trace's size, so the second `nd x` on a big trace starts instantly.

Reads are the one thing the log does not record -- recording every name a
line reads would double the trace for information the source already has.
So reads come from the AST, attributed to the function whose body the line
falls inside. That is a static answer next to dynamic ones, and the
explorer labels it as such.
"""

import ast
import json
import os
import re
import struct
import time
from collections import Counter, defaultdict

from . import schema

CACHE = "analysis.json"
CACHE_VERSION = 2
BUCKETS = 160              # columns in the timeline picture
MAX_INVOCATIONS = 4000     # per function, for the call table
_FUNC_RE = re.compile(rb'"func":\s*(\d+)')
_REC = struct.Struct(schema.RECORD)


# --------------------------------------------------------------------------
# static side: which function does a line belong to, and what does it read
# --------------------------------------------------------------------------

def _scopes(source, path):
    """[(name, first_line, last_line)] innermost last, for line lookup."""
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", None) or node.lineno
            out.append((node.name, node.lineno, end))
    out.sort(key=lambda s: (s[2] - s[1]))      # smallest range first = inner
    return out


def _reads(source, path):
    """{line: Counter(names loaded on it)} from the source, not the run."""
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        return {}
    out = defaultdict(Counter)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            out[node.lineno][node.id] += 1
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx,
                                                            ast.Load):
            if isinstance(node.value, ast.Name):
                out[node.lineno][node.value.id] += 1
    return out


BUILTINS = frozenset(dir(__builtins__) if isinstance(__builtins__, dict)
                     else dir(__builtins__)) | {
    "self", "cls", "print", "len", "range", "enumerate", "zip", "sorted",
    "min", "max", "sum", "abs", "int", "float", "str", "list", "dict",
    "set", "tuple", "bool", "type", "open", "isinstance", "round", "map",
    "filter", "any", "all", "reversed", "repr", "format", "super",
}


def static_reads(reader):
    """{function name: Counter(variable -> times read in its body)}.

    Builtins are dropped: `len` and `range` are read on nearly every line
    and tell you nothing about the program's own data.
    """
    out = defaultdict(Counter)
    for fid, path in reader.files.items():
        source = reader.source(path)
        if not source:
            continue
        scopes = _scopes(source, path)
        for line, names in _reads(source, path).items():
            owner = "<module>"
            for name, lo, hi in scopes:
                if lo <= line <= hi:
                    owner = name
                    break
            for var, n in names.items():
                if var not in BUILTINS:
                    out[owner][var] += n
    return out


# --------------------------------------------------------------------------
# the pass
# --------------------------------------------------------------------------

class Analysis:
    """Plain data. Everything the explorer prints comes from these dicts."""

    def __init__(self, d, reader):
        self.r = reader
        self.functions = d["functions"]       # name -> dict
        self.variables = d["variables"]       # name -> dict
        self.activity = d["activity"]         # name -> [counts per bucket]
        self.invocations = d["invocations"]   # name -> [[enter, leave], ...]
        self.n_steps = d["n_steps"]
        self.seconds = d["seconds"]
        self._reads = None

    # -- derived views -----------------------------------------------------

    @property
    def reads(self):
        if self._reads is None:
            self._reads = static_reads(self.r)
        return self._reads

    def func(self, name):
        """Look a function up forgivingly: exact, then suffix, then prefix."""
        if name in self.functions:
            return name
        low = name.lower()
        for key in self.functions:
            if key.lower() == low:
                return key
        hits = [k for k in self.functions if low in k.lower()]
        return hits[0] if len(hits) == 1 else (hits[0] if hits else None)

    def callers_of(self, name):
        return Counter(self.functions.get(name, {}).get("callers", {}))

    def callees_of(self, name):
        return Counter(self.functions.get(name, {}).get("callees", {}))

    def writers_of(self, var):
        return Counter(self.variables.get(var, {}).get("by_func", {}))

    def readers_of(self, var):
        return Counter({fn: c[var] for fn, c in self.reads.items()
                        if c.get(var)})

    def lines_writing(self, var):
        return Counter(self.variables.get(var, {}).get("by_line", {}))


def analyse(reader, progress=None):
    """The single linear pass. ~1 microsecond a step."""
    t0 = time.perf_counter()
    n = reader.n_steps
    K = schema.KIND_CODE
    k_line, k_call = K[schema.LINE], K[schema.CALL]
    k_ret, k_unwind = K[schema.RETURN], K[schema.UNWIND]
    k_bind, k_mut, k_del = K[schema.BIND], K[schema.MUTATE], K[schema.DELETE]
    k_yield, k_resume = K[schema.YIELD], K[schema.RESUME]
    k_raise = K[schema.RAISE]

    # payload id -> function id, pulled out with a regex rather than a full
    # json.loads of every CALL's arguments.
    pid_func = {}
    for row in reader.db.execute("SELECT id, json FROM payloads"):
        m = _FUNC_RE.search(row["json"].encode("utf8", "replace"))
        if m:
            pid_func[row["id"]] = int(m.group(1))

    fname = {fid: (f["qualname"] or f["name"])
             for fid, f in reader.funcs.items()}

    calls = Counter()
    returns = Counter()
    raises = Counter()
    self_steps = Counter()
    total_steps = Counter()
    self_ns = Counter()
    callers = defaultdict(Counter)
    callees = defaultdict(Counter)
    writes_by_fn = defaultdict(Counter)
    first_step, last_step = {}, {}
    lines_of = defaultdict(set)
    activity = defaultdict(lambda: [0] * BUCKETS)
    invocations = defaultdict(list)

    var = defaultdict(lambda: {"writes": 0, "binds": 0, "mutates": 0,
                               "deletes": 0, "first": 0, "last": 0,
                               "by_func": Counter(), "by_line": Counter()})

    frame_fn = {}             # frame id -> function name
    entered = {}              # frame id -> (step, t_ns)
    stack = []                # frame ids, innermost last
    last_line_fn, last_line_t = None, 0
    mm = reader._mm
    unpack = _REC.unpack_from
    nbuckets = max(1, n)

    for i in range(n):
        step = i + 1
        t, k, fr, loc, nm, rk, ri, rf, pid = unpack(mm, i * 42)
        here = (frame_fn.get(stack[-1]) or "<module>") if stack \
            else "<module>"

        if k == k_line:
            # Exclusive time, measured between consecutive LINE events and
            # charged to whichever function was running BEFORE this one.
            # When `solve` calls `ok`, the gap before ok's first line is
            # solve's; the gap after ok's last line is ok's. So a line that
            # makes a call never absorbs the callee's time, which is what
            # went wrong when this was derived from `linehits` (there, the
            # call line is charged the whole subtree) or from the gaps
            # between all events (the log is not strictly monotonic: a CALL
            # record carries the timestamp from before its arguments were
            # encoded).
            if last_line_fn is not None and t > last_line_t:
                self_ns[last_line_fn] += t - last_line_t
            last_line_fn, last_line_t = here, t
            self_steps[here] += 1
            lines_of[here].add(loc)
            activity[here][step * BUCKETS // nbuckets - (step == n)] += 1
        elif k == k_bind or k == k_mut or k == k_del:
            name = reader.names.get(nm)
            if name is None:
                continue
            v = var[name]
            v["writes"] += 1
            v["binds" if k == k_bind else
              ("mutates" if k == k_mut else "deletes")] += 1
            v["by_func"][here] += 1
            v["by_line"][loc] += 1
            if not v["first"]:
                v["first"] = step
            v["last"] = step
            writes_by_fn[here][name] += 1
        elif k == k_call:
            fn = fname.get(pid_func.get(pid), "?")
            calls[fn] += 1
            if stack:
                parent = frame_fn.get(stack[-1], "<module>")
                callers[fn][parent] += 1
                callees[parent][fn] += 1
            frame_fn[fr] = fn
            entered[fr] = (step, t)
            stack.append(fr)
            first_step.setdefault(fn, step)
        elif k == k_ret or k == k_unwind:
            fn = frame_fn.pop(fr, None)
            if fr in stack:
                stack.remove(fr)
            s0, t0e = entered.pop(fr, (step, t))
            if fn is not None:
                total_steps[fn] += step - s0
                last_step[fn] = step
                (raises if k == k_unwind else returns)[fn] += 1
                if len(invocations[fn]) < MAX_INVOCATIONS:
                    invocations[fn].append([s0, step])
        elif k == k_yield:
            if fr in stack:
                stack.remove(fr)
        elif k == k_resume:
            if fr not in stack:
                stack.append(fr)
        elif k == k_raise:
            raises[frame_fn.get(fr, "<module>")] += 0

        if progress is not None and step % 250_000 == 0:
            progress(step, n)

    funcs_out = {}
    for fn in set(list(calls) + list(self_steps) + ["<module>"]):
        meta = next((f for f in reader.funcs.values()
                     if (f["qualname"] or f["name"]) == fn), None)
        funcs_out[fn] = {
            "name": fn,
            "file": reader.files.get(meta["file"]) if meta else
                    reader.meta.get("script", ""),
            "line": meta["first_line"] if meta else 0,
            "calls": calls.get(fn, 0),
            "returns": returns.get(fn, 0),
            "raises": raises.get(fn, 0),
            "self_steps": self_steps.get(fn, 0),
            "total_steps": total_steps.get(fn, 0),
            "self_ms": self_ns.get(fn, 0) / 1e6,
            "lines": len(lines_of.get(fn, ())),
            "first_step": first_step.get(fn, 0),
            "last_step": last_step.get(fn, 0),
            "callers": dict(callers.get(fn, {})),
            "callees": dict(callees.get(fn, {})),
            "writes": dict(writes_by_fn.get(fn, {})),
        }

    vars_out = {}
    for name, v in var.items():
        lines = Counter()
        for loc, c in v["by_line"].items():
            path, line = reader.where(loc)
            lines["%s:%s" % (os.path.basename(path or "?"), line)] += c
        vars_out[name] = {
            "writes": v["writes"], "binds": v["binds"],
            "mutates": v["mutates"], "deletes": v["deletes"],
            "first": v["first"], "last": v["last"],
            "by_func": dict(v["by_func"]), "by_line": dict(lines),
        }

    return Analysis({
        "version": CACHE_VERSION,
        "n_steps": n,
        "seconds": time.perf_counter() - t0,
        "functions": funcs_out,
        "variables": vars_out,
        "activity": {k: v for k, v in activity.items()},
        "invocations": {k: v for k, v in invocations.items()},
    }, reader)


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

def load(reader, progress=None, use_cache=True):
    """The analysis for this trace, computed once and kept on disk."""
    path = os.path.join(reader.dir, CACHE)
    if use_cache and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf8") as fh:
                d = json.load(fh)
            if (d.get("version") == CACHE_VERSION
                    and d.get("n_steps") == reader.n_steps):
                return Analysis(d, reader)
        except (OSError, ValueError):
            pass
    got = analyse(reader, progress=progress)
    if use_cache:
        try:
            with open(path, "w", encoding="utf8") as fh:
                json.dump({"version": CACHE_VERSION,
                           "n_steps": got.n_steps,
                           "seconds": got.seconds,
                           "functions": got.functions,
                           "variables": got.variables,
                           "activity": got.activity,
                           "invocations": got.invocations}, fh)
        except OSError:                              # read-only folder
            pass
    return got
