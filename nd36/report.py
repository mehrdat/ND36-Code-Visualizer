"""
`nd report trace.nd` -> one self-contained HTML dashboard.

No server, no npm, no CDN. One file you can open, keep, or email.

What is in it:
  * stat tiles                 the shape of the run at a glance
  * step explorer              a slider over the whole run; code on the left
                               with the current line marked, variables on the
                               right with what changed highlighted
  * hot lines                  time and hit count per source line
  * variable timelines         every numeric variable as a line over steps
  * call graph                 who called whom, edge width = call count,
                               grey = never ran
  * control flow graph         per function, with the branches that never ran
                               in grey
  * mutations                  every in-place change, with file and line

Every label says `step 1,204 · sort.py:14`, never a bare step number: a step
index means nothing on its own.
"""

import json
import os

from . import charts, graphs, schema, ui
from .lineage import (frame_lineage, shape_mismatch, summary_line,
                      tensor_lineage)
from .memory import summarise

MAX_EMBED = 20000        # steps embedded for the interactive explorer
MAX_SPARKS = 12          # variable timelines drawn
KEYFRAME_JS = 400        # JS-side replay keyframes, for a smooth slider


def _intern(table, index, value):
    got = index.get(value)
    if got is None:
        got = index[value] = len(table)
        table.append(value)
    return got


def collect(reader, max_embed=MAX_EMBED):
    """Pull the trace into a compact, JSON-safe structure for the page."""
    script = reader.meta.get("script", "")
    src = reader.source(script).splitlines()

    kinds, kind_ix = [], {}
    names, name_ix = [], {}
    vals, val_ix = [], {}
    ev = []

    n = min(reader.n_steps, max_embed)
    for step in range(1, n + 1):
        view = reader.view(step)
        k = _intern(kinds, kind_ix, view["kind"] or "?")
        nm = (_intern(names, name_ix, view["name"])
              if view["name"] else -1)
        if view["ref"] is not None:
            got = reader.resolve(view["ref"], step)
            text = got["repr"] if isinstance(got, dict) else repr(got)
            v = _intern(vals, val_ix, text)
        else:
            v = -1
        extra = view["extra"]
        fn = extra.get("func", -1) if extra else -1
        args = []
        if extra and extra.get("args"):
            for aname, triple in extra["args"].items():
                ai = _intern(names, name_ix, aname)
                ref = reader._norm(triple)
                got = reader.resolve(ref, step)
                text = got["repr"] if isinstance(got, dict) else repr(got)
                args.append([ai, _intern(vals, val_ix, text)])
        ev.append([k, view["frame"], view["line"] or 0, nm, v,
                   fn if fn is not None else -1, args])

    stats = {}
    for (path, line), (hits, t_ns) in reader.line_stats().items():
        if path == script:
            stats[line] = [hits, round(t_ns / 1e6, 3)]

    funcs = {str(fid): (f["qualname"] or f["name"])
             for fid, f in reader.funcs.items()}

    strip = [[r["step"], r["blocks"]] for r in reader.samples()
             if r["step"] <= n]

    return {"strip": strip,
            "script": os.path.basename(script), "path": script,
            "nSteps": reader.n_steps, "embedded": n,
            "src": src, "kinds": kinds, "names": names, "vals": vals,
            "ev": ev, "stats": stats, "funcs": funcs,
            "keyframeEvery": KEYFRAME_JS,
            "meta": {k: reader.meta.get(k) for k in
                     ("python", "platform", "driver", "wall_seconds",
                      "error", "seed", "language", "compiler",
                     "backend")}}


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------

def tiles(reader, data):
    native = reader.meta.get("language") == "c/c++"
    executed = len(data["stats"])
    total = max(1, len([l for l in data["src"] if l.strip()
                        and not l.strip().startswith("#")]))
    counts = reader.kind_counts()
    ms = sum(v[1] for v in data["stats"].values())
    rows = [
        ("steps recorded", "{:,}".format(data["nSteps"]), "events in the log"),
        ("lines executed", "%d / %d" % (executed, total),
         "%.0f%% of the file ran" % (100.0 * executed / total)),
        ("functions", "%d" % len(reader.funcs), "activations recorded: "
         "{:,}".format(counts.get(schema.CALL, 0))),
        ("in-place changes", "{:,}".format(counts.get(schema.MUTATE, 0)),
         "n->next = head, p.x = 3" if native
         else "a[i] = x, self.x += 1, xs.append(v)"),
        ("variable writes", "{:,}".format(counts.get(schema.BIND, 0)),
         "a name taking a new value"),
        ("traced time", "%.0f ms" % ms,
         "debugger time inside your code" if native
         else "wall clock inside the traced file"),
    ]
    out = ['<div class="tiles">']
    for label, value, sub in rows:
        out.append('<div class="tile"><div class="tl">%s</div>'
                   '<div class="tv">%s</div><div class="ts">%s</div></div>'
                   % (charts.esc(label), charts.esc(value), charts.esc(sub)))
    out.append("</div>")
    return "".join(out)


