# ND36

**Record what a program did, then replay it.** Python, C and C++.

| | |
|---|---|
| New here | **`QUICKSTART.md`** — one page |
| Every command and option | **`COMMANDS.md`** |
| Installing on a system (brew, pacman) | **`packaging/README.md`** |
| Something not working | run **`nd doctor`** |

A recording debugger. Run your code once; it writes down everything that
happened. Then replay it like a video — scrub backwards and forwards, and see
what every variable held at every step.

**Phase 0** built the trace schema and the recorder. **Phase 2** added the
things real programs actually do — `a[i] = x`, `self.total += 1`,
`xs.append(v)` — plus the control-flow and call-graph overlays that say which
code never ran, and an HTML dashboard. **Phase 3** added memory: a curve
under the step slider, per-line attribution, and a Perfetto export.
**Phase 4** added the domain plugins — DataFrame lineage, tensor shapes,
and shape-mismatch diagnosis. **Phase 5** added C and C++, driven by a real
debugger. **Phase 1** added the live UI, which lifts the static report's step
ceiling. **Phase 6** added the shells: a desktop window and a VS Code panel.
See `PLAN.md` for the full roadmap and what was deliberately deferred.

Every label says **`step 1,204 · sort.py:14`**, never a bare step number. A
step index on its own means nothing.

```bash
nd serve tests/programs/sort.py   # ← the live UI
nd run   tests/programs/sort.py -o sort.nd
nd info  sort.nd
nd step  sort.nd 60
nd watch sort.nd j
nd heat  sort.nd
nd flow   sort.nd          # call graph + dead code
nd report sort.nd          # the HTML dashboard
nd run    tests/programs/leaky.py --memory -o lk.nd
nd mem    lk.nd             # the memory curve + the leak
nd export lk.nd             # open at ui.perfetto.dev
nd bench  tests/programs/heavy.py

nd run     tests/programs/etl.py -o etl.nd
nd data    etl.nd            # the DataFrame ribbon
nd tensors sh.nd             # shapes + mismatches

nd run  tests/programs/native/linked_list.c -o l.nd
nd watch l.nd head          # follow the pointers

for p in 0 1 2 3 4 5 6; do python3 tests/test_phase$p.py; done
```

Standard library only, except that pandas / numpy / torch panels appear when
your script already uses them. Nothing here imports them.

No dependencies. Standard library only.

---

## Three front ends, one UI

```bash
nd app    script.py      # its own window — the one to use
nd serve  script.py      # same thing, in a browser tab
nd report trace.nd     # one HTML file you can email
```

Plus the VS Code panel (`vscode-extension/`), which starts the same server
and frames it beside your editor.

**The desktop shell is not Tauri.** The plan said it would be. Tauri means a
Rust toolchain, a build step and codesigning for what is, in the end, a
window with no address bar — and every Chromium-family browser already ships
that window behind `--app=`. So `nd app` opens one, and `nd install-app`
writes a real macOS `.app` bundle (an `Info.plist` and a shell script, which
is genuinely all a macOS app needs) that launches it. Nothing to install, no
build step, and if no such browser is present it falls back to an ordinary
tab rather than failing. A test asserts neither shell contains a reader or a
recorder of its own, so the three front ends cannot drift apart.

| | `serve` | `report` |
|---|---|---|
| Steps reachable | **all of them** | ~20,000 embedded |
| Seek on a 280k-step trace | **2.4 ms** median | n/a — won't fit |
| Needs the trace file present | yes | no, it is the file |
| Re-run the script from the page | yes | no |
| Send it to someone | no | yes |

The report embeds the trace in the page, which is what caps it. The server
sends the one step you are looking at, so the ceiling disappears: a
quarter-million step run drags exactly as smoothly as a three-hundred step
one. A small cache and a prefetch either side of the cursor keep it at 60fps.

`http.server` on a background thread, bound to 127.0.0.1. No framework —
one local reader does not need one, and it would have been the project's
first dependency.

Click a source line to jump to the next time it ran; click a row in the
DataFrame or tensor tables to jump to that step; type a variable name to get
its whole history and click any assignment to go there.

