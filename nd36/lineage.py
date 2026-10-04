"""
What happened to this DataFrame, step by step.

The store already keeps one row per (object, version), and a version is
written only when something observable changed. So the lineage of a frame is
just those rows, diffed pairwise:

    step 1,204   df   (10000, 12) -> (9873, 12)    -127 rows    dropna()
    step 1,251   df   (9873, 12)  -> (9873, 13)    +col ratio   float64
    step 1,340   df   (9873, 13)  -> (412, 13)     -9461 rows ! query()

That last line, with the warning, is the feature. Every data scientist has
spent an afternoon on a filter that was stricter than they thought, and no
tool points at it. Here it falls out of the trace for free.

The same machinery covers tensors: a shape that changes is a shape that can
be followed, and a shape mismatch is two numbers that should have matched.
"""

import ast
import os

from . import domains, schema

#: Flag a step that loses more than this share of the rows.
DROP_WARN = 0.30


def _versions(reader, kinds):
    """Every object version whose summary kind is one of `kinds`.

    Returned grouped by object, in step order, with the variable name that
    the step's own event carried -- so each row can say `df`, not `obj 41`.
    """
    import json
    rows = reader.db.execute(
        "SELECT obj_id, step, type, summary, repr, nbytes FROM objects "
        "ORDER BY obj_id, step")
    out = {}
    for r in rows:
        try:
            summary = json.loads(r["summary"])
        except Exception:
            continue
        if summary.get("kind") not in kinds:
            continue
        step = r["step"]
        name, line, path = None, None, None
        if 1 <= step <= reader.n_steps:
            ev = reader.view(step)
            name, line, path = ev["name"], ev["line"], ev["file"]
        out.setdefault(r["obj_id"], []).append({
            "obj_id": r["obj_id"], "step": step, "type": r["type"],
            "summary": summary, "repr": r["repr"], "nbytes": r["nbytes"],
            "name": name, "line": line, "file": path})
    return out


def _diff_frames(before, after):
    """What changed between two versions of one DataFrame."""
    a, b = before["summary"], after["summary"]
    sa = a.get("shape") or (0, 0)
    sb = b.get("shape") or (0, 0)
    rows_before = sa[0] if sa else 0
    rows_after = sb[0] if sb else 0

    cols_a = list(a.get("columns") or [])
    cols_b = list(b.get("columns") or [])
    added = [c for c in cols_b if c not in cols_a]
    dropped = [c for c in cols_a if c not in cols_b]

    dt_a = a.get("dtypes") or {}
    dt_b = b.get("dtypes") or {}
    retyped = {c: (dt_a[c], dt_b[c]) for c in dt_b
               if c in dt_a and dt_a[c] != dt_b[c]}

    change = {
        "rows": rows_after - rows_before,
        "rows_before": rows_before, "rows_after": rows_after,
        "cols_added": added, "cols_dropped": dropped,
        "retyped": retyped,
        "nulls": (b.get("n_nulls") or 0) - (a.get("n_nulls") or 0),
        "nbytes": (b.get("nbytes") or 0) - (a.get("nbytes") or 0),
    }
    lost = rows_before - rows_after
    change["warn"] = bool(rows_before and lost > 0
                          and lost / rows_before >= DROP_WARN)
    return change


def describe_frame_change(change):
    """The diff as one short human sentence."""
    bits = []
    if change["rows"]:
        bits.append("%+d rows" % change["rows"])
    for col in change["cols_added"][:4]:
        bits.append("+col %s" % col)
    for col in change["cols_dropped"][:4]:
        bits.append("-col %s" % col)
    for col, (was, now) in list(change["retyped"].items())[:3]:
        bits.append("%s: %s → %s" % (col, was, now))
    if change["nulls"]:
        bits.append("%+d nulls" % change["nulls"])
    if not bits:
        bits.append("values changed")
    return ", ".join(bits)


def _reads_by_line(source, path):
    """{line: {names read on it}}.

    Needed because data-science code almost never mutates a frame in place.
    It writes `b = f(a)`, so `b` is a brand new object with no previous
    version to diff against -- and a ribbon that only diffed an object with
    itself would say "created, 0 rows" at exactly the moment the rows
    vanished. The names a line READS are its candidate parents.
    """
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return {}
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            out.setdefault(node.lineno, set()).add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx,
                                                            ast.Load):
            base = node.value
            if isinstance(base, ast.Name):
                out.setdefault(node.lineno, set()).add(base.id)
    return out