def hot_lines(data, limit=14):
    rows = []
    for line, (hits, ms) in data["stats"].items():
        text = (data["src"][line - 1].strip()
                if line - 1 < len(data["src"]) else "")
        rows.append((("%s:%d  %s" % (data["script"], line, text))[:46],
                     ms,
                     "%s:%d · %s hits · %.2f ms\n%s"
                     % (data["script"], line, "{:,}".format(hits), ms,
                        text)))
    rows.sort(key=lambda r: -r[1])
    return charts.bars(rows[:limit], unit=" ms", fmt="%.2f")


def source_heat(data):
    """The source, annotated. The single most useful static view."""
    peak = max((v[0] for v in data["stats"].values()), default=1)
    peak_t = max((v[1] for v in data["stats"].values()), default=1) or 1
    out = ['<div class="code heat">']
    for i, text in enumerate(data["src"], 1):
        hits, ms = data["stats"].get(i, (0, 0))
        shade = charts.ramp_step((hits / peak) ** 0.5 if hits else 0)
        hot = " hot" if ms > peak_t * 0.4 else ""
        out.append(
            '<div class="row%s"><span class="gut" style="background:%s">'
            '</span><span class="ln">%d</span>'
            '<span class="hits">%s</span><span class="ms">%s</span>'
            '<code>%s</code></div>'
            % (hot, shade, i,
               "{:,}".format(hits) if hits else "",
               ("%.2f" % ms) if ms else "",
               charts.esc(text) or "&nbsp;"))
    out.append("</div>")
    return "".join(out)


def sparklines(data):
    """Every numeric variable, as a line over the run."""
    series = {}
    bind = data["kinds"].index(schema.BIND) if schema.BIND in data["kinds"] \
        else -1
    mut = data["kinds"].index(schema.MUTATE) \
        if schema.MUTATE in data["kinds"] else -1
    for i, row in enumerate(data["ev"]):
        k, _frame, _line, nm, v = row[0], row[1], row[2], row[3], row[4]
        if k not in (bind, mut) or nm < 0 or v < 0:
            continue
        text = data["vals"][v]
        try:
            num = float(text)
        except ValueError:
            continue
        series.setdefault(data["names"][nm], []).append((i + 1, num))
    ranked = sorted(series.items(), key=lambda kv: -len(kv[1]))
    out = ['<div class="sparks">']
    for i, (name, points) in enumerate(ranked[:MAX_SPARKS]):
        if len(points) < 2:
            continue
        out.append(charts.line_chart(points, name, colour=i))
    out.append("</div>")
    if not ranked:
        return '<p class="empty">no numeric variables in this run</p>'
    return "".join(out)


def call_graph_section(reader):
    nodes, edges = graphs.call_graph(reader)
    node_list = sorted(nodes.values(), key=lambda n: n["line"])
    edges = [e for e in edges if e["src"] is not None]
    never = [n for n in node_list if not n["calls"]]
    svg = charts.graph_svg(
        node_list, edges,
        label_of=lambda n: n["name"],
        tip_of=lambda n: "%s  (line %d)\ncalled %s times"
        % (n["name"], n["line"], "{:,}".format(n["calls"])),
        kind_of=lambda n: "called" if n["calls"] else "dead",
        weight_of=True)
    note = ""
    if never:
        note = ('<p class="note">Never called: %s</p>'
                % charts.esc(", ".join(n["name"] for n in never)))
    return '<div class="scroll">%s</div>%s' % (svg, note)


