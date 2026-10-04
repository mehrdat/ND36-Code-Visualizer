"""
nd -- the ND36 command line.

    nd run script.py              record a trace
    nd info  trace.nd           what is in it
    nd step  trace.nd 1200      state at one step
    nd watch trace.nd total     the whole life of one variable
    nd heat  trace.nd           execution heatmap over the source
    nd bench script.py            overhead and trace size
"""

import argparse
import json
import os
import sys

from . import schema
from .run import baseline_seconds, trace_script
from .store import TraceReader

# Terminal colours. Plain strings, so no dependency.
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
OFF = "\033[0m"


def _where(reader, step):
    """A step number means nothing on its own -- always say file:line."""
    ev = reader.view(step)
    return "%s:%s" % (os.path.basename(ev["file"] or "?"), ev["line"])


def _stamp(reader, step):
    """`step 1,204 · sort.py:14 · MUTATE data` -- the one label we print."""
    ev = reader.view(step)
    bits = ["step %s" % f"{step:,}", _where(reader, step),
            "%s%s%s" % (CYAN, ev["kind"], OFF)]
    if ev["name"]:
        bits.append(ev["name"])
    return "  ·  ".join(bits)


NATIVE_EXT = {".c", ".cc", ".cpp", ".cxx", ".c++"}


def _run_native(args, out):
    """Compile a C/C++ file and trace it with a real debugger."""
    from .native import compile_source, pick_backend, trace_binary
    backend = pick_backend(getattr(args, "backend", None))
    if backend is None:
        print("%sno debugger found.%s Install lldb (on macOS it comes with "
              "Xcode: xcode-select --install) or gdb." % (RED, OFF))
        return
    try:
        import shlex
        binary, compiler, argv = compile_source(
            args.script,
            extra_flags=tuple(shlex.split(getattr(args, "cflags", "") or "")))
    except RuntimeError as exc:
        print("%s%s%s" % (RED, exc, OFF))
        return
    print("%scompiled%s %s with %s" % (DIM, OFF,
                                       os.path.basename(binary), compiler))
    res = trace_binary(binary, [args.script], out, argv=args.argv,
                       backend=backend,
                       max_steps=getattr(args, "max_lines", 50000),
                       meta={"compiler": compiler,
                             "compile_argv": argv})
    size = _trace_bytes(out)
    print("%s%s%s  %s steps  %s source lines  %.2fs  %.1f MB  (%s)"
          % (GREEN, out, OFF, f"{res['steps']:,}", f"{res['lines']:,}",
             res["seconds"], size / 1e6, res["backend"]))
    if res["truncated"]:
        print("%sstopped at the step limit; raise it with --max-lines%s"
              % (YELLOW, OFF))
    if res["exit"]:
        print("%sprogram exited with status %s%s" % (YELLOW, res["exit"], OFF))


def _trace_bytes(out):
    """A trace is a directory, so add up its parts."""
    return sum(os.path.getsize(os.path.join(out, f))
               for f in os.listdir(out))


def _traces_here():
    """Every trace folder in the current directory, newest first."""
    found = [p for p in os.listdir(".")
             if p.endswith(".nd") and os.path.isdir(p)
             and os.path.exists(os.path.join(p, "trace.db"))]
    found.sort(key=lambda p: -os.path.getmtime(os.path.join(p, "trace.db")))
    return found


def _die(msg, hint=None):
    print("%s%s%s" % (RED, msg, OFF), file=sys.stderr)
    if hint:
        print(hint, file=sys.stderr)
    here = _traces_here()
    if here:
        print("\n%straces in this folder:%s" % (BOLD, OFF), file=sys.stderr)
        for p in here[:10]:
            print("  %s" % p, file=sys.stderr)
    else:
        print("\nthere are no traces in this folder. make one:\n"
              "  nd run yourscript.py", file=sys.stderr)
    raise SystemExit(1)


def _open(path):
    """Open a trace, or explain in plain words why we cannot.

    Every command that takes a TRACE goes through here, so a wrong path
    never reaches sqlite3 and never prints a Python traceback.
    """
    if path is None:
        here = _traces_here()
        if len(here) == 1:
            print("%susing %s%s" % (DIM, here[0], OFF))
            path = here[0]
        elif not here:
            _die("no trace given, and none in this folder.")
        else:
            _die("no trace given, and there is more than one here.",
                 "name the one you mean.")

    p = path.rstrip("/")
    if not os.path.exists(p):
        # The commonest mistake by far: the script name, not the trace name.
        guess = os.path.splitext(os.path.basename(p))[0] + ".nd"
        hint = None
        if os.path.isdir(guess):
            hint = "did you mean:  %s" % guess
        elif p.endswith((".py", ".c", ".cpp", ".cc")):
            hint = ("that is a program, not a trace. record it first:\n"
                    "  nd run %s      → makes %s" % (p, guess))
        _die("%s does not exist." % p, hint)
    if not os.path.isdir(p):
        _die("%s is a file; a trace is a folder." % p,
             "you want the %s.nd folder next to it."
             % os.path.splitext(os.path.basename(p))[0])
    if not os.path.exists(os.path.join(p, "trace.db")):
        _die("%s is not a trace folder (no trace.db inside)." % p)
    try:
        return TraceReader(p)
    except Exception as exc:                       # pragma: no cover - rare
        _die("could not read the trace in %s: %s" % (p, exc),
             "it may be from an older version, or half-written. "
             "re-record it.")


