"""
The trace store.

Layout of a trace directory (`something.vizt/`):

    events.bin   append-only, FIXED-WIDTH records. step N is at N*RECSIZE.
    trace.db     SQLite: the small tables -- files, source, locations,
                 functions, object versions, keyframes, name index, meta.

Why split them?  Because the two halves want opposite things.  The event
log is written once, in order, millions of times, and read by step number
-- a flat file with fixed records does that in O(1) with no index.  The
side tables are small, relational and queried ad hoc -- that is SQLite's job.

Why not full snapshots like Python Tutor?  Because that is what caps it at
1,000 steps.  We store only what changed, plus a full KEYFRAME every
`keyframe_every` steps.  "What did the program look like at step 847,312?"
= load the nearest keyframe below it, replay a few hundred events forward.
"""

import json
import mmap
import os
import sqlite3
import struct
import zlib
from collections import Counter, defaultdict

from . import schema

KEYFRAME_EVERY = 2000

# One reused encoder. json.dumps() with non-default arguments rebuilds a
# JSONEncoder on every call, which on the hot path costs more than the
# encoding itself.
dumps = json.JSONEncoder(separators=(",", ":"), default=str).encode

_REC = struct.Struct(schema.RECORD)
RECSIZE = _REC.size

_DDL = """
CREATE TABLE meta    (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE files   (id INTEGER PRIMARY KEY, path TEXT UNIQUE, source TEXT);
CREATE TABLE locs    (id INTEGER PRIMARY KEY, file INTEGER, line INTEGER,
                      col0 INTEGER, col1 INTEGER);
CREATE TABLE funcs   (id INTEGER PRIMARY KEY, name TEXT, qualname TEXT,
                      file INTEGER, first_line INTEGER);
CREATE TABLE names   (id INTEGER PRIMARY KEY, text TEXT UNIQUE);
CREATE TABLE strings (id INTEGER PRIMARY KEY, text TEXT);
CREATE TABLE payloads(id INTEGER PRIMARY KEY, json TEXT);
-- One row per (object, version). A list that changes 40 times has 40 rows,
-- so the UI can show what it held at any step.
CREATE TABLE objects (obj_id INTEGER, step INTEGER, type TEXT,
                      summary TEXT, children TEXT, repr TEXT, nbytes INTEGER,
                      PRIMARY KEY (obj_id, step));
CREATE TABLE keyframes (step INTEGER PRIMARY KEY, blob BLOB);
-- Built at close() from counters kept in memory during the run, so
-- `watch total` is one indexed lookup instead of a full scan.
CREATE TABLE binds   (name INTEGER, step INTEGER);
CREATE TABLE linehits(loc INTEGER PRIMARY KEY, hits INTEGER, t_ns INTEGER);
-- Phase 3. `samples` is the cheap curve: counters read every N steps.
-- `snapshots` + `allocs` are the expensive truth: which source line is
-- holding the bytes, captured when memory grew rather than on a timer.
CREATE TABLE samples  (step INTEGER PRIMARY KEY, t_ns INTEGER,
                       blocks INTEGER, cur INTEGER, peak INTEGER,
                       g0 INTEGER, g1 INTEGER, g2 INTEGER);
-- Pass 2 (tracemalloc, run on its own) writes these. `at_ns` is time from
-- the start of THAT run, not a step in the recorded trace.
CREATE TABLE memcurve (at_ns INTEGER PRIMARY KEY, cur INTEGER, peak INTEGER,
                       blocks INTEGER);
CREATE TABLE snapshots(id INTEGER PRIMARY KEY, step INTEGER, why TEXT,
                       total INTEGER);
CREATE TABLE allocs   (snap INTEGER, path TEXT, line INTEGER,
                       size INTEGER, count INTEGER);
"""