def cfg_section(reader, data):
    path = data["path"]
    if reader.meta.get("language") == "c/c++":
        return ('<p class="empty">Control-flow graphs are built from the '
                'Python AST, so they are not available for C or C++ yet — '
                'that needs clang. Everything else on this page works the '
                'same either way, because it comes from the trace rather '
                'than from parsing: the call graph above is built from the '
                'recorded calls, and the heat map from the recorded '
                'lines.</p>')
    cfgs = graphs.cfgs_for_source(reader.source(path), path)
    if not cfgs:
        return '<p class="empty">could not parse the source</p>'
    tabs, panes = [], []
    for i, g in enumerate(cfgs):
        hits = graphs.line_hits_per_node(reader, path, g)
        ran = sum(1 for n in g.nodes if hits.get(n["id"]))
        special = {n["id"] for n in g.nodes
                   if n["kind"] in ("entry", "exit")}
        edges = []
        for e in g.edges:
            # entry and exit are synthetic and have no source line, so they
            # take the hit count of whatever they connect to -- otherwise
            # every function looked like it never reached its own end.
            a = hits.get(e["src"], 0)
            b = hits.get(e["dst"], 0)
            if e["src"] in special:
                a = b
            if e["dst"] in special:
                b = a
            n = min(a, b)
            edges.append({"src": e["src"], "dst": e["dst"],
                          "label": e["label"], "n": n, "ran": n > 0})
        svg = charts.graph_svg(
            g.nodes, edges,
            label_of=lambda n: n["label"],
            tip_of=lambda n, _h=hits: (
                "%s:%s\n%s hits\n%s"
                % (data["script"],
                   "-".join(str(x) for x in
                            (n["lines"][0][0], n["lines"][-1][0]))
                   if len(n.get("lines", [])) > 1 else n["line"],
                   "{:,}".format(_h.get(n["id"], 0)),
                   "\n".join("%s  %s" % (l, t)
                              for l, t in n.get("lines", [])[:8]))
                if n["line"] else n["label"]),
            kind_of=lambda n, _h=hits: (
                n["kind"] if _h.get(n["id"]) or n["kind"] in ("entry", "exit")
                else "dead"),
            node_w=272, node_h=30, numbered=True)
        tabs.append('<button class="tab%s" data-cfg="%d">%s '
                    '<span class="badge">%d/%d</span></button>'
                    % (" on" if i == 0 else "", i,
                       charts.esc(g.name), ran, len(g.nodes)))
        panes.append('<div class="pane%s" data-cfg="%d">'
                     '<div class="scroll">%s</div></div>'
                     % (" on" if i == 0 else "", i, svg))
    return ('<div class="tabs">%s</div>%s'
            % ("".join(tabs), "".join(panes)))


def mutations_table(reader, limit=60):
    rows = []
    for step in range(1, reader.n_steps + 1):
        if reader.raw(step)[1] != schema.KIND_CODE[schema.MUTATE]:
            continue
        ev = reader.view(step)
        got = reader.resolve(ev["ref"], step)
        rows.append((ev["step"], os.path.basename(ev["file"] or ""),
                     ev["line"], ev["name"],
                     got["repr"] if isinstance(got, dict) else repr(got)))
        if len(rows) >= limit:
            break
    if not rows:
        return '<p class="empty">no in-place changes recorded</p>'
    out = ['<table class="grid"><thead><tr><th>where</th><th>step</th>'
           '<th>name</th><th>became</th></tr></thead><tbody>']
    for step, fname, line, name, value in rows:
        out.append('<tr><td class="where">%s:%d</td><td class="num">%s</td>'
                   '<td class="name">%s</td><td><code>%s</code></td></tr>'
                   % (charts.esc(fname), line, "{:,}".format(step),
                      charts.esc(name), charts.esc(value[:110])))
    out.append("</tbody></table>")
    return "".join(out)


def memory_section(reader):
    curve = reader.memcurve()
    if not curve:
        return ('<p class="empty">This trace has no memory measurements. '
                'Re-run with <code>--memory</code>: the script is run a '
                'second time under tracemalloc, on its own, so the recorder '
                'is not measuring itself.</p>')

    t0 = curve[0]["at_ns"]
    points = [((r["at_ns"] - t0) / 1e6, r["cur"] / 1024.0) for r in curve]
    peak = max(r["peak"] for r in curve)
    out = ['<div class="tiles"><div class="tile"><div class="tl">peak heap'
           '</div><div class="tv">%s</div><div class="ts">exact: tracemalloc '
           'tracks the peak itself</div></div>' % _kb(peak)]
    out.append('<div class="tile"><div class="tl">held at the end</div>'
               '<div class="tv">%s</div><div class="ts">never given back'
               '</div></div>' % _kb(curve[-1]["cur"]))
    out.append('<div class="tile"><div class="tl">samples</div>'
               '<div class="tv">%d</div><div class="ts">over %.0f ms'
               '</div></div></div>' % (len(curve), points[-1][0]))

    out.append('<div class="sparks" style="margin-top:12px">')
    out.append(charts.line_chart(points, "python heap (KB) over ms",
                                 width=640, height=150, colour=0))
    out.append("</div>")

    rows = [r for r in summarise(reader) if r["growth"] > 1024]
    if rows:
        out.append('<h3 class="sub-h">Still held when the program ended</h3>')
        out.append('<p class="lede">Biggest growth between the first and '
                   'last heap snapshot. A line near the top of this list, '
                   'in your own file, is where a leak lives.</p>')
        bars = [("%s:%d" % (os.path.basename(r["path"]), r["line"]),
                 r["growth"] / 1024.0,
                 "%s:%d\nholding %s\ngrew by %s"
                 % (os.path.basename(r["path"]), r["line"],
                    _kb(r["bytes"]), _kb(r["growth"])))
                for r in rows[:10]]
        out.append(charts.bars(bars, unit=" KB", fmt="%.0f"))
    return "".join(out)


