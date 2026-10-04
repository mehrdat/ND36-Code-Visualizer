# Code Visualizer & Analyser — Research + Build Plan

**Project root:** `~/Desktop/claude_code/code_visualizer` (currently empty)
**Decisions locked in:** Python deep first → C/C++ phase 5. All three delivery shells (local web app, desktop app, VS Code extension). Audience = professionals across algorithms, data science, AI engineering, and real-code debugging.
**Date:** 2026-10-03

---

## 1. What we are building, in one paragraph

A **recording debugger with a visual timeline**. You give it a Python script (later C/C++), it runs it once under an instrumented tracer, and writes an *event log* of everything that happened: every line executed, every variable bound or mutated, every call and return, every allocation. The UI then lets you scrub that log backwards and forwards like a video, with the code, the call stack, the variables, the heap diagram, the control-flow graph and the memory curve all synchronised to the same step number.

The crucial distinction from a normal debugger: a debugger shows you **one moment, live**. This shows you **all moments, after the fact**. That is what makes "watch how `x` changes through the loop" and "which step broke the DataFrame" possible.

---

## 2. Prior art — what exists, and what to take from each

### 2.1 The directly comparable tools

| Tool | What it does | Mechanism | Scale limit | Licence | What to take |
|---|---|---|---|---|---|
| **Python Tutor** (pgbovine) | Step-through with stack + heap + pointer diagrams. Py, Java, JS, C, C++ | `bdb` / `sys.settrace`; full state snapshot per step | **1,000 steps** (Py/Java/JS), **~300 steps** (C/C++), few thousand chars of code, no imports, no files/network | MIT (verify per directory — the repo mixes vendored code) | The *visual language* of stack frames + heap objects + arrows. The best-understood diagram in the field. Also its trace JSON format as a starting reference. |
| **opt-cpp-backend** (JuezUN) | C/C++ backend for Python Tutor | **Hacked Valgrind 3.11** → `vg_to_opt_trace.py` | ~300 steps, Linux x86 | **GPL-2.0** | The idea that byte-accurate memory needs dynamic binary instrumentation. ⚠️ GPL + stale Valgrind fork = read it, don't link it. |
| **birdseye** (alexmojaki) | Records the value of **every sub-expression**, not just lines. Hover any part of an expression to see what it evaluated to | **AST instrumentation** | Per-function, loop iterations sampled | MIT | Expression-level granularity. This is the single best idea in Python tooling that nobody has combined with a timeline. |
| **snoop / heartrate** (same author) | Line-by-line log of variable changes; live-animated execution heatmap | `sys.settrace` | script-sized | MIT | The *variable-diff* presentation (`x = 5` → `x = 6` per line), and the "execution heat" gutter. |
| **Thonny** | Teaching IDE with AST-level stepping — steps *into expressions*, shows evaluation as substitution | Custom tracer + AST | script-sized | MIT | The "expression substitution" animation. Extremely good for teaching, genuinely novel UX. |
| **VizTracer** (gaogaotiantian) | Timeline trace of calls with args + return values; Perfetto UI front-end | C tracer, `sys.monitoring` on 3.12+ | **Millions of events** — claims **<1x overhead** typical, 3–4x on heavy recursion | Apache-2.0 | The *storage + timeline* approach that actually scales. Argument/return logging. Perfetto as a ready-made zoomable timeline. |
| **memray** (Bloomberg) | Every allocation, with native + Python stacks; flamegraph + live mode | `malloc` interposition + native unwinding | production-scale | Apache-2.0 | Memory-over-time + allocation attribution per source line. |
| **Scalene** | CPU + GPU + memory per line, with copy-volume detection | sampling | production-scale | Apache-2.0 | Per-line resource attribution — and the idea of reporting *why* a line is slow (e.g. hidden copies). |
| **Debug Visualizer** (hediet, VS Code) | Renders watched values as graphs/trees/tables during a debug session | Debug Adapter Protocol | live only | MIT | The *pluggable visualiser* pattern: a value → a chooser → a renderer. Copy this plugin architecture wholesale. |
| **rr** (Mozilla) | Deterministic record & replay, **reverse step**, reverse watchpoints | ptrace + perf counters | whole programs | MIT | Reverse execution. Linux x86-64 only, so phase 7, not phase 1. |
| **Pernosco** | Commercial: omniscient debugging UI over rr — dataflow back in time ("where did this value come from?") | rr + indexed database | whole programs | proprietary | The **query model**: not "step until I see it" but "show me every write to this address". This is the aspiration for the dataflow panel. |
| **Sourcetrail** | Interactive code map, C++/Python cross-references | clang + custom indexer, SQLite graph DB | large codebases | GPL-3.0, **unmaintained** | Its SQLite graph schema lives on via **Numbat** (Quarkslab) — a Python API to write arbitrary code graphs into that format. Useful if we ever want an interchange format. |
| **Joern** | Code Property Graph (AST + CFG + dataflow in one queryable graph) for C/C++/Python | custom frontends, Scala | large codebases | Apache-2.0 | The **CPG concept**: one graph that merges syntax, control flow and data flow. Our static layer should be shaped like this. |
| **torchlens / torchview / Netron** | Model graph + per-layer activations and shapes | module hooks / `torch.fx` / file parse | model-sized | MIT / MIT / MIT | For the AI-engineer panel: tensor shape propagation and the forward-pass graph. torchlens already extracts hidden activations — don't rebuild it, wrap it. |

