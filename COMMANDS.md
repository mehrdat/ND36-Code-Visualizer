# ND36 — every command

`nd <command> [options]`. Run `nd <command> --help` for any of them.
Stuck? Run **`nd doctor`** — it says what is installed and what is not.

---

## The shape of it

```
   your code  ──nd run──▶  a trace folder  ──nd app──▶  a window
                           (yourfile.nd)   ──nd watch─▶ one variable
                                           ──nd flow──▶ what never ran
```

A **trace** is a folder (`sudoku.nd/`). `run` makes one; everything else
reads one. `app` and `serve` do both in one step.

---

## Start here, with nothing of your own

### `nd demo`
Records a program that ships with ND36 and tells you what to type next.
No setup, no file needed.

```bash
nd demo                 # a loop, a sort, some recursion
nd demo --bug           # a program with a real bug. practise finding it
nd demo --c             # the C sample
```

### `nd samples [--copy]`
Lists those programs. `--copy` puts them in the current folder so you can
edit them.

---

## Recording

### `nd run FILE [args…]`
Record a program. Writes `FILE.nd` next to you.

```bash
nd run sudoku.py                  # → sudoku.nd
nd run solver.c                   # C and C++ too — compiles it for you
nd run script.py --memory         # also measure memory
nd run script.py -o /tmp/x.nd     # choose where it goes
```

| Option | Default | |
|---|---|---|
| `-o, --out PATH` | `FILE.nd` | where to write the trace |
| `--memory` | off | per-line memory — runs the script a **second** time under tracemalloc |
| `--memory-snapshots N` | `0` | extra heap snapshots; each costs ~80 points off the memory curve |
| `--driver {monitoring,settrace}` | auto | Python only. `settrace` is the slow reference |
| `--keyframe-every N` | `2000` | steps between full snapshots. Lower = faster seek, bigger file |
| `--backend {lldb,gdb}` | auto | C/C++ only. lldb on macOS, gdb elsewhere |
| `--cflags "…"` | — | C/C++ only, **one quoted string**: `--cflags="-lm -std=c11"`. `-O1`+ is refused |
| `--max-lines N` | `50000` | C/C++ only. Stepping costs ~1 ms a line |

**Anything ND36 does not recognise goes to your program.** The options in
the table above are ND36's; everything else is yours:

```bash
nd run server.py --port 8080            # --port 8080 → server.py
nd run train.py --memory --epochs 3     # --memory is ND36's, --epochs yours
nd run app.py -- --memory               # after --, even --memory is yours
```

ND36 prints one line saying what it handed over, so there is never any
doubt.

---

## Looking at it

### `nd app [TARGET]` ← **start here**
Record if needed, then open the UI in its own window.

```bash
nd app sudoku.py        # record and open
nd app sudoku.nd        # open an existing trace
nd app --pick           # choose from the traces in this folder
```

Press **Ctrl-C in the terminal** to stop. Closing the window does not stop
it (the browser lies about when it has closed).

| Option | Default | |
|---|---|---|
| `--pick` | | list the `.nd` folders here and choose |
| `-p, --port N` | `7878` | tries the next 20 if busy |
| `-o, --out PATH` | | where to put the trace, when recording |
| `--memory` | off | as `run` |

### `nd serve [TARGET]`
The same thing in a normal browser tab. Same options, plus `--no-browser`.

### `nd report TRACE [-o FILE.html]`
One self-contained HTML file. No server — you can email it.

| Option | Default | |
|---|---|---|
| `-o, --out` | `TRACE.html` | |
| `--max-steps N` | `20000` | steps embedded in the page. This is the ceiling `serve` does not have |

---

## Asking questions

### `nd x TRACE` ← the deep one
### `nd explore TRACE`
An interactive prompt over the run — the cursor is a *moment*, not an
address. `?` inside it lists everything; `??` walks you through your own
trace.

```bash
nd x sudoku.nd                       # the prompt
nd x sudoku.nd -c afl                # one command and out, so it pipes
nd x sudoku.nd -c "axv grid"
```

```
nd[solve sudoku.py:32]> afl          which functions did the work
nd[solve sudoku.py:32]> af solve     callers, callees, its variables
nd[solve sudoku.py:32]> axv grid     who writes grid, who reads it
nd[solve sudoku.py:32]> fv           the function × variable grid
nd[solve sudoku.py:32]> pl tries     draw a variable over the whole run
nd[solve sudoku.py:32]> c tries>4000 run on until that is true
```

| Option | |
|---|---|
| `-c, --command CMD` | run it and exit. repeatable |
| *(no trace)* | uses the only trace in this folder |

Full reference: **`EXPLORE.md`**.

