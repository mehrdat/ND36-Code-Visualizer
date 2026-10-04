"""
`nd serve` -- the live UI.

The static report embeds the trace in the page, which caps it at about
20,000 steps before the file gets silly. This serves the same panels from
the trace instead, so the ceiling is whatever the trace holds: a ten-million
step run scrubs exactly as smoothly as a three-hundred step one, because the
browser only ever asks for the step it is showing.

Standard library only -- `http.server` on a background thread, bound to
127.0.0.1. There is no framework here because one local reader does not need
one, and a dependency would be the first in the project.

Endpoints, all JSON:

    /api/meta                 what the trace is, the source, per-line stats
    /api/state?step=N         the stack and every variable at step N
    /api/window?lo=&hi=       a run of events, for the minimap and prefetch
    /api/history?name=X       one variable's whole life
    /api/find?kind=&name=     search the run
    /api/panels               DataFrames, tensors, memory, call graph
    /api/rerun                record the script again and reload
"""

import http.server
import json
import os
import socketserver
import threading
import urllib.parse
import webbrowser

from . import charts, graphs, lineage, schema, ui
from .store import TraceReader


class Service:
    """Everything the UI can ask for, kept behind one lock.

    A reload swaps the reader, and a request must never see half of that.
    """

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.reader = TraceReader(path)
        self._panels = None

    def reload(self):
        with self.lock:
            self.reader = TraceReader(self.path)
            self._panels = None

    # -- the hot endpoint ---------------------------------------------------

    def state(self, step):
        with self.lock:
            r = self.reader
            step = max(1, min(int(step), r.n_steps))
            st = r.state_at(step)
            ev = r.view(step)
            prev = r.state_at(step - 1) if step > 1 else {"frames": [],
                                                          "globals": {}}

            def pool(s):
                out = {}
                out.update(s["globals"])
                for f in s["frames"]:
                    out.update(f["locals"])
                return out

            before = {k: _flat(r, v, step) for k, v in pool(prev).items()}
            now = pool(st)
            variables = []
            for name in sorted(now):
                text = _flat(r, now[name], step)
                was = before.get(name)
                variables.append({"name": name, "value": text,
                                  "was": was if was != text else None,
                                  "changed": was is not None and was != text,
                                  "fresh": step > 1 and name not in before})
            stack = [{"frame": f["frame"],
                      "func": (r.funcs.get(f.get("func")) or {})
                      .get("qualname") or "?",
                      "line": r.where(f["loc"])[1]} for f in st["frames"]]
            return {"step": step, "line": ev["line"], "file": ev["file"],
                    "kind": ev["kind"], "name": ev["name"],
                    "stack": stack, "variables": variables,
                    "n_steps": r.n_steps}

    def window(self, lo, hi, buckets=600):
        """Line numbers over a range, downsampled -- this draws the minimap."""
        with self.lock:
            r = self.reader
            lo = max(1, int(lo))
            hi = min(int(hi), r.n_steps)
            n = max(1, hi - lo + 1)
            stride = max(1, n // buckets)
            out = []
            for step in range(lo, hi + 1, stride):
                t, k, fr, loc, nm, rk, ri, rf, pid = r.raw(step)
                out.append([step, r.where(loc)[1] or 0, k, fr])
            return {"lo": lo, "hi": hi, "stride": stride, "rows": out}

    def history(self, name, limit=4000):
        with self.lock:
            r = self.reader
            rows = r.history_of(name, limit=limit)
            return {"name": name, "rows": [
                {"step": h["step"], "line": h["line"], "kind": h["kind"],
                 "value": _flat(r, h["ref"], h["step"])} for h in rows]}

    def find(self, kind=None, name=None, limit=200):
        with self.lock:
            r = self.reader
            rows = r.find(kind=kind or None, name=name or None, limit=limit)
            return {"rows": [{"step": e["step"], "line": e["line"],
                              "kind": e["kind"], "name": e["name"],
                              "value": _flat(r, e["ref"], e["step"])}
                             for e in rows]}

    def meta(self):
        with self.lock:
            r = self.reader
            script = r.meta.get("script", "")
            stats = {}
            for (path, line), (hits, t_ns) in r.line_stats().items():
                if path == script:
                    stats[line] = [hits, round(t_ns / 1e6, 3)]
            return {"script": os.path.basename(script), "path": script,
                    "source": r.source(script).splitlines(),
                    "n_steps": r.n_steps, "stats": stats,
                    "kinds": r.kind_counts(),
                    "names": sorted(r.name_ids),
                    "meta": {k: r.meta.get(k) for k in
                             ("python", "platform", "driver", "language",
                              "compiler", "backend", "wall_seconds",
                              "error", "memory")}}

    def panels(self):
        """The Phase 2-5 panels. Built once, then cached: a call graph walks
        the whole log, which is not something to redo on every poll."""
        with self.lock:
            if self._panels is not None:
                return self._panels
            r = self.reader
            frames = []
            for row in lineage.frame_lineage(r):
                where, shape, what = lineage.summary_line(row)
                frames.append({"where": where, "name": row["name"],
                               "shape": shape, "what": what,
                               "warn": row["warn"], "step": row["step"],
                               "rows": row["shape"][0] if row["shape"] else 0})
            tensors = [{"where": "%s:%s" % (os.path.basename(t["file"] or "?"),
                                            t["line"]),
                        "name": t["name"], "shape": str(tuple(t["shape"])),
                        "dtype": t["dtype"], "device": t["device"],
                        "grad_fn": t["grad_fn"], "step": t["step"]}
                       for t in lineage.tensor_lineage(r)[:200]]
            mismatch = [{"where": "%s:%s"
                         % (os.path.basename(m["file"] or "?"), m["line"]),
                         "exc": m["exc"], "msg": m["msg"], "step": m["step"],
                         "tensors": [{"name": t["name"] or "?",
                                      "shape": str(tuple(t["shape"]))}
                                     for t in m["tensors"][:10]]}
                        for m in lineage.shape_mismatch(r)]
            nodes, edges = graphs.call_graph(r)
            call = charts.graph_svg(
                sorted(nodes.values(), key=lambda n: n["line"]),
                [e for e in edges if e["src"] is not None],
                label_of=lambda n: n["name"],
                tip_of=lambda n: "%s (line %d)\n%s calls"
                % (n["name"], n["line"], "{:,}".format(n["calls"])),
                kind_of=lambda n: "called" if n["calls"] else "dead",
                weight_of=True)
            curve = [[(p["at_ns"] - r.memcurve()[0]["at_ns"]) / 1e6,
                      p["cur"] / 1024.0] for p in r.memcurve()] \
                if r.memcurve() else []
            self._panels = {"frames": frames, "tensors": tensors,
                            "mismatch": mismatch, "callgraph": call,
                            "memory": curve,
                            "strip": [[s["step"], s["blocks"]]
                                      for s in r.samples()]}
            return self._panels


def _flat(reader, ref, step):
    """A ref as one short string for the table."""
    if ref is None:
        return None
    value = reader.resolve(ref, step)
    if isinstance(value, dict):
        text = value["repr"]
        extra = [("%s=%s" % (k, value["summary"][k]))
                 for k in ("shape", "len", "dtype", "device")
                 if k in value["summary"]]
        return "%s  %s" % (text, " ".join(extra)) if extra else text
    return repr(value)


# --------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    service = None
    page = None

    def log_message(self, *args):
        pass                         # one local reader; the noise is not news

    def _send(self, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = body.encode("utf8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                     # the browser moved on; normal when scrubbing

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        def arg(key, default=None):
            return query.get(key, [default])[0]

        try:
            if parsed.path in ("/", "/index.html"):
                return self._send(self.page, "text/html; charset=utf-8")
            if parsed.path == "/favicon.ico":
                # Browsers ask unprompted; a 404 in the console reads like a
                # bug in the page.
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed.path == "/api/meta":
                return self._send(json.dumps(self.service.meta(),
                                             default=str))
            if parsed.path == "/api/state":
                return self._send(json.dumps(
                    self.service.state(arg("step", 1)), default=str))
            if parsed.path == "/api/window":
                return self._send(json.dumps(self.service.window(
                    arg("lo", 1), arg("hi", 1)), default=str))
            if parsed.path == "/api/history":
                return self._send(json.dumps(
                    self.service.history(arg("name", "")), default=str))
            if parsed.path == "/api/find":
                return self._send(json.dumps(self.service.find(
                    arg("kind"), arg("name")), default=str))
            if parsed.path == "/api/panels":
                return self._send(json.dumps(self.service.panels(),
                                             default=str))
        except Exception as exc:
            body = json.dumps({"error": "%s: %s" % (type(exc).__name__, exc)})
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode("utf8"))
            return
        self.send_error(404)

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path == "/api/rerun":
            try:
                message = self.service.rerun()
            except Exception as exc:
                message = {"ok": False, "error": str(exc)}
            return self._send(json.dumps(message, default=str))
        self.send_error(404)


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(trace_path, host="127.0.0.1", port=7878, open_browser=True,
          rerun=None, block=True):
    service = Service(trace_path)
    service.rerun = rerun or (lambda: {"ok": False,
                                       "error": "this trace was opened from "
                                                "a file, so there is nothing "
                                                "to re-run"})
    handler = type("Handler", (_Handler,),
                   {"service": service, "page": PAGE % {"css": ui.CSS}})

    for candidate in range(port, port + 20):
        try:
            httpd = _Server((host, candidate), handler)
            break
        except OSError:
            continue
    else:
        raise RuntimeError("no free port between %d and %d" % (port, port + 20))

    url = "http://%s:%d/" % (host, httpd.server_address[1])
    if not block:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd, url, service
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    return httpd, url, service


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>viz</title>
<style>%(css)s
/* live-only pieces */
.topbar{position:sticky;top:0;z-index:7;background:var(--bg);
 border-bottom:1px solid var(--line);padding:9px 0 9px;margin-bottom:4px}
.live{display:inline-block;width:7px;height:7px;border-radius:50%%;
 background:var(--good);margin-right:6px;vertical-align:1px}
#search{font:inherit;font-size:12px;font-family:var(--mono);padding:5px 9px;
 border:1px solid var(--line-2);border-radius:7px;background:var(--surface);
 color:var(--ink);min-width:150px}
#results{max-height:190px;overflow:auto;border:1px solid var(--line);
 border-radius:8px;margin-top:8px;display:none}
