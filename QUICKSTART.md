# ND36 — quickstart

Run your code once. Replay it like a video.

---

## 1. Install it, once

```bash
cd ~/Desktop/claude_code/nd36
pip install --user -e .
nd doctor                 # tells you what is and is not set up
```

Now `nd` works from anywhere.

Want it on your Dock and in VS Code:

```bash
nd install-app            # → ~/Applications/ND36.app   (⌘-space → "ND36")
nd install-vscode         # then QUIT VS Code and reopen
```

> `~/Applications` is your **home** Applications folder, not `/Applications`.
> Finder's sidebar usually hides it. `nd install-app --into /Applications`
> puts it in the system one instead.

---

## 2. Try it on nothing of your own

```bash
nd demo
```

Records a sample program and prints the next commands to type. Nothing to
download, nothing to write.

---

## 3. Your own code, in the terminal

```bash
nd run sudoku.py        # → sudoku.nd    works on .py, .c, .cpp
nd x   sudoku.nd        # explore it
```

Inside `nd x`, press **`?`** for the commands and **`??`** for a walk
through your own trace. The six that matter:

```
afl          which functions did the work
af solve     what solve is wired to — callers, callees, its variables
axv grid     who writes grid, who reads it, on which lines
s grid       jump to the first time grid changed
pd           the code there, with hit counts
pl tries     draw a variable over the whole run
```

Full reference: **`EXPLORE.md`**.

---

## 4. Or in a window

```bash
nd app sudoku.py
```

Records it and opens a window. **Ctrl-C in the terminal** stops it.

---

## 5. What the window shows

```
 ◀ ▶ ━━━━━━━━━●━━━━━━━━━━━━━━━   ← drag this. it is the whole run.
 ▁▂▃▄▅▆▇█▇▆▅▄▃                   ← minimap: which line was running, when
 step 140,000 of 1,417,847 · sudoku.py:26 MUTATE grid

 ┌── your code ────────┐  ┌── variables right now ──┐
 │ 25    grid[r][c]=v  │  │ r      4                │
 │▸26    if solve(grid)│  │ grid   [[5,3,1,2,…      │
 │ 27      return True │  │ v      7                │
 │                     │  │        was 6            │ ← only what changed
 └─────────────────────┘  └─────────────────────────┘
```

**Everything is clickable.** A source line jumps to the next time it ran.
Any table row jumps to that step. Arrow keys step; Shift+arrow jumps 25.

---

## 6. The question only this can answer

```bash
nd watch sudoku.nd            # what variables are there?
nd watch sudoku.nd grid       # follow one through the whole run
```

```
step   where           how     grid
100    sudoku.py:26    MUTATE  [[5,3,0,0,7,…
242    sudoku.py:25    MUTATE  [[5,3,1,2,7,…
```

Every assignment, with the line. A debugger cannot do that — it only knows
*now*.

---

## 7. Six more worth knowing

```bash
nd flow   sudoku.nd     call graph + every line that never ran
nd heat   sudoku.nd     hits and milliseconds per line
nd mem    trace.nd      memory curve + the line that leaks
nd data   trace.nd      DataFrame lineage (pandas)
nd report trace.nd      one HTML file you can email
nd doctor               what is installed
```

Full reference with every option: **`COMMANDS.md`**.

---

## 8. If something is wrong

Run **`nd doctor`** first. Then:

| | |
|---|---|
| `nd: command not found` | `pip install --user -e .` in this folder |
| Installed the app, cannot find it | It is in `~/Applications`. ⌘-space → "ND36" |
| VS Code command missing | Quit VS Code **completely** and reopen |
| "no debugger found" (C/C++) | `xcode-select --install` |
| Recording feels slow | ~20 µs a step. A 1.4M-step solver takes ~18 s |

---

## The one idea

A debugger shows you **one moment, live**.
ND36 shows you **every moment, afterwards** — so you can go back.
