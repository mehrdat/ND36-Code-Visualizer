"""
The Python recorder.

One shared brain (`Core`) and two drivers:

  MonitoringRecorder  -- sys.monitoring, PEP 669, Python 3.12+.   FAST.
  SettraceRecorder    -- sys.settrace, works everywhere.          SLOW.

The settrace one exists mainly so the test suite can check the fast one
against a dumb, obviously-correct implementation.  If they disagree, the
fast one is wrong.
"""

import sys
from time import perf_counter_ns

from . import schema
from .values import Encoder
from .writeset import mutation_roots, scan_file

# Containers larger than this are not re-walked on every line -- we only
# notice that the NAME was rebound, not that the contents changed.
# This single number is the main speed/detail dial in Standard mode.
DEEP_CONTAINER_LIMIT = 64

# `type(x) in _SCALARS` is a dict lookup on an exact type; isinstance() has
# to walk the MRO. On a path that runs millions of times that matters.
_SCALARS = frozenset((int, float, bool, str, bytes, complex))
_MUTABLE = frozenset((list, dict, set, bytearray))
_EMPTY = {}
_MISSING = object()


def _same_object(was, now):
    """Is this the SAME object with new contents (MUTATE), or a different
    value under the same name (BIND)?

    Identity decides, not the (id, len) fast key -- `counts["x"] = 1` changes
    the length but it is still the same dict, so it is a mutation.
    """
    return (was is not None and was[0] == schema.RK_OBJ
            and now[0] == schema.RK_OBJ and was[1] == now[1])

# Kind codes, resolved once at import. On the hot path we pass ints, never
# strings, because the record on disk stores a byte.
K_LINE = schema.KIND_CODE[schema.LINE]
K_CALL = schema.KIND_CODE[schema.CALL]
K_RETURN = schema.KIND_CODE[schema.RETURN]
K_YIELD = schema.KIND_CODE[schema.YIELD]
K_BIND = schema.KIND_CODE[schema.BIND]
K_DELETE = schema.KIND_CODE[schema.DELETE]
K_RAISE = schema.KIND_CODE[schema.RAISE]
K_UNWIND = schema.KIND_CODE[schema.UNWIND]
K_MUTATE = schema.KIND_CODE[schema.MUTATE]


class _Frame:
    """What we remember about one running function activation."""
    __slots__ = ("fid", "code", "names", "nameids", "prev_loc", "prev_t",
                 "prev_line", "cache", "refs", "is_module", "loccache")

    def __init__(self, fid, code, names, nameids, is_module):
        self.fid = fid
        self.code = code
        self.names = names        # names this function can bind (static)
        self.nameids = nameids    # name -> interned id, resolved once
        self.prev_loc = None      # the line we attribute changes to
        self.prev_line = 0        # its line number, to look up mutation roots
        self.prev_t = 0           # when that line started, for per-line time
        self.cache = {}           # name -> cheap identity of last value
        self.refs = {}            # name -> last emitted vref (for keyframes)
        self.is_module = is_module
        self.loccache = {}        # lineno -> loc id, so we skip the tuple
                                  # build + string hash on every single line


