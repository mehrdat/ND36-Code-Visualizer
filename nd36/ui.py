"""
The look of the thing, in one place.

Both the static report and the live server render the same panels, so they
share these styles rather than drifting apart. Colours come from the
validated reference palette; light and dark are both chosen, never an
automatic flip of one another.

Note there are no %-escapes in here. The CSS is substituted into a template
as a VALUE, not formatted, so a `%%` would survive into the page as a literal
`%%` and `width:100%%` would simply not be CSS.
"""

CSS = r"""
:root{
  color-scheme: light;
  --bg:#f6f6f4; --surface:#fcfcfb; --line:#e3e2dd; --line-2:#cfceC7;
  --ink:#0b0b0b; --ink-2:#52514e; --ink-3:#86857f;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100;
  --s5:#e87ba4; --s6:#008300; --s7:#4a3aa7; --s8:#e34948;
  --good:#1baf7a; --crit:#e34948; --dead:#b9b8b1;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Helvetica,sans-serif;
}
@media (prefers-color-scheme: dark){
 :root:where(:not([data-theme="light"])){
  color-scheme: dark;
  --bg:#121211; --surface:#1a1a19; --line:#2e2e2b; --line-2:#3f3f3b;
  --ink:#ffffff; --ink-2:#c3c2b7; --ink-3:#8d8c83;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
  --s5:#d55181; --s6:#008300; --s7:#9085e9; --s8:#e66767;
  --good:#199e70; --crit:#e66767; --dead:#4a4a45;
 }
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --bg:#121211; --surface:#1a1a19; --line:#2e2e2b; --line-2:#3f3f3b;
  --ink:#ffffff; --ink-2:#c3c2b7; --ink-3:#8d8c83;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
  --s5:#d55181; --s6:#008300; --s7:#9085e9; --s8:#e66767;
  --good:#199e70; --crit:#e66767; --dead:#4a4a45;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
 font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:0 16px 72px}
header{padding:30px 0 18px}
h1{margin:0;font-size:25px;letter-spacing:-.02em;font-family:var(--mono)}
.sub{color:var(--ink-3);font-size:12.5px;margin-top:5px}
h2{font-size:15px;margin:0 0 3px;letter-spacing:-.01em}
.lede{color:var(--ink-2);font-size:12.5px;margin:0 0 14px;max-width:72ch}
section{background:var(--surface);border:1px solid var(--line);
 border-radius:12px;padding:18px;margin-top:16px}
.note{color:var(--ink-3);font-size:12px;margin:10px 0 0}
.err{background:color-mix(in oklab,var(--crit) 12%,var(--surface));
 border:1px solid var(--crit);border-radius:8px;padding:9px 12px;
 font-size:12.5px;margin:12px 0 0}
.empty{color:var(--ink-3);font-size:12.5px;font-style:italic}
code,.mono{font-family:var(--mono)}

/* stat tiles */
.tiles{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(168px,1fr))}
.tile{background:var(--surface);border:1px solid var(--line);
 border-radius:10px;padding:12px 13px}
.tl{font-size:11px;text-transform:uppercase;letter-spacing:.07em;
 color:var(--ink-3)}
.tv{font-size:25px;font-family:var(--mono);letter-spacing:-.03em;
 margin:3px 0 1px}
.ts{font-size:11.5px;color:var(--ink-3)}

/* charts */
.chart .cat{font-size:10.5px;fill:var(--ink-2);text-anchor:end;
 font-family:var(--mono)}
.chart .val{font-size:10.5px;fill:var(--ink-3);font-family:var(--mono)}
.chart .bar{fill:var(--s1)}
.chart .bar:hover,.chart .bar:focus{fill:var(--s2);outline:none}
.chart .axis{stroke:var(--line-2);stroke-width:1}
.chart .series{stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.chart .cross{stroke:var(--ink-3);stroke-width:1;stroke-dasharray:2 2}
.sparks{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(250px,1fr))}
.spark{border:1px solid var(--line);border-radius:9px;padding:9px 10px 4px;
 background:var(--surface)}
.spark-head{display:flex;justify-content:space-between;align-items:baseline;
 gap:8px;margin-bottom:2px}
.spark-name{font-family:var(--mono);font-size:12px}
.spark-range{font-family:var(--mono);font-size:10.5px;color:var(--ink-3)}

/* graphs */
.scroll{overflow:auto;max-height:620px;padding:4px}
.graph .edge{stroke:var(--ink-3);fill:none;opacity:.85}
.graph .edge.dead{stroke:var(--dead);stroke-dasharray:4 4;opacity:.9}
.graph .edge.back{stroke:var(--s2)}
.graph .arrow{fill:var(--ink-3)}
.graph .elabel{font-size:9.5px;fill:var(--ink-3);font-family:var(--mono)}
.graph .node rect{fill:var(--surface);stroke:var(--line-2);stroke-width:1.5}
.graph .node text{font-size:11px;fill:var(--ink);text-anchor:middle;
 font-family:var(--mono)}
.graph .node text.ln{font-size:9.5px;fill:var(--ink-3);text-anchor:end}
.graph .node text.src{font-size:10.5px;fill:var(--ink);text-anchor:start}
.graph .k-dead text.src{fill:var(--ink-3)}
.graph .k-called rect,.graph .k-entry rect{stroke:var(--s1);
 fill:color-mix(in oklab,var(--s1) 9%,var(--surface))}
.graph .k-cond rect{stroke:var(--s4)}
.graph .k-loop rect{stroke:var(--s3)}
.graph .k-return rect,.graph .k-exit rect{stroke:var(--s7)}
.graph .k-dead rect{stroke:var(--dead);stroke-dasharray:4 3}
.graph .k-dead text{fill:var(--ink-3)}
.graph .node:hover rect,.graph .node:focus rect{stroke-width:2.5;outline:none}

/* tabs */
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.tab{font:inherit;font-size:12px;font-family:var(--mono);cursor:pointer;
 background:var(--surface);color:var(--ink-2);border:1px solid var(--line);
 border-radius:7px;padding:4px 9px}
.tab.on{border-color:var(--s1);color:var(--ink);
 background:color-mix(in oklab,var(--s1) 10%,var(--surface))}
.badge{color:var(--ink-3);font-size:10.5px}
.pane{display:none}.pane.on{display:block}

/* source with heat */
.code{font-family:var(--mono);font-size:12px;overflow:auto;max-height:560px;
 border:1px solid var(--line);border-radius:9px}
.code .row{display:flex;align-items:stretch;white-space:pre}
.code .row.hot{background:color-mix(in oklab,var(--s2) 8%,transparent)}
.code .gut{width:5px;flex:none}
.code .ln{width:46px;flex:none;text-align:right;padding-right:10px;
 color:var(--ink-3)}
.code .hits{width:62px;flex:none;text-align:right;padding-right:10px;
 color:var(--s1);font-size:11px}
.code .ms{width:54px;flex:none;text-align:right;padding-right:14px;
 color:var(--ink-3);font-size:11px}
.code code{padding-right:16px}

/* step explorer */
.bar-row{display:flex;align-items:center;gap:9px;flex-wrap:wrap;
 margin-bottom:12px}
.bar-row button{font:inherit;font-size:12px;cursor:pointer;min-width:34px;
 background:var(--surface);color:var(--ink);border:1px solid var(--line-2);
 border-radius:7px;padding:5px 9px}
.bar-row button:hover{border-color:var(--s1)}
#slider{flex:1;min-width:200px;accent-color:var(--s1)}
#strip{width:100%;height:38px;display:block;margin:2px 0 8px;cursor:pointer;
 border:1px solid var(--line);border-radius:8px;background:var(--bg)}
.sub-h{font-size:13px;margin:18px 0 2px}
#label{font-family:var(--mono);font-size:13px;margin:0 0 10px;
 padding:7px 11px;background:var(--bg);border:1px solid var(--line);
 border-radius:8px;display:flex;gap:4px;flex-wrap:wrap;align-items:baseline}
#label b{color:var(--s1)}
#label .kind{color:var(--s2)}
.split{display:grid;gap:12px;grid-template-columns:minmax(0,1.35fr) minmax(0,1fr)}
@media (max-width:860px){.split{grid-template-columns:1fr}}
#codepane{font-family:var(--mono);font-size:12px;overflow:auto;max-height:430px;
 border:1px solid var(--line);border-radius:9px}
#codepane .row{display:flex;white-space:pre}
#codepane .row.cur{background:color-mix(in oklab,var(--s1) 16%,transparent);
 outline:1px solid var(--s1)}
#codepane .ln{width:44px;flex:none;text-align:right;padding-right:10px;
 color:var(--ink-3)}
#codepane .gut{width:4px;flex:none}
.vars{border:1px solid var(--line);border-radius:9px;overflow:auto;
 max-height:430px}
.frame-head{font-family:var(--mono);font-size:11px;color:var(--ink-3);
 padding:6px 10px;background:var(--bg);border-bottom:1px solid var(--line);
 position:sticky;top:0}
table.grid{width:100%;border-collapse:collapse;font-size:12px}
table.grid th{text-align:left;font-weight:600;font-size:10.5px;
 text-transform:uppercase;letter-spacing:.06em;color:var(--ink-3);
 padding:6px 10px;border-bottom:1px solid var(--line);position:sticky;top:0;
 background:var(--bg)}
table.grid td{padding:4px 10px;border-bottom:1px solid var(--line);
 vertical-align:top}
table.grid tr:last-child td{border-bottom:none}
table.grid .name,table.grid .where,table.grid .num{font-family:var(--mono)}
table.grid .num{text-align:right;color:var(--ink-3)}
table.grid .where{color:var(--ink-3)}
tr.changed{background:color-mix(in oklab,var(--s4) 15%,transparent)}
tr.warn{background:color-mix(in oklab,var(--s8) 13%,transparent)}
tr.warn .name{font-weight:700}
.flag{font-size:10.5px;color:var(--s8);border:1px solid var(--s8);
 border-radius:5px;padding:0 5px;margin-left:6px;white-space:nowrap}
.barcell{position:relative;min-width:120px;text-align:right;
 font-family:var(--mono)}
.rowbar{position:absolute;left:0;top:7px;height:11px;border-radius:3px;
 background:color-mix(in oklab,var(--s1) 45%,transparent)}
.barcell .n{position:relative;color:var(--ink-2)}
tr.changed .name{font-weight:700}
.was{color:var(--ink-3);font-size:11px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;
 color:var(--ink-2);margin-top:10px}
.legend i{display:inline-block;width:11px;height:11px;border-radius:3px;
 margin-right:5px;vertical-align:-1px;border:1.5px solid}
#tip{position:fixed;z-index:9;pointer-events:none;display:none;
 background:var(--ink);color:var(--bg);font-size:11.5px;font-family:var(--mono);
 padding:5px 8px;border-radius:6px;max-width:330px;white-space:pre-line}
.themebtn{position:fixed;top:12px;right:14px;z-index:8;font:inherit;
 font-size:11.5px;cursor:pointer;background:var(--surface);color:var(--ink-2);
 border:1px solid var(--line-2);border-radius:7px;padding:4px 9px}
"""