def objects_section(reader):
    rows = reader.biggest_objects(12)
    rows = [r for r in rows if r["nbytes"]]
    if not rows:
        return ('<p class="empty">No object reported its own size. '
                'NumPy arrays, DataFrames and tensors do.</p>')
    # The label already starts with the type, so do not say it twice.
    bars = [(r["repr"][:46], r["nbytes"] / 1024.0,
             "%s\n%s\n%s" % (r["type"], _kb(r["nbytes"]), r["repr"]))
            for r in rows]
    return charts.bars(bars, unit=" KB", fmt="%.0f")


def frames_section(reader):
    rows = frame_lineage(reader)
    if not rows:
        return ('<p class="empty">No DataFrames in this run. This panel '
                'follows pandas and polars frames through a pipeline.</p>')
    out = ['<table class="grid"><thead><tr><th>where</th><th>name</th>'
           '<th>shape</th><th>what changed</th><th>rows</th>'
           '</tr></thead><tbody>']
    peak = max((r["shape"][0] if r["shape"] else 0) for r in rows) or 1
    for row in rows:
        where, shape, what = summary_line(row)
        n = row["shape"][0] if row["shape"] else 0
        # A bar per row, so losing 90% of the data is visible without
        # reading a single number.
        bar = ('<span class="rowbar" style="width:%.1f%%"></span>'
               % (100.0 * n / peak))
        out.append('<tr class="%s"><td class="where">%s</td>'
                   '<td class="name">%s</td><td class="num">%s</td>'
                   '<td>%s%s</td><td class="barcell">%s<span class="n">%s'
                   '</span></td></tr>'
                   % ("warn" if row["warn"] else "", charts.esc(where),
                      charts.esc(row["name"] or ""), charts.esc(shape),
                      charts.esc(what),
                      ' <span class="flag">big row loss</span>'
                      if row["warn"] else "",
                      bar, "{:,}".format(n)))
    out.append("</tbody></table>")
    return "".join(out)


def tensors_section(reader):
    rows = tensor_lineage(reader)
    mismatches = shape_mismatch(reader)
    if not rows and not mismatches:
        return ('<p class="empty">No tensors in this run. This panel '
                'follows PyTorch shapes through a forward pass.</p>')
    out = []
    for m in mismatches:
        src = reader.source(m["file"]).splitlines()
        code = src[m["line"] - 1].strip() if m["line"] and \
            m["line"] <= len(src) else ""
        out.append('<div class="err"><b>%s at %s:%d</b><br>%s<br>'
                   '<code>%s</code></div>'
                   % (charts.esc(m["exc"]),
                      os.path.basename(m["file"] or "?"), m["line"],
                      charts.esc(m["msg"][:200]), charts.esc(code)))
        out.append('<p class="lede" style="margin-top:8px">Every tensor '
                   'alive at that moment, newest first:</p>')
        out.append('<table class="grid"><thead><tr><th>name</th>'
                   '<th>shape</th><th>produced by</th></tr></thead><tbody>')
        for t in m["tensors"][:10]:
            out.append('<tr><td class="name">%s</td><td class="name">%s</td>'
                       '<td class="where">%s</td></tr>'
                       % (charts.esc(t["name"] or "?"),
                          charts.esc(str(tuple(t["shape"]))),
                          charts.esc(t["grad_fn"] or "")))
        out.append("</tbody></table>")

    if rows:
        out.append('<h3 class="sub-h">Shapes through the run</h3>')
        out.append('<table class="grid"><thead><tr><th>where</th>'
                   '<th>name</th><th>shape</th><th>dtype</th>'
                   '<th>device</th><th>produced by</th></tr></thead><tbody>')
        for t in rows[:80]:
            changed = t["prev_shape"] and t["prev_shape"] != t["shape"]
            out.append('<tr class="%s"><td class="where">%s:%s</td>'
                       '<td class="name">%s</td><td class="name">%s</td>'
                       '<td class="name">%s</td><td class="where">%s</td>'
                       '<td class="where">%s</td></tr>'
                       % ("changed" if changed else "",
                          charts.esc(os.path.basename(t["file"] or "?")),
                          t["line"], charts.esc(t["name"] or ""),
                          charts.esc(str(tuple(t["shape"]))),
                          charts.esc(t["dtype"] or ""),
                          charts.esc(t["device"] or ""),
                          charts.esc(t["grad_fn"] or "")))
        out.append("</tbody></table>")
    return "".join(out)


def _kb(n):
    if not n:
        return "0"
    if n >= 1 << 20:
        return "%.1f MB" % (n / (1 << 20))
    return "%.0f KB" % (n / 1024)


