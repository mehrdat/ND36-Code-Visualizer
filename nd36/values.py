"""
Turning a live Python value into something we can store.

Rule of the whole project: we store a SUMMARY, never the data.
A 2-million-row DataFrame becomes ~12 numbers.  That is what lets us
record 10 million steps instead of Python Tutor's 1,000.

Scalars are stored inline ("immediate").  Everything else gets an
obj_id and a row in the objects table, plus one *version* row each time
its contents change -- so the UI can show what a list held at step 4,217.
"""

import json
import weakref
from collections import deque

from . import domains, schema

# One reused encoder -- see the note in store.py about why this matters.
dumps = json.JSONEncoder(separators=(",", ":"), default=str).encode

# Limits. Tune these; they are the difference between a usable trace and a
# 40 GB one.
MAX_STR = 200        # characters of a string we keep
MAX_CHILDREN = 50    # items of a container we keep
MAX_DEPTH = 3        # how deep we walk nested containers

# Two budgets, both learned the hard way. Without them, a list that grows
# inside a loop gets its first 50 items re-walked on every single iteration:
# a 3,000-row build cost 16 seconds instead of 0.3.
MAX_WALK = 96        # child encodes allowed per top-level encode
DEEP_BUDGET = 150    # times one object gets its contents recorded; after
                     # that we keep only its summary (shape, length, dtype)

IMMEDIATE_TYPES = (int, float, bool, str, bytes, type(None))

# Types that are containers we walk into.
_SEQ = (list, tuple, set, frozenset)


#: Above this length we never call repr() -- see _short_repr.
REPR_LIMIT = 32


def _short_repr(value):
    """A short, safe, CHEAP repr.

    Three traps here, all of which cost real time before they were fixed:

    1. repr() of a 3,000-item list builds a 200 kB string that we then throw
       away to keep 200 characters. Done once per loop iteration, that was
       11 of the 12 seconds in the realistic benchmark. So for anything
       longer than REPR_LIMIT we assemble a preview ourselves.
    2. repr() runs user code (__repr__), which can raise. Hence the guard.
    3. A DataFrame's or tensor's repr formats the whole thing. Those never
       reach repr() -- their summariser already has shape and dtype.
    """
    try:
        n = len(value)
    except Exception:
        n = None

    if n is not None and n > REPR_LIMIT:
        name = type(value).__name__
        try:
            if isinstance(value, dict):
                head = ", ".join("%s: %s" % (_tiny(k), _tiny(v))
                                 for k, v in list(value.items())[:4])
                return "{%s, …}  %s items" % (head, n)
            if isinstance(value, (list, tuple, set, frozenset, bytearray)):
                head = ", ".join(_tiny(v) for v in list(value)[:6])
                open_, close = ("[", "]") if isinstance(value, list) \
                    else ("(", ")")
                return "%s%s, …%s  %s items" % (open_, head, close, n)
        except Exception:
            pass
        return "<%s, %s items>" % (name, n)

    try:
        text = repr(value)
    except Exception as exc:                      # a broken __repr__
        return "<repr failed: %s>" % type(exc).__name__
    return text if len(text) <= MAX_STR else text[:MAX_STR] + "…"


def _tiny(value):
    """A 24-character stand-in, used only inside previews."""
    t = type(value)
    if t in (int, float, bool, str, bytes) or value is None:
        text = repr(value)
    else:
        try:
            text = "%s(%d)" % (t.__name__, len(value))
        except Exception:
            text = "<%s>" % t.__name__
    return text if len(text) <= 24 else text[:24] + "…"


# --------------------------------------------------------------------------
# Summarisers: cheap metadata per type family.
# Each returns a plain dict.  Add new families here -- this is the plugin
# point that later carries pandas / torch / polars panels.
# --------------------------------------------------------------------------

def _summarise_generic(value):
    kind = type(value).__name__
    out = {"type": kind}
    # len() is cheap for builtins and is the single most useful fact.
    try:
        out["len"] = len(value)
    except Exception:
        pass
    return out