---

## The dashboard

`nd report trace.nd` writes one self-contained HTML file — no server, no
npm, no CDN, nothing fetched at runtime. Open it, keep it, email it.

| Panel | What it shows |
|---|---|
| **Step explorer** | a slider over the whole run. Code on the left with the current line marked; every variable in scope on the right, with the one that just changed highlighted and its previous value underneath |
| **Hot lines** | milliseconds per source line, longest first |
| **Variable timelines** | every numeric variable plotted over the run; click a point to jump the slider there |
| **Call graph** | who called whom, edge width = call count, grey dashed = never ran |
| **Control flow** | basic blocks per function, with the branches that never executed in grey. The tab badge is executed blocks / total |
| **Execution heat** | the whole file with hit count and ms per line |
| **In-place changes** | every mutation, with file and line |
| **Memory** | peak, held-at-the-end, the heap curve, and the lines still holding bytes |
| **DataFrames** | every step a frame changed, with a row-count bar and a flag when >30% of the rows vanish |
| **Tensors** | shapes through the run, and the mismatch panel when it dies on one |
| **Biggest objects** | the largest any single object got, by its own reckoning |

Under the slider sits a **memory strip** aligned to steps. Click a spike and
the explorer jumps to the line that caused it.

Light and dark are both designed, not an automatic flip.

---

## C and C++

`nd run prog.c` compiles it and steps it under a real debugger — LLDB on
macOS, GDB elsewhere. The recorder is new; **nothing else is**. The trace it
writes uses the same schema as the Python one, so `step`, `watch`, `heat`,
`flow` and the whole dashboard work on a C program without a line of change.
That was the entire point of spending Phase 0 on the schema.

```
$ nd run linked_list.c -o l.nd
compiled prog with clang
l.nd  63 steps  24 source lines  0.25s  (gdb)

$ nd watch l.nd head
step   where            how     head
3      linked_list.c:21 BIND    0x7fffffff9420 -> {value = 1431654496, next = …}
7      linked_list.c:22 MUTATE  0x0
19     linked_list.c:8  MUTATE  0x5555555592a0 -> {value = 10, next = 0x0}
27     linked_list.c:8  MUTATE  0x5555555592c0 -> {value = 20, next = 0x5555555592a0}
35     linked_list.c:8  MUTATE  0x5555555592e0 -> {value = 30, next = 0x5555555592c0}
44     linked_list.c:23 MUTATE  0x555555559300 -> {value = 40, next = 0x5555555592e0}
```

You can read the chain being built. A pointer prints as a bare address, which
tells you nothing, so each one is followed a level — but only when it has
changed, because every follow is another round trip to the debugger.

C++ containers come out of the debugger's own pretty-printers:

```
$ nd watch v.nd xs
13  vector_grow.cpp:10  MUTATE  'std::vector of length 1, capacity 1 = {1}'
19  vector_grow.cpp:10  MUTATE  'std::vector of length 2, capacity 2 = {1, 4}'
25  vector_grow.cpp:10  MUTATE  'std::vector of length 3, capacity 4 = {1, 4, 9}'
```

### Three things that had to be got right

**1. Step over, not step into.** `step` descends into anything that has line
info — and glibc on this machine has line info. One `malloc()` walked the
tracer through `ptmalloc_init`, `tcache_key_initialize` and `getrandom`, and
the frames the program actually cared about never came back. The fix is what
real debuggers do: step *over*, with a breakpoint on each function we have
source for. Only your code is entered, and it is three times faster.

**2. Depth alone cannot find a call.** `for (...) { head = push(head, i); }`
is one line, so four calls to `push` happen without the stack ever being
observed back at the caller — all four collapsed into one frame, and the call
graph said `push ×1`. The breakpoint hit is the reliable signal that a
function was entered. (The same shape of bug as the `sys.monitoring`
same-line loop in Phase 0.)

**3. `-O1` and above are refused, not warned about.** The compiler reorders
and removes code, so line numbers and variables stop corresponding to the
source. A trace of an optimised build is a confident lie, so `nd` declines
to make one.

