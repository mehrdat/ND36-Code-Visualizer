"""
Pictures made of text.

Everything the explorer shows is drawn here, so there is one place that
knows about terminal width, colour and rounding. No dependencies: a plot
is a list of strings.

The three shapes that carry almost all the meaning:

    sparkline   one line, the shape of a series          ▁▂▅█▆▃▁
    plot        ten lines, with a y axis                 for actual values
    heatrow     one line per function, density over time who ran when
"""

import math
import os
import shutil

BLOCKS = " ▁▂▃▄▅▆▇█"
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
BLUE = "\033[34m"
OFF = "\033[0m"


def width(default=100):
    """Terminal width, clamped. Pipes report 80; we do not want 200 either."""
    try:
        cols = shutil.get_terminal_size((default, 24)).columns
    except Exception:                                   # pragma: no cover
        cols = default
    return max(60, min(cols, 160))


def plain(text):
    """Length of a string as the eye sees it, ignoring colour codes."""
    out, i = 0, 0
    while i < len(text):
        if text[i] == "\033":
            while i < len(text) and text[i] != "m":
                i += 1
        else:
            out += 1
        i += 1
    return out


def pad(text, n):
    gap = n - plain(text)
    return text + " " * gap if gap > 0 else text


def sparkline(values, cells=None):
    """One row of blocks. The series' shape, in the space of a word."""
    vals = [v for v in values if v is not None]
    if not vals:
        return ""
    cells = cells or len(values)
    buckets = _resample(values, cells)
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    out = []
    for v in buckets:
        if v is None:
            out.append(" ")
            continue
        idx = int(round((v - lo) / span * (len(BLOCKS) - 1)))
        out.append(BLOCKS[max(0, min(len(BLOCKS) - 1, idx))])
    return "".join(out)