def _summarise_numpy(value):
    out = {"type": "ndarray",
           "shape": tuple(value.shape),
           "dtype": str(value.dtype),
           "nbytes": int(value.nbytes),
           # `base is not None` means this array is a VIEW of another one.
           # Silent view-vs-copy bugs are the classic numpy trap, so we flag it.
           "is_view": value.base is not None}
    try:
        if value.size and value.dtype.kind in "fiu":
            out["min"] = float(value.min())
            out["max"] = float(value.max())
        if value.dtype.kind == "f":
            # .any() on a boolean array -> True if any element is True
            out["has_nan"] = bool(value.__class__.__module__ and
                                  (value != value).any())
    except Exception:
        pass
    return out


def _summarise_dataframe(value):
    out = {"type": type(value).__name__,
           "shape": tuple(value.shape),
           "columns": [str(c) for c in list(value.columns)[:MAX_CHILDREN]],
           "dtypes": {str(k): str(v)
                      for k, v in list(value.dtypes.items())[:MAX_CHILDREN]}}
    try:
        # memory_usage(deep=True) counts the actual string bytes too.
        out["nbytes"] = int(value.memory_usage(deep=True).sum())
        out["nulls"] = {str(k): int(v)
                        for k, v in value.isna().sum().items()
                        if v}
    except Exception:
        pass
    return out


def _summarise_series(value):
    return {"type": type(value).__name__,
            "len": int(value.shape[0]),
            "dtype": str(value.dtype),
            "nulls": int(value.isna().sum())}


def _summarise_tensor(value):
    out = {"type": "Tensor",
           "shape": tuple(value.shape),
           "dtype": str(value.dtype),
           "device": str(value.device),
           "requires_grad": bool(value.requires_grad),
           "nbytes": int(value.element_size() * value.nelement())}
    try:
        if value.grad_fn is not None:
            out["grad_fn"] = type(value.grad_fn).__name__
    except Exception:
        pass
    return out


def _label_for(summary, tname):
    """A one-line name built from the summary, never from the data."""
    kind = summary.get("kind")
    shape = summary.get("shape")
    if kind == domains.FRAME:
        return "%s %sx%s  %s" % (summary.get("type", tname),
                                 shape[0] if shape else "?",
                                 shape[1] if shape and len(shape) > 1 else "?",
                                 ", ".join(summary.get("columns", [])[:6]))
    if kind == domains.TENSOR:
        return "Tensor%s %s %s" % (list(shape or []),
                                   summary.get("dtype", ""),
                                   summary.get("device", ""))
    if kind == domains.ARRAY:
        return "%s%s %s" % (summary.get("type", tname), list(shape or []),
                            summary.get("dtype", ""))
    if kind == domains.MODEL:
        n = summary.get("n_params")
        return "%s%s" % (summary.get("type", tname),
                         ("  %s params" % f"{n:,}") if n else "")
    return "%s %s" % (summary.get("type", tname), shape or "")


def _pick_summariser(value):
    """Choose a summariser WITHOUT importing numpy/pandas/torch.

    We look at type(value).__module__, so a script that never imports
    pandas never pays for pandas.
    """
    mod = type(value).__module__.split(".")[0]
    name = type(value).__name__
    if mod == "numpy" and name == "ndarray":
        return _summarise_numpy
    if mod in ("pandas", "polars"):
        if name == "DataFrame":
            return _summarise_dataframe
        if name == "Series":
            return _summarise_series
    if mod == "torch" and name in ("Tensor", "Parameter"):
        return _summarise_tensor
    return _summarise_generic


# --------------------------------------------------------------------------
# Encoder
# --------------------------------------------------------------------------

#: How many non-weakref-able objects we hold on to. See the note below.
KEEP_ALIVE = 20000