#results div{padding:4px 10px;font-family:var(--mono);font-size:12px;
 cursor:pointer;border-bottom:1px solid var(--line)}
#results div:hover{background:color-mix(in oklab,var(--s1) 14%%,transparent)}
#minimap{width:100%%;height:46px;display:block;margin:4px 0 8px;cursor:pointer;
 border:1px solid var(--line);border-radius:8px;background:var(--bg)}
.pill{font-size:11px;font-family:var(--mono);color:var(--ink-3);
 border:1px solid var(--line);border-radius:999px;padding:2px 9px}
.loading{opacity:.45}
.vgrid{display:grid;gap:12px;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr)}
@media (max-width:900px){.vgrid{grid-template-columns:1fr}}
</style></head><body>
<button class="themebtn" id="theme">light / dark</button>
<div class="wrap">
<div class="topbar">
  <h1 id="title">…</h1>
  <div class="sub"><span class="live"></span><span id="sub"></span></div>
</div>

<section>
  <div class="bar-row">
    <button id="first" title="first step">⏮</button>
    <button id="prev">◀</button>
    <button id="play">▶</button>
    <button id="next">▶|</button>
    <button id="last" title="last step">⏭</button>
    <input type="range" id="slider" min="1" value="1">
    <input id="search" placeholder="variable name…" list="names">
    <datalist id="names"></datalist>
    <button id="go">find</button>
    <button id="rerun" title="record the script again">⟳ re-run</button>
  </div>
  <canvas id="minimap" height="46"></canvas>
  <div id="label"></div>
  <div id="results"></div>
  <div class="vgrid">
    <div id="codepane"></div>
    <div class="vars"><div class="frame-head" id="frames"></div>
      <table class="grid"><tbody id="varbody"></tbody></table></div>
  </div>
