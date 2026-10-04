"""
Export a trace to the Chrome Trace Event format, which Perfetto reads.

Why bother when we have our own report? Because Perfetto is a very good
timeline viewer that already exists -- zoom, pan, flame graph, slice
selection, SQL over the trace -- and re-implementing it would be months.
VizTracer took the same route. Our report answers "what were the variables";
Perfetto answers "where did the time go", at any zoom level.

Open the output at https://ui.perfetto.dev (the file never leaves your
machine -- the UI is a local web app).

Format: one JSON array of events.
  ph "B"/"E"  a slice beginning and ending   -> the call stack over time
  ph "C"      a counter series               -> the memory curve
  ph "i"      an instant                     -> exceptions
`ts` and `dur` are MICROSECONDS. Getting that wrong gives a trace that
looks right and is off by 1000x, so it is converted in exactly one place.
"""

import json

from . import schema


def _us(ns):
    """ns -> us. Perfetto's unit, and the one easy mistake in this format."""
    return ns / 1000.0


def to_chrome_trace(reader, pid=1, tid=1):
    out = []
    K = schema.KIND_CODE
    k_call, k_ret = K[schema.CALL], K[schema.RETURN]
    k_unwind, k_raise = K[schema.UNWIND], K[schema.RAISE]

    out.append({"ph": "M", "pid": pid, "tid": tid, "name": "process_name",
                "args": {"name": reader.meta.get("script", "trace")}})
    out.append({"ph": "M", "pid": pid, "tid": tid, "name": "thread_name",
                "args": {"name": "python"}})

    depth = 0
    for step in range(1, reader.n_steps + 1):
        t, k, fr, loc, _nm, _rk, _ri, _rf, _pid = reader.raw(step)
        if k == k_call:
            view = reader.view(step)
            fid = (view["extra"] or {}).get("func")
            fn = reader.funcs.get(fid, {})
            path, line = reader.where(loc)
            out.append({"ph": "B", "ts": _us(t), "pid": pid, "tid": tid,
                        "name": fn.get("qualname") or fn.get("name") or "?",
                        "args": {"step": step, "where": "%s:%s"
                                 % (path, line)}})
            depth += 1
        elif k == k_ret or k == k_unwind:
            if depth:
                out.append({"ph": "E", "ts": _us(t), "pid": pid, "tid": tid})
                depth -= 1
        elif k == k_raise:
            view = reader.view(step)
            path, line = reader.where(loc)
            out.append({"ph": "i", "ts": _us(t), "pid": pid, "tid": tid,
                        "s": "t", "name": "raise %s" % (view["name"] or ""),
                        "args": {"step": step, "where": "%s:%s"
                                 % (path, line)}})

    # Close anything still open, or Perfetto shows one slice running forever.
    last = _us(reader.raw(reader.n_steps)[0]) if reader.n_steps else 0
    for _ in range(depth):
        out.append({"ph": "E", "ts": last, "pid": pid, "tid": tid})

    # Counter tracks: the memory curve, drawn under the flame graph.
    for sample in reader.samples():
        args = {"allocated blocks": sample["blocks"]}
        if sample["cur"]:
            args["python heap KB"] = sample["cur"] // 1024
        out.append({"ph": "C", "ts": _us(sample["t_ns"]), "pid": pid,
                    "name": "memory", "args": args})

    # The counter track was appended after every slice, so the stream is out
    # of order. Perfetto tolerates that; a reader walking it in sequence does
    # not. Metadata keeps its place at the front; everything else sorts by
    # time, with B before E at an identical timestamp so zero-length slices
    # do not invert.
    order = {"B": 0, "C": 1, "i": 1, "E": 2}
    meta = [e for e in out if e["ph"] == "M"]
    rest = [e for e in out if e["ph"] != "M"]
    rest.sort(key=lambda e: (e["ts"], order.get(e["ph"], 1)))
    return meta + rest


def write(reader, path):
    data = to_chrome_trace(reader)
    with open(path, "w", encoding="utf8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    return len(data)
