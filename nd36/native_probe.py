"""
The LLDB probe. This file does NOT run under your Python.

It runs inside the `lldb` binary's own embedded interpreter, launched as

    lldb --batch -o "script exec(open(this_file).read())"

because the lldb Python bindings are compiled against one exact CPython
version. On this machine they are built for 3.12 while the project runs on
3.13, and importing them from outside fails. Running them inside lldb always
works, on Linux and on macOS alike, and it is the same path Xcode's lldb
takes. The cost is a subprocess and a line-delimited JSON protocol, which is
a good trade for never caring about the host's Python version again.

Everything it prints is prefixed `@VZ ` so lldb's own chatter can be ignored.
"""

import json
import os
import time

MAX_CHILDREN = 24
MAX_DEPTH = 3
MAX_STEPS = 200000


def emit(kind, **row):
    row["k"] = kind
    print("@VZ " + json.dumps(row, default=str), flush=True)


def render(value, depth=0):
    """An SBValue as a short string, plus children for aggregates.

    Mirrors how GDB/MI already renders values (`{a = 1, b = 2}`), so both
    backends produce the same text and the UI never has to care which
    debugger was used.
    """
    try:
        if value.GetValue() is not None and value.GetNumChildren() == 0:
            return value.GetValue()
        summary = value.GetSummary()
        if summary:
            return summary
        n = value.GetNumChildren()
        if n and depth < MAX_DEPTH:
            parts = []
            for i in range(min(n, MAX_CHILDREN)):
                child = value.GetChildAtIndex(i)
                name = child.GetName()
                text = render(child, depth + 1)
                parts.append("%s = %s" % (name, text) if name else str(text))
            if n > MAX_CHILDREN:
                parts.append("…")
            return "{" + ", ".join(parts) + "}"
        got = value.GetValue()
        if got is None:
            return "<opaque>"
        # A pointer: show where it points and, one level deep, what is there.
        if value.TypeIsPointerType() and int(value.GetValueAsUnsigned(0)):
            target = value.Dereference()
            if target.IsValid() and depth < MAX_DEPTH:
                return "%s -> %s" % (got, render(target, depth + 1))
        return got
    except Exception as exc:
        return "<error: %s>" % type(exc).__name__


def frame_vars(frame):
    out = {}
    try:
        values = frame.GetVariables(True, True, False, True)
    except Exception:
        return out
    for value in values:
        name = value.GetName()
        if not name:
            continue
        out[name] = [render(value), value.GetTypeName() or ""]
    return out


def main():
    import lldb

    binary = os.environ["VIZ_BINARY"]
    scope = set(json.loads(os.environ.get("VIZ_SCOPE", "[]")))
    argv = json.loads(os.environ.get("VIZ_ARGV", "[]"))
    max_steps = int(os.environ.get("VIZ_MAX_STEPS", MAX_STEPS))

    dbg = lldb.debugger
    dbg.SetAsync(False)
    target = dbg.CreateTarget(binary)
    if not target.IsValid():
        emit("fatal", msg="could not load %s" % binary)
        return

    # A breakpoint on every function we have source for, so stepping OVER
    # still enters the user's own code. `StepInto` descends into anything
    # with line info, and glibc has line info -- one malloc() and you are
    # thirty frames deep in ptmalloc. See the note in native.py.
    target.BreakpointCreateByName("main")
    for unit_index in range(target.GetNumModules()):
        module = target.GetModuleAtIndex(unit_index)
        for sym in module:
            if sym.GetType() != lldb.eSymbolTypeCode:
                continue
            addr = sym.GetStartAddress()
            if not addr.IsValid():
                continue
            entry = addr.GetLineEntry()
            path = entry.GetFileSpec().fullpath if entry.IsValid() else None
            if path and path in scope and sym.GetName():
                target.BreakpointCreateByName(sym.GetName())
    process = target.LaunchSimple(argv or None, None, os.path.dirname(binary))
    if not process or not process.IsValid():
        emit("fatal", msg="could not launch %s" % binary)
        return

    t0 = time.perf_counter_ns()
    thread = process.GetSelectedThread()
    steps = 0
    while process.GetState() == lldb.eStateStopped and steps < max_steps:
        frame = thread.GetFrameAtIndex(0)
        entry = frame.GetLineEntry()
        path = entry.GetFileSpec().fullpath or ""
        line = entry.GetLine()

        if path not in scope:
            # Library code. Run to the end of it rather than stepping
            # through thousands of lines of libc.
            if thread.GetNumFrames() <= 1:
                break
            thread.StepOut()
            continue

        emit("line", t=time.perf_counter_ns() - t0, file=path, line=line,
             func=frame.GetFunctionName() or "?",
             depth=thread.GetNumFrames(),
             # See the note in native.py: a breakpoint hit is the only
             # reliable sign that a function was entered, because a one-line
             # loop can call it repeatedly without the stack ever being seen
             # back at the caller.
             entered=thread.GetStopReason() == lldb.eStopReasonBreakpoint,
             vars=frame_vars(frame))
        steps += 1
        thread.StepOver()

    stop = thread.GetStopDescription(256) if thread.IsValid() else ""
    emit("done", steps=steps, exit=process.GetExitStatus(),
         stop=stop or "")
    process.Kill()


main()