### `nd watch TRACE [NAME]`
**One variable's whole life.** Leave `NAME` out to see what you can watch.

```bash
nd watch sudoku.nd              # lists every variable + how often it changed
nd watch sudoku.nd grid         # follow it
nd watch sudoku.nd v --limit 200
```

```
step       where              how     grid
100        sudoku.py:26       MUTATE  [[5,3,0,…
242        sudoku.py:25       MUTATE  [[5,3,1,2,7,…
```

`BIND` = the name now points at something new. `MUTATE` = same object,
changed contents (`a[i] = x`, `self.n += 1`, `xs.append(v)`).

### `nd step TRACE N`
What was happening at step N, with the surrounding source.

### `nd info TRACE`
Size, Python/compiler, how many of each kind of event.

### `nd heat TRACE`
The whole file with hit count and milliseconds per line.

### `nd flow TRACE`
Call graph, plus **every line that never ran**. Your untested branches.

### `nd data TRACE`
DataFrame lineage — every step a frame changed, flagged when >30% of the
rows vanish. Needs pandas in the traced script.

### `nd tensors TRACE [--limit N]`
Tensor shapes through the run, and the line a shape mismatch happened on.

### `nd mem TRACE`
Memory curve, and the lines still holding bytes at the end. Needs
`--memory` at record time for per-line attribution.

### `nd export TRACE [-o FILE.json]`
Chrome Trace / Perfetto format. Open it at **ui.perfetto.dev** for a real
flame timeline. The file never leaves your machine.

### `nd bench [FILE]`
How much the recorder costs on this program: µs per step, trace size, seek
time. **With no file** it benchmarks the bundled sample, so there is always
something to run.

---

## Installing

### `nd doctor`
What is installed, what is missing, what traces are here. **Run this first
when something does not work.**

### `nd install-app [--into DIR]`
Puts the app in **`~/Applications`** — your *home* Applications folder, not
`/Applications`. Finder's sidebar usually does not show it.

```bash
nd install-app                        # ~/Applications/ND36.app
nd install-app --into /Applications   # the system one
```

Find it: ⌘-space → type **ND36**. Or Finder → Go → Home → Applications.
It opens the folder for you unless you pass `--no-reveal`.

On Linux this writes `~/.local/share/applications/nd36.desktop` instead.

### `nd install-vscode [--into DIR]`
Copies the extension into `~/.vscode/extensions/`. **Quit VS Code
completely and reopen** — it only reads that folder at startup.

Then right-click a `.py`/`.c`/`.cpp` file → **ND36: Record and replay this
file**.

---

## Troubleshooting

| What you see | What it is |
|---|---|
| `unable to open database file` | Fixed. Every command now says plainly that the trace is not there, and lists the ones that are |
| `unrecognized arguments: --port` | Fixed. Unknown options go to your program now |
| `no variable called 'grid'` | It now suggests near-misses. `self.grid` is recorded under `self` |
| `nd bench` with no file to use | `nd bench` alone works; `nd demo` records a sample |
| VS Code: *the server never came up* | Fixed: it tries four places for `nd` and shows the real error. If it still fails, run `which nd` and put that path in the `nd.command` setting |
| `error: the following arguments are required: name` | Old build. `nd watch TRACE` with no name now lists them |
| The window opens but is blank | Fixed. The server used to die when Chrome's process exited |
| `nd: command not found` | `pip install -e .` from the project folder |
| `no debugger found` | C/C++: `xcode-select --install` (mac) or `pacman -S gdb` |
| `refusing to trace an optimised build` | Working as intended. `-O2` makes line numbers lie |
| Recording is slow | ~20 µs a step. A 1.4M-step solver takes ~18 s |
| C variables look like garbage | Real. A C local holds stack junk until it is assigned |
| Installed the app but cannot find it | It is in `~/Applications`, not `/Applications`. `nd doctor` prints the path |

---

## Cheat sheet

```
nd demo                       try it with no file of your own
nd doctor                     is everything installed?
nd run  FILE.py               record
nd x    TRACE                 explore it            ← the one to remember
nd watch TRACE                what variables exist
nd watch TRACE NAME           follow one
nd flow TRACE                 what never ran
nd heat TRACE                 time per line
nd mem  TRACE                 where the memory went
nd report TRACE               an HTML file to send
nd app  FILE.py               record + a window
```

Inside `nd x`:

```
?            the list                ??           a worked example
afl          the functions           af NAME      one of them
vl           the variables           axv NAME     one of them
fv           who touches what        pl NAME      draw it
s NAME       jump to a change        pd           the code here
c NAME>5     run until true          pv           variables right now
```
