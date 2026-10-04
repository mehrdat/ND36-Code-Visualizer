# `nd x` — the explorer

Like `radare2`, but the cursor is a **moment in the run**, not an address.

```bash
nd run sudoku.py      # record it once
nd x  sudoku.nd       # then explore it
```

Inside, press **`?`** for the list and **`??`** for a worked example built
from your own trace.

---

## The one idea

```
   a debugger:   you are at ONE moment, and it is now
   nd x:         you are at ANY moment, and you can move
```

The cursor is a step number. `s 1200` goes there. Every "look" command
answers *at this moment*; every "analyse" command answers *across the
whole run*. That is the whole mental model.

---

## Five minutes

```bash
nd x sudoku.nd
```

```
afl                  which functions did the work
af solve             what solve is wired to
axv grid             who writes grid, who reads it
s grid               jump to the first time grid changed
pd                   see the code there
pl tries             draw tries over the whole run
q
```

---

## Where am I

| | |
|---|---|
| `s 1200` | go to step 1200 |
| `s +50` `s -50` | relative |
| `s 50%` `s end` `s start` | proportional, last, first |
| `s grid` | the next change of a variable |
| `s fn:solve` | the next call of a function |
| `s :26` | the next time line 26 ran |
| `1200` | a bare number means `s 1200` |
| `n` / `p` | one step forward / back (`n 100` for a hundred) |
| `f` / `F` | next / previous event **in this frame** — step over a call |
| `u` | step out: where this frame returns |
| `c i>5` | run on until it is true |
| `c grid` | run on to the next change of `grid` |
| `c raise` | run on to the next exception |

`c` with nothing repeats the last condition — hold it down to walk a loop.

---

## Look at this moment

| | |
|---|---|
| `pd` | the source here, with hit counts and what each line writes |
| `pv` | the variables that exist right now |
| `pV grid` | one variable in full: shape, dtype, children |
| `bt` | the call stack |
| `ll` | the raw events around the cursor |

```
▸   32   4,208      done, tries = solve(grid, tries + 1)  → done,grid,tries
    33   4,208      if done:                              → grid
    34      51          return True, tries
```

The left column is **how many times that line ran**, so a `51` next to a
`4,208` tells you which branch is the rare one. The arrow is what the line
writes.

---

## Look at the whole run

| | |
|---|---|
| `afl` | every function: calls, steps, ms, how wired up |
| `af solve` | one function: callers, callees, its variables, each call |
| `axf solve` | who calls it, who it calls |
| `axv grid` | who writes a variable, who reads it, on which lines |
| `fv` | the **function × variable grid** — the connection map |
| `vl` | every variable, by how often it changes |
| `ch grid` | the value table: every change, with the line |
| `pl tries` | draw a variable over the whole run |
| `tt` | timeline: which function was busy when |
| `tree` | the call tree, by cost |
| `cmp 100 2000` | what is different between two steps |
| `dis` | the hottest lines in this file |
| `/ text` | find a name, a value, or a line of source |
| `i` | what this run was |

### `fv` — the connections

```
function      i   c   j   v   r  tries  grid
ok           Wr   r  Wr   r   r      ·     r
find_empty    ·  Wr   ·   ·  Wr      ·    Wr
solve         ·  Wr   ·  Wr  Wr     Wr    Wr
```

`W` = writes it during the run. `r` = reads it in the source. `·` = no
connection at all. One screen, and you know which function owns which
piece of state.

### `pl` — a variable, drawn

```
    4208 │                                            ▁▂▃▄▅▅▆▇
    3.7k │                                  ▁▂▃▃▄▅▆▇██████████
    2.1k │                      ▁▂▃▄▅▆▇███████████████████████
       5 │ ▁▂▃▃▄▅▆▇███████████████████████████████████████████
         └──────────────────────────────────────────────────────
          step 1,195 → 1,467,000
```

That is a backtracking search climbing. Numbers come straight out of the
log, so a million-point series costs nothing.

---

## Use it from a script

`-c` runs a command and exits, so it pipes:

```bash
nd x sudoku.nd -c afl
nd x sudoku.nd -c "axv grid" -c "ch grid 5"
nd x sudoku.nd -c "af solve" > solve.txt
```

Several commands on one line with `;`, as in r2:

```
nd[solve 32]> s grid ; pd ; pv
```

---

## Speed

The first `nd x` walks the log once and caches the result in the trace
folder. After that every command is instant.

| trace | first run | every run after |
|---|---|---|
| 621 steps | 0.01 s | 0.01 s |
| 1,467,162 steps | 1.7 s | **0.07 s** |

---

## Honest limits

| | |
|---|---|
| **Reads are static** | The log records writes, not reads — recording every name a line reads would double the trace for something the source already knows. So `r` in `fv` and "read by" in `axv` come from the AST. They are labelled wherever they appear. |
| **`c` tests written values** | `c i>5` looks at each write of `i`, not at every step. That is what makes it fast, and it is almost always what you want. |
| **Attributes fold to their root** | `self.grid` is recorded under `self`. `axv self` shows it. |
| **C/C++ traces** | `s`, `pd`, `pv`, `ch`, `dis` work. `fv` and `axv`'s read column need Python source and stay empty. |
