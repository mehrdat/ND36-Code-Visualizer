# ND36 — VS Code extension

Right-click a `.py`, `.c` or `.cpp` file → **ND36: Record and replay this
file**. It records a trace and opens the replay UI beside your editor.

## Install

From the project folder:

```bash
nd install-vscode        # or: python3 -m nd36.cli install-vscode
```

Then **quit VS Code completely and reopen it** — it only reads the
extensions folder at startup.

That copies this folder into `~/.vscode/extensions/nd36.nd-0.1.0/`, which is
what packaging a `.vsix` ends up doing anyway, and needs nothing installed.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `nd.pythonPath` | `python3` | The interpreter that can import `nd36` |
| `nd.repoPath` | *(empty)* | Folder containing `nd36/` — set this if the command cannot find it |
| `nd.port` | `7879` | Local port for the server |
| `nd.memory` | `false` | Also measure memory |

## Commands

| Command | |
|---|---|
| ND36: Record and replay this file | the main one |
| ND36: Open an existing trace (.nd) | pick a folder you already recorded |
| ND36: Stop the server | tidy up |

The panel is the same UI as `nd serve`; the extension only starts the server
and frames it.