### 2.2 The honest conclusion from the survey

Two clusters exist, and **nothing bridges them**:

```
  CLUSTER A — "educational"             CLUSTER B — "professional"
  Python Tutor, Thonny, birdseye        VizTracer, memray, Scalene, rr
  ───────────────────────────────       ──────────────────────────────
  + beautiful state diagrams            + scales to millions of events
  + shows every variable               + low overhead
  + step backwards                      + production-grade
  − caps at ~1,000 steps                − shows aggregates, not state
  − toy programs only                   − no variable values over time
  − no real libraries                   − no heap/pointer diagrams
                                        − no step-by-step scrub
```

**The gap this project fills: Cluster A's state visibility at Cluster B's scale.** That is the whole thesis, and it is also precisely where the engineering difficulty lives.

---

## 3. The four hard problems (solve these or the project fails)

**P1 — State explosion.** Python Tutor caps at 1,000 steps because it stores a *full snapshot of everything* at every step. A real script does 10⁶–10⁹ steps. Snapshots are impossible.
→ **Solution:** event-sourced delta log + periodic keyframes. Store *changes*, reconstruct state by replaying from the nearest keyframe. (This is exactly rr's checkpoint strategy and Pernosco's index.)

**P2 — Large values.** A pandas DataFrame or a `[512, 768]` tensor cannot be snapshotted every line.
→ **Solution:** a **summariser registry**. Each type gets a summary function returning cheap metadata (shape, dtype, nbytes, nulls, min/max, a content fingerprint). Full materialisation only on explicit user request, by re-running to that step.

**P3 — Overhead.** A per-line Python tracer that also diffs locals is the slowest thing you can write.
→ **Solution:** `sys.monitoring` (3.12+) with per-code-object `DISABLE`, a scope filter (trace only *your* files, not site-packages), adaptive sampling in hot loops, and a tiered mode selector.

**P4 — Visual legibility.** 10⁶ steps and a 500-node call graph will produce an unreadable mess by default.
→ **Solution:** aggressive aggregation by default (collapse loop iterations, collapse library frames, show "the 5 variables that actually changed"), with drill-down on demand. Design for the *zoomed-out* view first.

---

## 4. Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  SHELLS  (same SPA, three hosts)                                     │
│   A. local web app     B. Tauri desktop     C. VS Code webview       │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ HTTP + WebSocket (JSON / Arrow IPC)
┌───────────────────────────────▼──────────────────────────────────────┐
│  vizserver   FastAPI — session mgmt, trace queries, live stepping    │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────────┐
│  vizcore  (pure Python library — the real product)                   │
│                                                                      │
│  ┌─ RECORDERS ──────────┐ ┌─ STATIC ─────────┐ ┌─ RESOURCE ───────┐ │
│  │ py_monitor (3.12+)   │ │ ast + symtable   │ │ tracemalloc      │ │
│  │ py_settrace (3.8+)   │ │ CFG builder      │ │ memray hook      │ │
│  │ py_ast (expr-level)  │ │ call graph       │ │ torch.cuda mem   │ │
│  │ lldb_native (C/C++)  │ │   static ∪ dynamic│ │ per-line timing  │ │
│  └──────────┬───────────┘ │ dataflow (SSA)   │ └────────┬─────────┘ │
│             │             │ clang/libclang   │          │           │
│             │             └────────┬─────────┘          │           │
│  ┌──────────▼──────────────────────▼────────────────────▼─────────┐ │
│  │  TRACE STORE   append-only events + keyframes + object table   │ │
│  │  SQLite (index) + Arrow/Parquet (bulk events)                  │ │
│  │  API: state_at(step), history_of(var), writes_to(obj), diff()  │ │
│  └────────────────────────────┬───────────────────────────────────┘ │
│                               │                                      │
│  ┌─ SUMMARISERS (plugin registry) ◄┘                              │ │
│  │ builtin · numpy · pandas · polars · torch · sklearn · custom   │ │
│  └────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

**The one architectural rule:** `vizcore` never imports anything UI-related, and the SPA is built once as an embeddable bundle. That is what makes three shells cheap instead of three projects.