# Options that belong to `nd run` itself, and how many words each takes.
# Everything else after the script name is your program's business.
RUN_OPTS = {"-o": 1, "--out": 1, "--driver": 1, "--keyframe-every": 1,
            "--memory": 0, "--backend": 1, "--cflags": 1, "--max-lines": 1,
            "--memory-snapshots": 1, "-h": 0, "--help": 0}


def _split_run(tokens):
    """Split `run` arguments into ours and the program's, keeping order.

        nd run server.py --port 8080 --memory
        ours   = ['server.py', '--memory']
        theirs = ['--port', '8080']

    argparse cannot do this: it rejects anything it does not recognise.
    A bare `--` sends everything after it to the program, always.
    """
    ours, theirs, script = [], [], None
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--":
            theirs.extend(tokens[i + 1:])
            break
        head = t.split("=", 1)[0]
        if t.startswith("-") and head in RUN_OPTS:
            if "=" in t or RUN_OPTS[head] == 0:
                ours.append(t)
                i += 1
            else:
                ours.extend(tokens[i:i + 2])
                i += 2
            continue
        if t.startswith("-") or script is not None:
            theirs.append(t)          # not ours, or sits after the script
            i += 1
            continue
        script = t
        ours.append(t)
        i += 1
    return ours, theirs


def _short(reader, ref, step, width=44):
    """One-line rendering of a ref."""
    val = reader.resolve(ref, step)
    if isinstance(val, dict):
        text = val["repr"]
        s = val["summary"]
        extra = []
        for k in ("shape", "len", "dtype", "device"):
            if k in s:
                extra.append("%s=%s" % (k, s[k]))
        if extra:
            text = "%s  %s[%s]%s" % (text, DIM, " ".join(extra), OFF)
    else:
        text = repr(val)
    if len(text) > width * 3:
        text = text[:width * 3] + "…"
    return text


# ---------------------------------------------------------------- commands

def cmd_run(args):
    out = args.out or (os.path.splitext(os.path.basename(args.script))[0]
                       + ".nd")
    ext = os.path.splitext(args.script)[1]
    if ext in NATIVE_EXT:
        return _run_native(args, out)
    res = trace_script(args.script, out, argv=args.argv, force=args.driver,
                       keyframe_every=args.keyframe_every,
                       memory=getattr(args, "memory", False),
                       memory_snapshots=getattr(args, "memory_snapshots", 0))
    size = _trace_bytes(out)
    print("%s%s%s  %s steps  %.2fs  %.1f MB  (%s)"
          % (GREEN, out, OFF, f"{res['steps']:,}", res["seconds"],
             size / 1e6, res["driver"]))
    if res["error"]:
        print("%sscript raised:%s %s" % (YELLOW, OFF, res["error"]))


def cmd_info(args):
    r = _open(args.trace)
    print("%ssteps%s      %s" % (BOLD, OFF, f"{r.n_steps:,}"))
    for key in ("script", "python", "platform", "driver", "wall_seconds",
                "error", "seed"):
        if key in r.meta:
            print("%-11s%s" % (key, r.meta[key]))
    counts = r.kind_counts()
    peak = max(counts.values()) if counts else 1
    print("\n%sevents%s" % (BOLD, OFF))
    for k, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print("  %-8s %9s  %s" % (k, f"{n:,}", "▇" * int(30 * n / peak)))


def cmd_step(args):
    r = _open(args.trace)
    step = args.step
    st = r.state_at(step)
    if 1 <= step <= r.n_steps:
        print("%s%s%s" % (BOLD, _stamp(r, step), OFF))
        src = r.source(r.view(step)["file"] or "").splitlines()
        line = r.view(step)["line"] or 0
        for i in range(max(1, line - 2), min(len(src), line + 3) + 1):
            mark = "%s▸%s" % (CYAN, OFF) if i == line else " "
            print("  %s %s%4d%s  %s" % (mark, DIM, i, OFF, src[i - 1]))
    for depth, f in enumerate(st["frames"]):
        _, line = r.where(f["loc"])
        fn = r.funcs.get(f.get("func"), {})
        print("  %s%sframe %s  %s  line %s%s"
              % (DIM, "  " * depth, f["frame"],
                 fn.get("qualname", "?"), line, OFF))
        for name, ref in f["locals"].items():
            print("      %-14s %s" % (name, _short(r, ref, step)))
    if st["globals"]:
        print("  %sglobals%s" % (DIM, OFF))
        for name, ref in st["globals"].items():
            print("      %-14s %s" % (name, _short(r, ref, step)))


def cmd_watch(args):
    """The feature a normal debugger cannot give you: a variable's whole life."""
    r = _open(args.trace)
    if not args.name:
        return _list_variables(r, args.trace)
    rows = r.history_of(args.name, limit=args.limit)
    if not rows:
        return _no_such_variable(r, args.trace, args.name)
    print("%s%-10s %-18s %-7s %s%s"
          % (BOLD, "step", "where", "how", args.name, OFF))
    for row in rows:
        arrow = ""
        if row["prev"] is not None and row["prev"][0] == "i":
            arrow = "%s  (was %r)%s" % (DIM, row["prev"][1], OFF)
        how = row["kind"]
        colour = YELLOW if how == schema.MUTATE else ""
        print("%-10s %-18s %s%-7s%s %s%s"
              % (f"{row['step']:,}",
                 "%s:%s" % (os.path.basename(row["file"] or "?"),
                            row["line"]),
                 colour, how, OFF if colour else "",
                 _short(r, row["ref"], row["step"]), arrow))


