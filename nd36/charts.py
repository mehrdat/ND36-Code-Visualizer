"""
Chart primitives: SVG built in Python, no plotting library, no CDN.

A trace report has to be one file you can email, so every pixel is inline
SVG. The rules applied here (thin marks, one axis, recessive grid, a legend
only when there are two or more series, rounded data-ends, colour by role
rather than by rank) come from the data-viz method, not from taste.

Palette: the validated reference palette. Light and dark steps are both
chosen, never an automatic flip.
"""

# Categorical slots, in fixed order -- never cycled past the end.
SERIES = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)",
          "var(--s5)", "var(--s6)", "var(--s7)", "var(--s8)"]

# Sequential blue ramp, light -> dark. For magnitude only.
RAMP = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7",
        "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281",
        "#0d366b"]
RAMP_DARK = ["#0d366b", "#104281", "#184f95", "#1c5cab", "#256abf",
             "#2a78d6", "#3987e5", "#5598e7", "#6da7ec", "#86b6ef",
             "#9ec5f4", "#b7d3f6", "#cde2fb"]


def esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def ramp_step(fraction, dark=False):
    """A magnitude in 0..1 -> one step of the sequential ramp."""
    steps = RAMP_DARK if dark else RAMP
    if fraction <= 0:
        return "transparent"
    i = int(round(fraction * (len(steps) - 1)))
    return steps[max(0, min(i, len(steps) - 1))]


# --------------------------------------------------------------------------

def bars(rows, width=620, bar_h=18, gap=7, unit="", fmt="%.1f"):
    """Horizontal bars. One series, so no legend -- the title names it.

    rows: [(label, value, tooltip)] already sorted the way you want it.
    Data-ends are rounded 4px and anchored to the baseline at x=0.
    """
    if not rows:
        return '<p class="empty">nothing to show</p>'
    label_w = 190
    peak = max(v for _, v, _ in rows) or 1
    plot_w = width - label_w - 70
    height = len(rows) * (bar_h + gap) + 6
    out = ['<svg class="chart" viewBox="0 0 %d %d" width="100%%" '
           'height="%d" role="img">' % (width, height, height)]
    for i, (label, value, tip) in enumerate(rows):
        y = i * (bar_h + gap) + 3
        w = max(2.0, plot_w * value / peak)
        out.append(
            '<text class="cat" x="%d" y="%.1f">%s</text>'
            % (label_w - 8, y + bar_h * 0.72, esc(label)))
        out.append(
            '<rect class="bar" x="%d" y="%.1f" width="%.1f" height="%d" '
            'rx="4" ry="4" tabindex="0" data-tip="%s"/>'
            % (label_w, y, w, bar_h, esc(tip)))
        out.append(
            '<text class="val" x="%.1f" y="%.1f">%s%s</text>'
            % (label_w + w + 7, y + bar_h * 0.72, fmt % value, unit))
    out.append("</svg>")
    return "".join(out)


def line_chart(points, title, width=320, height=92, colour=0):
    """A single numeric series over steps, with a hover crosshair.

    points: [(step, value)]. One series, so no legend box.
    """
    if len(points) < 2:
        return ""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 == x0:
        x1 = x0 + 1
    pad_l, pad_r, pad_t, pad_b = 6, 6, 14, 16
    span = (y1 - y0) or 1

    def px(x):
        return pad_l + (x - x0) / (x1 - x0) * (width - pad_l - pad_r)

    def py(y):
        return (height - pad_b
                - (y - y0) / span * (height - pad_t - pad_b))

    path = " ".join("%.1f,%.1f" % (px(x), py(y)) for x, y in points)
    stroke = SERIES[colour % len(SERIES)]
    data = ";".join("%d,%s" % (x, _num(y)) for x, y in points)
    out = ['<div class="spark" data-series="%s" data-x0="%d" data-x1="%d">'
           % (esc(data), x0, x1)]
    out.append('<div class="spark-head"><span class="spark-name">%s</span>'
               '<span class="spark-range">%s → %s</span></div>'
               % (esc(title), _num(ys[0]), _num(ys[-1])))
    out.append('<svg viewBox="0 0 %d %d" width="100%%" height="%d" '
               'preserveAspectRatio="none" class="chart line">'
               % (width, height, height))
    # Recessive baseline only -- no grid.
    out.append('<line class="axis" x1="%d" y1="%.1f" x2="%d" y2="%.1f"/>'
               % (pad_l, height - pad_b, width - pad_r, height - pad_b))
    out.append('<polyline class="series" points="%s" '
               'style="stroke:%s" fill="none"/>' % (path, stroke))
    # Direct-label the two ends, never every point.
    out.append('<circle class="end" cx="%.1f" cy="%.1f" r="2.6" '
               'style="fill:%s"/>' % (px(xs[-1]), py(ys[-1]), stroke))
    out.append('<line class="cross" x1="0" y1="%d" x2="0" y2="%.1f" '
               'style="display:none"/>' % (pad_t - 8, height - pad_b))
    out.append("</svg></div>")
    return "".join(out)


