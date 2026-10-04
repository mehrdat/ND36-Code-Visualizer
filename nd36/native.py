"""
C and C++: compile it, step it, write the SAME trace.

This is the payoff of the schema-first discipline from Phase 0. Nothing
below invents a format. It produces LINE / CALL / RETURN / BIND / MUTATE
events exactly as the Python recorder does, so `nd step`, `nd watch`,
`nd heat`, `nd flow` and the whole dashboard work on a C program without
one line of change.

Two backends, one row format:

  LLDB   the macOS path, and the plan's primary. Driven through the `lldb`
         binary's own interpreter (see native_probe.py for why).
  GDB    the Linux path. GDB/MI over a pipe -- plain text, no bindings, no
         version pinning. The most portable thing in this file.

Both are real debuggers reading real DWARF, so a variable's value is what
the program actually holds, not a reconstruction.

Known limits, stated rather than discovered:
  * Stepping costs about a millisecond per source line. Algorithm-sized
    programs are comfortable; a million-line trace is not.
  * -O0 is required. At -O1 and above the compiler reorders and elides, so
    line numbers and variables stop corresponding to the source. We check
    and refuse rather than showing you a lie.
  * A variable is in scope from the opening brace, so before its first
    assignment it holds whatever was on the stack. That garbage is real --
    it is what C does -- and it is shown as-is.
"""

import json
import os
import re
import shutil
import subprocess
import time

from . import schema
from .store import TraceWriter

PROBE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "native_probe.py")

C_EXT = {".c"}
CXX_EXT = {".cc", ".cpp", ".cxx", ".C", ".c++"}
SOURCE_EXT = C_EXT | CXX_EXT

#: Optimisation flags that make line/variable mapping untrustworthy.
_BAD_OPT = re.compile(r"-O[123sgzfast]")


def have(tool):
    return shutil.which(tool) is not None


def pick_backend(force=None):
    """lldb first on macOS, gdb first elsewhere; whichever exists wins."""
    if force:
        return force if have(force) else None
    import sys
    order = ("lldb", "gdb") if sys.platform == "darwin" else ("gdb", "lldb")
    for tool in order:
        if have(tool):
            return tool
    return None


# --------------------------------------------------------------------------
# compile
# --------------------------------------------------------------------------

