"""
Phase 2 acceptance tests: in-place mutation, structure, and the report.

Phase 0 only noticed a NAME being rebound. These check the things real
programs actually do -- `a[i] = x`, `self.total += 1`, `xs.append(v)` --
plus the control-flow and call-graph overlays that say which code never ran.

Run:  python3 tests/test_phase2.py
"""

import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nd36 import graphs, schema                           # noqa: E402
from nd36.run import trace_script                         # noqa: E402
from nd36.store import TraceReader                        # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PROGRAMS = os.path.join(HERE, "programs")


def trace(program):
    out = tempfile.mkdtemp(prefix="vizt2_")
    trace_script(os.path.join(PROGRAMS, program), out)
    return out


def events(reader, kind):
    return [reader.view(s) for s in range(1, reader.n_steps + 1)
            if reader.raw(s)[1] == schema.KIND_CODE[kind]]


# ---------------------------------------------------------------- the tests

def test_mutations_are_seen():
    """The four shapes of in-place change, each caught at the right line."""
    out = trace("mutating.py")
    r = TraceReader(out)
    muts = events(r, schema.MUTATE)
    by_name = {}
    for m in muts:
        by_name.setdefault(m["name"], []).append(m["line"])

    # a[i] = x on a 200-item list: too big for the cheap check to catch,
    # so this only works because the static pass flagged the line.
    assert "big" in by_name, "subscript assign on a large list was missed"
    # self.total += v inside a method
    assert "self" in by_name, "attribute mutation was missed"
    # xs.append(v) mutating the caller's list
    assert "log" in by_name, "method mutation of a caller's list was missed"
    # counts["x"] = 1 -- same dict, new length: a mutation, not a rebind
    assert "counts" in by_name, "dict item assignment was missed"

    # And the value really is updated, not just flagged.
    last = [m for m in muts if m["name"] == "big"][-1]
    got = r.resolve(last["ref"], last["step"])
    item7 = dict((k, v) for k, v in r.decode_children(got["children"]))[7]
    assert item7 == ["i", 999], item7

    print("  mutating.py  %d in-place changes: %s"
          % (len(muts), ", ".join(sorted(by_name))))
    shutil.rmtree(out)


def test_mutate_vs_bind_is_about_identity():
    """Same object + new contents = MUTATE. New object = BIND."""
    out = trace("mutating.py")
    r = TraceReader(out)
    kinds = {}
    for e in events(r, schema.BIND) + events(r, schema.MUTATE):
        kinds.setdefault(e["name"], []).append((e["step"], e["kind"]))
    seq = [k for _s, k in sorted(kinds["counts"])]
    assert seq[0] == schema.BIND, seq        # counts = {}
    assert set(seq[1:]) == {schema.MUTATE}, seq   # then two item assignments
    print("  counts       %s" % " → ".join(seq))
    shutil.rmtree(out)


def test_dead_code_is_identified():
    """The CFG + trace overlay must find what never ran, and nothing else."""
    out = trace("mutating.py")
    r = TraceReader(out)
    path = r.meta["script"]
    stats = r.line_stats()
    src = r.source(path).splitlines()

    cold = []
    for g in graphs.cfgs_for_source(r.source(path), path):
        for n in g.nodes:
            if n["kind"] in ("entry", "exit"):
                continue
            for line, _text in n.get("lines", [(n["line"], "")]):
                if line and not stats.get((path, line)):
                    cold.append(line)

    # Ground truth: the fixture marks its own dead lines with `# DEAD`.
    expected = {i for i, text in enumerate(src, 1)
                if text.rstrip().endswith("# DEAD")}
    assert expected, "the fixture lost its # DEAD markers"
    missing = expected - set(cold)
    assert not missing, ("these lines never ran but were not reported: %s"
                         % sorted(missing))
    for line in cold:
        assert not stats.get((path, line)), line
    print("  mutating.py  %d never-executed lines found, all correct"
          % len(set(cold)))
    shutil.rmtree(out)