def _num(v):
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            return "%d" % int(v)
        return "%.4g" % v
    return str(v)


# --------------------------------------------------------------------------
# Graph layout: layers by longest path, which is what makes a call graph or
# a CFG readable. Back edges (loops, recursion) are found and drawn curved
# rather than being allowed to break the layering.
# --------------------------------------------------------------------------

def layered(nodes, edges, node_w=176, node_h=34, gap_x=26, gap_y=40,
            heights=None):
    """nodes: [{id, ...}] edges: [{src, dst, ...}] -> positions + back edges.

    `heights` lets a merged basic block be taller than a single statement."""
    ids = [n["id"] for n in nodes]
    idset = set(ids)
    out_edges = {i: [] for i in ids}
    indeg = {i: 0 for i in ids}
    for e in edges:
        if e["src"] in idset and e["dst"] in idset:
            out_edges[e["src"]].append(e["dst"])
            indeg[e["dst"]] += 1

    # Longest-path layering, with a visited guard so cycles terminate.
    layer = {i: 0 for i in ids}
    roots = [i for i in ids if indeg[i] == 0] or ids[:1]
    order = []
    seen = set()
    stack = [(r, 0) for r in reversed(roots)]
    while stack:
        nid, depth = stack.pop()
        if depth > layer[nid]:
            layer[nid] = depth
        if nid in seen and depth <= layer[nid]:
            if nid in seen:
                continue
        seen.add(nid)
        order.append(nid)
        if depth > 40:                      # runaway guard
            continue
        for dst in out_edges[nid]:
            stack.append((dst, depth + 1))
    for i in ids:                            # unreached nodes go last
        if i not in seen:
            layer[i] = max(layer.values(), default=0) + 1

    rows = {}
    for i in ids:
        rows.setdefault(layer[i], []).append(i)

    hof = (lambda i: heights.get(i, node_h)) if heights \
        else (lambda i: node_h)
    pos = {}
    width = max((len(v) for v in rows.values()), default=1) \
        * (node_w + gap_x) + gap_x
    y = gap_y / 2
    for ly in sorted(rows):
        row = rows[ly]
        span = len(row) * (node_w + gap_x) - gap_x
        start = (width - span) / 2
        for k, nid in enumerate(row):
            pos[nid] = (start + k * (node_w + gap_x), y)
        y += max(hof(nid) for nid in row) + gap_y
    height = y + gap_y / 2
    back = {(e["src"], e["dst"]) for e in edges
            if e["src"] in idset and e["dst"] in idset
            and layer.get(e["dst"], 0) <= layer.get(e["src"], 0)}
    return pos, width, height, back


LINE_H = 15
MAX_LINES = 5


def _node_lines(n):
    """A merged basic block shows its statements; anything else, its label."""
    lines = n.get("lines")
    if not lines:
        return [(n.get("line"), str(n.get("label", "")))]
    return lines


def _node_height(n, base):
    rows = min(len(_node_lines(n)), MAX_LINES)
    extra = 1 if len(_node_lines(n)) > MAX_LINES else 0
    return base + (rows - 1 + extra) * LINE_H