### Honest limits

- **About a millisecond per source line.** Algorithm-sized programs are
  comfortable; a million-line trace is not. `--max-lines` caps it and the
  output says when the cap was hit.
- **Control-flow graphs are Python-only.** They are built from the Python
  AST; C would need clang. The dashboard says so rather than showing an
  empty panel. The call graph and heat map work on both, because they come
  from the trace rather than from parsing.
- **A variable is in scope from the opening brace**, so before its first
  assignment it holds whatever was on the stack. That garbage is real — it
  is what C does — and it is shown as-is rather than hidden.
- `-g3 -O0 -fno-omit-frame-pointer` is added for you. One translation unit
  at a time; multi-file projects are not wired up yet.

---

## Following a DataFrame

`etl.py` has one planted bug: the raw data says `"north "` with a trailing
space, the filter says `"north"`. The run succeeds and still prints a number.

```
$ nd data etl.nd
where        name      shape     what changed
etl.py:16    raw       4000x4    created
etl.py:25    clean     3820x4    from raw: -180 rows
etl.py:26    clean     3820x5    +col total
etl.py:27    clean     3820x5    qty: int64 → float64
etl.py:30    north     0x5       from clean: -3820 rows   ← big row loss
etl.py:32    summary   0x2       from north: -col order_id, -col amount

most rows lost here  etl.py:30
    29  # THE BUG: the data says "north ", this says "north".
  ▸ 30  north = clean[clean["region"] == "north"]
```

Two things make this work:

1. **Only metadata is recorded.** Shape, columns, dtypes, null counts — never
   a row of data. A 2-million-row frame is about a dozen numbers.
2. **Derived frames are linked.** Data-science code writes `b = f(a)`, so `b`
   is a brand new object with no previous version to diff against. A ribbon
   that only compared an object with itself would print "created, 0 rows" at
   exactly the moment the rows vanished. So a new frame's parent is whichever
   frame variable its line *reads*, found from the AST.

---

## Following a tensor

```
$ nd tensors sh.nd
where           name   shape             produced by
shapes.py:31    batch  (8, 1, 20, 20)
shapes.py:22    h      (8, 16, 18, 18)   ReluBackward0
shapes.py:23    h      (8, 16, 9, 9)     MaxPool2DWithIndicesBackward0
shapes.py:24    h      (8, 32, 7, 7)     ReluBackward0
shapes.py:25    flat   (8, 1568)         ViewBackward0

shape mismatch  shapes.py:26  RuntimeError
  mat1 and mat2 shapes cannot be multiplied (8x1568 and 800x10)
  ▸ return self.fc(flat)

  live tensors at that moment
    flat      (8, 1568)
    h         (8, 32, 7, 7)
    batch     (8, 1, 20, 20)
```

PyTorch's message names two matrices and nothing else — not the variable,
not the line, not what the shape was three operations ago. All three are in
the trace already.

---

## Why a DataFrame does not cost O(rows) per line

Every domain has two tiers:

| | what it computes | cost | when |
|---|---|---|---|
| `cheap` | shape, columns, dtypes, device, requires_grad | O(columns) | every line |
| `rich` | null counts, min/max, deep memory | O(rows) | only when the cheap facts moved |

`df.isna().sum()` on a 3-million-row frame takes ~40 ms. Called once per line
in a loop it never finishes; called only when the shape or dtypes actually
changed it is free. A test asserts that 40 encodes of an unchanged frame cost
exactly one expensive call.

Above 100,000 rows `memory_usage(deep=True)` is skipped too — it walks every
Python string object. That one change took a 200k-row pipeline from 4.2x to
**2.1x** slower than untraced.

---

## Finding a leak

`leaky.py` has a cache that is never cleared, sitting next to a function that
allocates exactly as much and gives it all back. The second one must not be
accused:

```
$ nd run tests/programs/leaky.py --memory -o lk.nd
$ nd mem lk.nd
peak             6.0 MB
samples          86  (from the separate memory pass)

    ▁▁▁▁▁▂▂▂▂▂▂▃▃▃▃▃▄▄▄▄▄▅▅▅▅▅▆▆▆▆▆▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇█  python heap
11 KB  →  6.0 MB

still held at the end, biggest growth first
where                            held     growth
leaky.py:10                    5.8 MB    +5.8 MB
leaky.py:8                      66 KB     +66 KB

likely leak  leaky.py:10
     9      if key not in CACHE:
  ▸ 10          CACHE[key] = [n] * 500      # LEAK
    11      return CACHE[key][0]
```

`tidy()`, which allocates the same 500-element list 4,000 times and frees
every one, does not appear.

---

## Why memory is measured in a second run

tracemalloc records a stack for every allocation — including the thousands
the recorder makes each second. Measured on the same program:

| | cost per step |
|---|---|
| recorder alone | 23 µs |
| recorder + tracemalloc together | **1,245 µs** |
| recorder, then tracemalloc separately | 23 µs + one extra run |

Fifty times slower, and most of it spent measuring ourselves. So `--memory`
runs the script a second time under tracemalloc and nothing else, sampled by
a background thread so the program carries no instrumentation at all. This is
exactly what the determinism manifest (seed, argv, cwd, version) is for: the
second run is the same run.

Two curves come out of it:

- **allocated blocks**, sampled during the trace and aligned to **steps** —
  that is the strip under the slider, and clicking a spike jumps to the line
  that caused it. Costs two counter reads, so it is always on.
- **python heap bytes**, exact, from the second pass. Needs `--memory`.

---

## What `watch` does, and why it matters

A normal debugger shows you **one moment, live**. This shows **all moments**,
after the fact. So you can ask for a variable's entire life:

```
$ nd watch sort.nd j
step       where              how     j
15         sort.py:11         BIND    0
20         sort.py:14         BIND    -1  (was 0)
29         sort.py:11         BIND    1   (was -1)
37         sort.py:11         BIND    2   (was 1)
45         sort.py:11         BIND    3   (was 2)
```

and `flow` tells you what never ran:

```
$ nd flow branch.nd
call graph
  <module>          → classify          5 calls
never called  unused_helper

control flow -- lines that never ran
  <module>
    branch.py:19   total = -1
  classify
    branch.py:3    return "negative"
```

No breakpoint, no re-running, no print statements. That is the whole point of
recording instead of stepping.

---

## Where it stands

Measured on this machine, Python 3.13:

| Program | What it is | Steps | Cost per step | `state_at()` |
|---|---|---|---|---|
| `sort.py` | insertion sort, 12 items | 284 | 28 µs | 0.11 ms |
| `mixed.py` | classes, recursion, exceptions | 265 | 19 µs | 0.37 ms |
| `realistic.py` | parse → clean → group, 3,000 rows | 73,145 | 26 µs | 0.48 ms |
| `heavy.py` | tight arithmetic loop (worst case) | 279,991 | 3.1 µs | 0.83 ms |

Mutation tracking costs about a third more per step than Phase 0 did. That is
the price of seeing `a[i] = x`, and it is worth it.

For comparison, Python Tutor — the tool whose visual language we are
borrowing — caps at **1,000 steps** for Python and about **300 for C/C++**,
and refuses imports. `heavy.py` alone is 280× past that ceiling.

`cost per step` is the honest number. "Slowdown ×" depends entirely on how
much work the script's lines do, so it flatters or damns a program for
reasons that have nothing to do with the recorder.

---

## How it works

```
your script
    │
    ▼
sys.monitoring  (PEP 669, Python 3.12+)        ← 0.23 µs per callback
    │  LINE / CALL / RETURN / JUMP / RAISE
    ▼
Core            diff only the names the file can bind (static AST pre-pass)
    │
    ▼
events.bin      fixed-width 42-byte records; step N is at byte N × 42
trace.db        SQLite: source, locations, functions, object versions,
                keyframes, name index
```

### Why not store a snapshot per step