def compile_source(path, out=None, extra_flags=(), std=None):
    """Compile one file with debug info and no optimisation.

    Returns (binary_path, compiler, argv). Raises RuntimeError with the
    compiler's own message, which is more useful than anything we'd write.
    """
    path = os.path.abspath(path)
    ext = os.path.splitext(path)[1]
    cxx = ext in CXX_EXT
    compiler = None
    for candidate in (("clang++", "g++") if cxx else ("clang", "gcc")):
        if have(candidate):
            compiler = candidate
            break
    if compiler is None:
        raise RuntimeError("no C%s compiler found (looked for %s)"
                           % ("++" if cxx else "",
                              "clang++/g++" if cxx else "clang/gcc"))

    out = out or os.path.join(os.path.dirname(path),
                              os.path.splitext(os.path.basename(path))[0])
    bad = [f for f in extra_flags if _BAD_OPT.match(f)]
    if bad:
        raise RuntimeError(
            "refusing to trace an optimised build (%s). At -O1 and above the "
            "compiler reorders and removes code, so line numbers and "
            "variables no longer match the source. Build with -O0."
            % " ".join(bad))

    argv = [compiler, "-g3", "-O0", "-fno-omit-frame-pointer"]
    if std:
        argv.append("-std=%s" % std)
    argv += list(extra_flags) + [path, "-o", out]
    done = subprocess.run(argv, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError("compile failed:\n%s" % (done.stderr.strip()
                                                    or done.stdout.strip()))
    return out, compiler, argv


# --------------------------------------------------------------------------
# backends -- each yields the same row dicts
# --------------------------------------------------------------------------

def _lldb_rows(binary, scope, argv, max_steps, timeout):
    env = dict(os.environ)
    env.update({"VIZ_BINARY": binary,
                "VIZ_SCOPE": json.dumps(sorted(scope)),
                "VIZ_ARGV": json.dumps(list(argv or [])),
                "VIZ_MAX_STEPS": str(max_steps)})
    proc = subprocess.Popen(
        ["lldb", "--batch", "--no-lldbinit",
         "-o", "script exec(open(%r).read())" % PROBE, "-o", "quit"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        for raw in proc.stdout:
            if raw.startswith("@VZ "):
                yield json.loads(raw[4:])
    finally:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()


# -- GDB machine interface --------------------------------------------------

_MI_TOKEN = re.compile(r'([A-Za-z_-]+)=')


def _mi_parse(text):
    """Parse one GDB/MI value into Python.

    MI is nearly JSON: `key=value`, `{...}` for tuples, `[...]` for lists,
    `"..."` for strings -- but keys are bare and C escapes appear inside
    strings. Small enough to parse by hand, and that avoids a dependency.
    """
    pos = [0]

    def skip():
        while pos[0] < len(text) and text[pos[0]] in ", ":
            pos[0] += 1

    def value():
        skip()
        if pos[0] >= len(text):
            return None
        ch = text[pos[0]]
        if ch == '"':
            pos[0] += 1
            out = []
            while pos[0] < len(text):
                c = text[pos[0]]
                if c == "\\":
                    nxt = text[pos[0] + 1]
                    out.append({"n": "\n", "t": "\t", '"': '"',
                                "\\": "\\"}.get(nxt, nxt))
                    pos[0] += 2
                    continue
                if c == '"':
                    pos[0] += 1
                    return "".join(out)
                out.append(c)
                pos[0] += 1
            return "".join(out)
        if ch == "{":
            pos[0] += 1
            out = {}
            while pos[0] < len(text) and text[pos[0]] != "}":
                skip()
                if text[pos[0]] == "}":
                    break
                key = name()
                if key is None:
                    value()
                    continue
                out[key] = value()
                skip()
            pos[0] += 1
            return out
        if ch == "[":
            pos[0] += 1
            out = []
            while pos[0] < len(text) and text[pos[0]] != "]":
                skip()
                if text[pos[0]] == "]":
                    break
                start = pos[0]
                key = name()
                if key is None:
                    pos[0] = start
                    out.append(value())
                else:
                    out.append({key: value()})
                skip()
            pos[0] += 1
            return out
        start = pos[0]
        while pos[0] < len(text) and text[pos[0]] not in ",}]":
            pos[0] += 1
        return text[start:pos[0]]

    def name():
        start = pos[0]
        while pos[0] < len(text) and (text[pos[0]].isalnum()
                                      or text[pos[0]] in "_-"):
            pos[0] += 1
        if pos[0] < len(text) and text[pos[0]] == "=" and pos[0] > start:
            key = text[start:pos[0]]
            pos[0] += 1
            return key
        pos[0] = start
        return None

    out = {}
    while pos[0] < len(text):
        skip()
        key = name()
        if key is None:
            break
        out[key] = value()
    return out


class _Gdb:
    """A very small GDB/MI client.

    The one thing to get right: for an execution command (`-exec-step`,
    `-exec-run`) the result record is `^running`, NOT `^done`. The stop is
    reported later, asynchronously, as `*stopped`. Waiting for `^done` after
    a step therefore blocks forever -- which it duly did, the first time.
    So: read to the result record, then, for exec commands, read on to the
    next `*stopped`.
    """

    RESULTS = ("done", "running", "error", "exit", "connected")

    def __init__(self, binary, timeout=120):
        self.proc = subprocess.Popen(
            ["gdb", "--interpreter=mi3", "-q", "--nx", binary],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.stop = None
        self.exited = False
        self.deadline = time.time() + timeout
        self._read_banner()

    def _line(self):
        if time.time() > self.deadline:
            self.exited = True
            return None
        line = self.proc.stdout.readline()
        if not line:
            self.exited = True
            return None
        # The MI prompt is "(gdb) " -- with a trailing space. Comparing
        # against "(gdb)" after stripping only the newline matches nothing
        # and blocks forever waiting for a prompt that already arrived.
        return line.rstrip()

    def _note(self, line):
        if line.startswith("*stopped"):
            _, _, rest = line.partition(",")
            self.stop = _mi_parse(rest) if rest else {}
            if str(self.stop.get("reason", "")).startswith("exited"):
                self.exited = True
            return True
        return False

    def _read_banner(self):
        """Startup emits `=thread-group-added` and the `(gdb)` prompt, and no
        result record at all -- so waiting for one here hangs forever. Read
        to the first prompt instead."""
        while True:
            line = self._line()
            if line is None or line == "(gdb)":
                return

    def _read_until_result(self):
        out = []
        while True:
            line = self._line()
            if line is None:
                return out
            if self._note(line):
                continue
            if line.startswith("^"):
                head, _, rest = line.partition(",")
                status = head[1:]
                out.append((status, _mi_parse(rest) if rest else {}))
                if status in self.RESULTS:
                    return out

    def _read_until_stop(self):
        while not self.exited:
            line = self._line()
            if line is None:
                return
            if self._note(line):
                return

    def cmd(self, text, wait_stop=False):
        if self.exited:
            return []
        try:
            self.proc.stdin.write(text + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            self.exited = True
            return []
        out = self._read_until_result()
        if wait_stop and not self.exited:
            if any(status == "running" for status, _ in out):
                self._read_until_stop()
        return out

    def ok(self, out):
        return any(status == "done" or status == "running"
                   for status, _ in out)

    def close(self):
        try:
            self.proc.stdin.write("-gdb-exit\n")
            self.proc.stdin.flush()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


#: A bare address, which is all GDB prints for a pointer.
_PTR = re.compile(r"^0x[0-9a-fA-F]+$")


def _evaluate(gdb, expr):
    """`-data-evaluate-expression` guarded: a bad dereference is normal."""
    for status, body in gdb.cmd("-data-evaluate-expression \"%s\"" % expr):
        if status == "done" and body.get("value"):
            return str(body["value"])[:300]
    return None


def _functions_in(gdb, scope):
    """Every function the debugger has source for, in our files."""
    names = []
    for status, body in gdb.cmd("-symbol-info-functions"):
        symbols = (body.get("symbols") or {})
        for group in symbols.get("debug") or []:
            path = group.get("fullname") or group.get("filename") or ""
            if path not in scope:
                continue
            for sym in group.get("symbols") or []:
                if sym.get("name"):
                    names.append(sym["name"])
    return names


def _gdb_rows(binary, scope, argv, max_steps, timeout):
    gdb = _Gdb(binary, timeout=timeout)
    try:
        for setting in ("mi-async off", "confirm off", "print pretty off",
                        "print frame-arguments all", "backtrace limit 256"):
            gdb.cmd("-gdb-set " + setting)
        # Step OVER, with a breakpoint on each of the user's own functions.
        #
        # `-exec-step` descends into anything that has line info, and on this
        # machine glibc has line info -- so one malloc() walked us through
        # ptmalloc_init, tcache_key_initialize and getrandom, and the frames
        # the program actually cared about never came back. Stepping over
        # and letting breakpoints catch our own functions enters exactly the
        # code we have source for, and nothing else. It is also far faster.
        for name in _functions_in(gdb, scope):
            gdb.cmd("-break-insert %s" % name)
        if not gdb.cmd("-break-insert main"):
            pass
        if argv:
            gdb.cmd("-exec-arguments " + " ".join(str(a) for a in argv))
        gdb.cmd("-exec-run", wait_stop=True)

        t0 = time.perf_counter_ns()
        steps = 0
        seen_ptr, ptr_cache = {}, {}
        while not gdb.exited and steps < max_steps:
            frame = (gdb.stop or {}).get("frame") or {}
            path = frame.get("fullname") or frame.get("file") or ""
            if path not in scope:
                # Library code. Leave the frame rather than stepping through
                # thousands of lines of libc.
                out = gdb.cmd("-exec-finish", wait_stop=True)
                if not gdb.ok(out):
                    gdb.cmd("-exec-continue", wait_stop=True)
                continue

            depth = 1
            for status, body in gdb.cmd("-stack-list-frames"):
                if isinstance(body.get("stack"), list):
                    depth = len(body["stack"])

            variables = {}
            for status, body in gdb.cmd("-stack-list-variables --all-values"):
                for item in body.get("variables") or []:
                    if isinstance(item, dict) and "name" in item:
                        variables[item["name"]] = [item.get("value", ""), ""]

            # A pointer prints as a bare address, which tells you nothing
            # about the list you are walking. Follow it one level -- but only
            # when it has changed, because each follow is another round trip
            # to the debugger.
            for name, pair in variables.items():
                text = pair[0]
                if not _PTR.match(text) or text in ("0x0", "0"):
                    continue
                if seen_ptr.get(name) == text:
                    pair[0] = "%s -> %s" % (text, ptr_cache[name])
                    continue
                target = _evaluate(gdb, "*" + name)
                if target:
                    seen_ptr[name] = text
                    ptr_cache[name] = target
                    pair[0] = "%s -> %s" % (text, target)

            yield {"k": "line", "t": time.perf_counter_ns() - t0,
                   "file": path, "line": int(frame.get("line") or 0),
                   "func": frame.get("func") or "?", "depth": depth,
                   # A breakpoint hit means a function was just ENTERED.
                   # Depth alone cannot tell: `for (...) { head = push(...); }`
                   # is one line, so four calls to push happen without the
                   # stack ever being observed back at the caller, and all
                   # four would collapse into a single frame.
                   "entered": (gdb.stop or {}).get("reason")
                   == "breakpoint-hit",
                   "vars": variables}
            steps += 1
            gdb.cmd("-exec-next", wait_stop=True)
        yield {"k": "done", "steps": steps, "exit": 0,
               "stop": "step limit reached" if steps >= max_steps else ""}
    finally:
        gdb.close()


# --------------------------------------------------------------------------
# rows -> the trace schema
# --------------------------------------------------------------------------

class _NativeFrame:
    __slots__ = ("fid", "func", "depth", "loc", "last", "refs", "nameids")

    def __init__(self, fid, func, depth):
        self.fid = fid
        self.func = func
        self.depth = depth
        self.loc = None
        self.last = {}      # name -> last rendered text, to spot changes
        self.refs = {}      # name -> last vref, so keyframes carry state
        self.nameids = {}


def trace_binary(binary, sources, out_dir, argv=(), backend=None,
                 max_steps=50000, timeout=120, keyframe_every=500,
                 meta=None):
    """Drive the debugger and write a normal .vizt trace."""
    backend = backend or pick_backend()
    if backend is None:
        raise RuntimeError("no debugger found: install lldb (macOS: it ships "
                           "with Xcode) or gdb")
    scope = {os.path.abspath(s) for s in sources}
    rows = (_lldb_rows if backend == "lldb" else _gdb_rows)(
        binary, scope, argv, max_steps, timeout)

    writer = TraceWriter(out_dir, keyframe_every=keyframe_every)
    for key, value in (meta or {}).items():
        writer.set_meta(key, value)
    writer.set_meta("language", "c/c++")
    writer.set_meta("backend", backend)
    writer.set_meta("binary", binary)
    writer.set_meta("script", os.path.abspath(sources[0]) if sources else "")
    writer.set_meta("sources", [os.path.abspath(s) for s in sources])
    writer.set_meta("argv", list(argv or []))

    stack = []
    next_fid = 1
    wall0 = time.perf_counter()
    done = {}
    steps = 0
    last_t = 0

    for row in rows:
        if row.get("k") == "fatal":
            raise RuntimeError(row.get("msg", "debugger failed"))
        if row.get("k") == "done":
            done = row
            continue
        if row.get("k") != "line":
            continue

        steps += 1
        t_ns = row.get("t", 0)
        loc = writer.loc_id(row["file"], row["line"])
        depth = row.get("depth", 1)
        func = row.get("func", "?")
        entered = bool(row.get("entered"))

        # The debugger reports stack depth, not call events. A deeper stack
        # than last time is a call; a shallower one is a return. That is the
        # whole bridge between a stepping debugger and an event log.
        while stack and stack[-1].depth > depth:
            gone = stack.pop()
            writer.event(t_ns, schema.KIND_CODE[schema.RETURN], gone.fid,
                         gone.loc or loc)
        if (not stack or stack[-1].depth < depth
                or stack[-1].func != func or entered):
            if stack and stack[-1].depth == depth:
                gone = stack.pop()
                writer.event(t_ns, schema.KIND_CODE[schema.RETURN], gone.fid,
                             gone.loc or loc)
            frame = _NativeFrame(next_fid, func, depth)
            next_fid += 1
            stack.append(frame)
            fid = writer.func_id(func, func, row["file"], row["line"])
            writer.event(t_ns, schema.KIND_CODE[schema.CALL], frame.fid, loc,
                         payload=writer.payload_id(
                             {"func": fid, "args": {}, "scope": "local"}))
        frame = stack[-1]
        writer.note_line(loc, frame.loc, max(0, t_ns - last_t))
        frame.loc = loc
        last_t = t_ns

        writer.event(t_ns, schema.KIND_CODE[schema.LINE], frame.fid, loc)

        for name, (text, ctype) in (row.get("vars") or {}).items():
            was = frame.last.get(name)
            if was == text:
                continue
            nid = frame.nameids.get(name)
            if nid is None:
                nid = frame.nameids[name] = writer.name_id(name)
            obj = _native_object(writer, frame.fid, name, text, ctype,
                                 writer.step + 1)
            kind = schema.MUTATE if was is not None else schema.BIND
            frame.last[name] = text
            frame.refs[name] = obj
            writer.event(t_ns, schema.KIND_CODE[kind], frame.fid, loc,
                         name=nid, vref=obj)
            writer.note_bind(nid)

        if writer.should_keyframe():
            _keyframe(writer, stack)

    for frame in reversed(stack):
        writer.event(0, schema.KIND_CODE[schema.RETURN], frame.fid,
                     frame.loc or 0)
    _keyframe(writer, stack)

    wall = time.perf_counter() - wall0
    writer.set_meta("driver", "%sRecorder" % backend.upper())
    writer.set_meta("wall_seconds", round(wall, 4))
    writer.set_meta("n_steps", writer.step)
    writer.set_meta("exit_status", done.get("exit"))
    writer.set_meta("stop_reason", done.get("stop") or None)
    writer.set_meta("error", done.get("stop") or None)
    writer.set_meta("truncated", steps >= max_steps)
    writer.close()
    return {"steps": writer.step, "lines": steps, "seconds": wall,
            "out": out_dir, "backend": backend,
            "truncated": steps >= max_steps,
            "exit": done.get("exit")}


#: C values arrive already rendered (`{a = 1, b = 2}`, `0x55f... -> {…}`).
#: Numbers stay numbers so the charts and `watch` still work on them.
_INT = re.compile(r"^-?\d+$")
_FLOAT = re.compile(r"^-?\d+\.\d+(e[-+]?\d+)?$", re.I)


def _native_object(writer, frame_id, name, text, ctype, step):
    if text is None:
        return schema.ABSENT
    if _INT.match(text):
        return (schema.RK_INT, int(text), 0.0)
    if _FLOAT.match(text):
        return (schema.RK_FLOAT, 0, float(text))
    if len(text) <= 60 and not text.startswith(("{", "0x")):
        return (schema.RK_STR, writer.string_id(text), 0.0)
    # An aggregate or a pointer: give it an object id so the UI can show it
    # the same way it shows a Python container.
    obj_id = writer.native_object_id(frame_id, name)
    writer.object_version(obj_id, ctype or "value", step,
                          {"type": ctype or "value", "kind": "native",
                           "c_type": ctype or ""},
                          None, text[:400], None)
    return (schema.RK_OBJ, obj_id, 0.0)


def _keyframe(writer, stack):
    # Carry each frame's current values, or state_at() would only know what
    # changed since the keyframe and every variable would vanish on a seek.
    writer.keyframe(writer.step, {
        "frames": [{"frame": f.fid, "func": None, "scope": "local",
                    "loc": f.loc, "locals": dict(f.refs)} for f in stack],
        "globals": {}})
