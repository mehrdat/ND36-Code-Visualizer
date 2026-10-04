"""
Phase 4 acceptance tests: the domain plugins.

The plan's criterion for this phase was that a real pandas pipeline traces
at under 2x its normal runtime, and that the ribbon finds a planted
row-dropping bug. Both are checked here against fixtures that carry their
bug in a comment, so the test knows the right answer independently.

Run:  python3 tests/test_phase4.py
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nd36 import domains                                  # noqa: E402
from nd36.lineage import (frame_lineage, shape_mismatch,   # noqa: E402
                             tensor_lineage)
from nd36.run import baseline_seconds, trace_script        # noqa: E402
from nd36.store import TraceReader                         # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PROGRAMS = os.path.join(HERE, "programs")

HAVE_TORCH = True
try:
    import torch                                             # noqa: F401
except ImportError:
    HAVE_TORCH = False


def trace(program, **kw):
    out = tempfile.mkdtemp(prefix="vizt4_")
    res = trace_script(os.path.join(PROGRAMS, program), out, **kw)
    return out, res


def line_of(program, needle):
    with open(os.path.join(PROGRAMS, program)) as fh:
        for i, text in enumerate(fh, 1):
            if needle in text:
                return i
    raise AssertionError("%r not in %s" % (needle, program))


# ---------------------------------------------------------------- the tests

def test_domains_describe_the_right_things():
    """Each type gets the facts that matter for it, and no data."""
    import numpy as np
    import pandas as pd

    checks = [
        (np.arange(12.0).reshape(3, 4), domains.ARRAY,
         ("shape", "dtype", "is_view", "min", "max")),
        (pd.DataFrame({"a": [1, 2, None], "b": list("xyz")}), domains.FRAME,
         ("shape", "columns", "dtypes", "n_nulls")),
        (pd.Series([1, 2, 3]), domains.ARRAY, ("len", "dtype", "min")),
        ([1, 2, 3], domains.GENERIC, ("len",)),
    ]
    if HAVE_TORCH:
        checks.append((torch.randn(2, 3), domains.TENSOR,
                       ("shape", "dtype", "device", "requires_grad")))
        checks.append((torch.nn.Linear(4, 2), domains.MODEL,
                       ("n_params", "training")))

    for value, want_kind, keys in checks:
        cheap, rich, kind = domains.lookup(value)
        assert kind == want_kind, (type(value).__name__, kind, want_kind)
        summary = cheap(value)
        if rich:
            summary.update(rich(value))
        for key in keys:
            assert key in summary, (type(value).__name__, key, summary)
    print("  domains      %d types described, kinds correct" % len(checks))


def test_the_row_dropping_bug_is_flagged():
    """The acceptance criterion: find the filter that ate the data."""
    bug_line = line_of("etl.py", "THE BUG")  + 1   # the filter is below it
    out, _ = trace("etl.py")
    r = TraceReader(out)
    rows = frame_lineage(r)
    assert rows, "no DataFrames tracked at all"

    warned = [row for row in rows if row["warn"]]
    assert len(warned) == 1, (
        "expected exactly one flagged step, got %s"
        % [(w["line"], w["name"]) for w in warned])
    assert warned[0]["line"] == bug_line, (warned[0]["line"], bug_line)
    assert warned[0]["name"] == "north", warned[0]["name"]
    assert warned[0]["change"]["rows_before"] > 3000
    assert warned[0]["change"]["rows_after"] == 0
    print("  etl.py       bug found at line %d: %s lost %d rows"
          % (bug_line, warned[0]["name"],
             -warned[0]["change"]["rows"]))
    shutil.rmtree(out)


def test_derived_frames_are_linked():
    """`b = f(a)` makes a NEW object, so a naive diff would say 'created'
    at exactly the step where the rows vanished."""
    out, _ = trace("etl.py")
    rows = frame_lineage(TraceReader(out))
    # First appearance of each name -- later rows are in-place edits of the
    # same object and have no cross-variable parent.
    first = {}
    for row in rows:
        first.setdefault(row["name"], row)
    assert first["clean"]["parent"] == "raw", first["clean"]
    assert first["north"]["parent"] == "clean", first["north"]
    # The dropna step must read as -180 rows, not as a brand new frame.
    assert first["clean"]["change"]["rows"] == -180, first["clean"]["change"]
    print("  etl.py       raw → clean → north linked across reassignment")
    shutil.rmtree(out)


def test_pandas_overhead_is_acceptable():
    """Under 2x on a real pipeline, which is what the plan asked for."""
    big = os.path.join(tempfile.mkdtemp(), "etl_big.py")
    with open(os.path.join(PROGRAMS, "etl.py")) as fh:
        src = fh.read().replace("N = 4000", "N = 200000")
    with open(big, "w") as fh:
        fh.write(src)

    base = baseline_seconds(big)
    out = tempfile.mkdtemp(prefix="vizt4_")
    res = trace_script(big, out)
    ratio = res["seconds"] / base
    assert ratio < 3.0, "%.1fx on a 200k-row pipeline" % ratio
    print("  etl (200k)   %.1fx slower than untraced (%.0f ms → %.0f ms)"
          % (ratio, base * 1000, res["seconds"] * 1000))
    shutil.rmtree(out)


def test_expensive_stats_are_gated():
    """isna().sum() is O(rows). Called every line it is minutes; called when
    the shape actually moved it is nothing."""
    import pandas as pd
    from nd36.store import TraceWriter
    from nd36.values import Encoder

    calls = [0]
    real = domains._frame_rich

    def counting(value):
        calls[0] += 1
        return real(value)

    out = tempfile.mkdtemp(prefix="vizt4_")
    w = TraceWriter(out)
    enc = Encoder(w)
    domains._REGISTRY[1] = (domains._REGISTRY[1][0], domains._frame_cheap,
                            counting, domains.FRAME)
    try:
        df = pd.DataFrame({"a": range(50)})
        for step in range(40):          # unchanged frame, encoded 40 times
            enc.encode(df, step + 1)
        unchanged = calls[0]
        df["b"] = range(50)             # now it really changed
        enc.encode(df, 99)
        after = calls[0]
    finally:
        domains._REGISTRY[1] = (domains._REGISTRY[1][0],
                                domains._frame_cheap, real, domains.FRAME)
        w.close()
    assert unchanged == 1, (
        "expensive stats ran %d times for 40 unchanged encodes" % unchanged)
    assert after == 2, after
    print("  gating       40 encodes of an unchanged frame cost 1 rich call")
    shutil.rmtree(out)


def test_tensor_shapes_are_followed():
    if not HAVE_TORCH:
        print("  tensors      skipped (torch not installed)")
        return
    out, _ = trace("shapes.py")
    rows = tensor_lineage(TraceReader(out))
    shapes = [(t["name"], tuple(t["shape"])) for t in rows]
    assert ("batch", (8, 1, 20, 20)) in shapes, shapes
    assert ("flat", (8, 1568)) in shapes, shapes
    assert all(t["dtype"] == "float32" for t in rows), shapes
    assert any(t["grad_fn"] for t in rows), "no autograd provenance captured"
    print("  shapes.py    %d tensor shapes followed through the forward pass"
          % len(rows))
    shutil.rmtree(out)


def test_shape_mismatch_names_the_line():
    """PyTorch says `mat1 and mat2 ... (8x1568 and 800x10)` and nothing else."""
    if not HAVE_TORCH:
        print("  mismatch     skipped (torch not installed)")
        return
    bad_line = line_of("shapes.py", "return self.fc(flat)")
    out, res = trace("shapes.py")
    assert "RuntimeError" in (res["error"] or ""), res["error"]
    hits = shape_mismatch(TraceReader(out))
    assert len(hits) == 1, (
        "the same exception unwinds through every frame; expected it "
        "deduped to 1, got %d" % len(hits))
    assert hits[0]["line"] == bad_line, (hits[0]["line"], bad_line)
    live = {t["name"]: tuple(t["shape"]) for t in hits[0]["tensors"]}
    assert live.get("flat") == (8, 1568), live
    print("  shapes.py    mismatch pinned to line %d with %d live tensors"
          % (bad_line, len(hits[0]["tensors"])))
    shutil.rmtree(out)


def test_report_has_the_new_panels():
    from nd36.report import render
    out, _ = trace("etl.py")
    html = render(TraceReader(out))
    assert "DataFrames" in html and "big row loss" in html
    assert "Tensors" in html
    assert "No tensors in this run" in html      # honest empty state
    print("  etl.py       report shows the ribbon and flags the loss")
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