def frame_lineage(reader):
    """[{name, step, line, change, parent, ...}] for every DataFrame."""
    script = reader.meta.get("script", "")
    reads = _reads_by_line(reader.source(script), script)

    versions = _versions(reader, (domains.FRAME,))
    flat = []
    for obj_id, rows in versions.items():
        name = next((v["name"] for v in rows if v["name"]), None)
        for i, version in enumerate(rows):
            version["first"] = (i == 0)
            version["prev"] = rows[i - 1] if i else None
            version["name"] = version["name"] or name
            flat.append(version)
    flat.sort(key=lambda v: v["step"])

    out = []
    latest = {}                      # variable name -> its newest version
    for version in flat:
        parent = version["prev"]
        parent_name = None
        if parent is None:
            # A new object: its parent is whichever frame variable this line
            # reads. Prefer the one most recently touched.
            # The same name counts: `clean = clean.assign(...)` rebinds the
            # name to a brand new frame, and the old one is exactly the
            # thing to compare against.
            candidates = [latest[n] for n in reads.get(version["line"], ())
                          if n in latest
                          and latest[n]["obj_id"] != version["obj_id"]]
            if candidates:
                parent = max(candidates, key=lambda v: v["step"])
                parent_name = parent["name"]
        change = _diff_frames(parent, version) if parent else None
        out.append({"obj_id": version["obj_id"], "step": version["step"],
                    "line": version["line"], "file": version["file"],
                    "name": version["name"], "parent": parent_name,
                    "shape": version["summary"].get("shape") or (0, 0),
                    "change": change,
                    "warn": bool(change and change["warn"]),
                    "summary": version["summary"]})
        if version["name"]:
            latest[version["name"]] = version
    return out


def tensor_lineage(reader):
    """Shape, dtype and device of every tensor, in step order."""
    out = []
    for obj_id, versions in _versions(reader,
                                      (domains.TENSOR,)).items():
        prev = None
        for version in versions:
            s = version["summary"]
            row = {"obj_id": obj_id, "step": version["step"],
                   "line": version["line"], "file": version["file"],
                   "name": version["name"],
                   "shape": tuple(s.get("shape") or ()),
                   "dtype": s.get("dtype"), "device": s.get("device"),
                   "grad_fn": s.get("grad_fn"),
                   "requires_grad": s.get("requires_grad"),
                   "n_nan": s.get("n_nan"), "n_inf": s.get("n_inf"),
                   "prev_shape": prev}
            prev = row["shape"]
            out.append(row)
    out.sort(key=lambda r: r["step"])
    return out


def shape_mismatch(reader):
    """If the run died on a shape error, say which shapes were in play.

    The worst error message in deep learning is `mat1 and mat2 shapes cannot
    be multiplied (32x784 and 128x10)`, because it names no variable and no
    line. The trace knows both.
    """
    hits = []
    seen = set()
    want = schema.KIND_CODE[schema.RAISE]
    for step in range(1, reader.n_steps + 1):
        if reader.raw(step)[1] != want:
            continue
        ev = reader.view(step)
        msg = (ev["extra"] or {}).get("msg", "")
        low = msg.lower()
        if not any(w in low for w in ("shape", "size", "dimension", "mat1",
                                      "broadcast", "must match")):
            continue
        # The same exception fires again in every frame it unwinds through.
        # The first one is the innermost, which is where the bug is.
        if msg in seen:
            continue
        seen.add(msg)
        # Every tensor that existed at the moment it blew up.
        live = [t for t in tensor_lineage(reader) if t["step"] <= step]
        latest = {}
        for t in live:
            latest[t["obj_id"]] = t
        hits.append({"step": step, "line": ev["line"], "file": ev["file"],
                     "exc": ev["name"], "msg": msg,
                     "tensors": sorted(latest.values(),
                                       key=lambda t: -t["step"])[:12]})
    return hits


def summary_line(row):
    """One formatted line of the ribbon, with file:line, never a bare step."""
    where = "%s:%s" % (os.path.basename(row["file"] or "?"), row["line"])
    shape = "%sx%s" % (row["shape"][0],
                       row["shape"][1] if len(row["shape"]) > 1 else "")
    what = (describe_frame_change(row["change"]) if row["change"]
            else "created")
    if row.get("parent") and row["parent"] != row["name"]:
        what = "from %s: %s" % (row["parent"], what)
    return (where, shape, what)