That is exactly why Python Tutor stops at 1,000 steps. We store **only what
changed**, plus a full **keyframe** every 2,000 steps. Answering "what did the
program look like at step 847,312?" means loading the nearest keyframe below
it and replaying a few hundred events forward — so seeking anywhere in a huge
trace stays under a millisecond.

### Why events are not in SQLite

Measured on a 280,000-event trace:

| Write path | Cost per event |
|---|---|
| SQLite `executemany` | 1.90 µs |
| `struct.pack` into a `bytearray` | **0.44 µs** |

The event log is append-only and addressed by step number, so the database
bought nothing and cost 4×. It also made seeking 18× slower, because a
fixed-width file needs no index at all: `offset = (step - 1) × 42`.

---

## Five bugs the tests caught

These are the ones worth remembering, because each would have silently
corrupted the output rather than crashing.

**1. `sys.monitoring`'s LINE event does not fire on a backward jump to the
same line.** So `while n < 3: n += 1` and list comprehensions looked like one
step instead of N:

```
while n < 3: n += 1

sys.settrace      lines: [3, 3, 3]      ← correct
sys.monitoring    lines: [3]            ← one event
```

Fixed by also watching `JUMP`. This one would have poisoned every loop in the
timeline.

**2. A function defined on line 1 wiped out the module's variables.** The
static write-set table was keyed by first line, and a module's first line is
also 1 — so `def classify(n):` on line 1 overwrote the module's entry and no
module-level variable was ever tracked. Now keyed by `(name, line)`.

**3. Hits were counted for the line being left, not entered.** Which made the
last line of every function look like it never executed, so the dead-code
report was confidently wrong. Time belongs to the line you leave; the hit
belongs to the line you enter. They are different lines.

**4. `RAISE` and `PY_UNWIND` cannot be disabled.** They are not "local
events" in PEP 669, so returning `DISABLE` from them raises `ValueError` and
tears the callback down mid-run.

**5. BIND vs MUTATE must be decided by identity, not by the fast key.**
`counts["x"] = 1` changes the dict's length, so the cheap `(id, len)` check
sees a difference and would call it a rebind. It is the same dict. Compare
object ids.

---

## Four things that cost real time before they were found

Each of these was measured, not guessed.

**1. `json.dumps()` rebuilds its encoder.** Called with non-default
arguments, `json.dumps` constructs a fresh `JSONEncoder` *every call*. Making
one encoder and reusing `.encode` was a 3× saving on the write path.

**2. `repr()` of a big list is O(n).** `repr()` on a 3,000-item list builds a
200 kB string, which we then truncate to 200 characters. Done once per loop
iteration, that was 11 of the 12 seconds in `realistic.py`. Long containers
now get a preview assembled by hand.

**3. Re-walking containers that keep changing.** A list that grows inside a
loop had its first 50 items re-encoded on every iteration. Two budgets fixed
it: 96 child-encodes per value, and 150 content recordings per object, after
which only its summary (length, shape, dtype) is kept.

**4. Forced re-walks after a mutation.** Marking a line as "this may mutate
`rows`" and then re-walking `rows` with no budget at all tripled the cost per
step. `force` now raises the budget rather than removing it.

---

## Correctness

The recorder is checked against an **independent second opinion**: a plain
`sys.settrace` hook that copies `repr()` of every local at every line and
shares no code with the recorder. Replaying our event log must reproduce it
exactly.

```
$ python3 tests/test_phase0.py
test_state_matches_ground_truth
  sort.py      169 lines,   912 variable values verified
  mixed.py     127 lines,   273 variable values verified
test_monitoring_matches_settrace
  sort.py      282 events identical across both drivers
test_keyframes_do_not_change_answers
  mixed.py     255 steps agree at keyframe spacing 1 / 7 / none
test_other_python_versions
  versions   py3.11 → Settrace, 282 steps | py3.12 → Monitoring | py3.13 → Monitoring
test_scales_past_python_tutor
  heavy.py   279,991 steps, 0.58 ms/seek, 53 bytes/step

7/7 passed
```