</section>

<section>
  <h2>Variable history</h2>
  <p class="lede">Type a name above and press find. Every assignment, with the
  line it happened on — click one to jump there.</p>
  <div id="hist"><p class="empty">nothing selected yet</p></div>
</section>

<section>
  <h2>Call graph</h2>
  <div class="scroll" id="callgraph"><p class="empty">loading…</p></div>
</section>

<section id="framesec" style="display:none">
  <h2>DataFrames</h2>
  <div id="frametable"></div>
</section>

<section id="tensorsec" style="display:none">
  <h2>Tensors</h2>
  <div id="tensortable"></div>
</section>
</div>
<div id="tip"></div>
<script>
const $ = s => document.querySelector(s);
const esc = t => String(t).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

let META = null, N = 1, cur = 1, rows = [], timer = null;

/* ---- fetching -------------------------------------------------------
   The whole reason this exists: the page never holds the trace. It asks
   for the one step it is showing, so a ten-million step run costs the same
   as a three-hundred step one. A small cache and a prefetch either side
   keep dragging the slider at 60fps.                                   */
const cache = new Map();
async function state(step){
  if (cache.has(step)) return cache.get(step);
  const p = fetch('/api/state?step=' + step).then(r => r.json());
  cache.set(step, p);
  if (cache.size > 900) for (const k of [...cache.keys()].slice(0, 400))
    cache.delete(k);
  return p;
}
function prefetch(step){
  for (const d of [1, 2, 3, 5, 8, -1, -2, -3])
    { const s = step + d; if (s >= 1 && s <= N) state(s); }
}