def _resample(values, cells):
    """Average `values` down to exactly `cells` buckets."""
    n = len(values)
    if n == 0:
        return []
    if n <= cells:
        return list(values)
    out = []
    for i in range(cells):
        lo = i * n // cells
        hi = max(lo + 1, (i + 1) * n // cells)
        chunk = [v for v in values[lo:hi] if v is not None]
        out.append(sum(chunk) / len(chunk) if chunk else None)
    return out


def plot(values, height=9, cells=None, label="", xlabel=""):
    """A real chart: y axis with numbers, so the shape has a scale.

    Used for `pl NAME` -- a numeric variable over the whole run. This is
    the thing a terminal debugger has never been able to show you.
    """
    vals = [v for v in values if v is not None]
    if not vals:
        return ["%s(nothing numeric to plot)%s" % (DIM, OFF)]
    cells = cells or (width() - 14)
    series = _resample(values, cells)
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi = lo + 1
    rows = []
    grid = [[" "] * len(series) for _ in range(height)]
    for x, v in enumerate(series):
        if v is None:
            continue
        # Height in eighths, so a value can half-fill its cell.
        eighths = int(round((v - lo) / (hi - lo) * height * 8))
        full, rest = divmod(max(0, eighths), 8)
        for y in range(min(full, height)):
            grid[height - 1 - y][x] = "█"
        if full < height and rest:
            grid[height - 1 - full][x] = BLOCKS[rest]

    def tick(y):
        frac = (height - 1 - y) / max(1, height - 1)
        return _num(lo + frac * (hi - lo))

    if label:
        rows.append("%s%s%s" % (BOLD, label, OFF))
    for y in range(height):
        rows.append("%s%10s%s %s│%s %s%s%s"
                    % (DIM, tick(y), OFF, DIM, OFF,
                       CYAN, "".join(grid[y]), OFF))
    rows.append("%s%10s └%s%s" % (DIM, "", "─" * len(series), OFF))
    if xlabel:
        rows.append("%s%11s %s%s" % (DIM, "", xlabel, OFF))
    return rows


def _num(v):
    """A number short enough for an axis."""
    a = abs(v)
    if v == int(v) and a < 1e6:
        return "%d" % int(v)
    if a >= 1e9:
        return "%.1fG" % (v / 1e9)
    if a >= 1e6:
        return "%.1fM" % (v / 1e6)
    if a >= 1e3:
        return "%.1fk" % (v / 1e3)
    if a < 0.01:
        return "%.1e" % v
    return "%.2f" % v


def bar(frac, cells=20, char="▇"):
    frac = max(0.0, min(1.0, frac or 0.0))
    n = int(round(frac * cells))
    return char * n + " " * (cells - n)


def table(headers, rows, aligns=None, limit=None, colour=None):
    """Aligned columns. Short tables are the whole point of the explorer.

    `colour` is a function (row_index, col_index, cell) -> colour code.
    """
    rows = list(rows)
    cut = None
    if limit and len(rows) > limit:
        cut = len(rows) - limit
        rows = rows[:limit]
    cols = len(headers)
    cells = [[("" if c is None else str(c)) for c in r] for r in rows]
    widest = [plain(headers[i]) for i in range(cols)]
    for r in cells:
        for i in range(min(cols, len(r))):
            widest[i] = max(widest[i], plain(r[i]))
    aligns = aligns or ["<"] * cols
    cap = width()

    def lay(parts):
        out, used = [], 0
        for i, text in enumerate(parts):
            w = widest[i]
            if used + w + 2 > cap and i:
                break
            out.append(pad(text, w) if aligns[i] == "<"
                       else " " * max(0, w - plain(text)) + text)
            used += w + 2
        return "  ".join(out).rstrip()

    lines = ["%s%s%s" % (DIM, lay(list(headers)), OFF)]
    for ri, r in enumerate(cells):
        parts = list(r) + [""] * (cols - len(r))
        if colour:
            parts = [(colour(ri, ci, p) or "") + p +
                     (OFF if colour(ri, ci, p) else "")
                     for ci, p in enumerate(parts)]
        lines.append(lay(parts))
    if cut:
        lines.append("%s… %d more%s" % (DIM, cut, OFF))
    return lines


def tree(root, children, label, prefix="", seen=None, depth=0, maxdepth=6):
    """An ASCII tree. `children(node)` -> list, `label(node)` -> str."""
    seen = seen if seen is not None else set()
    out = []
    if not prefix:
        out.append(label(root))
    if depth >= maxdepth or root in seen:
        return out
    seen = seen | {root}
    kids = list(children(root))
    for i, kid in enumerate(kids):
        last = i == len(kids) - 1
        out.append("%s%s%s" % (prefix, "└── " if last else "├── ",
                               label(kid)))
        out.extend(tree(kid, children, label,
                        prefix + ("    " if last else "│   "),
                        seen, depth + 1, maxdepth))
    return out


def cursor_bar(step, total, cells=None):
    """Where you are in the run: `──────●────────────  step 1,204 / 9,881`."""
    cells = cells or (width() - 34)
    at = 0 if total <= 1 else int((step - 1) / (total - 1) * (cells - 1))
    line = "─" * at + "%s●%s" % (YELLOW, OFF) + "─" * (cells - at - 1)
    return "%s%s%s  %s%s / %s%s" % (DIM, line, OFF, BOLD,
                                    f"{step:,}", f"{total:,}", OFF)


def heatrows(names, series, cells=None, name_width=22):
    """One row per name: a density sparkline over the same time axis.

    This is the picture of a program's shape over time -- which function
    was busy when, all of them stacked so you can see the phases.
    """
    cells = cells or (width() - name_width - 4)
    peak = max((max(s) if s else 0) for s in series) or 1
    out = []
    for name, s in zip(names, series):
        buckets = _resample(s, cells)
        row = "".join(
            " " if not v else BLOCKS[max(1, min(8, int(round(
                math.sqrt(v / peak) * 8))))]
            for v in buckets)
        out.append("%-*s %s%s%s" % (name_width, name[:name_width],
                                    CYAN, row, OFF))
    return out


def rel(path):
    return os.path.basename(path or "?")
