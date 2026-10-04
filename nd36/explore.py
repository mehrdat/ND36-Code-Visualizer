"""
`nd x` -- the explorer.

A prompt that sits on top of a recorded run, in the spirit of radare2: a
cursor, very short commands, and a table out of every one of them. The
difference is that radare2's cursor is an address in a dead file, and this
one is a *moment in time*. So `s` seeks to a step, and every other command
answers "at this moment" or "across the whole run".

    nd[solve 26]> af solve         what this function is wired to
    nd[solve 26]> axv grid         who writes grid, who reads it
    nd[solve 26]> c grid           jump to the next change of grid
    nd[solve 26]> pl tries         draw tries over the whole run

The design rules, learned from the things that make r2 pleasant:
  - one or two letters, because you type them hundreds of times
  - every command works with no arguments, using the cursor
  - nothing ever scrolls off: tables are capped and say how much was cut
  - `?` is always the answer to being lost
"""

import os
import re
import shlex

from . import analyse, draw, schema
from .draw import (BOLD, CYAN, DIM, GREEN, OFF, RED, YELLOW, bar,
                   cursor_bar, heatrows, plot, sparkline, table, tree)

COND = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*(==|!=|>=|<=|=|>|<)\s*(.+)\s*$")


def _n(x):
    return f"{x:,}"


def _ms(x):
    return "%.1f" % x if x >= 0.05 else ""