def _no_such_variable(reader, trace, name):
    """A name that was never assigned. Guess what they meant before listing.

    Three kinds of near-miss, in the order they actually happen:
      - a typo              `bord`  -> board
      - part of a name      `grid`  -> grid_copy, old_grid
      - an attribute        `self.grid` is recorded under its root, `self`
    """
    import difflib
    names = list(reader.variable_counts())
    base = os.path.basename(trace.rstrip("/")) if trace else "trace.nd"
    print("%sno variable called %r was assigned in this trace.%s"
          % (YELLOW, name, OFF))

    if "." in name:
        root = name.split(".", 1)[0]
        if root in names:
            print("\n%s%s is an attribute.%s ND36 records it under the "
                  "object it hangs off:\n  nd watch %s %s"
                  % (BOLD, name, OFF, base, root))
            return

    low = name.lower()
    near = [n for n in names if low in n.lower() or n.lower() in low]
    near += [n for n in difflib.get_close_matches(name, names, 5, 0.6)
             if n not in near]
    if near:
        counts = reader.variable_counts()
        print("\n%sdid you mean:%s" % (BOLD, OFF))
        for n in near[:8]:
            print("  nd watch %-14s %-20s %s%s times%s"
                  % (base, n, DIM, f"{counts[n]:,}", OFF))
        return
    _list_variables(reader, trace)


def _list_variables(reader, trace):
    """What you can actually watch -- shown instead of an argparse error."""
    counts = [(n, name) for name, n in reader.variable_counts().items()]
    if not counts:
        print("this trace recorded no variable assignments at all")
        return
    counts.sort(reverse=True)
    print("%s%d variables were assigned in this trace%s"
          % (BOLD, len(counts), OFF))
    print("%s%-22s %s%s" % (DIM, "name", "times assigned", OFF))
    for n, name in counts[:40]:
        print("  %-22s %s" % (name, f"{n:,}"))
    if len(counts) > 40:
        print("  %s… and %d more%s" % (DIM, len(counts) - 40, OFF))
    base = os.path.basename(trace.rstrip("/"))
    print("\n%sfollow one:%s  nd watch %s %s"
          % (BOLD, OFF, base, counts[0][1]))


def cmd_heat(args):
    """Execution heatmap + time per line, printed over the source."""
    r = _open(args.trace)
    stats = r.line_stats()
    if not stats:
        print("no line events")
        return
    by_file = {}
    for (path, line), (hits, t_ns) in stats.items():
        by_file.setdefault(path, {})[line] = (hits, t_ns)
    peak = max(v[0] for v in stats.values())
    peak_t = max(v[1] for v in stats.values()) or 1
    for path, lines in by_file.items():
        src = r.source(path).splitlines()
        print("%s%s%s" % (BOLD, path, OFF))
        print("%s%7s %7s %-10s  %s%s"
              % (DIM, "hits", "ms", "", "line", OFF))
        for i, text in enumerate(src, 1):
            hits, t_ns = lines.get(i, (0, 0))
            bar = "▇" * min(10, int(10 * hits / peak)) if hits else ""
            colour = (RED if t_ns > peak_t * 0.5
                      else (YELLOW if hits else DIM))
            print("%s%7s %7s %-10s%s %4d  %s"
                  % (colour, hits or "", ("%.1f" % (t_ns / 1e6)) if t_ns
                     else "", bar, OFF, i, text))


def cmd_report(args):
    """Build the HTML dashboard."""
    from .report import render
    r = _open(args.trace)
    out = args.out or (os.path.basename(args.trace.rstrip("/")) + ".html")
    html = render(r, max_embed=args.max_steps)
    with open(out, "w", encoding="utf8") as fh:
        fh.write(html)
    print("%s%s%s  %.0f KB  ·  %s steps, %s embedded for the slider"
          % (GREEN, out, OFF, os.path.getsize(out) / 1024,
             f"{r.n_steps:,}", f"{min(r.n_steps, args.max_steps):,}"))


def cmd_flow(args):
    """Call graph and never-executed branches, in the terminal."""
    from . import graphs
    r = _open(args.trace)
    nodes, edges = graphs.call_graph(r)
    print("%scall graph%s" % (BOLD, OFF))
    for e in sorted(edges, key=lambda e: -e["n"]):
        if e["src"] is None:
            continue
        src = nodes.get(e["src"], {}).get("name", "?")
        dst = nodes.get(e["dst"], {}).get("name", "?")
        if e["n"]:
            print("  %-26s → %-26s %s%s calls%s"
                  % (src, dst, CYAN, f"{e['n']:,}", OFF))
        else:
            print("  %s%-26s → %-26s never ran%s" % (DIM, src, dst, OFF))
    never = [n["name"] for n in nodes.values() if not n["calls"]]
    if never:
        print("\n%snever called%s  %s" % (YELLOW, OFF, ", ".join(never)))

    path = r.meta.get("script", "")
    print("\n%scontrol flow -- lines that never ran%s" % (BOLD, OFF))
    stats = r.line_stats()
    dead = 0
    for g in graphs.cfgs_for_source(r.source(path), path):
        cold = [n for n in g.nodes
                if n["line"] and n["kind"] not in ("entry", "exit")
                and not stats.get((path, n["line"]))]
        if not cold:
            continue
        print("  %s%s%s" % (BOLD, g.name, OFF))
        for n in cold:
            dead += 1
            print("    %s%s:%-4d%s %s" % (DIM, os.path.basename(path),
                                          n["line"], OFF, n["label"]))
    if not dead:
        print("  %severy statement ran at least once%s" % (GREEN, OFF))