async function show(step){
  step = Math.max(1, Math.min(N, Math.round(step)));
  cur = step;
  if (+$('#slider').value !== step) $('#slider').value = step;
  const token = ++show._t;
  const s = await state(step);
  if (token !== show._t) return;        // a newer drag won
  $('#label').innerHTML = 'step <b>' + step.toLocaleString() + '</b> of ' +
    N.toLocaleString() + ' · ' + esc(META.script) + ':<b>' + s.line +
    '</b> <span class="kind">' + esc(s.kind || '') + '</span> ' +
    esc(s.name || '');
  paintCode(s.line);
  $('#frames').textContent = s.stack.length
    ? s.stack.map(f => f.func + ':' + f.line).join('  ►  ')
    : '(module level)';
  $('#varbody').innerHTML = s.variables.map(v =>
    '<tr class="' + ((v.changed || v.fresh) ? 'changed' : '') +
    '"><td class="name">' + esc(v.name) + '</td><td><code>' +
    esc(String(v.value).slice(0, 180)) + '</code>' +
    (v.was ? '<div class="was">was ' + esc(String(v.was).slice(0,70)) +
             '</div>' : '') + '</td></tr>').join('') ||
    '<tr><td colspan="2" class="name">' +
    (step > 1 ? 'the program has finished — every frame has returned, so ' +
                'nothing is in scope any more'
              : 'nothing in scope yet') + '</td></tr>';
  drawMap();
  prefetch(step);
}
show._t = 0;

/* ---- code pane ------------------------------------------------------ */
function paintCode(line){
  const pane = $('#codepane');
  if (!rows.length){
    rows = META.source.map((text, i) => {
      const d = document.createElement('div');
      d.className = 'row';
      d.innerHTML = '<span class="gut"></span><span class="ln">' + (i+1) +
                    '</span><code></code>';
      d.querySelector('code').textContent = text || ' ';
      if (META.stats[i+1]) d.querySelector('.gut').style.background =
        'var(--s1)';
      d.onclick = () => jumpToLine(i + 1);
      return d;
    });
    rows.forEach(r => pane.appendChild(r));
  }
  rows.forEach(r => r.classList.remove('cur'));
  const r = rows[line - 1];
  if (r){
    r.classList.add('cur');
    if (r.offsetTop < pane.scrollTop + 20 ||
        r.offsetTop > pane.scrollTop + pane.clientHeight - 40)
      pane.scrollTop = r.offsetTop - pane.clientHeight / 2;
  }
}