class Core:
    """Driver-independent recording logic."""

    def __init__(self, writer, scope, trace_lines=True, sampler=None,
                 keep_alive=None):
        self.w = writer
        self.enc = (Encoder(writer) if keep_alive is None
                    else Encoder(writer, keep_alive=keep_alive))
        self.scope = scope             # set of absolute filenames to trace
        self.trace_lines = trace_lines
        self.sampler = sampler
        self.writesets = {p: scan_file(p) for p in scope}
        self.mutroots = {p: mutation_roots(p) for p in scope}
        self.frames = {}               # id(frame) -> _Frame
        self.stack = []                # _Frame, innermost last
        self._next_fid = 1
        self._offsets = {}
        self._offsets_keep = []
        self.t0 = perf_counter_ns()

    # -- helpers -----------------------------------------------------------

    def in_scope(self, code):
        return code.co_filename in self.scope

    def _t(self):
        return perf_counter_ns() - self.t0

    def _loc(self, code, line):
        return self.w.loc_id(code.co_filename, line)

    def _names_for(self, code):
        table = self.writesets.get(code.co_filename) or {}
        if code.co_name == "<module>":
            return table.get(("<module>", 0), frozenset())
        got = table.get((code.co_name, code.co_firstlineno))
        if got is not None:
            return got
        # Fall back to any scope declared on this line (a lambda, or a
        # version quirk in where co_firstlineno points).
        for (name, line), names in table.items():
            if line == code.co_firstlineno:
                return names
        return frozenset()

    def line_of_offset(self, code):
        """{bytecode offset: source line} for one code object, built once.

        Needed because sys.monitoring's LINE event does NOT fire when a loop
        jumps backwards to the SAME line -- so `while n < 3: n += 1` and list
        comprehensions would look like one step instead of N. We watch JUMP
        events to put those iterations back, and that means knowing which
        line a jump destination belongs to.
        """
        got = self._offsets.get(id(code))
        if got is None:
            got = {}
            for start, end, line in code.co_lines():
                if line is None:
                    continue
                for off in range(start, end, 2):
                    got[off] = line
            self._offsets[id(code)] = got
            self._offsets_keep.append(code)
        return got

    def _cheap_id(self, value):
        """A fast, allocation-free stand-in for 'is this the same value?'.

        Immutable scalars compare by value; everything else by identity plus
        length, which catches append/pop without walking the container.
        """
        if value is None or isinstance(value, (int, float, bool, str, bytes)):
            return value
        try:
            return (id(value), len(value))
        except Exception:
            return (id(value),)

    # -- events ------------------------------------------------------------

    def push(self, frame, code):
        names = self._names_for(code)
        f = _Frame(self._next_fid, code, names,
                   {n: self.w.name_id(n) for n in names},
                   frame.f_locals is frame.f_globals)
        self._next_fid += 1
        self.frames[id(frame)] = f
        self.stack.append(f)

        loc = self._loc(code, code.co_firstlineno)
        f.prev_loc = loc
        f.prev_line = code.co_firstlineno
        f.prev_t = self._t()
        args = {}
        local_vars = frame.f_locals
        for i in range(code.co_argcount + code.co_kwonlyargcount):
            name = code.co_varnames[i]
            if name in local_vars:
                vref = self.enc.encode(local_vars[name], self.w.step + 1)
                args[name] = vref
                f.refs[name] = vref
                f.cache[name] = self._cheap_id(local_vars[name])
                if name not in f.nameids:
                    f.nameids[name] = self.w.name_id(name)
        func = self.w.func_id(code.co_name,
                              getattr(code, "co_qualname", code.co_name),
                              code.co_filename, code.co_firstlineno)
        pid = self.w.payload_id({"func": func, "args": args,
                                 "scope": "global" if f.is_module
                                 else "local"})
        self.w.event(f.prev_t, K_CALL, f.fid, loc, payload=pid)
        return f

    def line(self, frame, code, lineno):
        f = self.frames.get(id(frame))
        if f is None:
            f = self.push(frame, code)
        self.diff(frame, f)
        loc = f.loccache.get(lineno)
        if loc is None:
            loc = f.loccache[lineno] = self._loc(code, lineno)
        now = perf_counter_ns() - self.t0
        self.w.note_line(loc, f.prev_loc, now - f.prev_t)
        f.prev_loc = loc
        f.prev_line = lineno
        f.prev_t = now
        self.w.event(now, K_LINE, f.fid, loc)
        if self.sampler is not None:
            self.sampler.maybe(self.w.step, now)
        if self.w.should_keyframe():
            self.write_keyframe()

    def pop(self, frame, retval, unwinding=False):
        f = self.frames.pop(id(frame), None)
        if f is None:
            return
        self.diff(frame, f)
        if f in self.stack:
            self.stack.remove(f)
        if unwinding:
            self.w.event(self._t(), K_UNWIND, f.fid, f.prev_loc)
        else:
            self.w.event(self._t(), K_RETURN, f.fid, f.prev_loc,
                         vref=self.enc.encode(retval, self.w.step + 1))

    def raised(self, frame, code, exc):
        f = self.frames.get(id(frame))
        self.w.event(self._t(), K_RAISE,
                     f.fid if f else 0,
                     f.prev_loc if f else self._loc(code, code.co_firstlineno),
                     name=self.w.name_id(type(exc).__name__),
                     payload=self.w.payload_id({"msg": str(exc)[:200]}))

    def diff(self, frame, f):
        """Emit BIND / MUTATE / DELETE for whatever changed since the last line.

        Two mechanisms:

          BIND    the name now points at a different value. Detected with the
                  cheap (id, len) check on the names the static pre-pass says
                  this function can bind.
          MUTATE  the name points at the SAME object but its contents changed
                  -- `a[i] = x`, `self.total += 1`, `xs.append(v)`. Only the
                  names the previous line could statically have mutated get
                  the expensive re-walk, so this stays affordable.
        """
        force = self.mutroots.get(f.code.co_filename, _EMPTY).get(
            f.prev_line, ())
        local_vars = frame.f_locals
        cache = f.cache
        refs = f.refs
        nameids = f.nameids
        event = self.w.event
        note = self.w.note_bind
        loc = f.prev_loc
        fid = f.fid
        t = f.prev_t
        step = self.w.step + 1
        for name in f.names:
            if name not in local_vars:
                if name in cache:
                    del cache[name]
                    refs.pop(name, None)
                    event(t, K_DELETE, fid, loc, name=nameids[name])
                continue
            value = local_vars[name]
            # Fast path: scalars compare by value, containers by (id, len).
            if value is None or type(value) in _SCALARS:
                cheap = value
            else:
                try:
                    cheap = (id(value), len(value))
                except Exception:
                    cheap = (id(value),)
            same = name in cache and cache[name] == cheap
            if same:
                # The name still points at the same thing. Re-walk it only if
                # the previous line could have changed it, or it is small
                # enough that re-walking is free.
                if name not in force:
                    if type(value) not in _MUTABLE:
                        continue
                    try:
                        if len(value) > DEEP_CONTAINER_LIMIT:
                            continue
                    except Exception:
                        continue
            vref = self.enc.encode(value, step, force=name in force)
            if same and not self.enc.last_changed:
                continue
            was = refs.get(name)
            cache[name] = cheap
            refs[name] = vref
            nid = nameids[name]
            event(t, K_MUTATE if _same_object(was, vref) else K_BIND,
                  fid, loc, name=nid, vref=vref)
            note(nid)

        # Names the line could mutate that this function never binds -- a
        # module-level list that a function appends to, for instance.
        for name in force:
            if name in f.names:
                continue
            value = local_vars.get(name, _MISSING)
            if value is _MISSING:
                value = frame.f_globals.get(name, _MISSING)
                if value is _MISSING:
                    continue
            if value is None or type(value) in _SCALARS:
                continue                      # scalars cannot be mutated
            nid = nameids.get(name)
            if nid is None:
                nid = nameids[name] = self.w.name_id(name)
            vref = self.enc.encode(value, step, force=True)
            if not self.enc.last_changed and name in refs:
                continue
            was = refs.get(name)
            refs[name] = vref
            event(t, K_MUTATE if _same_object(was, vref) else K_BIND,
                  fid, loc, name=nid, vref=vref)
            note(nid)

    def write_keyframe(self):
        """A full snapshot, so seeking does not replay from step 1."""
        state = {"frames": [{"frame": f.fid,
                             "func": None,
                             "scope": "global" if f.is_module else "local",
                             "loc": f.prev_loc,
                             "locals": dict(f.refs)}
                            for f in self.stack],
                 "globals": {}}
        self.w.keyframe(self.w.step, state)