def _kb(n):
    if n is None:
        return ""
    if n >= 1 << 20:
        return "%.1f MB" % (n / (1 << 20))
    return "%.0f KB" % (n / 1024)


def cmd_mem(args):
    """Memory over the run, and which line is holding it."""
    from .memory import summarise
    r = _open(args.trace)
    detailed = bool(r.meta.get("memory"))
    # Two curves exist: the cheap one sampled during the trace (always), and
    # the exact one from the tracemalloc pass (only with --memory).
    curve = r.memcurve() if detailed else []
    samples = curve or r.samples()
    if not samples:
        print("no memory samples in this trace")
        return

    key = "cur" if curve else "blocks"
    unit = "python heap" if curve else "allocated blocks"
    # tracemalloc tracks the peak itself, so this is exact even if the
    # sampler never happened to land on the highest moment.
    peak = max(s["peak"] for s in samples) if curve \
        else max(s[key] for s in samples)
    print("%speak%s             %s"
          % (BOLD, OFF, _kb(peak) if curve else "%s blocks" % f"{peak:,}"))
    print("%ssamples%s          %d%s\n"
          % (BOLD, OFF, len(samples),
             "  (from the separate memory pass)" if curve else ""))

    vals = [s[key] for s in samples]
    lo, hi = min(vals), max(vals) or 1
    blocks = " ▁▂▃▄▅▆▇█"
    span = (hi - lo) or 1
    # Downsample to the terminal width rather than truncating, so the shape
    # of the whole run is visible instead of just its opening.
    width = 100
    if len(vals) > width:
        bucket = len(vals) / width
        vals = [max(vals[int(i * bucket):int((i + 1) * bucket)] or [0])
                for i in range(width)]
    line = "".join(blocks[min(8, int(8 * (v - lo) / span))] for v in vals)
    print("%s%s%s  %s" % (CYAN, line, OFF, unit))
    print("%s%s  →  %s%s\n" % (DIM, _kb(lo) if curve else lo,
                               _kb(hi) if curve else hi, OFF))

    if not curve:
        # The cheap curve is aligned to steps, so it can name a line.
        top = max(samples, key=lambda s: s[key])
        print("%shighest point%s   step %s  ·  %s"
              % (BOLD, OFF, f"{top['step']:,}",
                 _where(r, min(top["step"], r.n_steps))))

    if not detailed:
        print("\n%sRun with --memory for per-line attribution.%s" % (DIM, OFF))
        return

    rows = summarise(r)
    if not rows:
        print("\nno growth between the first and last snapshot")
        return
    print("\n%sstill held at the end, biggest growth first%s" % (BOLD, OFF))
    print("%s%-26s %10s %10s%s" % (DIM, "where", "held", "growth", OFF))
    for row in rows[:12]:
        if not row["growth"]:
            continue
        tag = "" if row["mine"] else DIM
        print("%s%-26s %10s %10s%s"
              % (tag, "%s:%d" % (os.path.basename(row["path"]), row["line"]),
                 _kb(row["bytes"]), "+" + _kb(row["growth"]), OFF))
    mine = [r2 for r2 in rows if r2["mine"] and r2["growth"] > 0]
    if mine:
        src = r.source(mine[0]["path"]).splitlines()
        line = mine[0]["line"]
        print("\n%slikely leak%s  %s:%d"
              % (YELLOW, OFF, os.path.basename(mine[0]["path"]), line))
        for i in range(max(1, line - 1), min(len(src), line + 2) + 1):
            mark = "%s▸%s" % (YELLOW, OFF) if i == line else " "
            print("  %s %s%4d%s  %s" % (mark, DIM, i, OFF, src[i - 1]))


def cmd_serve(args):
    """Open the live UI on a local port."""
    from .server import serve
    # `nd serve script.py` records first, then serves -- and the re-run
    # button in the page records again, which is the whole point of having
    # a server rather than a static file.
    trace, rerun = _prepare(args, args.target)

    httpd, url, service = serve(trace, port=args.port,
                                open_browser=not args.no_browser,
                                rerun=rerun)
    if rerun is not None:
        service.rerun = lambda: (rerun(), service.reload(),
                                 {"ok": True})[-1]
    r = _open(trace)
    print("%s%s%s  %s steps  ·  %s" % (GREEN, url, OFF,
                                       f"{r.n_steps:,}", trace))
    print("%sthe page asks for one step at a time, so the whole trace is "
          "scrubbable however big it is%s" % (DIM, OFF))
    print("%sCtrl-C to stop%s" % (DIM, OFF))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        httpd.shutdown()


def _record(args, target, out):
    """Record `target` into `out`, Python or C/C++."""
    if os.path.splitext(target)[1] in NATIVE_EXT:
        from .native import compile_source, pick_backend, trace_binary
        binary, compiler, _ = compile_source(target)
        trace_binary(binary, [target], out, backend=pick_backend(),
                     meta={"compiler": compiler})
    else:
        trace_script(target, out, memory=getattr(args, "memory", False))


