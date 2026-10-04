/*
 * ND36 for VS Code.
 *
 * The extension is deliberately thin. It starts `nd serve --no-browser`
 * on a local port and shows the very same page in a webview, so there is
 * one UI to maintain rather than three. Everything the panel can do -- the
 * step slider, variable history, the DataFrame ribbon -- is the server's
 * doing, not the extension's.
 *
 * The one piece of real work here is asExternalUri: in a remote or
 * Codespaces window the webview runs somewhere else entirely, and a bare
 * 127.0.0.1 URL would point at the wrong machine. asExternalUri forwards
 * the port and hands back a URL that resolves from wherever the UI is.
 */

const vscode = require('vscode');
const cp = require('child_process');
const path = require('path');
const http = require('http');
const fs = require('fs');

let server = null;      // the child process
let panel = null;       // the webview
let output = null;

function log(text) {
  if (!output) output = vscode.window.createOutputChannel('ND36');
  output.appendLine(text);
}

function config() {
  const c = vscode.workspace.getConfiguration('nd');
  return {
    command: c.get('command') || '',
    python: c.get('pythonPath') || 'python3',
    repo: c.get('repoPath') || '',
    port: c.get('port') || 7879,
    memory: !!c.get('memory'),
  };
}

/** Wait until the server answers, rather than guessing at a delay.
 *
 * `alive()` lets us fail in half a second when the command could not even
 * start, instead of waiting out the whole timeout and then saying nothing
 * useful. A big script legitimately takes a while to record, so the
 * timeout only applies while the process is still running. */
function waitForServer(port, alive, seconds = 600) {
  const deadline = Date.now() + seconds * 1000;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const req = http.get(
        { host: '127.0.0.1', port, path: '/api/meta', timeout: 500 },
        res => { res.resume(); resolve(); });
      req.on('error', () => {
        if (!alive()) return reject(new Error('EXITED'));
        if (Date.now() > deadline) return reject(new Error('TIMEOUT'));
        setTimeout(attempt, 250);
      });
      req.on('timeout', () => { req.destroy(); });
    };
    attempt();
  });
}

/** Where ND36 might be. VS Code often has a shorter PATH than your shell,
 *  so `~/.local/bin/nd` is missing even though `nd` works in the terminal. */
function candidates(cfg) {
  const home = process.env.HOME || '';
  const list = [];
  if (cfg.command) list.push({ cmd: cfg.command, args: [] });
  list.push({ cmd: 'nd', args: [] });
  for (const p of [`${home}/.local/bin/nd`, '/usr/local/bin/nd',
                   '/opt/homebrew/bin/nd', `${home}/.pyenv/shims/nd`]) {
    if (fs.existsSync(p)) list.push({ cmd: p, args: [] });
  }
  list.push({ cmd: cfg.python, args: ['-m', 'nd36.cli'] });
  return list;
}

function stop() {
  if (server) { try { server.kill(); } catch (_) {} server = null; }
  if (panel) { panel.dispose(); panel = null; }
}

async function start(target, title) {
  stop();
  const cfg = config();
  const cwd = cfg.repo || (vscode.workspace.workspaceFolders || [])[0]
    ?.uri.fsPath || path.dirname(target);
  const tail = [];
  let lastError = null;

  for (const c of candidates(cfg)) {
    const args = [...c.args, 'serve', target,
                  '--port', String(cfg.port), '--no-browser'];
    if (cfg.memory) args.push('--memory');
    log(`$ ${c.cmd} ${args.join(' ')}   (cwd ${cwd})`);

    let exited = false;
    try {
      server = cp.spawn(c.cmd, args, { cwd });
    } catch (err) {                      // ENOENT: that command is not there
      log(`  cannot run ${c.cmd}: ${err.message}`);
      lastError = err;
      continue;
    }
    tail.length = 0;
    const keep = d => {
      const text = String(d).trimEnd();
      log(text);
      for (const line of text.split('\n')) {
        tail.push(line);
        if (tail.length > 12) tail.shift();
      }
    };
    server.stdout.on('data', keep);
    server.stderr.on('data', keep);
    server.on('error', err => { exited = true; lastError = err; log(String(err)); });
    server.on('exit', code => { exited = true; log(`server exited (${code})`); });

    try {
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification,
          title: `ND36: recording ${path.basename(target)}…` },
        () => waitForServer(cfg.port, () => !exited));
      lastError = null;
      break;                             // this one worked
    } catch (err) {
      stop();
      if (err.message === 'TIMEOUT') {
        throw new Error('recording is taking longer than 10 minutes. ' +
                        'Record it in a terminal instead: nd serve ' +
                        path.basename(target));
      }
      lastError = new Error(tail.join('\n') ||
                            `${c.cmd} would not start`);
      log(`  ${c.cmd} did not come up; trying the next way to run ND36`);
    }
  }
  if (lastError) {
    throw new Error(
      'could not start ND36.\n' + lastError.message +
      '\n\nFix it in a terminal first — run:  nd doctor\n' +
      'then set "nd.command" to the full path that `which nd` prints.');
  }

  // Works locally and over a remote/Codespaces connection alike.
  const external = await vscode.env.asExternalUri(
    vscode.Uri.parse(`http://127.0.0.1:${cfg.port}/`));

  panel = vscode.window.createWebviewPanel(
    'nd36', title, vscode.ViewColumn.Beside,
    { enableScripts: true, retainContextWhenHidden: true });
  panel.webview.html = frame(external.toString());
  panel.onDidDispose(() => { panel = null; stop(); });
}

function frame(url) {
  return `<!doctype html><html><head><meta charset="utf-8">
<style>
  html,body,iframe{margin:0;padding:0;height:100%;width:100%;border:0}
  body{background:var(--vscode-editor-background)}
</style></head><body>
<iframe src="${url}" sandbox="allow-scripts allow-same-origin allow-forms"
        allow="clipboard-write"></iframe>
</body></html>`;
}

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand('nd.visualizeFile',
      async uri => {
        const file = (uri && uri.fsPath) ||
          vscode.window.activeTextEditor?.document.uri.fsPath;
        if (!file) {
          vscode.window.showWarningMessage(
            'Open a .py, .c or .cpp file first.');
          return;
        }
        try {
          await start(file, `Replay — ${path.basename(file)}`);
        } catch (err) {
          vscode.window.showErrorMessage(
            `ND36: ${err.message}. See the ND36 ` +
            `output channel; you may need to set nd.repoPath.`);
          log(String(err));
        }
      }),

    vscode.commands.registerCommand('nd.openTrace', async () => {
      const picked = await vscode.window.showOpenDialog({
        canSelectFolders: true, canSelectFiles: false,
        openLabel: 'Open .vizt trace',
      });
      if (!picked || !picked.length) return;
      try {
        await start(picked[0].fsPath,
                    `Replay — ${path.basename(picked[0].fsPath)}`);
      } catch (err) {
        vscode.window.showErrorMessage(`ND36: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand('nd.stop', stop),
    { dispose: stop });
}

module.exports = { activate, deactivate: stop };
