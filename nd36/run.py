"""
Run a script under the recorder and write a trace directory.

Also writes a determinism manifest: the Python version, platform, argv,
cwd and random seeds.  Without it, "re-run to step 40,000 and show me that
DataFrame" silently lies.
"""

import os
import platform
import random
import sys
import time

from .memory import Sampler, profile_script
from .recorder import pick_recorder
from .store import TraceWriter


def trace_script(script, out_dir, argv=None, force=None, extra_files=(),
                 keyframe_every=2000, seed=0, memory=False,
                 memory_snapshots=0):
    script = os.path.abspath(script)
    scope = {script} | {os.path.abspath(p) for p in extra_files}

    writer = TraceWriter(out_dir, keyframe_every=keyframe_every)
    writer.set_meta("schema_version", 1)
    writer.set_meta("script", script)
    writer.set_meta("python", sys.version)
    writer.set_meta("platform", platform.platform())
    writer.set_meta("machine", platform.machine())
    writer.set_meta("argv", list(argv or []))
    writer.set_meta("cwd", os.getcwd())
    writer.set_meta("seed", seed)
    writer.set_meta("hashseed", os.environ.get("PYTHONHASHSEED"))
    writer.set_meta("started_at", time.time())
    writer.set_meta("memory", bool(memory))

    random.seed(seed)

    with open(script, "r", encoding="utf8") as fh:
        source = fh.read()
    code = compile(source, script, "exec")

    # A fresh namespace, so the traced script behaves like `python script.py`.
    ns = {"__name__": "__main__", "__file__": script, "__builtins__":
          __builtins__}

    old_argv = sys.argv
    sys.argv = [script] + list(argv or [])
    sampler = Sampler(writer, scope, detailed=memory)
    # Measuring memory means not perturbing it: no retained references.
    rec = pick_recorder(writer, scope, force=force, sampler=sampler,
                        keep_alive=0 if memory else None)

    wall0 = time.perf_counter()
    error = None
    sampler.start()
    rec.start()
    try:
        exec(code, ns)
    except BaseException as exc:          # the script's own error, not ours
        error = "%s: %s" % (type(exc).__name__, exc)
    finally:
        rec.stop()
        sampler.stop(writer.step)
        sys.argv = old_argv

    wall = time.perf_counter() - wall0
    rec.core.write_keyframe()

    # Pass 2: memory, measured on its own. See memory.profile_script for why
    # this is a second run rather than more work inside the first.
    mem_seconds = None
    if memory:
        curve, snaps, mem_seconds = profile_script(
            script, scope, argv=argv, seed=seed,
            max_snapshots=memory_snapshots)
        t0 = curve[0][0] if curve else 0
        for t_ns, cur, peak, blocks in curve:
            writer.mem_point(t_ns - t0, cur, peak, blocks)
        for t_ns, why, rows in snaps:
            writer.snapshot(t_ns - t0, why,
                            sum(r[2] for r in rows), rows)
        if curve:
            sampler.peak = max(p for _t, _c, p, _b in curve)
    writer.set_meta("memory_seconds", mem_seconds)
    writer.set_meta("driver", type(rec).__name__)
    writer.set_meta("wall_seconds", round(wall, 4))
    writer.set_meta("n_steps", writer.step)
    writer.set_meta("error", error)
    writer.set_meta("peak_bytes", sampler.peak)
    writer.close()
    return {"steps": writer.step, "seconds": wall, "error": error,
            "out": out_dir, "driver": type(rec).__name__,
            "peak_bytes": sampler.peak}


def baseline_seconds(script, argv=None, repeats=3):
    """How long the script takes with NO tracing -- for the overhead number.

    Best of N, because a single timing on a loaded machine can be off by 2x
    and then the overhead figure is meaningless.
    """
    return min(_one_baseline(script, argv) for _ in range(repeats))


def _one_baseline(script, argv=None):
    script = os.path.abspath(script)
    with open(script, "r", encoding="utf8") as fh:
        code = compile(fh.read(), script, "exec")
    ns = {"__name__": "__main__", "__file__": script,
          "__builtins__": __builtins__}
    old_argv = sys.argv
    sys.argv = [script] + list(argv or [])
    random.seed(0)
    t0 = time.perf_counter()
    try:
        exec(code, ns)
    except BaseException:
        pass
    finally:
        sys.argv = old_argv
    return time.perf_counter() - t0