class Explorer:
    """One session. `do(line)` runs one command; everything else is state."""

    def __init__(self, reader, path, quiet=False):
        self.r = reader
        self.path = path
        self.base = os.path.basename(path.rstrip("/"))
        if not quiet:
            print("%sreading the run…%s" % (DIM, OFF), end="", flush=True)
        self.a = analyse.load(reader)
        if not quiet:
            print("\r%sanalysed %s steps, %d functions, %d variables%s"
                  % (DIM, _n(reader.n_steps), len(self.a.functions),
                     len(self.a.variables), OFF))
        self.cursor = 1
        self.running = True
        self._last_cond = None
        self._lines = reader.line_stats()

    # ------------------------------------------------------------------ bits

    def ev(self, step=None):
        return self.r.view(step or self.cursor)

    def here(self):
        """(file, line) at the cursor."""
        e = self.ev()
        return e["file"] or "", e["line"] or 0

    def fn_at(self, step=None):
        """Which function the cursor is inside, from the live stack."""
        st = self.r.state_at(step or self.cursor)
        frames = st["frames"]
        if not frames:
            return "<module>"
        name = frames[-1].get("func")
        if isinstance(name, int):
            f = self.r.funcs.get(name) or {}
            return f.get("qualname") or f.get("name") or "?"
        return name or "<module>"

    def prompt(self):
        path, line = self.here()
        return "%snd[%s%s %s%s]>%s " % (BOLD, OFF, self.fn_at(),
                                        draw.rel(path) + ":" + str(line),
                                        BOLD, OFF)

    def seek(self, step, say=True):
        self.cursor = max(1, min(int(step), self.r.n_steps))
        if say:
            self.status()

    def status(self):
        e = self.ev()
        print(cursor_bar(self.cursor, self.r.n_steps))
        bits = ["%s%s:%s%s" % (BOLD, draw.rel(e["file"]), e["line"], OFF),
                "%s%s%s" % (CYAN, e["kind"], OFF)]
        if e["name"]:
            bits.append(e["name"] + " = " + self.val(e["ref"]))
        print("  " + "   ".join(bits) + "   %sin %s%s"
              % (DIM, self.fn_at(), OFF))

    def val(self, ref, width=46, step=None):
        """A ref as one line. `step` matters: an object's contents change,
        so resolving at the wrong step shows the wrong version -- or, for a
        step before it existed, nothing at all."""
        if ref is None:
            return "%s–%s" % (DIM, OFF)
        got = self.r.resolve(ref, step or self.cursor)
        if isinstance(got, dict):
            text = got["repr"]
            extra = [("%s=%s" % (k, v)) for k, v in got["summary"].items()
                     if k in ("len", "shape", "dtype", "rows", "cols")]
            if extra:
                text = "%s %s[%s]%s" % (text, DIM, " ".join(extra), OFF)
        else:
            text = repr(got)
        if draw.plain(text) > width:
            text = text[:width] + "…"
        return text

    # ---------------------------------------------------------------- navigate

    def c_s(self, arg=None):
        """s TARGET -- seek. The one command everything else hangs off."""
        if arg is None:
            return self.status()
        t = arg.strip()
        if t in ("end", "$"):
            return self.seek(self.r.n_steps)
        if t in ("start", "^", "0"):
            return self.seek(1)
        if t.endswith("%") and t[:-1].strip().lstrip("-").isdigit():
            return self.seek(int(float(t[:-1]) / 100 * self.r.n_steps))
        if t[0] in "+-" and t[1:].strip().isdigit():
            return self.seek(self.cursor + int(t.replace(" ", "")))
        if t.lstrip("-").isdigit():
            return self.seek(int(t))
        if t.startswith("fn:") or t.startswith("@fn:"):
            return self._seek_call(t.split(":", 1)[1])
        m = re.match(r"^@?(?:([\w.]+\.\w+):)?(\d+)$", t)
        if m and m.group(2) and not t.isdigit():
            return self._seek_line(m.group(1), int(m.group(2)))
        if t in self.a.functions:
            return self._seek_call(t)
        return self._seek_write(t)

    def _seek_write(self, name, after=None, test=None):
        steps = self.r.steps_of(name)
        if not steps:
            print("%snothing called %r is written in this run.%s"
                  % (YELLOW, name, OFF))
            near = [k for k in self.a.variables if name.lower() in k.lower()]
            if near:
                print("  did you mean: %s" % ", ".join(near[:6]))
            return
        at = after if after is not None else self.cursor
        ahead = [s for s in steps if s > at]
        hit = self._first_true(ahead, test)
        if hit is None and test is not None:
            # Nothing ahead. Say whether it is ever true at all, rather
            # than wrapping to a step that does not satisfy the condition.
            hit = self._first_true([s for s in steps if s <= at], test)
            if hit is None:
                print("%sthat is never true in this run.%s" % (YELLOW, OFF))
                return
            print("%slast time it was true is behind you, step %s.%s"
                  % (DIM, _n(hit), OFF))
        if hit is None:
            print("%s%s does not change again. its last change was step "
                  "%s.%s" % (DIM, name, _n(steps[-1]), OFF))
            return self.seek(steps[-1])
        self.seek(hit)

    SCAN_CAP = 300_000

    def _first_true(self, steps, test):
        if test is None:
            return steps[0] if steps else None
        for i, s in enumerate(steps):
            if i >= self.SCAN_CAP:
                print("%slooked at %s changes and stopped there.%s"
                      % (DIM, _n(self.SCAN_CAP), OFF))
                return None
            if test(self._value_at(s)):
                return s
        return None

    def _value_at(self, step):
        """The value written at `step`, as cheaply as possible.

        An int, float, bool or short string sits inside the fixed-width
        record, so testing `i > 5` over a hundred thousand writes costs no
        queries at all. Only an object has to be looked up.
        """
        rec = self.r.raw(step)
        ref = self.r.vref(rec[5], rec[6], rec[7])
        if ref is None:
            return None
        if ref[0] == "i":
            return ref[1]
        return self.r.resolve(ref, step)

    def _seek_call(self, name):
        fn = self.a.func(name)
        if fn is None:
            print("%sno function called %r.%s" % (YELLOW, name, OFF))
            return
        for enter, _leave in self.a.invocations.get(fn, []):
            if enter > self.cursor:
                return self.seek(enter)
        info = self.a.functions.get(fn, {})
        if info.get("first_step"):
            print("%slast call already passed; wrapping to the first.%s"
                  % (DIM, OFF))
            return self.seek(info["first_step"])
        print("%s%s was never called.%s" % (YELLOW, fn, OFF))

    def _seek_line(self, filename, line):
        path, _ = self.here()
        if filename:
            for p in self.r.files.values():
                if os.path.basename(p) == filename or p == filename:
                    path = p
                    break
        want = None
        for loc, (f, ln) in self.r.locs.items():
            if ln == line and self.r.files.get(f) == path:
                want = loc
                break
        if want is None:
            print("%sline %s of %s never ran.%s"
                  % (YELLOW, line, draw.rel(path), OFF))
            return
        K = schema.KIND_CODE[schema.LINE]
        for s in range(self.cursor + 1, self.r.n_steps + 1):
            rec = self.r.raw(s)
            if rec[1] == K and rec[3] == want:
                return self.seek(s)
        print("%sthat line does not run again after here.%s" % (DIM, OFF))

    def c_n(self, arg=None):
        """n [K] -- forward K steps (1)."""
        self.seek(self.cursor + int(arg or 1))

    def c_p(self, arg=None):
        """p [K] -- back K steps (1)."""
        self.seek(self.cursor - int(arg or 1))

    def c_f(self, arg=None):
        """f -- the next event in THIS frame: step over any call."""
        frame = self.r.raw(self.cursor)[2]
        for s in range(self.cursor + 1, self.r.n_steps + 1):
            if self.r.raw(s)[2] == frame:
                return self.seek(s)
        print("%sthis frame never runs again.%s" % (DIM, OFF))

    def c_F(self, arg=None):
        """F -- the previous event in this frame."""
        frame = self.r.raw(self.cursor)[2]
        for s in range(self.cursor - 1, 0, -1):
            if self.r.raw(s)[2] == frame:
                return self.seek(s)
        print("%sthis is the start of the frame.%s" % (DIM, OFF))

    def c_u(self, arg=None):
        """u -- step out: where this frame returns."""
        frame = self.r.raw(self.cursor)[2]
        want = (schema.KIND_CODE[schema.RETURN],
                schema.KIND_CODE[schema.UNWIND])
        for s in range(self.cursor + 1, self.r.n_steps + 1):
            rec = self.r.raw(s)
            if rec[2] == frame and rec[1] in want:
                return self.seek(s)
        print("%sthis frame never returns (it is the main one).%s"
              % (DIM, OFF))

    def c_c(self, arg=None):
        """c COND -- run on until it is true.  c i>5   c grid   c raise"""
        if arg is None:
            arg = self._last_cond
            if arg is None:
                print("c needs a condition. try:  c i>5   c grid   c raise")
                return
        self._last_cond = arg
        arg = arg.strip()
        if arg in ("raise", "error", "except"):
            want = (schema.KIND_CODE[schema.RAISE],
                    schema.KIND_CODE[schema.UNWIND])
            for s in range(self.cursor + 1, self.r.n_steps + 1):
                if self.r.raw(s)[1] in want:
                    return self.seek(s)
            print("%snothing was raised after here.%s" % (DIM, OFF))
            return
        m = COND.match(arg)
        if not m:
            return self.c_s(arg)
        name, op, rhs = m.group(1), m.group(2), m.group(3).strip()
        try:
            target = eval(rhs, {"__builtins__": {}}, {})    # literals only
        except Exception:
            target = rhs.strip("'\"")
        ops = {"=": lambda a, b: a == b, "==": lambda a, b: a == b,
               "!=": lambda a, b: a != b, ">": lambda a, b: a > b,
               "<": lambda a, b: a < b, ">=": lambda a, b: a >= b,
               "<=": lambda a, b: a <= b}
        fn = ops[op]

        def test(got):
            if isinstance(got, dict):
                got = got.get("repr")
            try:
                return fn(got, target)
            except TypeError:
                return False
        self._seek_write(name, test=test)

    # -------------------------------------------------------------------- look

    def c_pd(self, arg=None):
        """pd [N] -- the source here, with what each line does. (N=9)"""
        span = int(arg or 9)
        path, line = self.here()
        src = self.r.source(path).splitlines()
        if not src:
            print("%sno source stored for %s%s" % (DIM, path, OFF))
            return
        writes = {}
        for var, info in self.a.variables.items():
            for key, count in info["by_line"].items():
                if key.startswith(draw.rel(path) + ":"):
                    writes.setdefault(int(key.rsplit(":", 1)[1]),
                                      []).append(var)
        lo = max(1, line - span // 2)
        hi = min(len(src), lo + span - 1)
        print("%s%s%s" % (BOLD, path, OFF))
        for i in range(lo, hi + 1):
            hits, t_ns = self._lines.get((path, i), (0, 0))
            mark = "%s▸%s" % (YELLOW, OFF) if i == line else " "
            w = writes.get(i)
            note = ("  %s→ %s%s" % (CYAN, ",".join(sorted(set(w))[:4]), OFF)
                    if w else "")
            print("%s %s%5s %7s%s  %s%s"
                  % (mark, DIM, i, _n(hits) if hits else "·", OFF,
                     src[i - 1][:draw.width() - 30], note))

    def c_pv(self, arg=None):
        """pv -- the variables that exist right now."""
        st = self.r.state_at(self.cursor)
        e = self.ev()
        changed = e["name"]
        rows = []
        for frame in st["frames"][::-1]:
            fname = frame.get("func")
            if isinstance(fname, int):
                f = self.r.funcs.get(fname) or {}
                fname = f.get("qualname") or f.get("name")
            for k, v in frame["locals"].items():
                rows.append([("%s%s%s" % (YELLOW, k, OFF)
                              if k == changed else k),
                             self.val(v), fname or "?"])
            break                      # innermost frame only; `bt` has rest
        glob = [[k, self.val(v), "%sglobal%s" % (DIM, OFF)]
                for k, v in st["globals"].items()
                if not k.startswith("__") and k not in self.a.functions]
        rows += glob[:12]
        if not rows:
            print("%sno variables in scope here.%s" % (DIM, OFF))
            return
        for line in table(["name", "value", "where"], rows, limit=40):
            print(line)
        if changed:
            print("%s%s is what step %s changed.%s"
                  % (DIM, changed, _n(self.cursor), OFF))

    def c_pV(self, arg=None):
        """pV NAME -- one variable in full, here."""
        if not arg:
            return print("pV needs a name. `pv` lists them.")
        st = self.r.state_at(self.cursor)
        ref = None
        for frame in st["frames"][::-1]:
            if arg in frame["locals"]:
                ref = frame["locals"][arg]
                break
        if ref is None:
            ref = st["globals"].get(arg)
        if ref is None:
            print("%s%s is not in scope at step %s.%s"
                  % (YELLOW, arg, _n(self.cursor), OFF))
            info = self.a.variables.get(arg)
            if info:
                print("  it is written %s times, first at step %s. "
                      "`s %s` jumps there."
                      % (_n(info["writes"]), _n(info["first"]), arg))
            return
        got = self.r.resolve(ref, self.cursor)
        if not isinstance(got, dict):
            print("%s%s%s = %r   %s(%s)%s"
                  % (BOLD, arg, OFF, got, DIM, type(got).__name__, OFF))
            return
        print("%s%s%s  %s%s%s" % (BOLD, arg, OFF, CYAN, got["type"], OFF))
        print("  %s" % got["repr"][:draw.width() - 4])
        for k, v in got["summary"].items():
            print("  %s%-10s%s %s" % (DIM, k, OFF, v))
        kids = self.r.decode_children(got["children"])
        if kids:
            rows = [[str(k), self.val(v)] for k, v in kids[:30]]
            for line in table(["key", "value"], rows):
                print("  " + line)

    def c_bt(self, arg=None):
        """bt -- the call stack at the cursor."""
        st = self.r.state_at(self.cursor)
        if not st["frames"]:
            print("%sat module level -- no calls open.%s" % (DIM, OFF))
            return
        rows = []
        for depth, frame in enumerate(st["frames"]):
            fname = frame.get("func")
            if isinstance(fname, int):
                f = self.r.funcs.get(fname) or {}
                fname = f.get("qualname") or f.get("name")
            path, line = self.r.where(frame["loc"])
            args = ", ".join("%s=%s" % (k, self.val(v, 14))
                             for k, v in list(frame["locals"].items())[:3])
            rows.append(["  " * depth + (fname or "?"),
                         "%s:%s" % (draw.rel(path), line), args])
        for line in table(["frame", "at", "locals"], rows[::-1]):
            print(line)

    def c_ll(self, arg=None):
        """ll [N] -- the raw events around the cursor. (N=16)"""
        span = int(arg or 16)
        lo = max(1, self.cursor - span // 2)
        rows = []
        for s in range(lo, min(self.r.n_steps, lo + span - 1) + 1):
            e = self.r.view(s)
            rows.append([("%s▸%s" % (YELLOW, OFF) if s == self.cursor
                          else " ") + _n(s),
                         "%s:%s" % (draw.rel(e["file"]), e["line"]),
                         e["kind"], e["name"] or "",
                         self.val(e["ref"], 30, s) if e["name"] else ""])
        for line in table(["step", "where", "event", "name", "value"], rows):
            print(line)

    # ----------------------------------------------------------------- analyse

    def c_afl(self, arg=None):
        """afl -- every function: how often, how long, what it touches."""
        fns = sorted(self.a.functions.values(),
                     key=lambda f: -f["self_steps"])
        peak = max((f["self_steps"] for f in fns), default=1) or 1
        rows = []
        for f in fns:
            rows.append([f["name"], _n(f["calls"]), _n(f["self_steps"]),
                         _ms(f["self_ms"]),
                         len(f["callers"]), len(f["callees"]),
                         len(f["writes"]),
                         "%s%s%s" % (CYAN, bar(f["self_steps"] / peak, 12),
                                     OFF)])
        for line in table(["function", "calls", "steps", "ms",
                           "←", "→", "vars", ""],
                          rows, aligns=["<", ">", ">", ">", ">", ">",
                                        ">", "<"], limit=30):
            print(line)
        print("%ssteps and ms are its own lines only, not the ones it "
              "called · ← callers → callees · vars = variables it writes%s"
              % (DIM, OFF))
        print("%s`af NAME` for one function · `tree` for the shape%s"
              % (DIM, OFF))

    def c_af(self, arg=None):
        """af [NAME] -- one function, wired up. The cursor's, by default."""
        name = self.a.func(arg) if arg else self.fn_at()
        f = self.a.functions.get(name)
        if f is None:
            print("%sno function called %r. `afl` lists them.%s"
                  % (YELLOW, arg, OFF))
            return
        print("%s%s%s  %s%s:%s%s"
              % (BOLD, name, OFF, DIM, draw.rel(f["file"]), f["line"], OFF))
        print("  called %s times · %s steps inside · %.1f ms of its own"
              % (_n(f["calls"]), _n(f["self_steps"]), f["self_ms"]))
        if f["raises"]:
            print("  %sleft by an exception %s times%s"
                  % (YELLOW, _n(f["raises"]), OFF))

        if f["callers"]:
            print("\n%scalled by%s" % (BOLD, OFF))
            for line in table(["function", "times"],
                              [[k, _n(v)] for k, v in
                               sorted(f["callers"].items(),
                                      key=lambda kv: -kv[1])],
                              aligns=["<", ">"], limit=10):
                print("  " + line)
        if f["callees"]:
            print("\n%scalls%s" % (BOLD, OFF))
            for line in table(["function", "times"],
                              [[k, _n(v)] for k, v in
                               sorted(f["callees"].items(),
                                      key=lambda kv: -kv[1])],
                              aligns=["<", ">"], limit=10):
                print("  " + line)

        reads = self.a.reads.get(name, {})
        writes = f["writes"]
        both = sorted(set(writes) | set(reads),
                      key=lambda v: -(writes.get(v, 0) + reads.get(v, 0)))
        if both:
            print("\n%svariables%s  %s(reads are from the source, writes "
                  "from the run)%s" % (BOLD, OFF, DIM, OFF))
            rows = [[v, _n(writes[v]) if v in writes else "",
                     reads.get(v, "") or "",
                     "write+read" if v in writes and v in reads else
                     ("writes" if v in writes else "reads only")]
                    for v in both]
            for line in table(["name", "writes", "reads", ""], rows,
                              aligns=["<", ">", ">", "<"], limit=20):
                print("  " + line)

        # Recorded when each call returns, so an inner call is stored before
        # the outer one it was made from. Sort, or the table reads backwards.
        invs = sorted(self.a.invocations.get(name, []))
        if invs:
            print("\n%seach call%s" % (BOLD, OFF))
            rows = []
            for enter, leave in invs[:8]:
                e = self.r.view(enter)
                args = ", ".join(
                    "%s=%s" % (k, self.val(self.r._norm(v), 16, enter))
                    for k, v in (e["extra"].get("args") or {}).items())
                ret = self.r.view(leave)
                rows.append([_n(enter), args or "–",
                             self.val(ret["ref"], 24, leave),
                             _n(leave - enter)])
            for line in table(["at step", "arguments", "returned", "steps"],
                              rows, aligns=["<", "<", "<", ">"]):
                print("  " + line)
            if len(invs) > 8:
                print("  %s… %s more calls. `s fn:%s` walks them.%s"
                      % (DIM, _n(len(invs) - 8), name, OFF))

    def c_axf(self, arg=None):
        """axf [NAME] -- cross references: in and out of a function."""
        name = self.a.func(arg) if arg else self.fn_at()
        if name is None:
            return print("%sno such function.%s" % (YELLOW, OFF))
        up = self.a.callers_of(name)
        down = self.a.callees_of(name)
        print("%s%s%s" % (BOLD, name, OFF))
        for who, n in up.most_common(8):
            print("  %s◀── %-24s%s %s calls" % (DIM, who, OFF, _n(n)))
        print("  %s%s%s" % (CYAN, name, OFF))
        for who, n in down.most_common(8):
            print("  %s──▶ %-24s%s %s calls" % (DIM, who, OFF, _n(n)))
        if not up and not down:
            print("  %snothing calls it and it calls nothing.%s" % (DIM, OFF))

    def c_axv(self, arg=None):
        """axv NAME -- cross references of a variable: writers and readers."""
        if not arg:
            return print("axv needs a variable. `vl` lists them.")
        name = arg.strip()
        info = self.a.variables.get(name)
        if info is None:
            near = [k for k in self.a.variables if name.lower() in k.lower()]
            print("%s%s is never written.%s" % (YELLOW, name, OFF))
            if near:
                print("  did you mean: %s" % ", ".join(near[:8]))
            return
        print("%s%s%s  written %s times  %s(%s new values, %s in-place, "
              "%s deletes)%s"
              % (BOLD, name, OFF, _n(info["writes"]), DIM,
                 _n(info["binds"]), _n(info["mutates"]),
                 _n(info["deletes"]), OFF))
        print("  alive from step %s to %s"
              % (_n(info["first"]), _n(info["last"])))

        print("\n%swritten by%s" % (BOLD, OFF))
        for line in table(["function", "times"],
                          [[k, _n(v)] for k, v in
                           sorted(info["by_func"].items(),
                                  key=lambda kv: -kv[1])],
                          aligns=["<", ">"], limit=10):
            print("  " + line)
        print("\n%son these lines%s" % (BOLD, OFF))
        for line in table(["line", "times"],
                          [[k, _n(v)] for k, v in
                           sorted(info["by_line"].items(),
                                  key=lambda kv: -kv[1])],
                          aligns=["<", ">"], limit=10):
            print("  " + line)
        readers = self.a.readers_of(name)
        if readers:
            print("\n%sread by%s  %s(from the source)%s" % (BOLD, OFF,
                                                            DIM, OFF))
            for line in table(["function", "places"],
                              [[k, v] for k, v in readers.most_common(10)],
                              aligns=["<", ">"]):
                print("  " + line)
        series, obj = self.r.numeric_series(name, cap=draw.width())
        nums = [v for _s, v in series if v is not None]
        if len(nums) > 3:
            print("\n  %s%s%s  %s%s → %s%s"
                  % (CYAN, sparkline([v for _s, v in series]), OFF,
                     DIM, draw._num(nums[0]), draw._num(nums[-1]), OFF))
        print("\n%s`ch %s` for the value table · `pl %s` to plot it · "
              "`s %s` to jump there%s" % (DIM, name, name, name, OFF))

    def c_fv(self, arg=None):
        """fv [NAME] -- the function x variable grid. W = writes, r = reads."""
        fns = [f for f in sorted(self.a.functions.values(),
                                 key=lambda f: -f["self_steps"])][:12]
        if arg:
            fns = [f for f in self.a.functions.values()
                   if arg.lower() in f["name"].lower()][:12]
        if not fns:
            return print("%sno functions match.%s" % (YELLOW, OFF))
        score = {}
        for f in fns:
            for v, n in f["writes"].items():
                score[v] = score.get(v, 0) + n
            for v, n in self.a.reads.get(f["name"], {}).items():
                score[v] = score.get(v, 0) + n
        skip = set(self.a.functions)
        names = [v for v in sorted(score, key=lambda v: -score[v])
                 if v not in skip][:14]
        if not names:
            return print("%snothing to cross.%s" % (DIM, OFF))
        head = ["function"] + [v[:7] for v in names]
        rows = []
        for f in fns:
            reads = self.a.reads.get(f["name"], {})
            cells = []
            for v in names:
                w, rd = f["writes"].get(v, 0), reads.get(v, 0)
                if w and rd:
                    cells.append("%sW%s" % (GREEN, OFF) + "r")
                elif w:
                    cells.append("%sW%s" % (GREEN, OFF))
                elif rd:
                    cells.append("%sr%s" % (CYAN, OFF))
                else:
                    cells.append("%s·%s" % (DIM, OFF))
            if any("·" not in c for c in cells):
                rows.append([f["name"][:18]] + cells)
        for line in table(head, rows,
                          aligns=["<"] + ["^"] * len(names)):
            print(line)
        print("%sW = writes it during the run · r = reads it in the source · "
              "· = no connection%s" % (DIM, OFF))

    def c_vl(self, arg=None):
        """vl [TEXT] -- every variable: writes, where, when."""
        items = sorted(self.a.variables.items(),
                       key=lambda kv: -kv[1]["writes"])
        if arg:
            items = [(k, v) for k, v in items if arg.lower() in k.lower()]
        rows = []
        for name, v in items:
            owner = max(v["by_func"], key=v["by_func"].get) \
                if v["by_func"] else "?"
            rows.append([name, _n(v["writes"]), _n(v["binds"]),
                         _n(v["mutates"]), owner,
                         "%s–%s" % (_n(v["first"]), _n(v["last"]))])
        for line in table(["name", "writes", "new", "in-place", "mostly in",
                           "steps"], rows,
                          aligns=["<", ">", ">", ">", "<", ">"], limit=35):
            print(line)

    def c_ch(self, arg=None):
        """ch NAME -- the value table: every change, with the line."""
        if not arg:
            return print("ch needs a variable. `vl` lists them.")
        parts = arg.split()
        name = parts[0]
        limit = int(parts[1]) if len(parts) > 1 else 25
        rows = self.r.history_of(name, limit=limit)
        if not rows:
            return self.c_axv(name)
        out = []
        for row in rows:
            was = ""
            if row["prev"] is not None and row["prev"][0] == "i":
                was = "%swas %r%s" % (DIM, row["prev"][1], OFF)
            out.append([_n(row["step"]),
                        "%s:%s" % (draw.rel(row["file"]), row["line"]),
                        row["kind"], self.val(row["ref"], 34, row["step"]),
                        was])
        for line in table(["step", "where", "how", name, ""], out):
            print(line)
        total = self.a.variables.get(name, {}).get("writes", len(rows))
        if total > len(rows):
            print("%sshowing %d of %s changes. `ch %s %d` for more.%s"
                  % (DIM, len(rows), _n(total), name, limit * 4, OFF))

    def c_pl(self, arg=None):
        """pl NAME -- draw it over the whole run."""
        if not arg:
            return print("pl needs a variable. `vl` lists them.")
        name = arg.split()[0]
        series, obj = self.r.numeric_series(name, cap=draw.width() - 14)
        if not series:
            return self.c_axv(name)
        values = [v for _s, v in series]
        if any(v is not None for v in values):
            for line in plot(values, label="%s over the run" % name,
                             xlabel="step %s → %s" % (_n(series[0][0]),
                                                      _n(series[-1][0]))):
                print(line)
            nums = [v for v in values if v is not None]
            print("%smin %s · max %s · first %s · last %s%s"
                  % (DIM, draw._num(min(nums)), draw._num(max(nums)),
                     draw._num(nums[0]), draw._num(nums[-1]), OFF))
            return
        if obj:
            versions = self.r.object_series(obj)
            for key in ("len", "rows", "nbytes"):
                got = [(s, (sm.get(key) if key != "nbytes" else nb))
                       for s, sm, nb, _t in versions]
                got = [(s, v) for s, v in got if isinstance(v, (int, float))]
                if len(got) > 3:
                    for line in plot([v for _s, v in got],
                                     label="%s · %s of the object"
                                           % (name, key)):
                        print(line)
                    return
        print("%s%s holds objects with nothing numeric to plot. "
              "try `ch %s`.%s" % (DIM, name, name, OFF))

    def c_tt(self, arg=None):
        """tt -- the timeline: which function was busy, when."""
        act = self.a.activity
        order = sorted(act, key=lambda k: -sum(act[k]))[:14]
        if not order:
            return print("%snothing to draw.%s" % (DIM, OFF))
        print("%swho was running, left = start of the run, "
              "right = the end%s" % (DIM, OFF))
        for line in heatrows(order, [act[k] for k in order]):
            print(line)
        cells = draw.width() - 26
        at = int((self.cursor - 1) / max(1, self.r.n_steps - 1) * (cells - 1))
        print("%-22s %s%s▲%s" % ("", " " * at, YELLOW, OFF))
        print("%-22s %syou are here: step %s%s"
              % ("", DIM, _n(self.cursor), OFF))

    def c_tree(self, arg=None):
        """tree [NAME] -- the call tree, by cost."""
        root = self.a.func(arg) if arg else "<module>"
        if root not in self.a.functions:
            root = "<module>"
        fns = self.a.functions

        def kids(name):
            got = fns.get(name, {}).get("callees", {})
            return [k for k in sorted(got, key=lambda k: -got[k])
                    if k in fns and k != name][:8]

        def label(name):
            f = fns.get(name, {})
            mark = ""
            if name in f.get("callees", {}):
                mark = "  %s↻ calls itself %s times%s" % (
                    YELLOW, _n(f["callees"][name]), OFF)
            return "%s%s%s %s%s calls · %s steps · %.1f ms%s%s" % (
                BOLD, name, OFF, DIM, _n(f.get("calls", 0)),
                _n(f.get("self_steps", 0)), f.get("self_ms", 0.0), OFF,
                mark)

        for line in tree(root, kids, label):
            print(line)

    def c_cmp(self, arg=None):
        """cmp A B -- what is different between two steps."""
        parts = (arg or "").split()
        if len(parts) < 1:
            return print("cmp needs two steps: cmp 100 2000 "
                         "(one step compares it with the cursor)")
        a = int(parts[0])
        b = int(parts[1]) if len(parts) > 1 else self.cursor
        sa, sb = self.r.state_at(a), self.r.state_at(b)

        def flat(st):
            out = dict(st["globals"])
            for f in st["frames"]:
                out.update(f["locals"])
            return out
        fa, fb = flat(sa), flat(sb)
        rows = []
        for k in sorted(set(fa) | set(fb)):
            if k.startswith("__"):
                continue
            va, vb = fa.get(k), fb.get(k)
            if va == vb:
                continue
            rows.append([k,
                         self.val(va, 28, a) if va is not None
                         else "%s(not yet)%s" % (DIM, OFF),
                         self.val(vb, 28, b) if vb is not None
                         else "%s(gone)%s" % (DIM, OFF)])
        print("%sstep %s → step %s%s" % (BOLD, _n(a), _n(b), OFF))
        if not rows:
            print("  %snothing visible changed.%s" % (DIM, OFF))
            return
        for line in table(["name", "at %s" % _n(a), "at %s" % _n(b)],
                          rows, limit=30):
            print(line)

    def c_search(self, arg=None):
        """/ TEXT -- find it in variable names, values or source lines."""
        if not arg:
            return print("/ needs something to look for.")
        text = arg.strip().lower()
        hits = [v for v in self.a.variables if text in v.lower()]
        fns = [f for f in self.a.functions if text in f.lower()]
        if fns:
            print("%sfunctions%s  %s" % (BOLD, OFF, ", ".join(fns[:12])))
        if hits:
            print("%svariables%s  %s" % (BOLD, OFF, ", ".join(hits[:14])))
        path, _ = self.here()
        src = self.r.source(path).splitlines()
        rows = []
        for i, line_text in enumerate(src, 1):
            if text in line_text.lower():
                hit, _t = self._lines.get((path, i), (0, 0))
                rows.append([str(i), _n(hit) if hit else
                             "%snever ran%s" % (YELLOW, OFF),
                             line_text.strip()[:70]])
        if rows:
            print("%sin %s%s" % (BOLD, draw.rel(path), OFF))
            for line in table(["line", "hits", "source"], rows, limit=15):
                print("  " + line)
        if not (hits or fns or rows):
            print("%snothing matches %r.%s" % (DIM, arg, OFF))

    def c_dis(self, arg=None):
        """dis -- the hottest lines in this file."""
        path, _ = self.here()
        src = self.r.source(path).splitlines()
        stats = [(ln, h, t) for (p, ln), (h, t) in self._lines.items()
                 if p == path]
        stats.sort(key=lambda s: -s[1])
        peak = stats[0][1] if stats else 1
        rows = []
        for ln, hits, t_ns in stats[:20]:
            rows.append([str(ln), _n(hits), "%.1f" % (t_ns / 1e6),
                         "%s%s%s" % (CYAN, bar(hits / peak, 10), OFF),
                         (src[ln - 1].strip() if ln <= len(src) else "")[:54]])
        for line in table(["line", "hits", "ms", "", "source"], rows,
                          aligns=["<", ">", ">", "<", "<"]):
            print(line)

    def c_info(self, arg=None):
        """i -- what this run was."""
        m = self.r.meta
        print("%s%s%s" % (BOLD, self.base, OFF))
        for key in ("script", "python", "platform", "driver",
                    "wall_seconds", "error"):
            if key in m:
                print("  %s%-13s%s %s" % (DIM, key, OFF, m[key]))
        print("  %s%-13s%s %s" % (DIM, "steps", OFF, _n(self.r.n_steps)))
        print("  %s%-13s%s %d functions, %d variables"
              % (DIM, "found", OFF, len(self.a.functions),
                 len(self.a.variables)))

    # -------------------------------------------------------------------- help

    GROUPS = [
        ("where am i", [
            ("s N", "go to step N.  s +50  s -50  s 50%  s end"),
            ("s NAME", "go to the next change of a variable"),
            ("s fn:NAME", "go to the next call of a function"),
            ("s :26", "go to the next time line 26 ran"),
            ("n / p", "one step forward / back   (n 100 for a hundred)"),
            ("f / F", "next / previous event in THIS frame (step over)"),
            ("u", "step out: where this frame returns"),
            ("c COND", "run on until true.  c i>5   c grid   c raise"),
        ]),
        ("look at this moment", [
            ("pd", "the source here, with hits and what each line writes"),
            ("pv", "the variables that exist right now"),
            ("pV NAME", "one variable in full, here"),
            ("bt", "the call stack"),
            ("ll", "the raw events around the cursor"),
        ]),
        ("look at the whole run", [
            ("afl", "every function: calls, time, how wired up it is"),
            ("af NAME", "one function: callers, callees, its variables"),
            ("axf NAME", "who calls it, who it calls"),
            ("axv NAME", "who writes a variable, who reads it, where"),
            ("fv", "the function x variable grid -- the connections"),
            ("vl", "every variable, by how often it changes"),
            ("ch NAME", "the value table: every change, with the line"),
            ("pl NAME", "draw a variable over the whole run"),
            ("tt", "timeline: which function was busy when"),
            ("tree", "the call tree, by cost"),
            ("cmp A B", "what is different between two steps"),
            ("dis", "the hottest lines in this file"),
            ("/ TEXT", "find a name, a value or a line of source"),
            ("i", "what this run was"),
        ]),
    ]

    def c_help(self, arg=None):
        if arg:
            fn = self.TABLE.get(arg.strip())
            if fn:
                print(fn.__doc__ or "no help")
            else:
                print("%sno command %r.%s" % (YELLOW, arg, OFF))
            return
        print("%severy command works with no arguments. "
              "the cursor is one step of the run.%s" % (DIM, OFF))
        for title, items in self.GROUPS:
            print("\n%s%s%s" % (BOLD, title, OFF))
            for cmd, text in items:
                print("  %s%-10s%s %s" % (CYAN, cmd, OFF, text))
        print("\n%sq%s to leave · %s??%s for a worked example"
              % (CYAN, OFF, CYAN, OFF))

    def c_tour(self, arg=None):
        """?? -- a worked example, using this very trace."""
        fns = sorted(self.a.functions.values(),
                     key=lambda f: -f["self_steps"])
        hot = next((f["name"] for f in fns if f["name"] != "<module>"),
                   "<module>")
        var = max(self.a.variables,
                  key=lambda v: self.a.variables[v]["writes"],
                  default="x")
        print("%sa walk through this run, in six commands%s" % (BOLD, OFF))
        for cmd, why in [
            ("afl", "which functions did the work"),
            ("af %s" % hot, "what %s is wired to" % hot),
            ("axv %s" % var, "who writes %s, and who reads it" % var),
            ("s %s" % var, "jump to the first change of %s" % var),
            ("pd", "see the code there, with the cursor on it"),
            ("pl %s" % var, "draw %s over the whole run" % var),
        ]:
            print("  %s%-18s%s %s%s%s" % (CYAN, cmd, OFF, DIM, why, OFF))

    def c_quit(self, arg=None):
        self.running = False

    TABLE = {}

    # --------------------------------------------------------------- dispatch

    def do(self, text):
        """Run one line. `;` separates commands, as in r2."""
        for piece in text.split(";"):
            piece = piece.strip()
            if not piece or piece.startswith("#"):
                continue
            self._one(piece)

    def _one(self, piece):
        if piece.startswith("/"):
            return self.c_search(piece[1:])
        if piece.startswith("?"):
            if piece.startswith("??"):
                return self.c_tour()
            return self.c_help(piece[1:] or None)
        head, _, rest = piece.partition(" ")
        fn = self.TABLE.get(head)
        if fn is None:
            # A bare number is a seek: `1200` means `s 1200`.
            if head.lstrip("+-").rstrip("%").isdigit():
                return self.c_s(piece)
            print("%sno command %r. press ? for the list.%s"
                  % (YELLOW, head, OFF))
            return
        try:
            fn(self, rest.strip() or None)
        except (ValueError, IndexError) as exc:
            print("%s%s%s  (%s)" % (YELLOW, exc, OFF, fn.__doc__ or head))
        except KeyboardInterrupt:
            print()


Explorer.TABLE = {
    "s": Explorer.c_s, "seek": Explorer.c_s,
    "n": Explorer.c_n, "next": Explorer.c_n,
    "p": Explorer.c_p, "prev": Explorer.c_p,
    "f": Explorer.c_f, "F": Explorer.c_F,
    "u": Explorer.c_u, "out": Explorer.c_u,
    "c": Explorer.c_c, "cont": Explorer.c_c,
    ".": Explorer.c_s,
    "pd": Explorer.c_pd, "src": Explorer.c_pd,
    "pv": Explorer.c_pv, "vars": Explorer.c_pv,
    "pV": Explorer.c_pV, "var": Explorer.c_pV,
    "bt": Explorer.c_bt, "stack": Explorer.c_bt,
    "ll": Explorer.c_ll, "events": Explorer.c_ll,
    "afl": Explorer.c_afl, "funcs": Explorer.c_afl,
    "af": Explorer.c_af, "func": Explorer.c_af,
    "axf": Explorer.c_axf, "axv": Explorer.c_axv,
    "fv": Explorer.c_fv, "grid": Explorer.c_fv,
    "vl": Explorer.c_vl, "list": Explorer.c_vl,
    "ch": Explorer.c_ch, "changes": Explorer.c_ch,
    "pl": Explorer.c_pl, "plot": Explorer.c_pl,
    "tt": Explorer.c_tt, "timeline": Explorer.c_tt,
    "tree": Explorer.c_tree,
    "cmp": Explorer.c_cmp, "diff": Explorer.c_cmp,
    "dis": Explorer.c_dis, "hot": Explorer.c_dis,
    "i": Explorer.c_info, "info": Explorer.c_info,
    "help": Explorer.c_help,
    "q": Explorer.c_quit, "quit": Explorer.c_quit, "exit": Explorer.c_quit,
}


# --------------------------------------------------------------------------
# the prompt
# --------------------------------------------------------------------------

def _readline(path):
    """History and tab completion, if the terminal has readline."""
    try:
        import readline
    except ImportError:                                  # pragma: no cover
        return None
    hist = os.path.expanduser("~/.nd36_history")
    try:
        readline.read_history_file(hist)
    except OSError:
        pass
    readline.set_history_length(2000)
    return hist


def repl(reader, path, commands=(), quiet=False):
    """The interactive session. `commands` run first, as if typed."""
    ex = Explorer(reader, path, quiet=quiet)
    hist = _readline(path)
    try:
        import readline
        words = sorted(set(list(Explorer.TABLE) + list(ex.a.functions)
                           + list(ex.a.variables)))

        def complete(text, state):
            got = [w for w in words if w.startswith(text)]
            return got[state] + " " if state < len(got) else None
        readline.set_completer(complete)
        readline.parse_and_bind("tab: complete")
        readline.set_completer_delims(" \t")
    except Exception:                                    # pragma: no cover
        pass

    for cmd in commands:
        print("%s%s%s" % (DIM, cmd, OFF))
        ex.do(cmd)
    if commands and not ex.running:
        return
    print("\n%s%s%s · %s steps · %spress ? for commands, q to leave%s"
          % (BOLD, ex.base, OFF, _n(reader.n_steps), DIM, OFF))
    ex.status()
    while ex.running:
        try:
            line = input(ex.prompt())
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line.strip():
            continue
        try:
            ex.do(line)
        except Exception as exc:                         # never lose the repl
            print("%s%s: %s%s" % (RED, type(exc).__name__, exc, OFF))
    if hist:
        try:
            import readline
            readline.write_history_file(hist)
        except OSError:                                  # pragma: no cover
            pass


def once(reader, path, commands):
    """`nd x TRACE -c 'afl'` -- run and exit, so it can be piped."""
    ex = Explorer(reader, path, quiet=True)
    for cmd in commands:
        ex.do(cmd)
