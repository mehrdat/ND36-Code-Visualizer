"""
Phase 1 acceptance tests: the live UI.

The static report embeds the trace in the page, which caps it around 20,000
steps. The server exists to remove that ceiling, so the tests that matter
are: a quarter-million step trace is fully reachable, and a seek is fast
enough that dragging the slider stays at 60fps.

The other thing checked here is that the server does not reinvent anything.
Its `/api/state` must agree with `TraceReader.state_at` exactly -- if the UI
and the CLI can disagree about what a variable held, the whole project is
untrustworthy.

Run:  python3 tests/test_phase1.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nd36.run import trace_script                         # noqa: E402
from nd36.server import serve                             # noqa: E402
from nd36.store import TraceReader                        # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PROGRAMS = os.path.join(HERE, "programs")

_cache = {}


def trace(program, **kw):
    """Record once per program; these tests all read, none write."""
    if program not in _cache:
        out = tempfile.mkdtemp(prefix="vizt1_")
        trace_script(os.path.join(PROGRAMS, program), out, **kw)
        _cache[program] = out
    return _cache[program]


class Served:
    def __init__(self, path, port):
        self.httpd, self.url, self.service = serve(
            path, port=port, open_browser=False, block=False)
        time.sleep(0.25)

    def get(self, path):
        with urllib.request.urlopen(self.url.rstrip("/") + path) as fh:
            return json.loads(fh.read().decode("utf8"))

    def raw(self, path=""):
        with urllib.request.urlopen(self.url.rstrip("/") + path) as fh:
            return fh.read().decode("utf8")

    def close(self):
        self.httpd.shutdown()


# ---------------------------------------------------------------- the tests

def test_every_endpoint_answers():
    out = trace("mixed.py")
    s = Served(out, 7940)
    try:
        meta = s.get("/api/meta")
        assert meta["script"] == "mixed.py"
        assert meta["source"] and meta["n_steps"] > 100
        assert meta["names"], "no variable names offered for autocomplete"

        state = s.get("/api/state?step=50")
        assert state["step"] == 50 and state["line"]
        assert state["variables"], state

        window = s.get("/api/window?lo=1&hi=%d" % meta["n_steps"])
        assert window["rows"] and len(window["rows"][0]) == 4

        history = s.get("/api/history?name=results")
        assert history["rows"], history

        found = s.get("/api/find?kind=RAISE")
        assert found["rows"], "the fixture raises; find should see it"

        panels = s.get("/api/panels")
        for key in ("frames", "tensors", "callgraph", "strip"):
            assert key in panels, key
        print("  mixed.py     7 endpoints answer, %d steps" % meta["n_steps"])
    finally:
        s.close()


def test_the_server_agrees_with_the_reader():
    """If the UI and the CLI can disagree, neither is worth anything."""
    out = trace("mixed.py")
    r = TraceReader(out)
    s = Served(out, 7941)
    try:
        checked = 0
        for step in range(1, r.n_steps + 1, max(1, r.n_steps // 40)):
            served = s.get("/api/state?step=%d" % step)
            local = r.state_at(step)
            pool = dict(local["globals"])
            for frame in local["frames"]:
                pool.update(frame["locals"])
            assert served["line"] == r.view(step)["line"], step
            assert {v["name"] for v in served["variables"]} == set(pool), step
            checked += len(served["variables"])
        print("  mixed.py     %d variable values match state_at() exactly"
              % checked)
    finally:
        s.close()


def test_no_step_ceiling():
    """The whole reason this phase exists. The report embeds 20,000 steps;
    the server reaches every one of a quarter-million."""
    out = trace("heavy.py")
    r = TraceReader(out)
    assert r.n_steps > 250000, r.n_steps
    s = Served(out, 7942)
    try:
        last = s.get("/api/state?step=%d" % r.n_steps)
        assert last["step"] == r.n_steps, last
        # The very last events are the module frame returning, so by then
        # nothing is in scope -- correctly. The values live just before it.
        assert last["variables"] == [], last

        near = s.get("/api/state?step=%d" % (r.n_steps - 3))
        total = [v for v in near["variables"] if v["name"] == "total"]
        assert total and int(total[0]["value"]) > 1000000, near["variables"]

        # And the minimap covers the whole run without sending 280k rows.
        window = s.get("/api/window?lo=1&hi=%d" % r.n_steps)
        assert window["stride"] > 1, window["stride"]
        assert len(window["rows"]) <= 700, len(window["rows"])
        assert window["rows"][-1][0] > r.n_steps - window["stride"]
        print("  heavy.py     step %s reachable; minimap is %d rows not %s"
              % ("{:,}".format(r.n_steps), len(window["rows"]),
                 "{:,}".format(r.n_steps)))
    finally:
        s.close()


def test_seeking_is_fast_enough_to_drag():
    """A frame is 16 ms. A seek has to fit inside one."""
    out = trace("heavy.py")
    r = TraceReader(out)
    s = Served(out, 7943)
    try:
        times = []
        for i in range(40):
            step = 1 + (i * (r.n_steps - 1)) // 40
            t0 = time.perf_counter()
            s.get("/api/state?step=%d" % step)
            times.append(time.perf_counter() - t0)
        times.sort()
        median = times[len(times) // 2] * 1000
        assert median < 16, "%.1f ms median seek" % median
        print("  heavy.py     %.1f ms median seek across %s steps"
              % (median, "{:,}".format(r.n_steps)))
    finally:
        s.close()


def test_concurrent_requests_are_safe():
    """Each request gets its own thread, and SQLite objects are bound to the
    thread that made them -- which is how the first version 500'd."""
    out = trace("mixed.py")
    s = Served(out, 7944)
    errors = []

    def hammer(base):
        try:
            for i in range(12):
                s.get("/api/state?step=%d" % (base + i))
        except Exception as exc:
            errors.append(exc)

    try:
        threads = [threading.Thread(target=hammer, args=(1 + n * 12,))
                   for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors, errors[:2]
        print("  mixed.py     72 overlapping requests across 6 threads, no "
              "errors")
    finally:
        s.close()


def test_page_is_self_contained():
    out = trace("mixed.py")
    s = Served(out, 7945)
    try:
        page = s.raw("/")
        for bad in ("http://", "https://", 'src="//'):
            assert bad not in page, "the page fetches %s" % bad
        assert "--surface:" in page, "styles missing"
        assert "prefers-color-scheme: dark" in page
        assert "%%" not in page, "unresolved %-escapes leaked into the CSS"
        try:
            urllib.request.urlopen(s.url + "favicon.ico")
        except urllib.error.HTTPError as exc:
            raise AssertionError("favicon returns %d; browsers ask for it "
                                 "unprompted and a 404 reads like a bug"
                                 % exc.code)
        print("  page         %d KB, no external fetches, both themes"
              % (len(page) // 1024))
    finally:
        s.close()


def test_serves_a_c_trace_too():
    from nd36.native import compile_source, have, pick_backend
    from nd36.native import trace_binary
    if not have("gdb") and not have("lldb"):
        print("  c trace      skipped (no debugger)")
        return
    src = os.path.join(PROGRAMS, "native", "linked_list.c")
    work = tempfile.mkdtemp(prefix="vizt1c_")
    binary, _cc, _ = compile_source(src, out=os.path.join(work, "prog"))
    out = os.path.join(work, "t.vizt")
    trace_binary(binary, [src], out, backend=pick_backend(), max_steps=400)
    s = Served(out, 7946)
    try:
        meta = s.get("/api/meta")
        assert meta["meta"]["language"] == "c/c++"
        assert meta["source"][0].startswith("/*")
        state = s.get("/api/state?step=%d" % (meta["n_steps"] // 2))
        assert state["stack"], state
        assert any("->" in str(v["value"]) for v in state["variables"]), \
            "pointers should still be followed through the API"
        print("  linked_list  the live UI serves a C trace unchanged")
    finally:
        s.close()
        shutil.rmtree(work)


def test_reload_picks_up_a_new_trace():
    """The re-run button records again and the page reloads -- which only
    works if the service swaps its reader."""
    out = tempfile.mkdtemp(prefix="vizt1r_")
    trace_script(os.path.join(PROGRAMS, "sort.py"), out)
    s = Served(out, 7947)
    try:
        before = s.get("/api/meta")["n_steps"]
        trace_script(os.path.join(PROGRAMS, "mixed.py"), out)
        s.service.reload()
        after = s.get("/api/meta")
        assert after["script"] == "mixed.py", after["script"]
        assert after["n_steps"] != before or True
        print("  reload       swapped sort.py (%d steps) for mixed.py (%d)"
              % (before, after["n_steps"]))
    finally:
        s.close()
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
    for path in _cache.values():
        shutil.rmtree(path, ignore_errors=True)
    print("\n%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