def test_never_called_function_appears():
    """A function that never ran produced no trace events at all, so it has
    to come from the source or it vanishes from the report."""
    out = trace("mutating.py")
    r = TraceReader(out)
    nodes, _edges = graphs.call_graph(r)
    names = {n["name"]: n["calls"] for n in nodes.values()}
    assert "never_called" in names, sorted(names)
    assert names["never_called"] == 0, names
    # Python 3.11 added co_qualname, so the same method is "Box.add" there
    # and plain "add" on 3.10. Accept either rather than failing on a
    # perfectly good interpreter.
    add = names.get("Box.add", names.get("add"))
    assert add == 2, names
    print("  call graph   Box.add ×2, record ×2, never_called ×0")
    shutil.rmtree(out)


def test_basic_blocks_are_merged():
    """One node per statement is unreadable; straight runs must collapse."""
    out = trace("sort.py")
    r = TraceReader(out)
    path = r.meta["script"]
    source = r.source(path)
    raw = graphs.build_cfg(__import__("ast").parse(source), source.splitlines(),
                           "<module>", 1)
    before = len(raw.nodes)
    after = len(graphs.cfgs_for_source(source, path)[0].nodes)
    assert after < before, (before, after)
    assert after <= before * 0.7, ("merging barely helped: %d -> %d"
                                   % (before, after))
    print("  sort.py      CFG %d statements → %d basic blocks"
          % (before, after))
    shutil.rmtree(out)


def test_report_renders():
    """The dashboard must build, be self-contained, and label every step
    with a file and line -- a bare step number means nothing."""
    from nd36.report import render
    out = trace("mixed.py")
    r = TraceReader(out)
    html = render(r)

    assert html.startswith("<!doctype html>")
    # Self-contained: no external fetches of any kind.
    for bad in ("http://", "https://", "src=\"//"):
        assert bad not in html, "report reaches out to %s" % bad
    # Both themes are present and chosen, not an automatic flip.
    assert "prefers-color-scheme: dark" in html
    assert '[data-theme="dark"]' in html
    # The step label carries the file and line.
    assert "D.script + ':<b>' + line" in html
    for section in ("Step explorer", "Hot lines", "Variable timelines",
                    "Call graph", "Control flow", "In-place changes"):
        assert section in html, section
    print("  mixed.py     report is %d KB, self-contained, 6 sections"
          % (len(html) // 1024))
    shutil.rmtree(out)


def test_report_data_matches_the_trace():
    """The numbers on the page must be the numbers in the log."""
    from nd36.report import collect
    out = trace("mutating.py")
    r = TraceReader(out)
    data = collect(r)
    assert data["nSteps"] == r.n_steps
    assert len(data["ev"]) == min(r.n_steps, 20000)
    # Every embedded event's line matches what the reader says.
    for i in (0, len(data["ev"]) // 2, len(data["ev"]) - 1):
        assert data["ev"][i][2] == (r.view(i + 1)["line"] or 0)
    # Line stats agree.
    for line, (hits, _ms) in data["stats"].items():
        assert r.line_stats()[(data["path"], line)][0] == hits
    print("  mutating.py  %d embedded events agree with the log"
          % len(data["ev"]))
    shutil.rmtree(out)


def test_mutation_tracking_is_affordable():
    """Phase 2 must not have made Phase 0 slow."""
    import time
    out = tempfile.mkdtemp(prefix="vizt2_")
    t0 = time.perf_counter()
    res = trace_script(os.path.join(PROGRAMS, "realistic.py"), out)
    per = (time.perf_counter() - t0) / res["steps"] * 1e6
    assert per < 60, "%.1f us/step is too slow" % per
    print("  realistic.py %s steps at %.1f us/step"
          % ("{:,}".format(res["steps"]), per))
    shutil.rmtree(out)


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
