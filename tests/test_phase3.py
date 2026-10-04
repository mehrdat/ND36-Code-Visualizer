"""
Phase 3 acceptance tests: memory, resources, and the Perfetto export.

The one that matters is test_the_leak_is_found. The plan's acceptance
criterion for this phase was "a known memory leak is located from the chart
in under three clicks", so the fixture contains one real leak sitting next to
a function that allocates exactly as much and gives it all back. Naming the
leak is only half the job; not accusing the innocent one is the other half.

Run:  python3 tests/test_phase3.py
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nd36 import schema                                   # noqa: E402
from nd36.exporter import to_chrome_trace                 # noqa: E402
from nd36.memory import summarise                         # noqa: E402
from nd36.run import trace_script                         # noqa: E402
from nd36.store import TraceReader                        # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PROGRAMS = os.path.join(HERE, "programs")


def trace(program, **kw):
    out = tempfile.mkdtemp(prefix="vizt3_")
    res = trace_script(os.path.join(PROGRAMS, program), out, **kw)
    return out, res


def line_of(path, needle):
    with open(path) as fh:
        for i, text in enumerate(fh, 1):
            if needle in text:
                return i
    raise AssertionError("%r not found in %s" % (needle, path))


# ---------------------------------------------------------------- the tests

def test_the_leak_is_found():
    """Name the leaking line, and do not accuse the innocent one."""
    src = os.path.join(PROGRAMS, "leaky.py")
    leak_line = line_of(src, "# LEAK")
    innocent_line = line_of(src, "scratch = [n]")

    out, _ = trace(os.path.basename(src), memory=True)
    r = TraceReader(out)
    rows = [row for row in summarise(r) if row["mine"]]
    assert rows, "no growth attributed to the traced file at all"

    top = rows[0]
    assert top["line"] == leak_line, (
        "biggest growth was line %d, the leak is on line %d"
        % (top["line"], leak_line))
    assert top["growth"] > 1_000_000, top["growth"]

    innocent = [row for row in rows if row["line"] == innocent_line]
    held = innocent[0]["growth"] if innocent else 0
    assert held < 100_000, (
        "line %d allocates and frees, but is reported as holding %d bytes"
        % (innocent_line, held))

    print("  leaky.py     leak at line %d (+%.1f MB); line %d correctly clear"
          % (leak_line, top["growth"] / (1 << 20), innocent_line))
    shutil.rmtree(out)


def test_recorder_does_not_retain_the_program():
    """Phase 0 held a strong reference to everything it saw, which stopped
    the garbage collector and made every memory number a fiction."""
    import gc
    import weakref
    from nd36.store import TraceWriter
    from nd36.values import Encoder

    out = tempfile.mkdtemp(prefix="vizt3_")
    w = TraceWriter(out)
    enc = Encoder(w)

    class Thing:
        pass

    thing = Thing()
    ref = weakref.ref(thing)
    enc.encode(thing, 1)
    del thing
    gc.collect()
    assert ref() is None, "the encoder is still holding the object"
    w.close()
    print("  encoder      releases what it has recorded (weak refs)")
    shutil.rmtree(out)


def test_memory_pass_does_not_change_the_trace():
    """Memory is measured in a separate run, so the recorded trace must be
    byte-for-byte what it would have been without --memory."""
    plain, a = trace("leaky.py")
    withmem, b = trace("leaky.py", memory=True)
    assert a["steps"] == b["steps"], (a["steps"], b["steps"])
    ra, rb = TraceReader(plain), TraceReader(withmem)
    for step in (1, ra.n_steps // 2, ra.n_steps):
        x, y = ra.raw(step), rb.raw(step)
        assert x[1:] == y[1:], (step, x, y)   # everything but the timestamp
    print("  leaky.py     %s steps identical with and without --memory"
          % "{:,}".format(a["steps"]))
    shutil.rmtree(plain)
    shutil.rmtree(withmem)


def test_memory_is_affordable():
    """The whole point of the second pass was that one pass cost 50x."""
    plain, a = trace("leaky.py")
    withmem, b = trace("leaky.py", memory=True)
    ra = TraceReader(withmem)
    per = b["seconds"] / b["steps"] * 1e6
    assert per < 60, "%.0f us/step with --memory" % per
    assert len(ra.memcurve()) > 20, (
        "only %d points on the memory curve" % len(ra.memcurve()))
    print("  leaky.py     recorder still %.0f us/step; %d curve points"
          % (per, len(ra.memcurve())))
    shutil.rmtree(plain)
    shutil.rmtree(withmem)


def test_cheap_samples_are_always_there():
    """The step-aligned curve needs no flag -- it is two counter reads."""
    out, _ = trace("realistic.py")
    r = TraceReader(out)
    samples = r.samples()
    assert len(samples) > 10, len(samples)
    steps = [s["step"] for s in samples]
    assert steps == sorted(steps)
    assert max(steps) <= r.n_steps
    assert all(s["blocks"] > 0 for s in samples)
    print("  realistic.py %d step-aligned samples, no flag needed"
          % len(samples))
    shutil.rmtree(out)


def test_perfetto_export_is_well_formed():
    """A B without its E gives a slice that runs to the end of time."""
    out, _ = trace("mixed.py")
    r = TraceReader(out)
    data = to_chrome_trace(r)
    json.dumps(data)                       # must be serialisable

    depth, ts = 0, -1
    for ev in data:
        if ev["ph"] in ("B", "E", "i", "C"):
            assert ev["ts"] >= ts, "timestamps go backwards at %r" % ev
            ts = ev["ts"]
        if ev["ph"] == "B":
            depth += 1
        elif ev["ph"] == "E":
            depth -= 1
            assert depth >= 0, "an E with no matching B"
    assert depth == 0, "%d slices left open" % depth

    opens = sum(1 for e in data if e["ph"] == "B")
    calls = r.kind_counts().get(schema.CALL, 0)
    assert opens == calls, (opens, calls)
    print("  mixed.py     %d slices, balanced, timestamps monotonic" % opens)
    shutil.rmtree(out)


def test_report_shows_memory():
    from nd36.report import render
    out, _ = trace("leaky.py", memory=True)
    r = TraceReader(out)
    html = render(r)
    assert "Memory" in html and "Biggest objects" in html
    assert "peak heap" in html
    assert '"strip"' in html or "D.strip" in html
    assert "leaky.py:%d" % line_of(os.path.join(PROGRAMS, "leaky.py"),
                                   "# LEAK") in html
    for bad in ("http://", "https://ui.perfetto"):
        if bad == "http://":
            assert bad not in html
    print("  leaky.py     report names the leaking line and draws the strip")
    shutil.rmtree(out)


def test_report_without_memory_explains_itself():
    """No measurements is a state the page has to handle gracefully."""
    from nd36.report import render
    out, _ = trace("sort.py")
    html = render(TraceReader(out))
    assert "--memory" in html, "the empty state should say how to fix it"
    print("  sort.py      missing memory data explained, not left blank")
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