# --------------------------------------------------------------------------
# Driver 1: sys.monitoring (Python 3.12+)
# --------------------------------------------------------------------------

class MonitoringRecorder:
    available = sys.version_info >= (3, 12)

    def __init__(self, writer, scope, sampler=None, keep_alive=None):
        self.core = Core(writer, scope, sampler=sampler,
                         keep_alive=keep_alive)
        self.tool_id = sys.monitoring.DEBUGGER_ID

    def start(self):
        mon = sys.monitoring
        E = mon.events
        mon.use_tool_id(self.tool_id, "nd36")
        mon.register_callback(self.tool_id, E.PY_START, self._start)
        mon.register_callback(self.tool_id, E.PY_RETURN, self._return)
        mon.register_callback(self.tool_id, E.PY_YIELD, self._yield)
        mon.register_callback(self.tool_id, E.PY_RESUME, self._resume)
        mon.register_callback(self.tool_id, E.PY_UNWIND, self._unwind)
        mon.register_callback(self.tool_id, E.LINE, self._line)
        mon.register_callback(self.tool_id, E.JUMP, self._jump)
        mon.register_callback(self.tool_id, E.RAISE, self._raise)
        mon.set_events(self.tool_id,
                       E.PY_START | E.PY_RETURN | E.PY_YIELD | E.PY_RESUME
                       | E.PY_UNWIND | E.LINE | E.JUMP | E.RAISE)

    def stop(self):
        mon = sys.monitoring
        mon.set_events(self.tool_id, 0)
        for ev in ("PY_START", "PY_RETURN", "PY_YIELD", "PY_RESUME",
                   "PY_UNWIND", "LINE", "JUMP", "RAISE"):
            mon.register_callback(self.tool_id, getattr(mon.events, ev), None)
        mon.free_tool_id(self.tool_id)

    # The monitored frame is our caller's caller: frame 0 is this callback.
    def _frame(self):
        return sys._getframe(2)

    def _start(self, code, offset):
        if not self.core.in_scope(code):
            return sys.monitoring.DISABLE
        self.core.push(self._frame(), code)

    def _resume(self, code, offset):
        if not self.core.in_scope(code):
            return sys.monitoring.DISABLE
        frame = self._frame()
        if id(frame) not in self.core.frames:
            self.core.push(frame, code)

    def _line(self, code, lineno):
        if not self.core.in_scope(code):
            return sys.monitoring.DISABLE
        self.core.line(self._frame(), code, lineno)

    def _jump(self, code, offset, dest):
        """A backward jump that lands on the same line is another iteration
        of that line, and LINE will not fire for it. Emit it ourselves."""
        if not self.core.in_scope(code):
            return sys.monitoring.DISABLE
        if dest >= offset:
            return                       # forward jump: LINE will cover it
        # The jump's OWN line comes from the line table, not frame.f_lineno:
        # on Python 3.12 f_lineno already reports the destination during a
        # JUMP callback, so every `for` header was recorded twice (1,2,2,3...
        # where settrace says 1,2,3...). ndr found this by disagreeing.
        lines = self.core.line_of_offset(code)
        here = lines.get(offset)
        if here is not None and lines.get(dest) == here:
            self.core.line(self._frame(), code, here)

    def _return(self, code, offset, retval):
        if not self.core.in_scope(code):
            return sys.monitoring.DISABLE
        self.core.pop(self._frame(), retval)

    def _yield(self, code, offset, retval):
        if not self.core.in_scope(code):
            return sys.monitoring.DISABLE
        frame = self._frame()
        f = self.core.frames.get(id(frame))
        if f is not None:
            self.core.diff(frame, f)
            self.core.w.event(self.core._t(), K_YIELD, f.fid, f.prev_loc,
                              vref=self.core.enc.encode(
                                  retval, self.core.w.step + 1))

    # PY_UNWIND and RAISE are not "local events" in PEP 669, so returning
    # DISABLE from them raises ValueError and the callback is torn down.
    # They just return.
    def _unwind(self, code, offset, exc):
        if self.core.in_scope(code):
            self.core.pop(self._frame(), None, unwinding=True)

    def _raise(self, code, offset, exc):
        if self.core.in_scope(code):
            self.core.raised(self._frame(), code, exc)