# --------------------------------------------------------------------------

def _subtitle(data):
    meta = data["meta"]
    bits = [meta.get("driver") or ""]
    if meta.get("language"):
        bits.append(meta["language"])
        if meta.get("compiler"):
            bits.append(meta["compiler"])
    elif meta.get("python"):
        bits.append(meta["python"].split()[0])
    if meta.get("platform"):
        bits.append(meta["platform"])
    return " · ".join(b for b in bits if b)


def render(reader, max_embed=MAX_EMBED):
    data = collect(reader, max_embed)
    blob = json.dumps(data, separators=(",", ":"), default=str)
    truncated = ""
    if data["embedded"] < data["nSteps"]:
        truncated = ('<p class="note">The step explorer covers the first '
                     '%s of %s steps. Use <code>--max-steps</code> for more.'
                     '</p>' % ("{:,}".format(data["embedded"]),
                               "{:,}".format(data["nSteps"])))
    err = ""
    if data["meta"].get("error"):
        err = ('<p class="err">The script raised: <code>%s</code></p>'
               % charts.esc(data["meta"]["error"]))

    return TEMPLATE % {
        "css": ui.CSS,
        "title": charts.esc(data["script"]),
        "subtitle": charts.esc(_subtitle(data)),
        "err": err,
        "tiles": tiles(reader, data),
        "truncated": truncated,
        "hot": hot_lines(data),
        "heat": source_heat(data),
        "sparks": sparklines(data),
        "callgraph": call_graph_section(reader),
        "cfg": cfg_section(reader, data),
        "mutations": mutations_table(reader),
        "memory": memory_section(reader),
        "frames": frames_section(reader),
        "tensors": tensors_section(reader),
        "objects": objects_section(reader),
        "data": blob,
    }


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s — trace report</title>
<style>%(css)s</style></head><body>
<button class="themebtn" id="theme">light / dark</button>
<div class="wrap">
<header>
  <h1>%(title)s</h1>
  <div class="sub">%(subtitle)s</div>
  %(err)s
</header>

%(tiles)s

<section>
  <h2>Step explorer</h2>
  <p class="lede">Drag the slider to move through the run. The left pane is
  your code with the current line marked; the right pane is every variable in
  scope at that moment, with the ones that just changed highlighted.</p>
  <div class="bar-row">
    <button id="first" title="first step">⏮</button>
    <button id="prev" title="back one step">◀</button>
    <button id="play" title="play / pause">▶</button>
    <button id="next" title="forward one step">▶|</button>
    <button id="last" title="last step">⏭</button>
    <input type="range" id="slider" min="1" value="1">
  </div>
  <canvas id="strip" height="38"></canvas>
  <div id="label"></div>
  <div class="split">
    <div id="codepane"></div>
    <div class="vars"><div class="frame-head" id="frames"></div>
      <table class="grid"><tbody id="varbody"></tbody></table></div>
  </div>
  %(truncated)s
</section>

<section>
  <h2>DataFrames</h2>
  <p class="lede">Every step where a frame changed, and what the change was.
  A row is flagged when more than 30%% of the rows disappear in one step —
  the filter that was stricter than you thought.</p>
  %(frames)s
</section>

<section>
  <h2>Tensors</h2>
  <p class="lede">Shapes through the run. If it died on a shape error, the
  message names two matrices and nothing else; this names the line and every
  tensor that was alive at that moment.</p>
  %(tensors)s
</section>

<section>
  <h2>Memory</h2>
  <p class="lede">Measured in a second run under tracemalloc, with nothing
  else attached — running it inside the recorder meant measuring the
  recorder, at fifty times the cost.</p>
  %(memory)s
</section>

<section>
  <h2>Biggest objects</h2>
  <p class="lede">The largest any single object got, by its own reckoning.
  Arrays, DataFrames and tensors report their bytes; plain containers do not.
  </p>
  %(objects)s
</section>

<section>
  <h2>Hot lines</h2>
  <p class="lede">Time spent on each source line, longest first. Hover a bar
  for its hit count.</p>
  %(hot)s
</section>

<section>
  <h2>Variable timelines</h2>
  <p class="lede">Every numeric variable, plotted over the whole run. One
  series per chart, so the title names it and no legend is needed.</p>
  %(sparks)s
</section>

<section>
  <h2>Call graph</h2>
  <p class="lede">Who called whom. Edge thickness is the number of calls;
  dashed grey means the path exists in the code but never ran.</p>
  %(callgraph)s
  <div class="legend">
    <span><i style="border-color:var(--s1);background:color-mix(in oklab,var(--s1) 20%%,transparent)"></i>called</span>
    <span><i style="border-color:var(--dead)"></i>never called</span>
    <span><i style="border-color:var(--s2);background:var(--s2)"></i>recursion / loop edge</span>
  </div>
