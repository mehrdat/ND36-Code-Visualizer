"""
Domain plugins: how to describe a DataFrame, an array, a tensor, a model.

The rule from Phase 0 still holds -- summarise, never snapshot. A
2-million-row DataFrame becomes about a dozen numbers. What Phase 4 adds is
that the dozen numbers are the RIGHT ones for the type, and that getting
them does not cost O(rows) on every line.

Two tiers per domain, and the split is the whole trick:

  cheap(value)   O(1), or O(columns). Shape, dtypes, device, requires_grad.
                 Safe to call on every line of a tight loop.
  rich(value)    May be O(rows): null counts, min/max, deep memory. Called
                 only when the cheap facts changed, and only so many times
                 per object.

`pandas.DataFrame.isna().sum()` on a 3-million-row frame takes ~40 ms. Call
that once per line and a 10-second script never finishes. Called only when
the shape or dtypes actually moved, it is free.

Nothing here imports pandas, numpy or torch. A domain is chosen by
`type(value).__module__`, so a script that never imports torch never pays
for torch.
"""

#: obj kinds the report knows how to render specially
FRAME = "frame"
ARRAY = "array"
TENSOR = "tensor"
MODEL = "model"
GENERIC = "generic"

#: How many times one object may have its expensive stats recomputed.
RICH_BUDGET = 400

#: Above this many rows, `memory_usage(deep=True)` walks every Python string
#: object and becomes the single most expensive thing in the trace. Past it
#: we take the shallow number and say so.
DEEP_ROWS = 100_000

_REGISTRY = []          # [(predicate, cheap, rich, kind)]


def register(predicate, cheap, rich=None, kind=GENERIC):
    """Add a domain. `predicate(module, typename, value) -> bool`."""
    _REGISTRY.append((predicate, cheap, rich, kind))


def lookup(value):
    """-> (cheap, rich, kind). Imports nothing that is not already loaded."""
    t = type(value)
    module = (t.__module__ or "").split(".")[0]
    name = t.__name__
    for predicate, cheap, rich, kind in _REGISTRY:
        try:
            if predicate(module, name, value):
                return cheap, rich, kind
        except Exception:
            continue
    return _generic_cheap, None, GENERIC


def _num(v):
    """Numpy and torch scalars are not JSON-safe; make them so, quietly.

    A torch tensor still attached to the graph warns when cast to a float,
    so detach first -- we only ever want the number.
    """
    try:
        detach = getattr(v, "detach", None)
        if detach is not None:
            v = detach()
        f = float(v)
    except Exception:
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return str(f)
    return f


# --------------------------------------------------------------------------
# generic
# --------------------------------------------------------------------------

def _generic_cheap(value):
    out = {"type": type(value).__name__, "kind": GENERIC}
    try:
        out["len"] = len(value)
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------
# numpy
# --------------------------------------------------------------------------

def _array_cheap(value):
    return {"type": "ndarray", "kind": ARRAY,
            "shape": tuple(int(d) for d in value.shape),
            "dtype": str(value.dtype),
            "nbytes": int(value.nbytes),
            "size": int(value.size),
            # `base is not None` means this array is a VIEW of another one.
            # Silent view-vs-copy confusion is the classic numpy bug, and it
            # costs nothing to surface.
            "is_view": value.base is not None,
            "c_contiguous": bool(value.flags["C_CONTIGUOUS"])}


def _array_rich(value):
    out = {}
    try:
        if value.size and value.dtype.kind in "fiub":
            out["min"] = _num(value.min())
            out["max"] = _num(value.max())
            out["mean"] = _num(value.mean()) if value.dtype.kind != "b" \
                else None
        if value.dtype.kind == "f":
            import numpy
            out["n_nan"] = int(numpy.isnan(value).sum())
            out["n_inf"] = int(numpy.isinf(value).sum())
    except Exception:
        pass
    return {k: v for k, v in out.items() if v is not None}


# --------------------------------------------------------------------------
# pandas / polars
# --------------------------------------------------------------------------

def _frame_cheap(value):
    """Shape, columns and dtypes only -- O(columns), never O(rows)."""
    try:
        cols = [str(c) for c in value.columns]
    except Exception:
        cols = []
    out = {"type": type(value).__name__, "kind": FRAME,
           "shape": tuple(int(d) for d in value.shape),
           "columns": cols[:200]}
    try:
        out["dtypes"] = {str(k): str(v)
                         for k, v in list(value.dtypes.items())[:200]}
    except Exception:
        try:                                  # polars: dtypes is a list
            out["dtypes"] = {c: str(d) for c, d
                             in zip(cols, value.dtypes)}
        except Exception:
            pass
    return out