### 4.1 The trace event schema (the keystone — get this right before writing anything else)

```python
# Every event is a flat row. Columnar-friendly. Append-only.

Event = {
  "step":    int,     # monotonic global step counter — the timeline axis
  "t_ns":    int,     # wall clock, for the profiling timeline
  "kind":    str,     # see below
  "frame":   int,     # frame id (unique per activation, not per function)
  "loc":     int,     # location id → (file, line, col_start, col_end)
  "payload": dict,    # kind-specific
}

kinds:
  LINE      {}                              # line about to execute
  CALL      {func_id, args:[ref], caller}   # new frame pushed
  RETURN    {value: ref}                    # frame popped
  YIELD     / RESUME  {value: ref}
  BIND      {name, ref, prev_ref}           # local/global assigned
  DELETE    {name}
  MUTATE    {obj_id, path, ref, prev_ref}   # container/attr changed in place
  EXPR      {node_id, ref}                  # expression-level mode only
  RAISE     {exc_id, handled: bool}
  ALLOC     {obj_id, nbytes, type_id}
  FREE      {obj_id}
  BRANCH    {taken: bool, target_loc}       # for CFG edge-hit counts
  IO        {stream, text}                  # stdout/stderr interleaved

ref = either an inline scalar, or an object_id into the object table.
```

**Object table** (separate, deduplicated — structural sharing):

```python
Object = {
  "obj_id": int,
  "type_id": int,
  "summary": dict,        # from the summariser registry
  "repr_short": str,
  "children": [(key, ref)] | None,   # for containers, depth-limited
  "first_step": int, "last_step": int,
  "nbytes": int | None,
}
```

**Keyframes** every *K* steps (tune K ≈ 2,000): a full materialised stack + globals snapshot, so `state_at(step)` is "load nearest keyframe ≤ step, replay forward ≤ K events". Seeking anywhere in a 10⁷-step trace stays under ~10 ms.

**Storage layout:**

| Data | Store | Why |
|---|---|---|
| Events (bulk) | Arrow IPC / Parquet, chunked | columnar scans, `writes_to(obj)` is a filter, zero-copy to the server |
| Object table, locations, functions | SQLite | relational joins, ad-hoc queries, small |
| Keyframes | msgpack blobs in SQLite | random access by step |
| Source files | verbatim copy in the trace bundle | the trace must be shareable and reproducible |

A trace is **one directory (or zip)** — `trace.vizt/`. Shareable, archivable, re-openable without the original machine. This is a genuine differentiator: nobody can send you their Python Tutor session with real data in it.

### 4.2 The query API (what the UI actually calls)

This is the Pernosco lesson — the power is in queries, not stepping.

```python
store.state_at(step)                 # stack, frames, locals, globals
store.history_of(frame, "x")         # [(step, ref)] — the variable timeline
store.writes_to(obj_id)              # every MUTATE/BIND touching this object
store.provenance(obj_id)             # backwards dataflow: what produced it
store.line_stats()                   # hit count, total time, self time per line
store.calls(aggregate=True)          # call tree, collapsed by callee
store.diff(step_a, step_b)           # what changed between two moments
store.find(predicate)                # "first step where len(xs) > 100"
```

`store.find()` deserves emphasis — a *conditional search over history* is the single most useful debugging feature here and it's trivially cheap once the log exists. "Jump to the first step where `df.shape[0]` dropped" is one scan.

---

## 5. Python recorder — design and the overhead budget

### 5.1 Mechanism comparison

| Mechanism | Granularity | Typical overhead | Python | Verdict |
|---|---|---|---|---|
| `sys.settrace` / `bdb` | line, call, return | **20–100×** | 3.0+ | Fallback only (3.8–3.11) |
| **`sys.monitoring`** (PEP 669) | line, branch, instruction, call, return, raise — each **individually disableable per code object** | ~**1–2% when idle**; "orders of magnitude less than `settrace`" for sparse events; LINE events still "large impact" | **3.12+** | **Primary spine** |
| AST instrumentation (birdseye) | **every sub-expression** | ~10×+ | any | Opt-in "expression mode", selected functions only |
| Bytecode rewriting | arbitrary | medium | any | Avoid — breaks every minor version |
| Frame-eval API (pydevd) | breakpoint-only | ~0 when not hit | 3.6+ | For the *live* debug mode, not recording |

PEP 669's `DISABLE` return value is the critical feature: a callback can return `sys.monitoring.DISABLE` to permanently stop that event firing *at that specific bytecode offset*. That is how we survive hot loops — after N iterations, disable and switch to sampled recording.

### 5.2 Tiered recording modes (user-facing control)