</section>

<section>
  <h2>Control flow</h2>
  <p class="lede">What the code <em>could</em> do, with what it actually did
  laid over the top. Grey dashed nodes and edges never executed — that is
  where your untested branches are. The badge counts executed nodes.</p>
  %(cfg)s
  <div class="legend">
    <span><i style="border-color:var(--s4)"></i>branch</span>
    <span><i style="border-color:var(--s3)"></i>loop</span>
    <span><i style="border-color:var(--s7)"></i>return / end</span>
    <span><i style="border-color:var(--dead)"></i>never ran</span>
  </div>
</section>

<section>
  <h2>Execution heat</h2>
  <p class="lede">The whole file, with hit count and milliseconds per line.
  The gutter shade is hit count; an orange row spent real time there.</p>
  %(heat)s
</section>

<section>
  <h2>In-place changes</h2>
  <p class="lede"><code>a[i] = x</code>, <code>self.total += 1</code> and
  <code>xs.append(v)</code> change an object without rebinding any name, so a
  name-only tracer misses them entirely. These are the ones caught.</p>
  %(mutations)s
</section>
</div>
<div id="tip"></div>
<script id="trace" type="application/json">%(data)s</script>
<script>
const D = JSON.parse(document.getElementById('trace').textContent);
const KIND = D.kinds, NAMES = D.names, VALS = D.vals, EV = D.ev;
const K = {};
KIND.forEach((k,i)=>K[k]=i);

/* ---- replay ---------------------------------------------------------
   Exactly the same idea as the Python side: the state at any step is the
   events up to it, applied in order. Keyframes every N steps so dragging
   the slider never replays from the start.                             */
const KF = D.keyframeEvery || 400, cache = new Map();

function blank(){ return {frames:[], globals:{}, byId:new Map()}; }

function clone(s){
  const out = {frames:[], globals:Object.assign({}, s.globals),
               byId:new Map()};
  for (const f of s.frames){
    const c = {frame:f.frame, func:f.func, line:f.line, scope:f.scope,
               vars:Object.assign({}, f.vars)};
    out.frames.push(c); out.byId.set(c.frame, c);
  }
  return out;
}

function apply(s, i){
  const [k, frame, line, nm, v, fn, args] = EV[i];
  const kind = KIND[k];
  let f = s.byId.get(frame);
  if (kind === 'CALL'){
    f = {frame:frame, func:fn, line:line, vars:{}};
    if (args) for (const [ai, av] of args) f.vars[NAMES[ai]] = av;
    s.frames.push(f); s.byId.set(frame, f);
  } else if (kind === 'RETURN' || kind === 'UNWIND'){
    const ix = s.frames.indexOf(f);
    if (ix >= 0) s.frames.splice(ix, 1);
    s.byId.delete(frame);
  } else if (kind === 'LINE'){
    if (f) f.line = line;
  } else if (kind === 'BIND' || kind === 'MUTATE'){
    if (f) f.vars[NAMES[nm]] = v; else s.globals[NAMES[nm]] = v;
  } else if (kind === 'DELETE'){
    if (f) delete f.vars[NAMES[nm]]; else delete s.globals[NAMES[nm]];
  }
}

function stateAt(step){
  const base = Math.floor((step-1)/KF)*KF;
  let s;
  if (base === 0) s = blank();
  else if (cache.has(base)) s = clone(cache.get(base));
  else { s = blank(); for (let i=0;i<base;i++) apply(s,i);
         cache.set(base, clone(s)); }
  for (let i=base;i<step;i++) apply(s,i);
  return s;
}

/* ---- render --------------------------------------------------------- */
const N = D.embedded;
const slider = document.getElementById('slider');
slider.max = N;
const codepane = document.getElementById('codepane');
const varbody  = document.getElementById('varbody');
const framesEl = document.getElementById('frames');
const labelEl  = document.getElementById('label');

const rows = D.src.map((text, i) => {
  const st = D.stats[i+1];
  const div = document.createElement('div');
  div.className = 'row';
  div.innerHTML = '<span class="gut"></span><span class="ln">' + (i+1) +
    '</span><code></code>';
  div.querySelector('code').textContent = text || ' ';
  if (st) div.querySelector('.gut').style.background = 'var(--s1)';
  return div;
});
rows.forEach(r => codepane.appendChild(r));

let cur = -1;

/* What was in scope one step earlier. Computed from the log rather than
   remembered from the last render, so the highlight means the same thing
   whether you stepped here or jumped. */