def cmd_app(args):
    """Open the UI in its own window rather than a browser tab."""
    from .app import open_window
    target = args.target
    if args.pick or not target:
        target = _pick_a_trace()
        if target is None:
            return
    trace, rerun = _prepare(args, target)
    from .server import serve
    httpd, url, service = serve(trace, port=args.port, open_browser=False,
                                rerun=rerun)
    if rerun is not None:
        service.rerun = lambda: (rerun(), service.reload(), {"ok": True})[-1]
    r = _open(trace)
    print("%s%s%s  %s steps" % (GREEN, url, OFF, f"{r.n_steps:,}"))
    how, proc = open_window(url)
    if how == "tab":
        print("%sno Chrome-family browser found, so this opened a normal "
              "tab%s" % (DIM, OFF))
    else:
        print("%swindow opened with %s%s" % (DIM, how, OFF))

    # The server used to stop when the browser process exited. On macOS,
    # launching Chrome when Chrome is ALREADY RUNNING hands the window to the
    # running instance and the process we spawned exits within a second --
    # so the server was killed a moment after the window appeared, and the
    # window showed nothing. The browser's lifetime says nothing about
    # whether you are still looking at the page, so it is ignored now.
    print("%spress Ctrl-C here when you are done%s" % (DIM, OFF))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        print("stopped")


def _pick_a_trace():
    """No argument given -- offer the traces lying around."""
    here = os.getcwd()
    traces = sorted((f for f in os.listdir(here) if f.endswith(".nd")),
                    key=lambda f: -os.path.getmtime(os.path.join(here, f)))
    if not traces:
        print("%sno .vizt traces in %s%s" % (YELLOW, here, OFF))
        print("record one first:  nd run yourscript.py")
        return None
    if len(traces) == 1:
        return traces[0]
    print("%swhich trace?%s" % (BOLD, OFF))
    for i, name in enumerate(traces[:9], 1):
        print("  %d  %s" % (i, name))
    try:
        choice = input("number (1): ").strip() or "1"
        return traces[int(choice) - 1]
    except (ValueError, IndexError, EOFError, KeyboardInterrupt):
        return traces[0]


def _prepare(args, target):
    """-> (trace path, rerun callable or None)."""
    if os.path.isdir(target) or not os.path.splitext(target)[1]:
        return target, None
    if target.endswith(".nd"):
        return target, None
    out = (getattr(args, "out", None)
           or os.path.splitext(os.path.basename(target))[0] + ".nd")
    _record(args, target, out)
    return out, lambda: _record(args, target, out)


def cmd_install_app(args):
    """Write a double-clickable launcher, and prove it is there."""
    import sys as _sys
    from .app import find_browser, install, reveal
    try:
        path = install(target_dir=args.into)
    except RuntimeError as exc:
        print("%s%s%s" % (RED, exc, OFF))
        return

    size = sum(os.path.getsize(os.path.join(root, f))
               for root, _d, files in os.walk(path) for f in files) \
        if os.path.isdir(path) else os.path.getsize(path)
    print("%sinstalled%s  %s" % (GREEN, OFF, path))
    print("%s%s · %.0f KB · exists: %s%s"
          % (DIM, "app bundle" if path.endswith(".app") else "desktop entry",
             size / 1024, os.path.exists(path), OFF))
    if _sys.platform == "darwin":
        print("\n%sIt is in your home Applications folder, not /Applications."
              "%s" % (BOLD, OFF))
        print("Open it from Spotlight (⌘-space, type %sND36%s), or from "
              "Finder: Go → Home → Applications." % (BOLD, OFF))
        print("To put it in the system folder instead:")
        print("  %snd install-app --into /Applications%s" % (BOLD, OFF))
    if not args.no_reveal and reveal(path):
        print("%s(opened the folder for you)%s" % (DIM, OFF))
    if find_browser() is None:
        print("\n%sNo Chrome, Edge, Brave or Chromium found, so it will "
              "open a normal browser tab rather than its own window.%s"
              % (YELLOW, OFF))


def cmd_install_vscode(args):
    """Install the VS Code extension by copying it into place."""
    from .app import install_vscode, vscode_extensions_dir
    try:
        dest = install_vscode(target_dir=args.into)
    except RuntimeError as exc:
        print("%s%s%s" % (RED, exc, OFF))
        return
    print("%sinstalled%s  %s" % (GREEN, OFF, dest))
    print("%sVS Code only reads this folder at startup — quit it completely "
          "and reopen.%s" % (BOLD, OFF))
    print("\nThen: right-click a .py / .c / .cpp file → %sND36: Record and "
          "replay this file%s" % (BOLD, OFF))
    print("%sor press ⌘⇧P and type \"ND36\"%s" % (DIM, OFF))
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print("\n%sIf it reports that it cannot find nd36, set this in VS Code "
          "settings:%s" % (DIM, OFF))
    print('  "nd.repoPath": "%s"' % repo)