def _frame_rich(value):
    out = {}
    try:
        rows = int(value.shape[0])
    except Exception:
        rows = 0
    try:
        # deep=True counts the actual string bytes rather than the pointers,
        # but it visits every object to do it.
        deep = rows <= DEEP_ROWS
        out["nbytes"] = int(value.memory_usage(deep=deep).sum())
        if not deep:
            out["nbytes_shallow"] = True
    except Exception:
        try:
            out["nbytes"] = int(value.estimated_size())     # polars
        except Exception:
            pass
    try:
        nulls = {str(k): int(v) for k, v in value.isna().sum().items() if v}
        out["nulls"] = dict(list(nulls.items())[:50])
        out["n_nulls"] = sum(nulls.values())
    except Exception:
        try:
            counts = value.null_count().row(0)               # polars
            nulls = {c: int(n) for c, n in zip(value.columns, counts) if n}
            out["nulls"] = dict(list(nulls.items())[:50])
            out["n_nulls"] = sum(nulls.values())
        except Exception:
            pass
    try:
        out["index"] = type(value.index).__name__
    except Exception:
        pass
    return out


def _series_cheap(value):
    return {"type": type(value).__name__, "kind": ARRAY,
            "shape": (int(value.shape[0]),),
            "len": int(value.shape[0]),
            "dtype": str(value.dtype)}


def _series_rich(value):
    out = {}
    try:
        out["n_nulls"] = int(value.isna().sum())
    except Exception:
        pass
    try:
        if str(value.dtype).startswith(("int", "float", "uint")):
            out["min"] = _num(value.min())
            out["max"] = _num(value.max())
    except Exception:
        pass
    try:
        out["nbytes"] = int(value.memory_usage(deep=True))
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------
# torch
# --------------------------------------------------------------------------

def _tensor_cheap(value):
    out = {"type": "Tensor", "kind": TENSOR,
           "shape": tuple(int(d) for d in value.shape),
           "dtype": str(value.dtype).replace("torch.", ""),
           "device": str(value.device),
           "requires_grad": bool(value.requires_grad),
           "nbytes": int(value.element_size() * value.nelement())}
    try:
        if value.grad_fn is not None:
            # The op that produced this tensor -- the autograd breadcrumb.
            out["grad_fn"] = type(value.grad_fn).__name__
    except Exception:
        pass
    return out


def _tensor_rich(value):
    out = {}
    try:
        import torch
        if value.numel() == 0 or value.device.type != "cpu":
            return out            # never pull a GPU tensor across the bus
        if value.dtype.is_floating_point:
            out["n_nan"] = int(torch.isnan(value).sum())
            out["n_inf"] = int(torch.isinf(value).sum())
            out["mean"] = _num(value.mean())
        if value.dtype.is_floating_point or not value.dtype.is_complex:
            out["min"] = _num(value.min())
            out["max"] = _num(value.max())
    except Exception:
        pass
    return {k: v for k, v in out.items() if v is not None}


def _module_cheap(value):
    out = {"type": type(value).__name__, "kind": MODEL}
    try:
        params = list(value.parameters())
        out["n_params"] = int(sum(p.numel() for p in params))
        out["n_trainable"] = int(sum(p.numel() for p in params
                                     if p.requires_grad))
        out["training"] = bool(value.training)
        if params:
            out["device"] = str(params[0].device)
        out["layers"] = [name for name, _m in list(value.named_children())][:30]
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------
# sklearn
# --------------------------------------------------------------------------

def _estimator_cheap(value):
    out = {"type": type(value).__name__, "kind": MODEL}
    try:
        # sklearn's convention: attributes ending in _ exist only after fit.
        out["fitted"] = any(k.endswith("_") and not k.startswith("_")
                            for k in vars(value))
    except Exception:
        pass
    try:
        params = value.get_params(deep=False)
        out["params"] = {k: str(v)[:40] for k, v in
                         list(params.items())[:12]}
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------
# Registration. Order matters: the first predicate that matches wins.
# --------------------------------------------------------------------------

def _is_torch_module(value):
    """True for an nn.Module.

    A model class is defined in the USER's module, so its __module__ is
    "__main__", not "torch" -- name matching cannot find it. isinstance can,
    but only if torch is already imported. sys.modules.get avoids importing
    it ourselves: a script that never uses torch never pays.
    """
    torch = _sys.modules.get("torch")
    if torch is None:
        return False
    try:
        return isinstance(value, torch.nn.Module)
    except Exception:
        return False


import sys as _sys                                           # noqa: E402

register(lambda m, n, v: m == "numpy" and n == "ndarray",
         _array_cheap, _array_rich, ARRAY)
register(lambda m, n, v: m in ("pandas", "polars", "modin", "cudf")
         and n == "DataFrame", _frame_cheap, _frame_rich, FRAME)
register(lambda m, n, v: m in ("pandas", "polars", "modin", "cudf")
         and n == "Series", _series_cheap, _series_rich, ARRAY)
register(lambda m, n, v: m == "torch" and n in ("Tensor", "Parameter"),
         _tensor_cheap, _tensor_rich, TENSOR)
register(lambda m, n, v: _is_torch_module(v), _module_cheap, None, MODEL)
register(lambda m, n, v: m == "sklearn", _estimator_cheap, None, MODEL)