function poolAt(step){
  if (step < 1) return {};
  const s = stateAt(step), pool = {};
  Object.assign(pool, s.globals);
  for (const f of s.frames) Object.assign(pool, f.vars);
  return pool;
}
function show(step){
  step = Math.max(1, Math.min(N, step|0));
  const ev = EV[step-1], kind = KIND[ev[0]], line = ev[2];
  const s = stateAt(step);

  /* Label: a step number alone tells you nothing, so always file:line. */
  let extra = '';
  if (ev[3] >= 0) extra = ' <span class="kind">' + kind + '</span> ' +
                          NAMES[ev[3]];
  else extra = ' <span class="kind">' + kind + '</span>';
  labelEl.innerHTML = 'step <b>' + step.toLocaleString() + '</b> of ' +
    N.toLocaleString() + ' · ' + D.script + ':<b>' + line + '</b>' + extra;

  if (cur >= 0 && rows[EV[cur][2]-1]) rows[EV[cur][2]-1].classList.remove('cur');
  if (rows[line-1]){
    rows[line-1].classList.add('cur');
    const r = rows[line-1];
    if (r.offsetTop < codepane.scrollTop + 20 ||
        r.offsetTop > codepane.scrollTop + codepane.clientHeight - 40)
      codepane.scrollTop = r.offsetTop - codepane.clientHeight/2;
  }
  cur = step-1;

  const stack = s.frames.map(f =>
    (D.funcs[f.func] || '?') + ':' + f.line);
  framesEl.textContent = stack.length ? stack.join('  ►  ')
                                      : '(module level)';

  const pool = {};
  Object.assign(pool, s.globals);
  for (const f of s.frames) Object.assign(pool, f.vars);
  const prevVars = poolAt(step - 1);
  const html = [];
  for (const name of Object.keys(pool).sort()){
    const v = VALS[pool[name]];
    const changed = prevVars[name] !== undefined &&
                    prevVars[name] !== pool[name];
    const fresh = step > 1 && prevVars[name] === undefined;
    const was = changed ? '<div class="was">was ' +
      esc(String(VALS[prevVars[name]]).slice(0,60)) + '</div>' : '';
    html.push('<tr class="' + ((changed||fresh) ? 'changed' : '') +
      '"><td class="name">' + esc(name) + '</td><td><code>' +
      esc(String(v).slice(0,150)) + '</code>' + was + '</td></tr>');
  }
  varbody.innerHTML = html.join('') ||
    '<tr><td class="name" colspan="2">nothing in scope yet</td></tr>';
  if (+slider.value !== step) slider.value = step;
  drawStrip();
}
function esc(t){ return t.replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

/* ---- the memory strip -------------------------------------------------
   Allocated blocks, sampled during the trace, so it is aligned to STEPS and
   a click can jump straight to the line that caused a spike. */
const strip = document.getElementById('strip');
function drawStrip(){
  const pts = D.strip || [];
  // A native trace has no step-aligned memory samples, so there is nothing
  // to draw -- an empty box is worse than no box.
  if (pts.length < 2){ strip.style.display = 'none'; return; }
  strip.style.display = 'block';
  const dpr = devicePixelRatio || 1;
  const w = strip.clientWidth, h = 38;
  strip.width = w * dpr; strip.height = h * dpr;
  const g = strip.getContext('2d');
  g.setTransform(dpr,0,0,dpr,0,0);
  g.clearRect(0,0,w,h);
  if (pts.length < 2) return;
  const css = getComputedStyle(document.body);
  const hi = Math.max(...pts.map(p => p[1])), lo = Math.min(...pts.map(p => p[1]));
  const span = (hi - lo) || 1;
  const x = s => (s - 1) / (N - 1) * w;
  const y = v => h - 4 - (v - lo) / span * (h - 10);
  g.beginPath(); g.moveTo(0, h);
  for (const [st, v] of pts) g.lineTo(x(st), y(v));
  g.lineTo(w, h); g.closePath();
  g.fillStyle = css.getPropertyValue('--s1').trim() + '33'; g.fill();
  g.beginPath();
  pts.forEach(([st,v],i) => i ? g.lineTo(x(st), y(v)) : g.moveTo(x(st), y(v)));
  g.strokeStyle = css.getPropertyValue('--s1').trim();
  g.lineWidth = 1.6; g.stroke();
  const cx = x(+slider.value);
  g.beginPath(); g.moveTo(cx, 0); g.lineTo(cx, h);
  g.strokeStyle = css.getPropertyValue('--s2').trim();
  g.lineWidth = 1.5; g.stroke();
}
strip.addEventListener('click', e => {
  const r = strip.getBoundingClientRect();
  show(Math.round(1 + (e.clientX - r.left) / r.width * (N - 1)));
});
strip.addEventListener('mousemove', e => {
  const r = strip.getBoundingClientRect();
  const st = Math.round(1 + (e.clientX - r.left) / r.width * (N - 1));
  const ev = EV[Math.max(0, Math.min(N-1, st-1))];
  tip.textContent = 'step ' + st.toLocaleString() + '\n' + D.script + ':' +
                    ev[2] + '\nclick to jump here';
  tip.style.display = 'block';
  tip.style.left = Math.min(innerWidth - tip.offsetWidth - 10, e.clientX+12)+'px';
  tip.style.top  = Math.max(8, e.clientY - tip.offsetHeight - 10) + 'px';
});
strip.addEventListener('mouseleave', () => { tip.style.display='none'; });
addEventListener('resize', drawStrip);

slider.addEventListener('input', e => show(+e.target.value));
document.getElementById('prev').onclick  = () => show(+slider.value - 1);
document.getElementById('next').onclick  = () => show(+slider.value + 1);
document.getElementById('first').onclick = () => drawStrip();
show(1);
document.getElementById('last').onclick  = () => show(N);
let timer = null;
document.getElementById('play').onclick = e => {
  if (timer){ clearInterval(timer); timer = null; e.target.textContent = '▶'; }
  else {
    e.target.textContent = '⏸';
    timer = setInterval(() => {
      if (+slider.value >= N){ clearInterval(timer); timer=null;
        e.target.textContent='▶'; return; }
      show(+slider.value + 1);
    }, 110);
  }
};
addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' && e.key !== 'ArrowLeft' &&
      e.key !== 'ArrowRight') return;
  if (e.key === 'ArrowRight') show(+slider.value + (e.shiftKey?25:1));
  if (e.key === 'ArrowLeft')  show(+slider.value - (e.shiftKey?25:1));
});
drawStrip();
show(1);

