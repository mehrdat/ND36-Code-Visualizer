"""
Phase 3: where the memory went, and which line put it there.

Two levels, because they cost very different amounts:

  always on   `sys.getallocatedblocks()` and `gc.get_count()`, sampled every
              N steps. Both are O(1) counter reads -- about 0.2 us -- so the
              memory curve is free.

  --memory    `tracemalloc`, which records a stack for every allocation and
              costs roughly 2x on its own. In exchange it gives exact byte
              counts and, crucially, attribution: which SOURCE LINE allocated
              the memory. That is what turns "memory went up" into "line 34
              is your leak".

Snapshots are taken on GROWTH, not on a timer. A program that allocates
nothing takes two snapshots; one that leaks takes one at every step up the
curve, which is exactly where you want the evidence.
"""

import gc
import os
import random
import sys
import time
import tracemalloc

#: The recorder allocates too. Its own lines must not appear in the report,
#: or you spend an afternoon chasing a leak inside the leak detector.
_OURS = os.path.dirname(os.path.abspath(__file__))

#: Files of ours that allocate WHILE recording. run.py is deliberately not
#: here: it is the launcher, so it sits at the bottom of the traced script's
#: own stack too -- filtering on it would discard the user's allocations.
_NOISY = ("store.py", "values.py", "recorder.py", "memory.py",
          "writeset.py", "schema.py")

SAMPLE_EVERY = 64          # steps between cheap counter reads
GROWTH_BYTES = 512 * 1024  # take a snapshot after this much new memory
#: Growth snapshots are OFF by default. take_snapshot() walks the whole
#: tracked heap -- measured at ~100 ms on a 6 MB heap -- and it runs in the
#: sampler thread, so each one costs about 80 curve points. The leak report
#: compares the first snapshot with the last, which needs neither of them.
#: Turn them on with --memory-snapshots N when you want the shape of the
#: growth rather than just its source.
MAX_SNAPSHOTS = 0
TOP_LINES = 25             # lines kept per snapshot


def profile_script(script, scope, argv=None, seed=0, interval=0.0008,
                   growth=128 * 1024, max_snapshots=MAX_SNAPSHOTS,
                   max_points=4000):
    """Run the script a SECOND time, under tracemalloc and nothing else.

    Why twice? Because tracemalloc records a stack for every allocation --
    including the thousands the recorder itself makes per second. Running
    both at once cost 1,245 us/step against 23 us/step for the recorder
    alone: fifty times slower, and most of it spent measuring ourselves.

    Run apart, tracemalloc costs about 2x and measures only the program.
    This is exactly what the determinism manifest (seed, argv, cwd, version)
    is for: the second run is the same run.

    Sampling is done by a background thread rather than a trace hook, so the
    program under measurement carries no instrumentation at all.
    """
    import threading

    curve = []
    snaps = []
    stop = threading.Event()

    def watch():
        last = 0
        gap = interval
        while not stop.is_set():
            try:
                cur, peak = tracemalloc.get_traced_memory()
            except RuntimeError:          # tracemalloc stopped under us
                return
            curve.append((time.perf_counter_ns(), cur, peak,
                          sys.getallocatedblocks()))
            if cur - last >= growth and len(snaps) < max_snapshots:
                last = cur
                snaps.append((time.perf_counter_ns(), "growth",
                              _attribute(scope)))
            # Start fine so a script that finishes in 10 ms still gets a
            # curve, then back off so an hour-long one does not get millions
            # of points.
            if len(curve) >= max_points:
                gap *= 2
                del curve[1::2]
            stop.wait(gap)

    with open(script, "r", encoding="utf8") as fh:
        code = compile(fh.read(), script, "exec")
    ns = {"__name__": "__main__", "__file__": script,
          "__builtins__": __builtins__}

    old_argv = sys.argv
    sys.argv = [script] + list(argv or [])
    random.seed(seed)
    # A pure-Python loop holds the GIL for a whole switch interval (5 ms by
    # default), which starves the sampler thread -- a 300 ms script was
    # getting two points. Shorten it for the duration of this pass.
    old_switch = sys.getswitchinterval()
    sys.setswitchinterval(0.0002)
    tracemalloc.start(12)
    snaps.append((time.perf_counter_ns(), "start", _attribute(scope)))
    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    t0 = time.perf_counter()
    try:
        exec(code, ns)
    except BaseException:
        pass
    finally:
        stop.set()
        thread.join(timeout=1.0)
        seconds = time.perf_counter() - t0
        snaps.append((time.perf_counter_ns(), "end", _attribute(scope)))
        tracemalloc.stop()
        sys.setswitchinterval(old_switch)
        sys.argv = old_argv
    return curve, snaps, seconds