| Mode | What's recorded | Est. slowdown | Use it for |
|---|---|---|---|
| **Overview** | calls, returns, args/returns summarised, exceptions, per-line timing | 1.5–3× | "what did my script do", first look at unknown code |
| **Standard** (default) | + LINE events, + locals diff per line, in-scope files only | 10–30× | algorithms, normal debugging |
| **Deep** | + MUTATE tracking, + full container snapshots, + allocations | 50–200× | the 20 lines you actually care about |
| **Expression** | + every sub-expression value (AST-instrumented) | 100–500× | one function, under a microscope |

Mode is per-**scope**, not global: `@viz.deep` on one function, Overview everywhere else. This is the thing that makes it usable on real code, and it's the feature Python Tutor fundamentally cannot have.

### 5.3 Locals diffing — the performance crux

Naive: read `frame.f_locals` every LINE event and compare to the previous dict. That is O(n_vars) dict build + compare per line.

Mitigations, in order of effect:

1. **Static pre-pass.** Parse the AST once; for each line, know which names it *can* write. Check only those. Most lines write 0 or 1 names. This alone is a 5–20× reduction.
2. **Identity-first comparison.** Compare `id()` before `==`. Only mutable containers need deeper checks.
3. **Version counters.** For dict/list/set, cheap proxies: `len()` + a cached structural hash, recomputed only when `len` changes or the line statically could mutate it.
4. **PEP 667 awareness** (3.13+): `frame.f_locals` is a write-through proxy, not a snapshot copy. Behaviour differs between 3.12 and 3.13 — abstract it behind one function and test both.
5. **Scope filter.** Never trace `site-packages`, `importlib`, or the stdlib unless asked. Typically removes >90% of events.

### 5.4 Non-negotiable correctness concerns

- **Determinism.** Record the seed of `random`, `numpy.random`, `PYTHONHASHSEED`, env, argv, cwd, and the resolved package versions into the trace manifest. Without this, "re-run to materialise this value" silently lies.
- **Observer effect.** Reading `__repr__` or `len()` can execute user code and have side effects. Summarisers must run with monitoring suspended and in a guarded try/except, and must never be called on objects with a custom `__getattr__` without a timeout.
- **Threads & async.** Frame ids must be `(thread_id, async_task_id, activation_counter)`. Decide early: v1 records all threads into one global step order (simple, slightly lossy on true parallelism), with a per-thread lane in the timeline UI.
- **Generators/coroutines.** A frame can be suspended and resumed many times. The frame id must survive YIELD/RESUME — this is where naive tracers produce garbage stacks.

---

## 6. C/C++ recorder (phase 5) — options

You're on a MacBook, which decides this.