def graph_svg(nodes, edges, label_of, tip_of=None, kind_of=None,
              weight_of=None, node_w=176, node_h=34, numbered=False):
    """Draw a laid-out graph. Edge width carries call count; a zero-count
    edge is drawn grey and dashed, because 'never ran' is information."""
    if not nodes:
        return '<p class="empty">nothing to show</p>'
    heights = {n["id"]: (_node_height(n, node_h) if numbered else node_h)
               for n in nodes}
    pos, width, height, back = layered(nodes, edges, node_w=node_w,
                                      node_h=node_h, heights=heights)
    out = ['<svg class="graph" viewBox="0 0 %.0f %.0f" width="%.0f" '
           'height="%.0f">' % (width, height, width, height)]
    out.append('<defs><marker id="ah" viewBox="0 0 8 8" refX="7" refY="4" '
               'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
               '<path d="M0,0 L8,4 L0,8 z" class="arrow"/></marker></defs>')
    for e in edges:
        if e["src"] not in pos or e["dst"] not in pos:
            continue
        x1, y1 = pos[e["src"]]
        x2, y2 = pos[e["dst"]]
        x1 += node_w / 2
        x2 += node_w / 2
        y1 += heights[e["src"]]
        ran = e.get("n", 1) > 0 or e.get("ran", True)
        w = 1.4
        if weight_of and e.get("n"):
            w = 1.4 + min(4.0, 1.1 * (e["n"] ** 0.4))
        cls = "edge" + ("" if ran else " dead")
        if (e["src"], e["dst"]) in back:
            # A loop or recursion: bow it out to the side so it reads as
            # going back rather than crossing the graph.
            bow = 42 + abs(y2 - y1) * 0.25
            d = ("M%.1f,%.1f C%.1f,%.1f %.1f,%.1f %.1f,%.1f"
                 % (x1, y1, x1 + bow, y1 + 18, x2 + bow, y2 - 18, x2, y2))
            cls += " back"
        else:
            mid = (y1 + y2) / 2
            d = ("M%.1f,%.1f C%.1f,%.1f %.1f,%.1f %.1f,%.1f"
                 % (x1, y1, x1, mid, x2, mid, x2, y2))
        out.append('<path class="%s" d="%s" style="stroke-width:%.1f" '
                   'marker-end="url(#ah)"/>' % (cls, d, w))
        if e.get("label"):
            out.append('<text class="elabel" x="%.1f" y="%.1f">%s</text>'
                       % ((x1 + x2) / 2 + 5, (y1 + y2) / 2, esc(e["label"])))
    for n in nodes:
        if n["id"] not in pos:
            continue
        x, y = pos[n["id"]]
        h = heights[n["id"]]
        kind = kind_of(n) if kind_of else "stmt"
        tip = tip_of(n) if tip_of else ""
        out.append('<g class="node k-%s" tabindex="0" data-tip="%s">' %
                   (kind, esc(tip)))
        out.append('<rect x="%.1f" y="%.1f" width="%d" height="%.1f" rx="7"/>'
                   % (x, y, node_w, h))
        if numbered:
            rows = _node_lines(n)
            shown = rows[:MAX_LINES]
            ty = y + 16
            for lineno, text in shown:
                out.append('<text class="ln" x="%.1f" y="%.1f">%s</text>'
                           % (x + 9, ty, esc(lineno or "")))
                out.append('<text class="src" x="%.1f" y="%.1f">%s</text>'
                           % (x + 38, ty, esc(_fit(text, 27))))
                ty += LINE_H
            if len(rows) > MAX_LINES:
                out.append('<text class="src" x="%.1f" y="%.1f">+%d more'
                           '</text>' % (x + 38, ty, len(rows) - MAX_LINES))
        else:
            out.append('<text x="%.1f" y="%.1f">%s</text>'
                       % (x + node_w / 2, y + h * 0.64,
                          esc(_fit(label_of(n), 26))))
        out.append("</g>")
    out.append("</svg>")
    return "".join(out)


def _fit(text, n):
    text = str(text)
    return text if len(text) <= n else text[:n - 1] + "…"