/* Click a source line to jump to the next time it ran. */
async function jumpToLine(line){
  const w = await (await fetch('/api/window?lo=1&hi=' + N)).json();
  const hit = w.rows.find(r => r[0] > cur && r[1] === line) ||
              w.rows.find(r => r[1] === line);
  if (hit) show(hit[0]);
}

/* ---- minimap: which line was running, across the whole run ---------- */
let mapRows = null;
async function loadMap(){
  mapRows = (await (await fetch('/api/window?lo=1&hi=' + N)).json()).rows;
  drawMap();
}
function drawMap(){
  const c = $('#minimap');
  if (!mapRows || !mapRows.length) return;
  const dpr = devicePixelRatio || 1, w = c.clientWidth, h = 46;
  c.width = w * dpr; c.height = h * dpr;
  const g = c.getContext('2d'); g.setTransform(dpr,0,0,dpr,0,0);
  g.clearRect(0,0,w,h);
  const css = getComputedStyle(document.body);
  const maxLine = Math.max(...mapRows.map(r => r[1])) || 1;
  g.fillStyle = css.getPropertyValue('--s1').trim() + '66';
  for (const [step, line] of mapRows){
    const x = (step - 1) / (N - 1 || 1) * w;
    const y = 3 + (line / maxLine) * (h - 8);
    g.fillRect(x, y, 1.2, 1.6);
  }
  const x = (cur - 1) / (N - 1 || 1) * w;
  g.strokeStyle = css.getPropertyValue('--s2').trim();
  g.lineWidth = 1.5;
  g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke();
}
$('#minimap').addEventListener('click', e => {
  const r = e.target.getBoundingClientRect();
  show(1 + (e.clientX - r.left) / r.width * (N - 1));
});
addEventListener('resize', drawMap);

/* ---- search / history ----------------------------------------------- */
async function lookup(){
  const name = $('#search').value.trim();
  if (!name) return;
  const data = await (await fetch('/api/history?name=' +
                                  encodeURIComponent(name))).json();
  const box = $('#hist');
  if (!data.rows.length){
    box.innerHTML = '<p class="empty">no assignments to <code>' +
                    esc(name) + '</code> in this run</p>';
    return;
  }
  box.innerHTML = '<table class="grid"><thead><tr><th>step</th>' +
    '<th>where</th><th>how</th><th>' + esc(name) + '</th></tr></thead><tbody>' +
    data.rows.map(r => '<tr data-step="' + r.step + '"><td class="num">' +
      r.step.toLocaleString() + '</td><td class="where">' +
      esc(META.script) + ':' + r.line + '</td><td class="name">' +
      esc(r.kind) + '</td><td><code>' +
      esc(String(r.value).slice(0,120)) + '</code></td></tr>').join('') +
    '</tbody></table>';
  box.querySelectorAll('tr[data-step]').forEach(tr =>
    tr.onclick = () => { show(+tr.dataset.step);
      $('#slider').scrollIntoView({behavior:'smooth', block:'center'}); });
}
$('#go').onclick = lookup;
$('#search').addEventListener('keydown', e => {
  if (e.key === 'Enter') lookup();
});

/* ---- controls -------------------------------------------------------- */
$('#slider').addEventListener('input', e => show(+e.target.value));
$('#prev').onclick  = () => show(cur - 1);
$('#next').onclick  = () => show(cur + 1);
$('#first').onclick = () => show(1);
$('#last').onclick  = () => show(N);
$('#play').onclick = e => {
  if (timer){ clearInterval(timer); timer = null; e.target.textContent = '▶'; }
  else { e.target.textContent = '⏸';
    timer = setInterval(() => {
      if (cur >= N){ clearInterval(timer); timer = null;
                     e.target.textContent = '▶'; return; }
      show(cur + 1);
    }, 110); }
};
addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' && e.target.id === 'search') return;
  if (e.key === 'ArrowRight') show(cur + (e.shiftKey ? 25 : 1));
  if (e.key === 'ArrowLeft')  show(cur - (e.shiftKey ? 25 : 1));
});
$('#rerun').onclick = async e => {
  e.target.textContent = '⟳ recording…'; e.target.disabled = true;
  const res = await (await fetch('/api/rerun', {method:'POST'})).json();
  e.target.textContent = '⟳ re-run'; e.target.disabled = false;
  if (!res.ok){ alert(res.error || 'could not re-run'); return; }
  cache.clear(); rows = []; $('#codepane').innerHTML = '';
  await boot();
};