| Approach | Gives you | Portability | Effort | Verdict |
|---|---|---|---|---|
| **LLDB Python (SB) API** | source-line stepping, locals via DWARF, backtrace, memory reads, expression eval | macOS + Linux; **native on Apple Silicon** | medium | **Primary.** `gdb` on macOS is a codesigning nightmare; `lldb` ships with Xcode. |
| GDB/MI or gdb Python API (`pygdbmi`) | same | Linux-first | medium | Linux parity layer later |
| Valgrind-based tracer (Python Tutor's) | **byte-accurate** heap, pointer provenance, uninitialised reads | Linux x86 only | high | Read for ideas. GPL-2.0 + a fork of Valgrind 3.11 — do not depend on it. |
| **rr** record/replay + gdb | deterministic replay, **reverse-step**, reverse watchpoints | Linux x86-64, needs perf counters | medium–high | Phase 7, Linux-only superpower |
| clang libtooling source rewriting | portable event printing | everywhere | medium | Risky — changes semantics, misses stack pointers, breaks on templates |
| **Compile to WASM** (Emscripten/clang-wasm) | sandboxed execution + **linear memory is a flat byte array you can render exactly** | browser, anywhere | high | The shareable/demo path. Also the best possible memory visualiser: WASM memory *is* a `Uint8Array`. |

**Plan:** LLDB SB API as the engine. Stepping is slow (≈0.3–3 ms per source line via the API), so expect ~10³–10⁴ steps/second → fine for algorithm-sized C/C++, and we honestly cap it and say so. Build the C/C++ recorder to emit **the same event schema**, so the entire UI works unchanged. That schema-first discipline is what makes phase 5 a few weeks instead of a rewrite.

**Pointer diagrams** need type-aware memory interpretation: DWARF gives the type of each variable; walk pointers using the type graph and read target memory with `SBProcess.ReadMemory`. Cap depth and cycle-detect.

**Build integration:** require `compile_commands.json` (CMake `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON`) or accept a single-file `clang++ -g -O0` invocation. `-O0 -g3` is mandatory — any optimisation destroys the variable↔line mapping, and we must warn loudly if we see otherwise.

---

## 7. Static analysis layer

Dynamic trace = ground truth for what *did* happen. Static analysis = what *could* happen. The UI should overlay them: **grey edges = never executed, coloured edges = executed, thickness = hit count.** This single overlay is remarkably informative and almost nobody ships it.

| Artefact | Python | C/C++ |
|---|---|---|
| AST / symbols | `ast`, `symtable` (stdlib — scope resolution for free) | `libclang` / `clangd` |
| Control-flow graph | own builder over `ast` (don't depend on `staticfg`/`py2cfg` — both are thin and lightly maintained; a CFG builder is ~400 lines and you need exact node↔line mapping) | `clang -S -emit-llvm` + `opt -dot-cfg`, or libclang's `CFG` |
| Call graph (static) | **PyCG** (research-grade, handles closures/classes reasonably) or **Jarvis** (application-centred, more precise); merge with dynamic call edges | clang xrefs |
| Dataflow / def-use | SSA over the CFG; reaching-definitions for "where did this value come from" | LLVM IR analysis passes |
| Module/import graph | `pydeps`-style import scan | include graph |
| Deep queries (optional) | **Joern** CPG — one graph merging AST+CFG+dataflow, queryable | Joern's C/C++ frontend is its strongest |

The dataflow layer powers the best feature in the whole app: **`provenance(value)`** — click a variable at step 40,000, get the chain of assignments that produced it, back to a literal or an input. Static def-use narrows the candidates; the trace's `BIND`/`MUTATE` events confirm which one actually ran.

---

## 8. Resource analysis

| Signal | Python | C/C++ |
|---|---|---|
| Python-object memory over time | `tracemalloc` (stdlib, per-line attribution, ~2× overhead) | — |
| Native + Python allocations | **memray** (Apache-2.0, `malloc` interposition, native unwinding) | heaptrack / `massif` |
| Per-object size | `sys.getsizeof` + `pympler`, deep size with cycle detection | DWARF type sizes |
| Object reference graph | `gc.get_referrers` / `objgraph` → render as a graph | pointer walk via DWARF |
| GPU memory | `torch.cuda.memory._record_memory_history()` → snapshot (official PyTorch memory timeline) | CUDA API |
| Per-line time | from `t_ns` on LINE events; cross-check against `cProfile`/Scalene-style sampling | LLDB timestamps |

Present it as one **synchronised resource strip** under the timeline: RSS, Python heap, allocation rate, GPU memory. Clicking a spike jumps to the step that caused it. That interaction — spike → responsible line → variable state at that moment — does not exist in any tool today and is maybe 200 lines of UI once the trace store exists.

---

## 9. Domain plugins — where this becomes worth using at work

These are summariser + renderer pairs. They are the reason a professional picks this over Python Tutor.

### 9.1 Data science (pandas / polars / numpy)

Per step, for each DataFrame in scope record *metadata only*:

```python
{ "shape": (n, m), "columns": [...], "dtypes": {...},
  "nbytes": int, "null_counts": {...}, "index_type": str,
  "fingerprint": uint64 }   # cheap hash of head+tail+shape
```

Then the panel shows a **DataFrame lineage ribbon**: one row per step where a DataFrame changed, with the diff spelled out —

```
step 1,204   df   (10000, 12) → (9873, 12)       −127 rows      dropna()
step 1,251   df   (9873, 12)  → (9873, 13)       +col "ratio"   float64
step 1,288   df   (9873, 13)  → (9873, 13)       dtype: age     object → int64
step 1,340   df   (9873, 13)  → (412, 13)        −9461 rows  ⚠   query()
```

That ⚠ on an unexpectedly large row drop is the feature that pays for the whole project in your day job. Nobody ships this.

NumPy gets the same treatment: shape, dtype, strides, **base** (is this a view or a copy? — the single most common silent bug in numerical Python), nan/inf counts, min/max.

### 9.2 AI engineering (torch)

- **Tensor summariser:** shape, dtype, device, `requires_grad`, `grad_fn` type, has_nan/has_inf, min/max/mean/std, `nbytes`.
- **Forward-pass graph:** module hooks (the torchlens approach) or `torch.fx` symbolic trace → a layer graph annotated with the actual shape flowing on each edge. Wrap torchlens rather than reimplementing it.
- **Shape-mismatch pinpointing:** on a `RuntimeError` about shapes, show the graph with the two incompatible edges highlighted and the last 20 steps of shape history. This turns the worst error message in deep learning into a picture.
- **Training-loop view:** loss/grad-norm/lr per step, gradient histogram per layer, GPU memory. Step axis = optimiser step, not line.

### 9.3 Algorithms

- Auto-detect list/array/dict/tree/graph structures and offer the right renderer (the Debug Visualizer plugin pattern).
- **Array animation:** a bar chart per element, coloured by read/write/compare at the current step — sorting algorithms become instantly legible.
- Recursion tree from the CALL/RETURN events, with memoisation hits marked.
- Loop-iteration collapse: 10,000 iterations shown as one node, expandable, with a sparkline of the loop variable.

### 9.4 Real-code debugging

- Attach to an existing script/module/pytest run: `viz run -m pytest tests/test_x.py::test_y`.
- Exception mode: record in Overview, and when an exception fires, **retroactively upgrade** the last N steps of the failing frames to Deep by re-running (determinism manifest makes this sound).
- `store.find()` exposed as a search box: "first step where `len(queue) > 1000`".

---

## 10. Frontend

### 10.1 Library choices

| Need | Pick | Why not the alternative |
|---|---|---|
| Code editor | **CodeMirror 6** | Monaco is ~5–10× the bundle and brings a whole IDE; CM6's decoration/gutter/widget API is better suited to inline value badges, and it embeds cleanly in a VS Code webview (where Monaco would collide) |
| Layered graphs (CFG, call graph) | **ELK.js** (`elk.layered`) | dagre is faster but noticeably worse on orthogonal routing and nested/compound nodes, which CFGs need |
| Graph rendering | **React Flow** (xyflow) for interactive/editable graphs | hand-rolled SVG is fine for ≤200 nodes; React Flow gives pan/zoom/selection free |
| Big force-directed graphs (module/object graphs, 1k+ nodes) | **Cytoscape.js** | React Flow degrades past ~1,000 nodes |
| Deep profiling timeline | **Perfetto** (deep-link or embed via `postMessage`) | re-implementing a multi-track zoomable trace viewer is months of work; VizTracer proved this works |
| Step timeline / scrubber | **custom canvas** | 10⁶ steps must never touch the DOM |
| Charts (variable history, memory, sparklines) | **D3 scales + hand-written SVG/canvas** | chart libraries fight you on brushing + linked selection, which is the whole point here |
| State | Zustand + a single `currentStep` atom | everything derives from one number |

### 10.2 Panel layout

```
┌───────────────────────────────────────────────────────────────────────┐
│ ▶ ⏸ ⏮ ⏭ ⤵step-in ⤴step-out  ×1 ×4 ×16   step 48,213 / 1,204,887    │
│ ░░░░▓▓▓▓▓░░░▒▒▒▒▒▒░░░░░░░█░░░░▒▒▒░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ │ ← timeline (canvas)
│  mem ╱‾‾╲____╱‾‾‾‾‾╲___     calls ▁▂▅█▅▂▁    exceptions  ▲          │ ← resource strip
├──────────────────────────────┬────────────────────────────────────────┤
│ CODE                         │ STACK          VARIABLES               │
│  12  for i in range(n):      │ ▸ main:42      n   = 1000              │
│  13 ▸   t = a[i] ◂ 7         │ ▸ sort:13      i   = 213  ↑ was 212    │
│  14    if t < piv: ◂ True    │                t   = 7    ↑ was 91     │
│  ░░ heat: 213 hits, 1.2ms    │                piv = 48               │
│                              │                a   = list[1000] ▸      │
├──────────────────────────────┴────────────────────────────────────────┤
│ [Heap] [CFG] [Call graph] [Dataflow] [DataFrames] [Tensors] [Memory]  │
│                                                                       │
│   a ──▶ ┌──┬──┬──┬──┬──┬──┐        i ──▶ 213                         │
│         │48│12│91│ 7│55│..│                                           │
│         └──┴──┴──┴▲─┴──┴──┘                                           │
│                   └── read at this step                               │
└───────────────────────────────────────────────────────────────────────┘
```

Every panel is a pure function of `currentStep`. One scrubber drives all of them. That is the entire interaction model and it must be ruthlessly enforced — the moment a panel keeps its own notion of "where we are", the app becomes confusing.

### 10.3 UX principles (learned from what makes the existing tools pleasant or not)

1. **The step number is the URL.** Deep-linkable, shareable, bookmarkable.
2. **Inline value badges beat a side panel** for the current line — birdseye and snoop both prove this. `t = a[i] ◂ 7`.
3. **Show the delta, not the value.** `i = 213 ↑ was 212` is more useful than `i = 213`.
4. **Execution heat in the gutter** — instantly reveals the hot loop and the dead branch.
5. **Collapse by default, expand on click.** Library frames, loop iterations, deep containers.
6. **Never block on the backend.** Prefetch a window of ±2,000 steps around the cursor; scrubbing must be 60fps.
7. **Edit-and-rerun in one keystroke.** The user asked for this explicitly: change the code, hit re-run, the UI keeps the step position if the line still exists and diffs the two traces. **Trace diffing** ("before my fix vs after") is a strong feature and is nearly free once traces are files.

---

## 11. Three shells, one core

| Shell | Stack | Extra work | Notes |
|---|---|---|---|
| **A. Local web app** | `viz run script.py` → FastAPI on 127.0.0.1 + opens browser | — (baseline) | Build this first. Full access to lldb, compilers, GPUs. No sandboxing needed — it's your own machine, your own code. |
| **B. Desktop app** | **Tauri 2** wrapping the same SPA; sidecar = the Python server | ~1–2 weeks | Tauri over Electron: ~10 MB vs ~120 MB, native webview. Ship the Python runtime via PyInstaller or `uv`-managed venv. |
| **C. VS Code extension** | Webview hosting the same SPA + a thin DAP bridge for live mode | ~2 weeks | Reuses your editor, your venv, your breakpoints. The webview CSP forbids remote assets — bundle everything. |

If a hosted/multi-user version ever happens, sandboxing becomes the dominant concern. Current state of the art for untrusted code: **gVisor** (syscall interception, ~moderate overhead, good general isolation) or **microVMs** (Firecracker — strongest isolation, ~125 ms boot) over plain containers; **WASM** (Pyodide for Python, clang-wasm for C++) for the weakest-trust, in-browser case. Defer all of this — local-first means it isn't a problem yet, and designing for it now would cripple the feature set.

---

## 12. Phased roadmap

Durations assume evenings/weekends, one person, with AI assistance.

### Phase 0 — Schema spike (3–5 days) ⚠ most important phase
Nothing but the trace schema, the store, and a `sys.monitoring` recorder that logs LINE/CALL/RETURN/BIND for one 10,000-step script. No UI.
**Acceptance:** `state_at(step)` is correct for 100 random steps, verified against a `settrace` reference implementation; trace file for 10⁵ steps is under 50 MB; `state_at` median latency < 10 ms.
*Why first: the schema is the one decision you cannot change later without rewriting everything.*

### Phase 1 — The live UI (DONE)
`viz serve` on `http.server`, standard library only, bound to 127.0.0.1. The page holds no trace: it asks for the step it is showing, so the static report's ~20,000-step embed ceiling is gone. Minimap over the whole run, click-a-line-to-jump, variable history with click-to-seek, search, and a re-run button that records the script again and reloads. Styles shared with the report via `ui.py`. Works unchanged on Python and C traces.
**Delivered:** 8/8 Phase 1 tests. Step 279,991 of a 280k-step trace is reachable; **2.4 ms median seek** (a frame is 16 ms); `/api/state` asserted to agree with `TraceReader.state_at` exactly, so the UI and the CLI can never disagree.


### Phase 2 — Structure + in-place changes (DONE)
`MUTATE` events for `a[i] = x`, `self.x += 1`, `xs.append(v)`, found by a static pass over subscript/attribute/mutating-method lines. CFG with basic-block merging and an executed/unexecuted overlay. Call graph, dynamic ∪ static, including functions that never ran. A self-contained HTML dashboard (`viz report`) with a step explorer, hot lines, variable timelines, both graphs, execution heat and a mutations table. `viz flow` for the same in the terminal.
**Delivered:** 8/8 Phase 2 tests; 26 µs/step on realistic code; dead-code detection verified against `# DEAD` markers.
*Still open for a later phase: the heap/object diagram with pointer arrows, and loop-iteration collapse in the timeline.*

### Phase 3 — Resources (DONE)
`tracemalloc` in its own second pass (running it inside the recorder cost 1,245 µs/step against 23 — it was mostly measuring the recorder). Cheap step-aligned counters always on; exact byte curve and per-line attribution with `--memory`. Resource strip under the slider with spike→line navigation. `viz mem` in the terminal. Chrome Trace / Perfetto export (`viz export`). The encoder now holds weak references, so the recorder no longer stops the garbage collector.
**Delivered:** 8/8 Phase 3 tests; the leak fixture is located to the exact line while the equally-allocating control function is correctly cleared.
*Deferred: memray integration (native allocations) and GPU memory — both wait on Phase 4's domain plugins.*

### Phase 4 — Domain plugins (DONE)
A two-tier summariser registry (`cheap` O(columns) every line, `rich` O(rows) only when the cheap facts moved) covering numpy, pandas, polars, torch tensors and modules, and sklearn estimators. DataFrame lineage ribbon with derived-frame linking via the names each line reads. Tensor shape tracking with autograd provenance. Shape-mismatch diagnosis naming the line and every live tensor. `viz data`, `viz tensors`, and two new dashboard panels.
**Delivered:** 8/8 Phase 4 tests. The planted row-dropping bug is found at the right line; the 200k-row pipeline traces at **2.1x** untraced (the criterion was under 2x in Overview mode, met in full Standard mode).
*Deferred: torchlens integration for a full forward-pass graph, and the training-loop view (loss / grad-norm / GPU memory per optimiser step).*

### Phase 5 — C/C++ (DONE)
Two backends behind one row format: LLDB (macOS primary, driven through the `lldb` binary's own interpreter so the bindings always match) and GDB/MI over a pipe (Linux, no bindings to version-pin). Both emit the *same* schema, so every Phase 0–4 tool works on C unchanged. Step-over plus breakpoints on the user's own functions, so libc is never entered. Pointer dereference one level deep, cached per value. `-O1` and above refused outright. `viz run prog.c` compiles and traces in one step.
**Delivered:** 9/9 Phase 5 tests, including a test that both backends report identical call counts. Linked-list pointer chains and `std::vector` growth both render; `-O2`/`-O1`/`-Os`/`-Ofast` all refused; dead branches correctly cold.
*Deferred: control-flow graphs for C (needs clang), `compile_commands.json` for multi-file projects, and Valgrind-grade heap diagrams.*

### Phase 6 — Shells (2–3 weeks)
Tauri desktop packaging; VS Code extension with webview + DAP bridge for live breakpoint mode.
**Acceptance:** both install clean on a fresh machine and open the same trace file.

### Phase 7 — Power features (ongoing)
Expression-level mode (AST instrumentation, birdseye-style hover). `provenance()` dataflow queries. Trace diffing. `rr` integration on Linux for true reverse execution. Joern CPG backend. Shareable trace bundles.

---

## 13. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Schema churn forces a rewrite | medium | **severe** | Phase 0 exists for exactly this. Version the schema from day one. Write the C/C++ event mapping on paper *in phase 0*, before the Python recorder is finished. |
| Overhead makes it useless on real code | high | severe | Tiered modes; scope filter; `DISABLE` on hot code; measure overhead in CI on a fixed benchmark set every phase |
| UI becomes an unreadable hairball | high | high | Design the collapsed view first; hard node-count caps with explicit "expand" affordances; test on a 500-node graph in phase 2 |
| Summarisers trigger side effects / hang | medium | high | Run with monitoring suspended, in try/except, with a wall-clock guard; never call custom `__repr__` in Overview mode |
| Scope creep across four audiences | **high** | high | The audiences share one engine and differ only in summarisers + renderers. Enforce that. Any audience-specific code in `vizcore` outside the registry is a design failure. |
| C/C++ stepping too slow to be useful | medium | medium | Be honest: cap it, show a progress bar, document the limit. 10⁴ steps covers algorithm work, which is the real use case. |
| 3.12/3.13/3.14 `f_locals` and monitoring differences | high | medium | One abstraction layer; CI matrix across 3.11 (settrace fallback), 3.12, 3.13, 3.14 |

---

## 14. Success criteria (measurable)

| Metric | Target |
|---|---|
| Max steps in one trace | ≥ 10⁷ (vs Python Tutor's 10³) |
| Overview-mode slowdown | ≤ 3× |
| Standard-mode slowdown | ≤ 30× |
| `state_at(step)` latency, p50 | ≤ 10 ms |
| Scrub frame rate | 60 fps sustained |
| Trace size per 10⁶ steps | ≤ 500 MB uncompressed |
| Works on an unmodified real script (no edits, imports allowed) | yes |
| Trace is a portable file, re-openable elsewhere | yes |

---

## 15. Reading list — clone these before writing code

**Read for the trace format and the visual language:** `pgbovine/OnlinePythonTutor` (v5-unity + `v3/docs/opt-trace-format.md`) · `JuezUN/opt-cpp-backend` (ideas only — GPL-2.0)

**Read for how to record at scale:** `gaogaotiantian/viztracer` (C tracer + `sys.monitoring` usage + Perfetto output) · `bloomberg/memray` · `plasma-umass/scalene`

**Read for granularity and presentation:** `alexmojaki/birdseye` (AST instrumentation) · `alexmojaki/snoop` · `alexmojaki/heartrate` · `thonny/thonny` (expression-level stepping)

**Read for the plugin architecture:** `hediet/vscode-debug-visualizer`

**Read for static analysis:** `vitsalis/PyCG` · `joernio/joern` · `quarkslab/numbat` (Sourcetrail graph DB format)

**Read for the AI panel:** `johnmarktaylor91/torchlens` · `mert-kurttutan/torchview`

**Read for reverse execution (phase 7):** `rr-debugger/rr` · Pernosco's blog on omniscient debugging

**Specs to have open:** PEP 669 (`sys.monitoring`) · PEP 667 (`f_locals` proxy) · Debug Adapter Protocol · Perfetto trace format & embedding docs

---

## 16. Immediate next step

Phase 0, one file: the trace schema as a Python module plus a `sys.monitoring` recorder for LINE/CALL/RETURN/BIND and a `state_at()` that passes a differential test against `sys.settrace`. Everything else in this document depends on that being right.
