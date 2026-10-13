# ndr — ND36's recorder, in Rust

Same traces as `nd`, **10–20× faster to record**. Everything that reads a
trace — `nd x`, `nd flow`, `nd heat`, `nd report` — works on an `ndr` trace
unchanged.

```bash
ndr run anneal.py        # record   → anneal.nd
ndr x   anneal.nd        # explore  (`nd x`, in Rust: same commands, same output)
```

## Measured

Python 3.13, best of 5, cost per recorded step:

| Program | steps | `nd` (Python) | `ndr` (Rust) | faster |
|---|---|---|---|---|
| anneal.py | 335k | 8.8 µs | **0.47 µs** | ~19× |
| sudoku solver | 1.48M | 13.6 µs | **1.0 µs** | ~14× |

And it is not only faster. On `anneal.py`, `nd` records 603 in-place changes
and attributes them to the wrong lines; `ndr` records all 15,045, each on the
`swap(...)` line that made it. See *what verify compares* below.

## The six commands

```
ndr run    FILE.py [args…]     record a program          → FILE.nd
ndr bench  FILE.py             what recording costs, per step
ndr verify FILE.py             record with ndr AND nd, compare them
ndr info   TRACE               what is in a trace
ndr watch  TRACE [NAME]        one variable's whole life
ndr x      TRACE [-c CMD]…     explore a run (see ../EXPLORE.md)
```

| Command | Switch | |
|---|---|---|
| `run` | `-o, --out PATH` | where to write the trace (default `FILE.nd`) |
| | `--keyframe-every N` | steps between snapshots (default 2000) |
| | *anything else* | goes to your program; `--` forces it |
| `bench` | `--compare` | also time `nd` on the same program |
| `x` | `-c, --command CMD` | run a command and exit instead of the prompt; repeatable |
| `watch` | `--limit N` | changes to show (default 50) |

## Build

Needs Rust (`rustup`) and Python **3.12 or newer** — `sys.monitoring` is
what makes this fast, and it does not exist before 3.12. If the build finds
an older Python it stops and says so, with install steps for your system.

Check first: `python3 --version`. Ubuntu 20.04 ships 3.8 and 22.04 ships
3.10 — both too old. Get a newer one without touching the system Python:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # any Linux or Mac
uv python install 3.13
export PYO3_PYTHON=$(uv python find 3.13)
```

```bash
cd ndr
cargo build --release
cp target/release/ndr ~/.local/bin/      # or anywhere on PATH
ndr bench ../nd36/samples/sort_demo.py
```

`ndr` embeds the Python it was built against. To pick a specific one —
Anaconda, pyenv, a venv's base — say so at build time:

```bash
PYO3_PYTHON=$(which python3) cargo build --release
```

**macOS:** the build compiles SQLite from source, so it needs the Xcode
command-line tools: `xcode-select --install`.

**Arch:** `sudo pacman -S rust python` is all it needs.

## How it works, in five lines

1. The `sys.monitoring` callbacks are raw C functions written in Rust —
   CPython's dispatch to them costs ~22 ns; everything else is ours.
2. Every line, each live variable gets a cheap identity check and, for
   containers, a content hash. No `repr`, no JSON, no allocation.
3. Containers are only re-hashed after lines whose **bytecode** could mutate
   something (a store, a call, an in-place op). `if grid[r][i] == v` cannot,
   so it costs nothing. This halved the cost on the sudoku solver.
4. On such a line, only the variables the line **names** are re-hashed every
   time. The rest are re-checked on a back-off (1, 2, 4 … 32 lines), unless
   something they hold changed on this line — then they are checked at once
   (`b = a; a.append(1)`, or `solutions` holding `board`). On the test
   programs this reports exactly the same in-place changes as re-hashing
   everything, at a third of the cost on big containers.
5. Only when a hash moves does the expensive part run: `repr`, children, an
   object row. The trace is written to SQLite once, at the end.

## What verify compares

**Must match** — every line run, in order; calls per function; every value
each variable took (ints, floats, strings, bools, `None`). `ndr verify`
fails if any of these differ.

**May differ, on purpose:**

- *In-place changes.* `nd` stops re-checking a container after 150 looks,
  and when that budget runs out its fingerprint flips, so it also reports
  changes on lines that change nothing. `ndr` has no cut-off.
- *Aliases.* When `best = tour` and `tour` changes, `ndr` reports a change
  to **both** names. `nd` reports whichever it happened to check first —
  which hides exactly the aliasing bug you were looking for.
- *Deletions.* `nd` reports module globals as deleted while a list
  comprehension runs at module level (Python 3.12+ changes what
  `frame.f_locals` shows during one). Nothing was deleted; `ndr` reads the
  real namespace.

## Limits, today

| | |
|---|---|
| Python 3.12+ only | 3.11 would need the slow `sys.settrace` path |
| One thread | other threads are not recorded |
| No `--memory` | memory profiling stays in `nd` |
| numpy / pandas / torch | recorded by shape and dtype only, never walked |
| Huge containers | the content hash looks at the first 512 elements |
| C / C++ | still `nd run file.c` (gdb/lldb) |

## Tests

```bash
cargo test --release
```

Eleven tests, including recording five real programs with **both** recorders
and requiring identical control flow and values. That one skips itself, with
a message, if the Python `nd36` package is not importable.