/* ---- tabs ---------------------------------------------------------- */
document.querySelectorAll('.tab').forEach(btn => btn.onclick = () => {
  const id = btn.dataset.cfg;
  document.querySelectorAll('.tab').forEach(b =>
    b.classList.toggle('on', b === btn));
  document.querySelectorAll('.pane').forEach(p =>
    p.classList.toggle('on', p.dataset.cfg === id));
});

/* ---- hover tooltip, shared by every chart -------------------------- */
const tip = document.getElementById('tip');
function place(e){
  tip.style.left = Math.min(innerWidth - tip.offsetWidth - 10,
                            e.clientX + 12) + 'px';
  tip.style.top  = Math.max(8, e.clientY - tip.offsetHeight - 10) + 'px';
}
document.addEventListener('mousemove', e => {
  const host = e.target.closest('[data-tip]');
  if (host){ tip.textContent = host.dataset.tip; tip.style.display='block';
             place(e); }
  else if (!e.target.closest('.spark')) tip.style.display='none';
});

/* crosshair + value readout on the variable timelines */
document.querySelectorAll('.spark').forEach(box => {
  const pts = box.dataset.series.split(';').map(p => p.split(','));
  const svg = box.querySelector('svg');
  const cross = box.querySelector('.cross');
  const out = box.querySelector('.spark-range');
  const orig = out.textContent;
  svg.addEventListener('mousemove', e => {
    const r = svg.getBoundingClientRect();
    const f = (e.clientX - r.left) / r.width;
    const i = Math.max(0, Math.min(pts.length-1,
                                   Math.round(f * (pts.length-1))));
    const vb = svg.viewBox.baseVal;
    const x = f * vb.width;
    cross.setAttribute('x1', x); cross.setAttribute('x2', x);
    cross.style.display = 'block';
    out.textContent = 'step ' + (+pts[i][0]).toLocaleString() +
                      ' = ' + pts[i][1];
    tip.textContent = 'step ' + (+pts[i][0]).toLocaleString() + '\n' +
                      box.querySelector('.spark-name').textContent +
                      ' = ' + pts[i][1] + '\nclick to jump there';
    tip.style.display = 'block'; place(e);
  });
  svg.addEventListener('mouseleave', () => {
    cross.style.display='none'; out.textContent = orig;
    tip.style.display='none';
  });
  svg.addEventListener('click', e => {
    const r = svg.getBoundingClientRect();
    const f = (e.clientX - r.left) / r.width;
    const i = Math.max(0, Math.min(pts.length-1,
                                   Math.round(f * (pts.length-1))));
    show(+pts[i][0]);
    document.getElementById('slider').scrollIntoView({behavior:'smooth',
                                                      block:'center'});
  });
});

/* ---- theme toggle --------------------------------------------------- */
const tb = document.getElementById('theme');
try { const saved = localStorage.getItem('viz-theme');
      if (saved) document.documentElement.dataset.theme = saved; } catch(_){}
tb.onclick = () => {
  const dark = matchMedia('(prefers-color-scheme: dark)').matches;
  const now = document.documentElement.dataset.theme ||
              (dark ? 'dark' : 'light');
  const next = now === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('viz-theme', next); } catch(_){}
};
</script>
</body></html>
"""