def cmd_doctor(args):
    """Say what is installed and what is not. No guessing."""
    import shutil as _shutil
    import sys as _sys
    from .app import (find_browser, vscode_extensions_dir)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def row(label, ok, detail=""):
        mark = "%s✓%s" % (GREEN, OFF) if ok else "%s✗%s" % (YELLOW, OFF)
        print("  %s %-26s %s" % (mark, label, detail))

    installed = "site-packages" in repo
    print("%sND36%s  %s" % (BOLD, OFF, repo))
    print("%s%s%s" % (DIM, "installed as a package" if installed
                      else "running from the source folder", OFF))
    print("\n%score%s" % (BOLD, OFF))
    row("python", True, _sys.version.split()[0])
    # If you are reading this, you ran `nd` somehow -- so say how, rather
    # than claiming it is missing because this shell's PATH lacks it.
    on_path = _shutil.which("nd")
    launched = (os.path.abspath(_sys.argv[0])
                if os.path.basename(_sys.argv[0]) in ("nd", "nd36")
                else None)
    row("nd command", bool(on_path or launched),
        on_path or launched or "run: pip install --user -e .")
    if launched and not on_path:
        row("", False, "it works here but is not on this shell's PATH; add "
                       "%s" % os.path.dirname(launched))

    print("\n%sdesktop app%s" % (BOLD, OFF))
    if _sys.platform == "darwin":
        for place in (os.path.expanduser("~/Applications/ND36.app"),
                      "/Applications/ND36.app"):
            row(place, os.path.exists(place),
                "" if os.path.exists(place) else "not installed")
    else:
        place = os.path.expanduser(
            "~/.local/share/applications/nd36.desktop")
        row(place, os.path.exists(place))
    browser = find_browser()
    row("app-window browser", bool(browser),
        browser or "none — nd app will open a normal tab")

    print("\n%svs code%s" % (BOLD, OFF))
    ext_dir = vscode_extensions_dir()
    installed = [d for d in (os.listdir(ext_dir)
                             if os.path.isdir(ext_dir) else [])
                 if d.startswith("nd36.nd")]
    row("code on PATH", bool(_shutil.which("code")),
        _shutil.which("code") or "optional")
    row("extension", bool(installed),
        installed[0] if installed else "run: nd install-vscode")

    print("\n%sC / C++%s" % (BOLD, OFF))
    for tool in ("lldb", "gdb", "clang", "gcc"):
        row(tool, bool(_shutil.which(tool)), _shutil.which(tool) or "")

    print("\n%straces here%s" % (BOLD, OFF))
    traces = [f for f in os.listdir(".") if f.endswith((".nd", ".vizt"))]
    if traces:
        for name in sorted(traces)[:8]:
            print("    %s" % name)
    else:
        print("    %snone — record one: nd run yourscript.py%s" % (DIM, OFF))


def cmd_data(args):
    """The DataFrame ribbon: every step where a frame changed, and how."""
    from .lineage import frame_lineage, summary_line
    r = _open(args.trace)
    rows = frame_lineage(r)
    if not rows:
        print("no DataFrames in this trace")
        return
    print("%s%-15s %-10s %-10s %s%s"
          % (BOLD, "where", "name", "shape", "what changed", OFF))
    for row in rows:
        where, shape, what = summary_line(row)
        colour = YELLOW if row["warn"] else ""
        print("%s%-15s %-10s %-10s %s%s%s"
              % (colour, where, row["name"] or "", shape, what,
                 "   ← big row loss" if row["warn"] else "",
                 OFF if colour else ""))

    warned = [r2 for r2 in rows if r2["warn"]]
    if warned:
        row = warned[0]
        src = r.source(row["file"]).splitlines()
        print("\n%smost rows lost here%s  %s:%d"
              % (YELLOW, OFF, os.path.basename(row["file"]), row["line"]))
        for i in range(max(1, row["line"] - 1),
                       min(len(src), row["line"] + 2) + 1):
            mark = "%s▸%s" % (YELLOW, OFF) if i == row["line"] else " "
            print("  %s %s%4d%s  %s" % (mark, DIM, i, OFF, src[i - 1]))


def cmd_tensors(args):
    """Tensor shapes through the run, and any shape mismatch."""
    from .lineage import shape_mismatch, tensor_lineage
    r = _open(args.trace)
    rows = tensor_lineage(args and r)
    if not rows:
        print("no tensors in this trace")
        return
    print("%s%-15s %-9s %-20s %-9s %s%s"
          % (BOLD, "where", "name", "shape", "dtype", "produced by", OFF))
    for t in rows[:args.limit]:
        changed = t["prev_shape"] and t["prev_shape"] != t["shape"]
        colour = CYAN if changed else ""
        print("%s%-15s %-9s %-20s %-9s %s%s"
              % (colour,
                 "%s:%s" % (os.path.basename(t["file"] or "?"), t["line"]),
                 t["name"] or "", str(tuple(t["shape"])), t["dtype"] or "",
                 t["grad_fn"] or "", OFF if colour else ""))

    for m in shape_mismatch(r):
        print("\n%sshape mismatch%s  %s:%d  %s"
              % (RED, OFF, os.path.basename(m["file"] or "?"), m["line"],
                 m["exc"]))
        print("  %s" % m["msg"][:160])
        src = r.source(m["file"]).splitlines()
        if m["line"] and m["line"] <= len(src):
            print("  %s▸%s %s" % (RED, OFF, src[m["line"] - 1].strip()))
        print("\n  %slive tensors at that moment%s" % (DIM, OFF))
        for t in m["tensors"][:8]:
            print("    %-9s %-20s %s"
                  % (t["name"] or "?", str(tuple(t["shape"])),
                     t["grad_fn"] or ""))


def cmd_export(args):
    """Write a Perfetto / Chrome Trace file."""
    from .exporter import write
    r = _open(args.trace)
    out = args.out or (os.path.basename(args.trace.rstrip("/"))
                       + ".perfetto.json")
    n = write(r, out)
    print("%s%s%s  %s events  ·  open it at https://ui.perfetto.dev"
          % (GREEN, out, OFF, f"{n:,}"))