class TraceWriter:
    def __init__(self, path, keyframe_every=KEYFRAME_EVERY):
        os.makedirs(path, exist_ok=True)
        self.path = path
        db_path = os.path.join(path, "trace.db")
        if os.path.exists(db_path):
            os.remove(db_path)
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA journal_mode=OFF")
        self.db.execute("PRAGMA synchronous=OFF")
        self.db.executescript(_DDL)

        self.log = open(os.path.join(path, "events.bin"), "wb")
        self._buf = bytearray()
        self._pack = _REC.pack          # bound once; called millions of times

        self.keyframe_every = keyframe_every
        self._next_keyframe = keyframe_every
        self.step = 0
        self._objects = []
        self._files = {}
        self._locs = {}
        self._funcs = {}
        self._names = {}
        self._strings = {}
        self._payloads = []
        self._samples = []
        self._memcurve = []
        self._snapshots = []
        self._allocs = []
        self._native = {}                 # (frame, name) -> obj id
        self._binds = defaultdict(list)   # name_id -> [step, ...]
        self._linehits = Counter()
        self._linetime = Counter()

    # -- interning ---------------------------------------------------------

    def file_id(self, path):
        fid = self._files.get(path)
        if fid is None:
            fid = len(self._files) + 1
            self._files[path] = fid
            try:
                with open(path, "r", encoding="utf8", errors="replace") as fh:
                    source = fh.read()
            except OSError:
                source = ""
            self.db.execute("INSERT INTO files VALUES (?,?,?)",
                            (fid, path, source))
        return fid

    def loc_id(self, path, line, col0=None, col1=None):
        key = (path, line, col0, col1)
        lid = self._locs.get(key)
        if lid is None:
            lid = len(self._locs) + 1
            self._locs[key] = lid
            self.db.execute("INSERT INTO locs VALUES (?,?,?,?,?)",
                            (lid, self.file_id(path), line, col0, col1))
        return lid

    def func_id(self, name, qualname, path, first_line):
        key = (qualname, path, first_line)
        fid = self._funcs.get(key)
        if fid is None:
            fid = len(self._funcs) + 1
            self._funcs[key] = fid
            self.db.execute("INSERT INTO funcs VALUES (?,?,?,?,?)",
                            (fid, name, qualname, self.file_id(path),
                             first_line))
        return fid

    def name_id(self, text):
        nid = self._names.get(text)
        if nid is None:
            nid = len(self._names) + 1
            self._names[text] = nid
            self.db.execute("INSERT INTO names VALUES (?,?)", (nid, text))
        return nid

    def string_id(self, text):
        """Intern a string VALUE. Repeated strings cost one int after the
        first time, which is the common case in real programs."""
        sid = self._strings.get(text)
        if sid is None:
            sid = len(self._strings) + 1
            self._strings[text] = sid
            self.db.execute("INSERT INTO strings VALUES (?,?)", (sid, text))
        return sid

    def native_object_id(self, frame_id, name):
        """A stable object id for a C variable.

        Python objects are identified by id(); a C local has no such handle,
        so its identity is (which activation, which name). That is enough for
        the UI to track one struct across the steps of a function.
        """
        key = ("native", frame_id, name)
        got = self._native.get(key)
        if got is None:
            got = self._native[key] = 10_000_000 + len(self._native)
        return got

    def payload_id(self, payload):
        self._payloads.append((len(self._payloads) + 1, dumps(payload)))
        return len(self._payloads)

    # -- the hot path ------------------------------------------------------

    def event(self, t_ns, kind, frame, loc, name=0, vref=schema.ABSENT,
              payload=0):
        """Append one event. `name` is a name id, `vref` a (rk, ri, rf)."""
        self.step += 1
        self._buf += self._pack(t_ns, kind, frame, loc, name,
                                vref[0], vref[1], vref[2], payload)
        if len(self._buf) >= 1 << 20:
            self.log.write(self._buf)
            del self._buf[:]
        return self.step

    def note_bind(self, name_id):
        self._binds[name_id].append(self.step)

    def note_line(self, loc, prev_loc, dt):
        """Two different lines: the hit belongs to the line we are ENTERING,
        the elapsed time to the one we are LEAVING. Crediting both to the
        same loc made the last line of every function look unexecuted."""
        self._linehits[loc] += 1
        if prev_loc is not None:
            self._linetime[prev_loc] += dt

    def object_version(self, obj_id, type_name, step, summary, children,
                       repr_short, nbytes):
        self._objects.append((obj_id, step, type_name, dumps(summary),
                              dumps(children) if children is not None
                              else None, repr_short, nbytes))
        if len(self._objects) >= 4096:
            self._flush_objects()

    def sample(self, step, t_ns, blocks, cur, peak, g0, g1, g2):
        self._samples.append((step, t_ns, blocks, cur, peak, g0, g1, g2))

    def mem_point(self, at_ns, cur, peak, blocks):
        self._memcurve.append((at_ns, cur, peak, blocks))

    def snapshot(self, step, why, total, rows):
        snap = len(self._snapshots) + 1
        self._snapshots.append((snap, step, why, total))
        for path, line, size, count in rows:
            self._allocs.append((snap, path, line, size, count))

    def keyframe(self, step, state):
        blob = zlib.compress(dumps(state).encode("utf8"), 1)
        self.db.execute("INSERT OR REPLACE INTO keyframes VALUES (?,?)",
                        (step, blob))

    def should_keyframe(self):
        if self.step >= self._next_keyframe:
            self._next_keyframe = self.step + self.keyframe_every
            return True
        return False

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                        (key, dumps(value)))

    def _flush_objects(self):
        self.db.executemany(
            "INSERT OR REPLACE INTO objects VALUES (?,?,?,?,?,?,?)",
            self._objects)
        self._objects.clear()

    def close(self):
        if self._buf:
            self.log.write(self._buf)
            del self._buf[:]
        self.log.close()
        self._flush_objects()
        self.db.executemany("INSERT INTO payloads VALUES (?,?)",
                            self._payloads)
        self.db.executemany("INSERT INTO samples VALUES (?,?,?,?,?,?,?,?)",
                            self._samples)
        self.db.executemany(
            "INSERT OR REPLACE INTO memcurve VALUES (?,?,?,?)",
            self._memcurve)
        self.db.executemany("INSERT INTO snapshots VALUES (?,?,?,?)",
                            self._snapshots)
        self.db.executemany("INSERT INTO allocs VALUES (?,?,?,?,?)",
                            self._allocs)
        self.db.executemany("INSERT INTO binds VALUES (?,?)",
                            ((nid, s) for nid, steps in self._binds.items()
                             for s in steps))
        self.db.executemany(
            "INSERT INTO linehits VALUES (?,?,?)",
            ((loc, n, self._linetime[loc])
             for loc, n in self._linehits.items()))
        self.db.executescript(
            "CREATE INDEX ix_binds ON binds(name, step);"
            "CREATE INDEX ix_allocs ON allocs(snap);")
        self.db.commit()
        self.db.close()