/* ---- panels ---------------------------------------------------------- */
async function panels(){
  const p = await (await fetch('/api/panels')).json();
  $('#callgraph').innerHTML = p.callgraph || '<p class="empty">none</p>';
  if (p.frames && p.frames.length){
    $('#framesec').style.display = '';
    const peak = Math.max(...p.frames.map(f => f.rows)) || 1;
    $('#frametable').innerHTML =
      '<table class="grid"><thead><tr><th>where</th><th>name</th>' +
      '<th>shape</th><th>what changed</th><th>rows</th></tr></thead><tbody>' +
      p.frames.map(f => '<tr class="' + (f.warn ? 'warn' : '') +
        '" data-step="' + f.step + '"><td class="where">' + esc(f.where) +
        '</td><td class="name">' + esc(f.name || '') + '</td>' +
        '<td class="num">' + esc(f.shape) + '</td><td>' + esc(f.what) +
        (f.warn ? ' <span class="flag">big row loss</span>' : '') +
        '</td><td class="barcell"><span class="rowbar" style="width:' +
        (100 * f.rows / peak).toFixed(1) + '%%"></span><span class="n">' +
        f.rows.toLocaleString() + '</span></td></tr>').join('') +
      '</tbody></table>';
    $('#frametable').querySelectorAll('tr[data-step]').forEach(tr =>
      tr.onclick = () => show(+tr.dataset.step));
  }
  if ((p.tensors && p.tensors.length) || (p.mismatch && p.mismatch.length)){
    $('#tensorsec').style.display = '';
    let html = '';
    for (const m of p.mismatch || [])
      html += '<div class="err"><b>' + esc(m.exc) + ' at ' + esc(m.where) +
              '</b><br>' + esc(m.msg) + '</div>';
    html += '<table class="grid"><thead><tr><th>where</th><th>name</th>' +
      '<th>shape</th><th>dtype</th><th>produced by</th></tr></thead><tbody>' +
      (p.tensors || []).map(t => '<tr data-step="' + t.step +
        '"><td class="where">' + esc(t.where) + '</td><td class="name">' +
        esc(t.name || '') + '</td><td class="name">' + esc(t.shape) +
        '</td><td class="name">' + esc(t.dtype || '') +
        '</td><td class="where">' + esc(t.grad_fn || '') +
        '</td></tr>').join('') + '</tbody></table>';
    $('#tensortable').innerHTML = html;
    $('#tensortable').querySelectorAll('tr[data-step]').forEach(tr =>
      tr.onclick = () => show(+tr.dataset.step));
  }
}

/* ---- boot ------------------------------------------------------------ */
async function boot(){
  META = await (await fetch('/api/meta')).json();
  N = META.n_steps || 1;
  $('#slider').max = N;
  $('#title').textContent = META.script;
  const m = META.meta;
  $('#sub').textContent = [m.driver, m.language || (m.python || '').split(' ')[0],
                           m.compiler, m.platform,
                           N.toLocaleString() + ' steps']
                          .filter(Boolean).join(' · ');
  $('#names').innerHTML = META.names.map(n =>
    '<option value="' + esc(n) + '">').join('');
  if (m.error) $('#sub').textContent += ' · raised ' + m.error;
  await show(1);
  loadMap(); panels();
}

const tb = $('#theme');
try { const saved = localStorage.getItem('viz-theme');
      if (saved) document.documentElement.dataset.theme = saved; } catch(_){}
tb.onclick = () => {
  const dark = matchMedia('(prefers-color-scheme: dark)').matches;
  const now = document.documentElement.dataset.theme || (dark?'dark':'light');
  const next = now === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('viz-theme', next); } catch(_){}
  drawMap();
};
boot();
</script></body></html>
"""