def cmd_x(args):
    """The explorer: a prompt that sits on top of the run."""
    from . import explore
    r = _open(args.trace)
    cmds = list(args.command or [])
    if args.command:
        return explore.once(r, args.trace or r.dir, cmds)
    explore.repl(r, args.trace or r.dir)


def samples_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")


def sample_path(name):
    """A bundled sample program, so no command ever needs a file you lack."""
    return os.path.join(samples_dir(), name)


def cmd_samples(args):
    """The programs that ship with ND36, and how to use them."""
    print("%sND36 ships these, so you always have something to try%s"
          % (BOLD, OFF))
    blurb = {
        "sort_demo.py": "a loop, a list sorted in place, recursion",
        "bug_demo.py": "has a real bug in it. practise finding it",
        "loop_demo.c": "C: a growing array, malloc and realloc",
    }
    for name in sorted(blurb):
        print("  %-16s %s%s%s" % (name, DIM, blurb[name], OFF))
    print("\n%scopy them here and record one:%s" % (BOLD, OFF))
    print("  nd samples --copy")
    print("  nd run sort_demo.py")
    print("\n%sor skip the copying:%s  nd demo" % (BOLD, OFF))
    if args.copy:
        import shutil
        for name in sorted(blurb):
            dst = os.path.join(".", name)
            if os.path.exists(dst):
                print("%s%s is already here, left alone%s" % (YELLOW, name, OFF))
                continue
            shutil.copy(sample_path(name), dst)
            print("%swrote%s %s" % (GREEN, OFF, name))


def cmd_demo(args):
    """Record a bundled sample and say what to do next. Zero setup."""
    name = "loop_demo.c" if args.c else ("bug_demo.py" if args.bug
                                         else "sort_demo.py")
    src = sample_path(name)
    out = os.path.splitext(name)[0] + ".nd"
    print("%srecording the sample program%s %s" % (DIM, OFF, name))
    args.script, args.out, args.argv = src, out, []
    for key, val in (("driver", None), ("keyframe_every", 2000),
                     ("memory", False), ("memory_snapshots", 0),
                     ("backend", None), ("cflags", ""), ("max_lines", 50000)):
        if not hasattr(args, key):
            setattr(args, key, val)
    cmd_run(args)
    if not os.path.isdir(out):
        return
    r = _open(out)
    top = sorted(r.variable_counts().items(), key=lambda kv: -kv[1])[:3]
    print("\n%snow try these, in order%s" % (BOLD, OFF))
    print("  nd info  %-18s what is in the recording" % out)
    print("  nd watch %-18s the variables you can follow" % out)
    for nm, _n in top:
        print("  nd watch %-18s %s" % (out, nm))
        break
    print("  nd flow  %-18s which lines never ran" % out)
    print("  nd heat  %-18s time spent per line" % out)
    print("\n%sthe source is at%s %s" % (DIM, OFF, src))