# --------------------------------------------------------------------------

class TraceReader:
    """Read side. Everything the CLI and the future UI ask for goes here."""

    def __init__(self, path):
        self.dir = path[:-9] if path.endswith("/trace.db") else path
        # check_same_thread=False because `nd serve` answers each request
        # on its own thread. The trace is read-only and the server holds a
        # lock around every call, so there is nothing to race over.
        self.db = sqlite3.connect(
            "file:%s?mode=ro" % os.path.join(self.dir, "trace.db"),
            uri=True, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.meta = {r["key"]: json.loads(r["value"])
                     for r in self.db.execute("SELECT * FROM meta")}
        self.locs = {r["id"]: (r["file"], r["line"])
                     for r in self.db.execute("SELECT id,file,line FROM locs")}
        self.files = {r["id"]: r["path"]
                      for r in self.db.execute("SELECT id, path FROM files")}
        self.funcs = {r["id"]: dict(r)
                      for r in self.db.execute("SELECT * FROM funcs")}
        self.names = {r["id"]: r["text"]
                      for r in self.db.execute("SELECT * FROM names")}
        self.name_ids = {v: k for k, v in self.names.items()}
        self.strings = {r["id"]: r["text"]
                        for r in self.db.execute("SELECT * FROM strings")}

        log_path = os.path.join(self.dir, "events.bin")
        self._size = os.path.getsize(log_path)
        self.n_steps = self._size // RECSIZE
        self._fh = open(log_path, "rb")
        # mmap gives us the whole log as one bytes-like object, so reading
        # step N is a slice, not a seek+read syscall.
        self._mm = (mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
                    if self._size else b"")

    # -- raw access --------------------------------------------------------

    def raw(self, step):
        """The packed tuple for one step. O(1) -- no index involved."""
        off = (step - 1) * RECSIZE
        return _REC.unpack_from(self._mm, off)

    def events(self, lo, hi):
        """Packed tuples for steps lo+1 .. hi, as a list."""
        lo = max(lo, 0)
        hi = min(hi, self.n_steps)
        return [_REC.unpack_from(self._mm, i * RECSIZE)
                for i in range(lo, hi)]

    def vref(self, rk, ri, rf):
        """Packed value -> the ["i", v] / ["o", id] shape the UI wants."""
        if rk == schema.RK_ABSENT:
            return None
        if rk == schema.RK_INT:
            return ["i", ri]
        if rk == schema.RK_FLOAT:
            return ["i", rf]
        if rk == schema.RK_STR:
            return ["i", self.strings.get(ri, "")]
        if rk == schema.RK_BOOL:
            return ["i", bool(ri)]
        if rk == schema.RK_OBJ:
            return ["o", ri]
        return ["i", None]

    def _norm(self, maybe_triple):
        """Keyframes and CALL payloads go through JSON, so a packed vref
        arrives as a 3-element list. Turn it back into a display ref."""
        if isinstance(maybe_triple, (list, tuple)) and len(maybe_triple) == 3:
            return self.vref(maybe_triple[0], maybe_triple[1],
                             maybe_triple[2])
        return maybe_triple

    def decode_children(self, children):
        """[[key, triple], ...] -> [[key, display_ref], ...]"""
        if not children:
            return children
        return [[k, self._norm(v)] for k, v in children]

    def view(self, step):
        """One event, fully decoded -- for display and for the UI."""
        t, k, fr, loc, nm, rk, ri, rf, pid = self.raw(step)
        path, line = self.where(loc)
        out = {"step": step, "t_ns": t, "kind": schema.CODE_KIND.get(k),
               "frame": fr, "loc": loc, "file": path, "line": line,
               "name": self.names.get(nm), "ref": self.vref(rk, ri, rf)}
        if pid:
            row = self.db.execute("SELECT json FROM payloads WHERE id=?",
                                  (pid,)).fetchone()
            out["extra"] = json.loads(row["json"]) if row else {}
        else:
            out["extra"] = {}
        return out

    # -- lookups -----------------------------------------------------------

    def where(self, loc_id):
        got = self.locs.get(loc_id)
        if not got:
            return (None, None)
        return (self.files.get(got[0]), got[1])

    def source(self, path):
        row = self.db.execute("SELECT source FROM files WHERE path=?",
                              (path,)).fetchone()
        return row["source"] if row else ""

    def resolve(self, ref, step):
        """A ref -> something printable, at a given step."""
        if ref is None:
            return None
        if ref[0] == "i":
            return ref[1]
        row = self.db.execute(
            "SELECT * FROM objects WHERE obj_id=? AND step<=? "
            "ORDER BY step DESC LIMIT 1", (ref[1], step)).fetchone()
        if row is None:
            return "<obj %d>" % ref[1]
        return {"obj_id": ref[1], "type": row["type"],
                "summary": json.loads(row["summary"]),
                "children": json.loads(row["children"])
                if row["children"] else None,
                "repr": row["repr"]}

    # -- the core query ----------------------------------------------------

    def state_at(self, step):
        """Program state as it was when `step` was about to execute."""
        step = max(1, min(step, self.n_steps))
        row = self.db.execute(
            "SELECT step, blob FROM keyframes WHERE step<=? "
            "ORDER BY step DESC LIMIT 1", (step,)).fetchone()
        if row is None:
            base, state = 0, {"frames": [], "globals": {}}
        else:
            base = row["step"]
            state = json.loads(zlib.decompress(row["blob"]).decode("utf8"))
            for f in state["frames"]:
                f["locals"] = {k: self._norm(v)
                               for k, v in f["locals"].items()}
            state["globals"] = {k: self._norm(v)
                                for k, v in state["globals"].items()}

        frames = state["frames"]
        glob = state["globals"]
        by_id = {f["frame"]: f for f in frames}
        K = schema.KIND_CODE
        k_line, k_bind = K[schema.LINE], K[schema.BIND]
        k_call, k_ret = K[schema.CALL], K[schema.RETURN]
        k_unwind, k_del = K[schema.UNWIND], K[schema.DELETE]

        for rec in self.events(base, step):
            _, k, fr, loc, nm, rk, ri, rf, pid = rec
            if k == k_line:                       # by far the most common
                f = by_id.get(fr)
                if f is not None:
                    f["loc"] = loc
            elif k == k_bind:
                f = by_id.get(fr)
                (f["locals"] if f else glob)[self.names.get(nm)] = \
                    self.vref(rk, ri, rf)
            elif k == k_call:
                extra = {}
                if pid:
                    r2 = self.db.execute(
                        "SELECT json FROM payloads WHERE id=?",
                        (pid,)).fetchone()
                    extra = json.loads(r2["json"]) if r2 else {}
                f = {"frame": fr, "func": extra.get("func"),
                     "scope": extra.get("scope", "local"),
                     "loc": loc,
                     "locals": {k: self._norm(v) for k, v in
                                (extra.get("args") or {}).items()}}
                frames.append(f)
                by_id[fr] = f
            elif k == k_ret or k == k_unwind:
                f = by_id.pop(fr, None)
                if f in frames:
                    frames.remove(f)
            elif k == k_del:
                f = by_id.get(fr)
                (f["locals"] if f else glob).pop(self.names.get(nm), None)

        # A module-level frame's locals ARE the globals.
        out_frames = []
        for f in frames:
            if f.get("scope") == "global":
                glob.update(f["locals"])
            else:
                out_frames.append(f)
        return {"step": step, "frames": out_frames, "globals": glob}

    def history_of(self, name, limit=None):
        """The whole life of one variable -- what a live debugger cannot show.

        One indexed lookup in `binds`, then O(1) reads from the log.
        """
        nid = self.name_ids.get(name)
        if nid is None:
            return []
        sql = "SELECT step FROM binds WHERE name=? ORDER BY step"
        if limit:
            sql += " LIMIT %d" % int(limit)
        out = []
        prev = None
        for r in self.db.execute(sql, (nid,)):
            ev = self.view(r["step"])
            ev["prev"] = prev
            prev = ev["ref"]
            out.append(ev)
        return out

    def steps_of(self, name):
        """Every step that wrote `name`. One indexed read, no event views."""
        nid = self.name_ids.get(name)
        if nid is None:
            return []
        return [r["step"] for r in self.db.execute(
            "SELECT step FROM binds WHERE name=? ORDER BY step", (nid,))]

    def numeric_series(self, name, cap=4000):
        """[(step, number)] for plotting -- straight out of the log.

        An int or float value lives inside the fixed-width record itself,
        so a million-point series costs no queries at all. Object-valued
        names come back with None and are measured separately.
        """
        steps = self.steps_of(name)
        if not steps:
            return [], None
        stride = max(1, len(steps) // cap)
        out, obj = [], None
        for s in steps[::stride]:
            rec = _REC.unpack_from(self._mm, (s - 1) * RECSIZE)
            rk, ri, rf = rec[5], rec[6], rec[7]
            if rk == schema.RK_INT:
                out.append((s, ri))
            elif rk == schema.RK_FLOAT:
                out.append((s, rf))
            elif rk == schema.RK_BOOL:
                out.append((s, 1 if ri else 0))
            elif rk == schema.RK_OBJ:
                obj = obj or ri
                out.append((s, None))
            else:
                out.append((s, None))
        return out, obj

    def object_series(self, obj_id):
        """[(step, summary, nbytes)] -- every version of one object."""
        rows = self.db.execute(
            "SELECT step, summary, nbytes, type FROM objects "
            "WHERE obj_id=? ORDER BY step", (obj_id,)).fetchall()
        out = []
        for r in rows:
            try:
                summary = json.loads(r["summary"]) if r["summary"] else {}
            except ValueError:
                summary = {}
            out.append((r["step"], summary, r["nbytes"], r["type"]))
        return out

    def mutations(self, limit=None):
        """Every in-place change: a[i] = x, self.x += 1, xs.append(v)."""
        want = schema.KIND_CODE[schema.MUTATE]
        out = []
        for i in range(self.n_steps):
            if _REC.unpack_from(self._mm, i * RECSIZE)[1] == want:
                out.append(self.view(i + 1))
                if limit and len(out) >= limit:
                    break
        return out

    def variable_counts(self):
        """{name: times assigned}, straight from the index.

        Counting by calling history_of() per name meant building a full
        event view for every assignment -- on a 1.4-million-step trace that
        is hundreds of thousands of objects to answer "what can I watch?".
        One GROUP BY does it.
        """
        out = {}
        for r in self.db.execute(
                "SELECT name, COUNT(*) n FROM binds GROUP BY name"):
            text = self.names.get(r["name"])
            if text:
                out[text] = r["n"]
        return out

    def line_stats(self):
        """{(file, line): (hits, total_ns)} -- the execution heatmap."""
        out = {}
        for r in self.db.execute("SELECT * FROM linehits"):
            out[self.where(r["loc"])] = (r["hits"], r["t_ns"])
        return out

    def find(self, kind=None, name=None, line=None, limit=100):
        """Search the whole history."""
        if name is not None:
            rows = self.history_of(name)
        else:
            want = schema.KIND_CODE.get(kind) if kind else None
            rows = []
            for i in range(self.n_steps):
                k = _REC.unpack_from(self._mm, i * RECSIZE)[1]
                if want is None or k == want:
                    rows.append(self.view(i + 1))
                    if len(rows) >= limit * 4:
                        break
        if line is not None:
            rows = [r for r in rows if r["line"] == line]
        if kind is not None:
            rows = [r for r in rows if r["kind"] == kind]
        return rows[:limit]

    def samples(self):
        """The memory curve: [{step, t_ns, blocks, cur, peak, gc}]."""
        return [{"step": r["step"], "t_ns": r["t_ns"], "blocks": r["blocks"],
                 "cur": r["cur"], "peak": r["peak"],
                 "gc": (r["g0"], r["g1"], r["g2"])}
                for r in self.db.execute("SELECT * FROM samples ORDER BY step")]

    def memcurve(self):
        """The exact byte curve from the tracemalloc pass."""
        return [{"at_ns": r["at_ns"], "cur": r["cur"], "peak": r["peak"],
                 "blocks": r["blocks"]}
                for r in self.db.execute(
                    "SELECT * FROM memcurve ORDER BY at_ns")]

    def snapshots(self):
        """Heap snapshots with their per-line attribution."""
        out = []
        for r in self.db.execute("SELECT * FROM snapshots ORDER BY id"):
            rows = [{"path": a["path"], "line": a["line"], "size": a["size"],
                     "count": a["count"]}
                    for a in self.db.execute(
                        "SELECT * FROM allocs WHERE snap=? "
                        "ORDER BY size DESC", (r["id"],))]
            out.append({"id": r["id"], "step": r["step"], "why": r["why"],
                        "total": r["total"], "rows": rows})
        return out

    def biggest_objects(self, limit=20):
        """The objects that took the most bytes, at their largest."""
        rows = self.db.execute(
            "SELECT obj_id, type, MAX(nbytes) n, repr FROM objects "
            "WHERE nbytes IS NOT NULL GROUP BY obj_id "
            "ORDER BY n DESC LIMIT ?", (limit,))
        return [{"obj_id": r["obj_id"], "type": r["type"], "nbytes": r["n"],
                 "repr": r["repr"]} for r in rows]

    def step_at_time(self, t_ns):
        """Which step was running at this timestamp -- for spike → line."""
        lo, hi = 1, self.n_steps
        while lo < hi:
            mid = (lo + hi) // 2
            if self.raw(mid)[0] < t_ns:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def kind_counts(self):
        counts = Counter()
        for i in range(self.n_steps):
            counts[_REC.unpack_from(self._mm, i * RECSIZE)[1]] += 1
        return {schema.CODE_KIND.get(k, k): n for k, n in counts.items()}