# --------------------------------------------------------------------------
# Driver 2: sys.settrace (the slow reference implementation)
# --------------------------------------------------------------------------

class SettraceRecorder:
    available = True

    def __init__(self, writer, scope, sampler=None, keep_alive=None):
        self.core = Core(writer, scope, sampler=sampler,
                         keep_alive=keep_alive)

    def start(self):
        sys.settrace(self._global)

    def stop(self):
        sys.settrace(None)

    def _global(self, frame, event, arg):
        """Called once per new frame. Returning a function opts that frame in."""
        if not self.core.in_scope(frame.f_code):
            return None
        self.core.push(frame, frame.f_code)
        return self._local

    def _local(self, frame, event, arg):
        code = frame.f_code
        if event == "line":
            self.core.line(frame, code, frame.f_lineno)
        elif event == "return":
            self.core.pop(frame, arg)
        elif event == "exception":
            self.core.raised(frame, code, arg[1])
        return self._local


def pick_recorder(writer, scope, force=None, sampler=None, keep_alive=None):
    if force == "settrace" or not MonitoringRecorder.available:
        return SettraceRecorder(writer, scope, sampler=sampler,
                                keep_alive=keep_alive)
    return MonitoringRecorder(writer, scope, sampler=sampler,
                              keep_alive=keep_alive)