def cmd_bench(args):
    if not args.script:
        args.script = sample_path("sort_demo.py")
        print("%sno file given, so benchmarking the bundled sample "
              "(sort_demo.py)%s" % (DIM, OFF))
    base = baseline_seconds(args.script)
    out = "/tmp/_viz_bench.nd"
    res = trace_script(args.script, out, force=args.driver)
    size = _trace_bytes(out)
    r = TraceReader(out)
    import time as _t
    t0 = _t.perf_counter()
    n = min(200, max(1, r.n_steps))
    for i in range(n):
        r.state_at(1 + (i * r.n_steps) // n)
    seek = (_t.perf_counter() - t0) / n * 1000

    steps = max(res["steps"], 1)
    print("%-22s %s" % ("driver", res["driver"]))
    print("%-22s %.4f s" % ("untraced", base))
    print("%-22s %.4f s" % ("traced", res["seconds"]))
    print("%-22s %.1fx" % ("slowdown", res["seconds"] / max(base, 1e-6)))
    # The honest metric. `slowdown` flatters or damns a script depending on
    # how much work its lines do; cost per recorded step is comparable
    # across programs and is what we optimise against.
    print("%s%-22s %.2f us%s" % (BOLD, "cost per step",
                                 res["seconds"] / steps * 1e6, OFF))
    print("%-22s %s" % ("steps", f"{steps:,}"))
    print("%-22s %.1f MB" % ("trace size", size / 1e6))
    print("%-22s %.0f bytes" % ("per step", size / steps))
    print("%s%-22s %.2f ms%s" % (BOLD, "state_at() median", seek, OFF))


def main(argv=None):
    p = argparse.ArgumentParser(prog="nd")
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("run", help="record a trace")
    q.add_argument("script")
    q.add_argument("argv", nargs="*")
    q.add_argument("-o", "--out")
    q.add_argument("--driver", choices=["monitoring", "settrace"])
    q.add_argument("--keyframe-every", type=int, default=2000)
    q.add_argument("--memory", action="store_true",
                   help="per-line memory attribution: runs the script a "
                        "second time under tracemalloc")
    q.add_argument("--backend", choices=["lldb", "gdb"],
                   help="C/C++ only: which debugger to drive")
    q.add_argument("--cflags", default="",
                   help='C/C++ only: extra compiler flags as one quoted '
                        'string, e.g. --cflags="-lm -std=c11". -O is refused.')
    q.add_argument("--max-lines", type=int, default=50000,
                   help="C/C++ only: stop after this many source lines")
    q.add_argument("--memory-snapshots", type=int, default=0, metavar="N",
                   help="extra heap snapshots during growth. Each costs "
                        "about 80 points off the memory curve.")
    q.set_defaults(fn=cmd_run)

    q = sub.add_parser("info", help="summarise a trace")
    q.add_argument("trace")
    q.set_defaults(fn=cmd_info)

    q = sub.add_parser("step", help="state at one step")
    q.add_argument("trace")
    q.add_argument("step", type=int)
    q.set_defaults(fn=cmd_step)

    q = sub.add_parser("watch", help="one variable's whole history")
    q.add_argument("trace")
    q.add_argument("name", nargs="?",
                   help="leave it out to list the variables in this trace")
    q.add_argument("--limit", type=int, default=50)
    q.set_defaults(fn=cmd_watch)

    q = sub.add_parser("heat", help="execution heatmap")
    q.add_argument("trace")
    q.set_defaults(fn=cmd_heat)

    q = sub.add_parser("report", help="build the HTML dashboard")
    q.add_argument("trace")
    q.add_argument("-o", "--out")
    q.add_argument("--max-steps", type=int, default=20000)
    q.set_defaults(fn=cmd_report)

    q = sub.add_parser("flow", help="call graph + never-executed lines")
    q.add_argument("trace")
    q.set_defaults(fn=cmd_flow)

    q = sub.add_parser("serve", help="the live UI -- any trace size")
    q.add_argument("target", help="a .vizt trace, or a script to record")
    q.add_argument("-o", "--out")
    q.add_argument("-p", "--port", type=int, default=7878)
    q.add_argument("--no-browser", action="store_true")
    q.add_argument("--memory", action="store_true")
    q.set_defaults(fn=cmd_serve)

    q = sub.add_parser("app", help="open the UI in its own window")
    q.add_argument("target", nargs="?",
                   help="a .vizt trace, or a script to record")
    q.add_argument("--pick", action="store_true",
                   help="choose from the traces in this folder")
    q.add_argument("-o", "--out")
    q.add_argument("-p", "--port", type=int, default=7878)
    q.add_argument("--memory", action="store_true")
    q.set_defaults(fn=cmd_app)

    q = sub.add_parser("install-app",
                       help="put the app in Applications / your menu")
    q.add_argument("--into", help="where to put it (default ~/Applications)")
    q.add_argument("--no-reveal", action="store_true",
                   help="do not open the folder afterwards")
    q.set_defaults(fn=cmd_install_app)

    q = sub.add_parser("install-vscode", help="install the VS Code extension")
    q.add_argument("--into", help="VS Code extensions folder")
    q.set_defaults(fn=cmd_install_vscode)

    q = sub.add_parser("doctor", help="what is installed, and what is not")
    q.set_defaults(fn=cmd_doctor)

    q = sub.add_parser("data", help="DataFrame lineage: what changed, where")
    q.add_argument("trace")
    q.set_defaults(fn=cmd_data)

    q = sub.add_parser("tensors", help="tensor shapes, and shape mismatches")
    q.add_argument("trace")
    q.add_argument("--limit", type=int, default=40)
    q.set_defaults(fn=cmd_tensors)

    q = sub.add_parser("mem", help="memory over the run, and the leak")
    q.add_argument("trace")
    q.set_defaults(fn=cmd_mem)

    q = sub.add_parser("export", help="write a Perfetto / Chrome trace")
    q.add_argument("trace")
    q.add_argument("-o", "--out")
    q.set_defaults(fn=cmd_export)

    q = sub.add_parser("bench", help="overhead + trace size")
    q.add_argument("script", nargs="?",
                   help="leave it out to benchmark a bundled sample")
    q.add_argument("--driver", choices=["monitoring", "settrace"])
    q.set_defaults(fn=cmd_bench)

    for word in ("x", "explore"):
        q = sub.add_parser(
            word, help="explore a run: functions, variables, connections",
            description="An interactive prompt over a recorded run. "
                        "Press ? inside it for the command list.")
        q.add_argument("trace", nargs="?",
                       help="leave it out if there is one trace here")
        q.add_argument("-c", "--command", action="append", metavar="CMD",
                       help="run a command and exit, instead of the prompt. "
                            "repeatable: -c afl -c 'axv total'")
        q.set_defaults(fn=cmd_x)

    q = sub.add_parser("demo", help="record a bundled sample, with no setup")
    q.add_argument("--bug", action="store_true",
                   help="use the sample that has a bug in it")
    q.add_argument("--c", action="store_true", help="use the C sample")
    q.set_defaults(fn=cmd_demo)

    q = sub.add_parser("samples", help="the programs that ship with ND36")
    q.add_argument("--copy", action="store_true",
                   help="copy them into this folder")
    q.set_defaults(fn=cmd_samples)

    argv = list(sys.argv[1:] if argv is None else argv)
    # `nd run script.py --port 8080` must give --port 8080 to the script.
    # argparse refuses unknown options, so we split them off by hand first.
    if argv and argv[0] == "run":
        ours, theirs = _split_run(argv[1:])
        args = p.parse_args(["run"] + ours)
        args.argv = theirs
        if theirs:
            print("%spassing to %s:%s %s"
                  % (DIM, os.path.basename(args.script), OFF,
                     " ".join(theirs)))
    else:
        args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