def _attribute(scope):
    """Live heap, grouped by the source line that allocated it.

    Only tracemalloc's own frames are excluded. Nothing of ours is filtered
    here, because in this pass nothing of ours is running: the exec sits in
    this very file, so an all_frames filter on it would discard every
    allocation the script makes.
    """
    snap = tracemalloc.take_snapshot().filter_traces([
        tracemalloc.Filter(False, tracemalloc.__file__, all_frames=True),
        # all_frames defaults to False, so this drops only allocations made
        # BY us (the sampler's own list of curve points), not the ones the
        # script makes through the exec that lives in this file.
        tracemalloc.Filter(False, os.path.join(_OURS, "*")),
    ])
    rows = []
    for stat in snap.statistics("lineno")[:200]:
        frame = stat.traceback[0]
        if not frame.lineno:
            continue
        rows.append((frame.filename, frame.lineno, stat.size, stat.count))
    mine = [r for r in rows if r[0] in scope]
    theirs = [r for r in rows if r[0] not in scope]
    return (mine + theirs)[:TOP_LINES]


class Sampler:
    def __init__(self, writer, scope, detailed=False,
                 sample_every=SAMPLE_EVERY, growth=GROWTH_BYTES):
        self.w = writer
        self.scope = scope
        self.detailed = detailed
        self.sample_every = sample_every
        self.growth = growth
        self.next_sample = 0
        self.snapshots = 0
        self.last_snapshot_bytes = 0
        self.peak = 0
        self.started = False

    def start(self):
        """Nothing to start: the cheap counters need no setup, and the
        expensive attribution happens in its own pass (profile_script)."""

    def stop(self, step):
        pass

    def maybe(self, step, t_ns):
        """Called from the recorder's hot path. Must stay cheap."""
        if step < self.next_sample:
            return
        self.next_sample = step + self.sample_every

        blocks = sys.getallocatedblocks()
        g0, g1, g2 = gc.get_count()
        current = peak = 0
        if self.started:
            current, peak = tracemalloc.get_traced_memory()
            self.peak = peak
        self.w.sample(step, t_ns, blocks, current, peak, g0, g1, g2)

        if self.started and current - self.last_snapshot_bytes >= self.growth \
                and self.snapshots < MAX_SNAPSHOTS:
            self.last_snapshot_bytes = current
            self._snapshot(step, "growth")

    def _snapshot(self, step, why):
        """Attribute the live heap to source lines.

        take_snapshot() walks every tracked allocation, so this is the
        expensive part -- which is why it is driven by growth, not a timer.
        """
        self.snapshots += 1
        # all_frames=True drops any allocation whose stack passes THROUGH
        # the recorder, not just ones that start there. Without it our own
        # json.dumps showed up as a 1.1 MB leak in the user's report.
        snap = tracemalloc.take_snapshot().filter_traces(
            [tracemalloc.Filter(False, tracemalloc.__file__, all_frames=True)]
            + [tracemalloc.Filter(False, os.path.join(_OURS, n),
                                  all_frames=True) for n in _NOISY])
        total = 0
        rows = []
        for stat in snap.statistics("lineno")[:200]:
            frame = stat.traceback[0]
            if not frame.lineno:
                continue
            total += stat.size
            rows.append((frame.filename, frame.lineno, stat.size,
                         stat.count))
        # Keep the user's own files first, then the biggest of the rest.
        mine = [r for r in rows if r[0] in self.scope]
        theirs = [r for r in rows if r[0] not in self.scope]
        keep = (mine + theirs)[:TOP_LINES]
        self.w.snapshot(step, why, total, keep)


def summarise(reader):
    """Turn the stored snapshots into a leak report.

    The useful comparison is first versus last: a line that grew between
    them is holding memory the program never gave back.
    """
    snaps = reader.snapshots()
    if len(snaps) < 2:
        return []
    first = {(r["path"], r["line"]): r["size"] for r in snaps[0]["rows"]}
    last = {(r["path"], r["line"]): r["size"] for r in snaps[-1]["rows"]}
    out = []
    for key, size in last.items():
        delta = size - first.get(key, 0)
        if delta <= 0:
            continue
        out.append({"path": key[0], "line": key[1], "bytes": size,
                    "growth": delta,
                    "mine": key[0] == reader.meta.get("script")})
    # Your own code first. Growth inside the standard library is usually a
    # consequence of your code, not a finding in itself.
    out.sort(key=lambda r: (not r["mine"], -r["growth"]))
    return out
