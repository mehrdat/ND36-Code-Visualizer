"""
The trace schema.  This is the most important file in the project.

Everything else -- the Python recorder, the future C/C++ recorder, the UI --
agrees on what is written here.  Change it only by bumping SCHEMA_VERSION.

An EVENT is one flat row:

    step     int    monotonic counter. This is the timeline axis.
    t_ns     int    perf_counter_ns() when it happened.
    kind     str    one of the KIND_* strings below.
    frame    int    which activation (NOT which function) it belongs to.
    loc      int    id into the `locs` table -> (file, line, col0, col1).
    payload  dict   kind-specific extra data.

A VREF is how a value is pointed at.  On the wire to the UI it is one of

    ["i", <json scalar>]   immediate: int, float, str, bool, None
    ["o", <obj_id>]        object: look it up in the `objects` table

On disk it is a packed triple (rk, ri, rf) -- a kind byte plus an int slot
and a float slot -- because that is fixed-width, and fixed-width means
step N lives at byte N*RECSIZE and seeking needs no index at all.

Events live in events.bin, NOT in SQLite.  Measured on a 280,000-event
trace: SQLite executemany cost 1.90 us/row, struct.pack into a bytearray
cost 0.44 us/row.  Since the event log is append-only and addressed by
step, the database bought us nothing and cost us 4x.
"""

SCHEMA_VERSION = 3

# ---- event kinds -----------------------------------------------------------

LINE = "LINE"        # a source line is about to execute. payload: {}
CALL = "CALL"        # a frame was pushed.   payload: {func, args:{name:ref}}
RETURN = "RETURN"    # a frame returned.     payload: {value: ref}
YIELD = "YIELD"      # a generator yielded.  payload: {value: ref}
RESUME = "RESUME"    # a generator resumed.  payload: {}
BIND = "BIND"        # a name got a value.   name + vref
MUTATE = "MUTATE"    # the name points at the SAME object but its contents
                     # changed: a[i] = x, self.total += 1, xs.append(v).
                     # name + vref (the vref is the object, unchanged id)
DELETE = "DELETE"    # a name disappeared.   name
RAISE = "RAISE"      # an exception started. payload: {exc, msg}
UNWIND = "UNWIND"    # a frame died from an exception. payload: {}
IO = "IO"            # stdout/stderr text.   payload: {stream, text}

ALL_KINDS = (LINE, CALL, RETURN, YIELD, RESUME, BIND, MUTATE, DELETE,
             RAISE, UNWIND, IO)

# On disk a kind is one byte.
KIND_CODE = {k: i + 1 for i, k in enumerate(ALL_KINDS)}
CODE_KIND = {v: k for k, v in KIND_CODE.items()}

# ---- value ref kinds -------------------------------------------------------

RK_ABSENT = 0   # there is no value here
RK_INT = 1      # ri holds the int
RK_FLOAT = 2    # rf holds the float
RK_STR = 3      # ri holds an id into the strings table
RK_BOOL = 4     # ri is 0 or 1
RK_OBJ = 5      # ri holds an obj_id
RK_NULL = 6     # the value is Python None

ABSENT = (RK_ABSENT, 0, 0.0)

# The on-disk event record. Fixed width on purpose.
#   t_ns  kind frame loc  name rk  ri  rf  payload
RECORD = "<Q B I I I B q d I"

