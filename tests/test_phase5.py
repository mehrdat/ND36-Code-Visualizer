"""
Phase 5 acceptance tests: C and C++.

The point of Phase 0's schema discipline was that a second language would be
a new recorder, not a new project. So most of what is checked here is that
the Python-era machinery -- state_at, watch, the heat map, the call graph --
works unchanged on a trace produced by a real debugger.

Both backends are exercised where both exist: GDB is the Linux path, LLDB is
the macOS one and the plan's primary. They must agree.

Run:  python3 tests/test_phase5.py
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nd36 import schema                                   # noqa: E402
from nd36.graphs import call_graph                        # noqa: E402
from nd36.native import (compile_source, have,            # noqa: E402
                            pick_backend, trace_binary)
from nd36.store import TraceReader                        # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
NATIVE = os.path.join(HERE, "programs", "native")
BACKENDS = [b for b in ("gdb", "lldb") if have(b)]


def build_and_trace(name, backend=None, max_steps=4000):
    src = os.path.join(NATIVE, name)
    work = tempfile.mkdtemp(prefix="vizt5_")
    binary, compiler, _argv = compile_source(
        src, out=os.path.join(work, "prog"))
    out = os.path.join(work, "trace.vizt")
    res = trace_binary(binary, [src], out, backend=backend or BACKENDS[0],
                       max_steps=max_steps, meta={"compiler": compiler})
    return work, out, res


def line_of(name, needle):
    with open(os.path.join(NATIVE, name)) as fh:
        for i, text in enumerate(fh, 1):
            if needle in text:
                return i
    raise AssertionError("%r not in %s" % (needle, name))


# ---------------------------------------------------------------- the tests

def test_a_debugger_is_available():
    assert BACKENDS, ("no debugger found. macOS: lldb ships with Xcode "
                      "(xcode-select --install). Linux: apt install gdb")
    assert pick_backend() in BACKENDS
    print("  backends     %s" % ", ".join(BACKENDS))


def test_optimised_builds_are_refused():
    """At -O1 and above the line table stops matching the source, so a
    trace would be a confident lie. Refuse rather than mislead."""
    src = os.path.join(NATIVE, "linked_list.c")
    work = tempfile.mkdtemp(prefix="vizt5_")
    for flag in ("-O2", "-O1", "-Os", "-Ofast"):
        try:
            compile_source(src, out=os.path.join(work, "p"),
                           extra_flags=(flag,))
        except RuntimeError as exc:
            assert "optimis" in str(exc).lower(), exc
        else:
            raise AssertionError("%s was accepted" % flag)
    # A harmless flag must still work.
    compile_source(src, out=os.path.join(work, "p"), extra_flags=("-Wall",))
    print("  compile      -O1/-O2/-Os/-Ofast refused, -Wall accepted")
    shutil.rmtree(work)


def test_c_trace_uses_the_same_schema():
    """Everything built for Python must work on C without changes."""
    work, out, res = build_and_trace("linked_list.c")
    r = TraceReader(out)
    assert r.meta["language"] == "c/c++"
    assert res["lines"] > 15, res

    kinds = r.kind_counts()
    for kind in (schema.LINE, schema.CALL, schema.RETURN, schema.BIND):
        assert kinds.get(kind), "no %s events in a C trace" % kind

    # state_at, the query the whole UI rests on.
    mid = r.n_steps // 2
    state = r.state_at(mid)
    assert state["frames"], state
    names = set()
    for frame in state["frames"]:
        names |= set(frame["locals"])
    assert names & {"head", "n", "value", "sum", "i"}, names

    # The heat map is populated, which means per-line stats were recorded.
    stats = r.line_stats()
    assert stats and max(h for h, _t in stats.values()) >= 4, stats
    print("  linked_list  %d steps, %d C lines, state_at works, heat map full"
          % (r.n_steps, res["lines"]))
    shutil.rmtree(work)


def test_pointers_are_followed():
    """A bare address says nothing about the list you are building."""
    work, out, _ = build_and_trace("linked_list.c")
    r = TraceReader(out)
    history = r.history_of("head")
    rendered = []
    for row in history:
        value = r.resolve(row["ref"], row["step"])
        rendered.append(value["repr"] if isinstance(value, dict)
                        else str(value))
    chain = [t for t in rendered if "->" in t and "value =" in t]
    assert len(chain) >= 3, rendered
    # The list is built 10, 20, 30, 40 -- each node must point at the last.
    assert any("value = 40" in t and "next = 0x" in t for t in chain), chain
    print("  linked_list  head followed through %d nodes, pointees rendered"
          % len(chain))
    shutil.rmtree(work)


def test_cpp_containers_and_structs():
    work, out, res = build_and_trace("vector_grow.cpp")
    r = TraceReader(out)
    texts = []
    for row in r.history_of("xs"):
        value = r.resolve(row["ref"], row["step"])
        texts.append(value["repr"] if isinstance(value, dict) else str(value))
    grown = [t for t in texts if "length" in t]
    assert len(grown) >= 4, texts
    assert any("capacity 4" in t or "capacity 8" in t for t in grown), grown

    structs = [r.resolve(row["ref"], row["step"]) for row in r.history_of("p")]
    rendered = [s["repr"] if isinstance(s, dict) else str(s) for s in structs]
    assert any("x = 3" in t and "y = 4" in t for t in rendered), rendered
    print("  vector_grow  std::vector growth and a struct both rendered")
    shutil.rmtree(work)


def test_repeated_calls_are_separate_frames():
    """`for (...) { head = push(...); }` is ONE line, so four calls happen
    without the stack ever being observed back at the caller."""
    work, out, _ = build_and_trace("linked_list.c")
    nodes, edges = call_graph(TraceReader(out))
    by_name = {n["name"]: n["calls"] for n in nodes.values()}
    assert by_name.get("push") == 4, by_name
    assert by_name.get("total") == 1, by_name
    print("  linked_list  push counted 4 times from a one-line loop")
    shutil.rmtree(work)


def test_dead_code_is_visible():
    work, out, _ = build_and_trace("branches.c")
    r = TraceReader(out)
    stats = r.line_stats()
    src = os.path.join(NATIVE, "branches.c")
    with open(src) as fh:
        lines = fh.read().splitlines()
    dead = {i for i, text in enumerate(lines, 1) if "/* DEAD */" in text}
    assert dead, "the fixture lost its DEAD markers"
    for line in dead:
        assert not stats.get((src, line)), "line %d ran but is marked DEAD" % line
    ran = {line for (path, line) in stats if path == src}
    assert len(ran) > 6, ran
    print("  branches.c   %d lines ran, both DEAD lines correctly cold"
          % len(ran))
    shutil.rmtree(work)


def test_both_backends_agree():
    if len(BACKENDS) < 2:
        print("  agreement    skipped (only %s available)" % BACKENDS[0])
        return
    seen = {}
    works = []
    for backend in BACKENDS:
        work, out, res = build_and_trace("linked_list.c", backend=backend)
        works.append(work)
        r = TraceReader(out)
        nodes, _edges = call_graph(r)
        seen[backend] = {n["name"]: n["calls"] for n in nodes.values()}
    gdb, lldb = seen["gdb"], seen["lldb"]
    for func in ("main", "push", "total"):
        assert gdb.get(func) == lldb.get(func), (func, gdb, lldb)
    print("  agreement    gdb and lldb report the same call counts: %s"
          % ", ".join("%s×%d" % (k, v) for k, v in sorted(gdb.items())))
    for work in works:
        shutil.rmtree(work)


def test_report_builds_for_c():
    from nd36.report import render
    work, out, _ = build_and_trace("linked_list.c")
    html = render(TraceReader(out))
    assert "linked_list.c" in html
    assert "Control-flow graphs are built from the Python AST" in html, \
        "the C trace should say why the CFG panel is empty"
    assert "http://" not in html
    print("  linked_list  dashboard builds, CFG gap explained not hidden")
    shutil.rmtree(work)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        print(fn.__name__)
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print("  FAILED: %s" % exc)
    print("\n%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