class Encoder:
    """Assigns obj_ids and records object versions.

    Identity is keyed on id(value), and CPython reuses an id as soon as the
    object dies -- so something has to tell us when that happens, or two
    unrelated objects silently merge into one.

    Phase 0 solved that by holding a strong reference to everything it had
    ever seen. That worked, and it also meant the recorder stopped the
    garbage collector dead: nothing the traced program allocated was ever
    freed. Harmless until Phase 3 tried to measure memory, at which point
    the measurement was measuring the measurer.

    So now: a weak reference with a callback that drops the id the moment
    the object dies. That is both correct AND free of the leak. The builtin
    containers (list, dict, tuple, set, str, int) do not support weak
    references, so those fall back to a bounded ring of strong refs -- past
    KEEP_ALIVE of them the oldest are released and a recycled id may merge
    two objects. That is a bounded, documented inaccuracy rather than an
    unbounded leak.
    """

    def __init__(self, writer, keep_alive=KEEP_ALIVE):
        self.writer = writer
        self._by_id = {}        # id(value) -> obj_id
        self._typeof = {}       # obj_id -> type name (to catch id reuse)
        self._fingerprint = {}  # obj_id -> last stored fingerprint
        self._next_id = 1
        self._weak = {}         # id(value) -> weakref, for types that allow it
        self._strong = deque(maxlen=keep_alive)   # bounded fallback
        self._deep_count = {}   # obj_id -> how often we walked its contents
        self._cheap_fp = {}     # obj_id -> last cheap summary, as json
        self._summary = {}      # obj_id -> last FULL summary (cheap + rich)
        self._rich_count = {}   # obj_id -> how often we paid for rich stats
        self._walk = 0          # remaining child-encode budget this call
        self.last_changed = False  # did the last encode() see new contents?
        self.truncated = 0      # how often a budget cut a walk short

    def _remember(self, value, key):
        """Notice when `value` dies, so its id can be safely recycled."""
        try:
            self._weak[key] = weakref.ref(value, lambda _r, k=key:
                                          self._forget(k))
        except TypeError:
            # list / dict / tuple / set / str / int cannot be weak-referenced.
            self._strong.append(value)

    def _forget(self, key):
        self._weak.pop(key, None)
        obj_id = self._by_id.pop(key, None)
        if obj_id is not None:
            self._typeof.pop(obj_id, None)
            self._fingerprint.pop(obj_id, None)
            self._deep_count.pop(obj_id, None)

    def encode(self, value, step, depth=0, force=False):
        """Return a packed vref (rk, ri, rf) for `value`.

        Side effect: sets self.last_changed, which the recorder uses to
        notice that a list was mutated in place.
        """
        if depth == 0:
            self.last_changed = False
            # A line statically identified as mutating this value gets a
            # bigger budget -- seeing the change is the point. But only
            # bigger: an unbounded re-walk of a list that grows inside a
            # loop tripled the cost per step.
            self._walk = MAX_WALK * 2 if force else MAX_WALK

        t = type(value)
        # bool before int: bool IS a subclass of int in Python.
        if t is bool:
            return (schema.RK_BOOL, 1 if value else 0, 0.0)
        if t is int:
            # The on-disk int slot is a signed 64-bit field. Python ints are
            # unbounded, so anything bigger is kept as text.
            if -9223372036854775808 <= value <= 9223372036854775807:
                return (schema.RK_INT, value, 0.0)
            return (schema.RK_STR, self.writer.string_id(str(value)[:MAX_STR]),
                    0.0)
        if t is float:
            return (schema.RK_FLOAT, 0, value)
        if value is None:
            return (schema.RK_NULL, 0, 0.0)
        if t is str:
            text = value if len(value) <= MAX_STR else value[:MAX_STR] + "…"
            return (schema.RK_STR, self.writer.string_id(text), 0.0)
        if t is bytes:
            text = value[:MAX_STR].decode("utf8", "replace")
            return (schema.RK_STR, self.writer.string_id(text), 0.0)

        key = id(value)
        tname = type(value).__name__
        obj_id = self._by_id.get(key)
        if obj_id is not None and self._typeof.get(obj_id) != tname:
            obj_id = None                          # id was recycled
        if obj_id is None:
            obj_id = self._next_id
            self._next_id += 1
            self._by_id[key] = obj_id
            self._typeof[obj_id] = tname
            self._remember(value, key)

        summary, kind = self._describe(obj_id, value, tname)
        if kind == domains.GENERIC:
            label = _short_repr(value)
        else:
            # A DataFrame or tensor repr formats the whole object. The
            # summary already holds shape and dtype, so the label comes from
            # there and the data is never touched.
            label = _label_for(summary, tname)

        children = None
        if kind != domains.GENERIC:
            # Shape and dtypes already say everything useful; walking the
            # rows would be both slow and unreadable. Still goes through the
            # fingerprint gate, or a DataFrame that sits unchanged across a
            # thousand lines would write a thousand identical rows.
            payload = dumps(summary)
            if self._fingerprint.get(obj_id) != payload:
                self._fingerprint[obj_id] = payload
                self.last_changed = True
                self.writer.object_version(obj_id, tname, step, summary,
                                           None, label,
                                           summary.get("nbytes"))
            return (schema.RK_OBJ, obj_id, 0.0)
        seen = self._deep_count.get(obj_id, 0)
        budget = DEEP_BUDGET * 3 if force else DEEP_BUDGET
        if depth < MAX_DEPTH and self._walk > 0 and seen < budget:
            self._deep_count[obj_id] = seen + 1
            children = self._children(value, step, depth)
        elif depth < MAX_DEPTH:
            self.truncated += 1
            summary["contents_omitted"] = True

        payload = dumps([summary, children])
        if self._fingerprint.get(obj_id) != payload:
            self._fingerprint[obj_id] = payload
            self.last_changed = True
            self.writer.object_version(obj_id, tname, step,
                                       summary, children, label,
                                       summary.get("nbytes"))
        return (schema.RK_OBJ, obj_id, 0.0)

    def _describe(self, obj_id, value, tname):
        """Summarise `value`, paying for the expensive stats only when the
        cheap ones moved.

        `df.isna().sum()` is O(rows). Called once per line in a loop over a
        3-million-row frame it is minutes; called only when the shape or
        dtypes actually changed it is nothing. That gate is the reason a
        real pandas script is traceable at all.
        """
        cheap_fn, rich_fn, kind = domains.lookup(value)
        try:
            summary = cheap_fn(value)
        except Exception as exc:
            return {"type": tname, "kind": domains.GENERIC,
                    "summary_error": type(exc).__name__}, domains.GENERIC

        if rich_fn is None:
            return summary, kind

        fp = dumps(summary)
        if self._cheap_fp.get(obj_id) == fp:
            # Nothing observable changed: reuse what we already paid for.
            return self._summary.get(obj_id, summary), kind
        self._cheap_fp[obj_id] = fp

        seen = self._rich_count.get(obj_id, 0)
        if seen < domains.RICH_BUDGET:
            self._rich_count[obj_id] = seen + 1
            try:
                summary.update(rich_fn(value))
            except Exception:
                pass
        else:
            summary["stats_omitted"] = True
        self._summary[obj_id] = summary
        return summary, kind

    def _children(self, value, step, depth):
        """[(key, ref), ...] for containers; None for everything else."""
        try:
            if isinstance(value, dict):
                items = list(value.items())[:MAX_CHILDREN]
            elif isinstance(value, _SEQ):
                items = list(enumerate(list(value)[:MAX_CHILDREN]))
            elif isinstance(value, bytearray):
                items = list(enumerate(value[:MAX_CHILDREN]))
            else:
                # A plain object: show its __dict__, which is how a person
                # thinks about "the fields of this thing".
                d = getattr(value, "__dict__", None)
                if not isinstance(d, dict):
                    return None
                items = [(str(k), v) for k, v in
                         list(d.items())[:MAX_CHILDREN]]
        except Exception:
            return None

        out = []
        for key, child in items:
            if self._walk <= 0:
                self.truncated += 1
                break
            self._walk -= 1
            out.append([key if isinstance(key, (int, str))
                        else _short_repr(key),
                        self.encode(child, step, depth + 1)])
        return out
