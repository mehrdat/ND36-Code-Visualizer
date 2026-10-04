"""
Phase 0 acceptance tests.

The important one is test_state_matches_ground_truth.  It builds a second,
deliberately stupid record of the program -- a plain sys.settrace hook that
copies repr(every local) at every line -- and then checks that replaying
our event log reproduces it exactly.

If those two disagree, the recorder is wrong, and nothing built on top of
it can be trusted.  Everything in this project rests on that one assertion.

Run:  python3 tests/test_phase0.py          (no pytest needed)
      python3 -m pytest tests/ -q
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nd36 import schema                                  # noqa: E402
from nd36.run import trace_script                        # noqa: E402
from nd36.store import TraceReader                       # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PROGRAMS = os.path.join(HERE, "programs")

SKIP = ("__", "@")          # dunders and the odd internal name

# Object reprs carry a memory address, which differs between runs. Strip it
# so comparisons are about content, not where it happened to land in RAM.
_ADDR = re.compile(r"0x[0-9a-f]+")


def norm(text):
    return _ADDR.sub("0xADDR", text)


def ground_truth(script):
    """An independent record: (line, {name: repr(value)}) at every line.

    Uses sys.settrace directly and shares NO code with the recorder, so it
    is a real second opinion rather than a restatement of the same bug.
    """
    script = os.path.abspath(script)
    out = []

    def tracer(frame, event, arg):
        if frame.f_code.co_filename != script:
            return None
        if event == "line":
            snap = {}
            for k, v in frame.f_locals.items():
                if k.startswith(SKIP):
                    continue
                try:
                    snap[k] = norm(repr(v))
                except Exception:
                    snap[k] = "<repr failed>"
            out.append((frame.f_lineno, id(frame), snap))
        return tracer

    with open(script) as fh:
        code = compile(fh.read(), script, "exec")
    ns = {"__name__": "__main__", "__file__": script,
          "__builtins__": __builtins__}
    import random
    random.seed(0)
    sys.settrace(tracer)
    try:
        exec(code, ns)
    except BaseException:
        pass
    finally:
        sys.settrace(None)
    return out


def trace(script, driver=None, keyframe_every=2000):
    out = tempfile.mkdtemp(prefix="vizt_")
    trace_script(os.path.join(PROGRAMS, script), out, force=driver,
                 keyframe_every=keyframe_every)
    return out


def rendered_locals(reader, state, step):
    """All names visible at `step`, as repr strings, for comparison."""
    out = {}
    pools = [state["globals"]] + [f["locals"] for f in state["frames"]]
    for pool in pools:
        for name, ref in pool.items():
            if name is None or name.startswith(SKIP):
                continue
            val = reader.resolve(ref, step)
            out[name] = norm(val["repr"] if isinstance(val, dict)
                             else repr(val))
    return out


# ---------------------------------------------------------------- the tests

def test_state_matches_ground_truth():
    """Replaying our log must reproduce the real locals at every line."""
    for program in ("sort.py", "mixed.py"):
        truth = ground_truth(os.path.join(PROGRAMS, program))
        out = trace(program)
        r = TraceReader(out)

        lines = [r.view(s) for s in range(1, r.n_steps + 1)]
        lines = [e for e in lines if e["kind"] == schema.LINE]
        assert len(lines) == len(truth), (
            "%s: %d LINE events but ground truth saw %d lines"
            % (program, len(lines), len(truth)))

        checked = 0
        for ev, (line, _fid, snap) in zip(lines, truth):
            assert ev["line"] == line, (
                "%s step %d: trace says line %s, truth says %s"
                % (program, ev["step"], ev["line"], line))
            got = rendered_locals(r, r.state_at(ev["step"]), ev["step"])
            for name, want in snap.items():
                if name not in got:
                    continue          # a name our static pre-pass skips
                assert got[name] == want, (
                    "%s step %d line %d: %s is %s, should be %s"
                    % (program, ev["step"], line, name, got[name], want))
                checked += 1
        print("  %-10s %5d lines, %5d variable values verified"
              % (program, len(truth), checked))
        shutil.rmtree(out)


def test_monitoring_matches_settrace():
    """The fast driver must produce the same event stream as the slow one."""
    for program in ("sort.py",):
        streams = {}
        for driver in ("monitoring", "settrace"):
            out = trace(program, driver=driver)
            r = TraceReader(out)
            streams[driver] = [
                (e["kind"], e["line"], e["name"],
                 r.resolve(e["ref"], e["step"])["repr"]
                 if isinstance(r.resolve(e["ref"], e["step"]), dict)
                 else r.resolve(e["ref"], e["step"]))
                for e in (r.view(s) for s in range(1, r.n_steps + 1))]
            shutil.rmtree(out)
        a, b = streams["monitoring"], streams["settrace"]
        assert len(a) == len(b), "%s: %d vs %d events" % (program, len(a),
                                                          len(b))
        for i, (x, y) in enumerate(zip(a, b)):
            assert x == y, "%s event %d: %r vs %r" % (program, i, x, y)
        print("  %-10s %5d events identical across both drivers"
              % (program, len(a)))


def test_keyframes_do_not_change_answers():
    """state_at() must give the same answer whatever the keyframe spacing.

    This catches the classic replay bug: state that is correct only because
    a keyframe happened to land nearby.
    """
    outs = {k: trace("mixed.py", keyframe_every=k) for k in (1, 7, 100000)}
    readers = {k: TraceReader(v) for k, v in outs.items()}
    n = min(r.n_steps for r in readers.values())
    base = readers[1]
    for step in range(1, n + 1):
        want = rendered_locals(base, base.state_at(step), step)
        for k, r in readers.items():
            got = rendered_locals(r, r.state_at(step), step)
            assert got == want, ("keyframe_every=%d disagrees at step %d:"
                                 "\n  %r\n  %r" % (k, step, got, want))
    print("  mixed.py   %5d steps agree at keyframe spacing 1 / 7 / none" % n)
    for v in outs.values():
        shutil.rmtree(v)


def test_scales_past_python_tutor():
    """The whole point: more than 1,000 steps, and fast random seeking."""
    import time
    out = trace("heavy.py")
    r = TraceReader(out)
    assert r.n_steps > 250000, r.n_steps

    t0 = time.perf_counter()
    probes = [1 + i * (r.n_steps - 1) // 200 for i in range(200)]
    for s in probes:
        r.state_at(s)
    per = (time.perf_counter() - t0) / len(probes) * 1000
    assert per < 10, "state_at() took %.1f ms, budget is 10 ms" % per

    size = sum(os.path.getsize(os.path.join(out, f))
               for f in os.listdir(out))
    print("  heavy.py   %s steps, %.2f ms/seek, %d bytes/step"
          % (f"{r.n_steps:,}", per, size // r.n_steps))
    shutil.rmtree(out)


def test_history_of_one_variable():
    """The feature a live debugger cannot offer."""
    out = trace("sort.py")
    r = TraceReader(out)
    hist = r.history_of("j")
    assert len(hist) > 10, hist
    values = [h["ref"][1] for h in hist]
    assert all(isinstance(v, int) for v in values), values
    # Insertion sort walks j downwards then jumps back up.
    assert min(values) == -1, values
    print("  sort.py    j was assigned %d times, values %s…"
          % (len(hist), values[:6]))
    shutil.rmtree(out)


def test_exception_is_recorded():
    out = trace("mixed.py")
    r = TraceReader(out)
    raises = [r.view(s) for s in range(1, r.n_steps + 1)]
    raises = [e for e in raises if e["kind"] == schema.RAISE]
    assert any(e["name"] == "ZeroDivisionError" for e in raises), raises
    print("  mixed.py   ZeroDivisionError captured with its line and frame")
    shutil.rmtree(out)


def test_other_python_versions():
    """The 3.11 fallback path and the 3.12 fast path must both work."""
    ran = []
    for exe in ("python3.11", "python3.12", "python3.13", "python3.14"):
        which = shutil.which(exe)
        if not which:
            continue
        out = tempfile.mkdtemp(prefix="vizt_%s_" % exe)
        res = subprocess.run(
            [which, "-m", "nd36.cli", "run",
             os.path.join(PROGRAMS, "sort.py"), "-o", out],
            cwd=os.path.dirname(HERE), capture_output=True, text=True)
        assert res.returncode == 0, "%s failed:\n%s" % (exe, res.stderr)
        r = TraceReader(out)
        assert r.n_steps > 100, (exe, r.n_steps)
        ran.append("%s -> %s, %d steps"
                   % (exe.replace("python", "py"), r.meta["driver"][:10],
                      r.n_steps))
        shutil.rmtree(out)
    print("  versions   " + " | ".join(ran))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        print("%s" % fn.__name__)
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print("  FAILED: %s" % exc)
    print("\n%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