`test_keyframes_do_not_change_answers` is the one that matters most after the
ground-truth test. It runs the same program with keyframes every 1 step,
every 7 steps, and never, then asserts all three give identical answers at
every step. That catches the classic replay bug: state that happens to be
right only because a keyframe landed nearby.

---

## Files

| File | Lines | What it is |
|---|---|---|
| `schema.py` | ~90 | **The contract.** Event kinds, value-ref encoding, record layout. The C/C++ recorder in Phase 5 must emit exactly this. |
| `store.py` | ~400 | `TraceWriter` / `TraceReader`. `state_at()`, `history_of()`, `line_stats()`, `find()`. |
| `recorder.py` | ~290 | `Core` plus two drivers: `sys.monitoring` (fast) and `sys.settrace` (reference). |
| `values.py` | ~260 | Summarise, never snapshot. A 2M-row DataFrame becomes ~12 numbers. Plugin point for pandas / numpy / torch. |
| `writeset.py` | ~110 | Static AST pre-pass: which names can each function bind? 5–20× off the hot path. |
| `run.py` | ~90 | Runs the script, writes the determinism manifest (version, platform, seeds, argv, cwd). |
| `cli.py` | ~330 | `run` · `info` · `step` · `watch` · `heat` · `flow` · `report` · `bench` |
| `graphs.py` | ~300 | CFG with basic-block merging; call graph, dynamic ∪ static |
| `charts.py` | ~290 | SVG bars, line charts, layered graph layout. No plotting library. |
| `report.py` | ~660 | The HTML dashboard, including the JS replay engine |
| `memory.py` | ~190 | Cheap counters during the trace; tracemalloc in its own pass |
| `domains.py` | ~300 | The plugin registry: numpy, pandas, polars, torch, sklearn |
| `lineage.py` | ~230 | DataFrame ribbon, tensor shapes, shape-mismatch diagnosis |
| `native.py` | ~430 | C/C++: compile, drive GDB/MI or LLDB, emit the same schema |
| `native_probe.py` | ~130 | Runs inside the `lldb` binary's own interpreter |
| `server.py` | ~470 | `nd serve`: the JSON API and the live page |
| `ui.py` | ~250 | The styles, shared by the report and the server |
| `exporter.py` | ~100 | Chrome Trace / Perfetto output |

---

## How mutation tracking works

A static pass finds every line that could change an object without rebinding
a name — subscript and attribute assignment, `del a[i]`, the mutating method
names (`append`, `update`, `add_`, …), and any name handed to a function,
since the callee may change it.

Only on those lines do we pay to re-walk the object and compare. Everywhere
else the cheap `(id, len)` check still decides. That is what keeps the cost
at 26 µs/step instead of 200.

```
$ nd watch mutating.nd counts
step   where             how      counts
48     mutating.py:31    BIND     {}
50     mutating.py:32    MUTATE   {'x': 1}
52     mutating.py:33    MUTATE   {'x': 1, 'y': 2}
```

---

## Known limits

Written down deliberately, so none of them is a surprise later.

- **A mutation is found by the line, not by the write.** If a line is not one
  the static pass flagged — a change made through an alias created at runtime,
  say — it is still missed. True write-watching needs the object graph work in
  a later phase.
- **Deeply nested changes can be missed.** Containers are walked 3 levels
  deep and 50 items wide, with a budget. A change below that is not seen.
- **Object identity across steps is best-effort.** Keyed on `id()`, which
  CPython recycles after garbage collection. Phase 2 uses a `gc`-backed
  tracker.
- **One global step order for all threads.** Correct for single-threaded
  code, slightly lossy on true parallelism.
- **Only the traced file's own code is recorded.** Imported libraries run
  untraced, which is what makes real scripts affordable.
- **Still ~10× slower than it could be.** `heavy.py` costs 2.4 µs/step of
  which `sys.monitoring` is only 0.23 µs. The rest is our Python glue. A C
  extension is Phase 7, not now.

---

## Status

All six planned phases are done; 56 tests pass across them. `PLAN.md` records
what each phase delivered and what was deliberately deferred.
