"""Self-contained status dashboard served at GET / (no frameworks, no CDNs)."""

from fastapi.responses import HTMLResponse

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="robots" content="noindex,nofollow">
<title>Claude Zen Proxy · Status</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%230071e3'/%3E%3Cpath d='M18 42V22h8l6 12 6-12h8v20h-7V31l-7 11-7-11v11z' fill='white'/%3E%3C/svg%3E">
<style>
:root{
  --bg:#f5f5f7; --surface:#ffffff; --surface2:#fbfbfd; --sidebar:rgba(246,246,248,.88);
  --text:#1d1d1f; --sub:#6e6e73; --hair:rgba(0,0,0,.10); --hair2:rgba(0,0,0,.055);
  --accent:#0071e3; --green:#1d8127; --amber:#b25e09; --red:#c8102e;
  --shadow:0 1px 2px rgba(0,0,0,.04),0 8px 24px rgba(0,0,0,.04);
  --mono:ui-monospace,'SF Mono',SFMono-Regular,Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',Inter,Helvetica,Arial,sans-serif;
  --header-h:58px; --tap:42px;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#1a1a1c; --surface:#242426; --surface2:#2b2b2e; --sidebar:rgba(36,36,38,.90);
    --text:#f5f5f7; --sub:#a1a1a6; --hair:rgba(255,255,255,.14); --hair2:rgba(255,255,255,.075);
    --accent:#2997ff; --green:#30d158; --amber:#ff9f0a; --red:#ff453a;
    --shadow:0 1px 2px rgba(0,0,0,.22),0 10px 30px rgba(0,0,0,.18);
  }
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{min-height:100%}
body{font-family:var(--sans);background:var(--bg);color:var(--text);font-size:14px;line-height:1.45;
  -webkit-font-smoothing:antialiased;-webkit-text-size-adjust:100%}
button{font:inherit}
.app{max-width:1120px;margin:28px auto;border:1px solid var(--hair);border-radius:14px;
  background:var(--surface);box-shadow:var(--shadow);min-height:calc(100dvh - 56px);overflow:visible}
.titlebar{display:flex;align-items:center;gap:12px;padding:0 16px;height:var(--header-h);
  border-bottom:1px solid var(--hair);background:var(--surface)}
.lights{display:flex;gap:8px;margin-right:2px;flex:none}
.lights i{width:12px;height:12px;border-radius:50%;display:block}
.lights .r{background:#ff5f57}.lights .y{background:#febc2e}.lights .g{background:#28c840}
.brand{display:flex;align-items:center;gap:10px;min-width:0}
.brand .mark{width:30px;height:30px;border-radius:9px;flex:none;display:grid;place-items:center;
  color:#fff;font-weight:800;font-size:12px;letter-spacing:-.04em;
  background:linear-gradient(145deg,#0a84ff,#0056b3);box-shadow:inset 0 1px 0 rgba(255,255,255,.3)}
.brand h1{font-size:15px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.brand .crumb{font-size:11px;color:var(--sub);margin-top:-1px}
.titlebar .grow{flex:1}
.live{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--sub);white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);display:inline-block;
  box-shadow:0 0 0 3px color-mix(in srgb,var(--green) 12%,transparent)}
.dot.warn{background:var(--amber);box-shadow:0 0 0 3px color-mix(in srgb,var(--amber) 14%,transparent)}
.dot.bad{background:var(--red);box-shadow:0 0 0 3px color-mix(in srgb,var(--red) 14%,transparent)}
.ghost{border:1px solid var(--hair);background:transparent;color:var(--text);border-radius:8px;
  min-height:32px;padding:5px 10px;display:inline-flex;align-items:center;gap:6px;cursor:pointer}
.ghost:hover{background:var(--hair2)}
.shell{display:flex;min-height:620px}
nav{width:212px;flex:none;padding:14px 10px;border-right:1px solid var(--hair);
  background:var(--sidebar);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px)}
nav button{display:flex;align-items:center;gap:10px;width:100%;border:0;border-radius:9px;
  background:transparent;color:var(--text);text-align:left;font-size:13px;min-height:var(--tap);
  padding:8px 12px;cursor:pointer;-webkit-tap-highlight-color:transparent}
nav button .ic{width:20px;text-align:center;opacity:.72;font-size:15px}
nav button:hover:not(.active){background:var(--hair2)}
nav button.active{background:color-mix(in srgb,var(--text) 10%,transparent);font-weight:600}
.nav-foot{margin-top:auto;padding:14px 12px 2px;color:var(--sub);font-size:11px;line-height:1.5}
main{flex:1;padding:24px 28px 28px;min-width:0}
section{display:none}
section.active{display:block;animation:fade .18s ease-out}
@keyframes fade{from{opacity:.55}to{opacity:1}}
@media (prefers-reduced-motion:reduce){section.active{animation:none}*{scroll-behavior:auto!important}}
h2{font-size:23px;line-height:1.18;font-weight:700;letter-spacing:-.015em}
h3{font-size:11px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:.06em}
.sub{color:var(--sub);font-size:13px;margin-top:4px}
.muted{color:var(--sub)}
.small{font-size:12px}
.eyebrow{font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--sub);margin-bottom:7px}
.hero{display:flex;align-items:flex-start;gap:20px;justify-content:space-between;margin-bottom:14px}
.hero-side{text-align:right;flex:none}
.hero-side .label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--sub);font-weight:700}
.hero-side strong{display:block;font-size:24px;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:11px}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;font-weight:600;
  padding:4px 10px;border-radius:999px;border:1px solid var(--hair);color:var(--sub);white-space:nowrap}
.badge.ok{color:var(--green);border-color:color-mix(in srgb,var(--green) 45%,var(--hair))}
.badge.warn{color:var(--amber);border-color:color-mix(in srgb,var(--amber) 45%,var(--hair))}
.badge.err{color:var(--red);border-color:color-mix(in srgb,var(--red) 45%,var(--hair))}
.metric-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:9px;margin:18px 0 20px}
.metric{border:1px solid var(--hair);background:var(--surface2);border-radius:11px;padding:11px 13px;min-width:0}
.metric .label{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.055em;color:var(--sub);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.metric .value{font-size:23px;font-weight:700;line-height:1.25;font-variant-numeric:tabular-nums;
  letter-spacing:-.02em;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.metric .note{font-size:11.5px;color:var(--sub);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.metric .value.err{color:var(--red)}.metric .value.warn{color:var(--amber)}.metric .value.ok{color:var(--green)}
.group{border-top:1px solid var(--hair);padding-top:4px;margin-top:18px}
.group h3{padding:10px 0 4px}
.rows{border-bottom:1px solid transparent}
.row{display:flex;align-items:baseline;gap:14px;padding:8px 0;border-bottom:1px solid var(--hair2)}
.row:last-child{border-bottom:0}
.row .k{flex:none;width:176px;color:var(--sub);font-size:12.5px}
.row .v{margin-left:auto;text-align:right;font-variant-numeric:tabular-nums;overflow-wrap:anywhere;min-width:0}
.row .v.mono{font-family:var(--mono);font-size:12px}
.row .v.wrap{text-align:left;margin-left:0;flex:1;font-family:var(--mono);font-size:11.8px;line-height:1.5}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:22px}
.dist{padding:8px 0;border-bottom:1px solid var(--hair2)}
.dist:last-child{border-bottom:0}
.dist-head{display:flex;align-items:baseline;gap:12px;justify-content:space-between}
.dist-k{font-family:var(--mono);font-size:12px;overflow-wrap:anywhere;color:var(--text)}
.dist-v{font-variant-numeric:tabular-nums;font-size:12px;white-space:nowrap;color:var(--sub)}
.bar{height:5px;border-radius:999px;background:color-mix(in srgb,var(--text) 9%,transparent);overflow:hidden;margin-top:5px}
.bar i{display:block;height:100%;background:var(--accent);border-radius:999px;min-width:2px}
.bar.green i{background:var(--green)}.bar.amber i{background:var(--amber)}.bar.red i{background:var(--red)}
.card{border:1px solid var(--hair);background:var(--surface2);border-radius:12px;padding:14px 16px;margin:12px 0}
.card h3{margin-bottom:6px}
.card p{color:var(--sub);font-size:12.5px}
.copyline{display:flex;align-items:center;gap:8px;margin-top:7px;flex-wrap:wrap}
.copyline code{font-family:var(--mono);font-size:12.5px;background:color-mix(in srgb,var(--text) 7%,transparent);
  border:1px solid var(--hair);border-radius:7px;padding:5px 8px;overflow-wrap:anywhere;min-width:0}
.copy{border:1px solid var(--hair);background:var(--surface);color:var(--text);border-radius:7px;
  padding:5px 9px;font-size:11.5px;cursor:pointer;white-space:nowrap}
.copy:hover{background:var(--hair2)}
pre{background:color-mix(in srgb,var(--text) 6%,transparent);border:1px solid var(--hair);border-radius:9px;
  padding:12px;font-family:var(--mono);font-size:11.5px;line-height:1.6;overflow:auto;max-height:290px;
  white-space:pre-wrap;overflow-wrap:anywhere}
.model-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:16px 0}
.model-card{border:1px solid var(--hair);background:var(--surface2);border-radius:11px;padding:12px 13px}
.model-card .tier{font-size:10.5px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:.06em}
.model-card strong{display:block;font-family:var(--mono);font-size:12.5px;margin:4px 0 1px;overflow-wrap:anywhere}
.model-card .alias{font-size:11.5px;color:var(--sub)}
.failure{border-left:3px solid var(--hair);padding:2px 0 2px 12px;margin:12px 0}
.failure .head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.failure .when{color:var(--sub);font-size:12px}
.failure .status{font-family:var(--mono);font-weight:700;font-size:12px}
.failure pre{margin-top:7px;max-height:170px}
.failure-list .row .k{width:90px}
.banner{display:flex;align-items:center;gap:10px;border:1px solid color-mix(in srgb,var(--red) 40%,var(--hair));
  background:color-mix(in srgb,var(--red) 8%,var(--surface));color:var(--text);border-radius:10px;
  padding:9px 12px;margin-bottom:16px;font-size:12.5px}
.banner[hidden]{display:none}
.empty{color:var(--sub);font-size:12.5px;padding:10px 0}
.foot{display:flex;align-items:center;gap:16px;flex-wrap:wrap;border-top:1px solid var(--hair);
  padding:10px 18px;color:var(--sub);font-size:11.5px;background:var(--surface)}
.foot .grow{flex:1}
.toast{position:fixed;left:50%;bottom:22px;transform:translate(-50%,12px);opacity:0;pointer-events:none;
  background:var(--text);color:var(--surface);padding:8px 14px;border-radius:999px;font-size:12px;
  transition:opacity .16s ease,transform .16s ease;z-index:300}
.toast.show{opacity:1;transform:translate(-50%,0)}
@media (max-width:900px){
  .metric-grid{grid-template-columns:repeat(3,1fr)}
  .row .k{width:150px}
}
@media (max-width:769px){
  .app{margin:0;border:0;border-radius:0;min-height:100dvh;max-width:none;box-shadow:none}
  .lights{display:none}
  .titlebar{height:54px;padding:0 13px;position:sticky;top:0;z-index:100}
  .brand .crumb{display:none}
  .titlebar .hide-sm{display:none}
  .shell{display:block;min-height:0}
  nav{display:flex;gap:2px;width:100%;overflow-x:auto;border-right:0;border-bottom:1px solid var(--hair);
    padding:7px 9px;position:sticky;top:54px;z-index:90;scrollbar-width:none}
  nav::-webkit-scrollbar{display:none}
  nav button{width:auto;flex:none;white-space:nowrap;min-height:38px;padding:6px 12px;gap:7px}
  .nav-foot{display:none}
  main{padding:17px 15px 24px}
  h2{font-size:21px}
  .hero{gap:12px;flex-direction:column}
  .hero-side{text-align:left;display:flex;align-items:baseline;gap:8px}
  .hero-side strong{font-size:18px}
  .metric-grid{grid-template-columns:repeat(2,1fr)}
  .grid2{grid-template-columns:1fr;gap:0}
  .model-grid{grid-template-columns:1fr}
  .row .k{width:126px;font-size:12px}
  .row .v{font-size:13px}
  .foot{padding:10px 15px}
}
@media (max-width:420px){
  .metric-grid{grid-template-columns:1fr 1fr}
  .metric .value{font-size:20px}
  .live{font-size:11px}
  .ghost{padding:5px 8px}
}
</style>
</head>
<body>
<div class="app">
  <header class="titlebar">
    <div class="lights" aria-hidden="true"><i class="r"></i><i class="y"></i><i class="g"></i></div>
    <div class="brand">
      <div class="mark" aria-hidden="true">CZ</div>
      <div><h1>Claude Zen Proxy</h1><div class="crumb">status &amp; traffic</div></div>
    </div>
    <div class="grow"></div>
    <button class="ghost" id="refresh" type="button" title="Refresh now">&#8635;<span class="hide-sm"> Refresh</span></button>
    <div class="live" id="live" role="status" aria-live="polite"><span class="dot" id="dot"></span><span id="live-label">connecting&hellip;</span></div>
  </header>
  <div class="shell">
    <nav id="nav" role="tablist" aria-label="Dashboard sections">
      <button type="button" role="tab" id="tab-overview" data-s="overview" class="active" aria-controls="s-overview" aria-selected="true"><span class="ic">&#9673;</span>Overview</button>
      <button type="button" role="tab" id="tab-traffic" data-s="traffic" aria-controls="s-traffic" aria-selected="false"><span class="ic">&#8646;</span>Traffic</button>
      <button type="button" role="tab" id="tab-models" data-s="models" aria-controls="s-models" aria-selected="false"><span class="ic">&#9783;</span>Models</button>
      <button type="button" role="tab" id="tab-upstream" data-s="upstream" aria-controls="s-upstream" aria-selected="false"><span class="ic">&#9729;</span>Upstream</button>
      <button type="button" role="tab" id="tab-endpoints" data-s="endpoints" aria-controls="s-endpoints" aria-selected="false"><span class="ic">&#8982;</span>Endpoints</button>
      <div class="nav-foot">Live view of the local proxy.<br>Data is read-only.</div>
    </nav>
    <main>
      <div id="banner" class="banner" role="alert" hidden></div>

      <section id="s-overview" class="active" role="tabpanel" aria-labelledby="tab-overview">
        <div class="hero">
          <div>
            <div class="eyebrow">Overview</div>
            <h2 id="ov-status">Loading&hellip;</h2>
            <p class="sub" id="ov-sub">Anthropic Messages &rarr; OpenAI-compatible upstream</p>
            <div class="chips" id="ov-chips"></div>
          </div>
          <div class="hero-side">
            <span class="label">Uptime</span>
            <strong id="ov-uptime">&mdash;</strong>
            <span class="muted small" id="ov-started"></span>
          </div>
        </div>
        <div class="metric-grid" id="ov-metrics"></div>
        <div class="group"><h3>Service</h3><div id="ov-rows" class="rows"></div></div>
      </section>

      <section id="s-traffic" role="tabpanel" aria-labelledby="tab-traffic">
        <h2>Traffic</h2>
        <div class="sub">Since process start. Token totals include translated streams; raw OpenAI passthrough streams are not counted here.</div>
        <div class="group"><h3>Totals</h3><div id="tr-rows" class="rows"></div></div>
        <div class="grid2">
          <div class="group"><h3>Final status</h3><div id="tr-status"></div></div>
          <div class="group"><h3>By endpoint</h3><div id="tr-ep"></div></div>
        </div>
        <div class="group"><h3>Token usage</h3><div id="tr-tokens" class="rows"></div></div>
      </section>

      <section id="s-models" role="tabpanel" aria-labelledby="tab-models">
        <h2>Models</h2>
        <div class="sub">Claude client aliases map to one upstream model family. The mapping is configuration, not a live model list.</div>
        <div class="model-grid">
          <div class="model-card"><span class="tier">Opus alias</span><strong id="mo-big">&mdash;</strong><span class="alias">BIG_MODEL</span></div>
          <div class="model-card"><span class="tier">Sonnet alias</span><strong id="mo-middle">&mdash;</strong><span class="alias">MIDDLE_MODEL</span></div>
          <div class="model-card"><span class="tier">Haiku alias</span><strong id="mo-small">&mdash;</strong><span class="alias">SMALL_MODEL</span></div>
        </div>
        <div class="group"><h3>Requests by upstream model</h3><div id="mo-rows"></div></div>
        <div class="card">
          <h3>Advertised capability</h3>
          <p>Model discovery at <code>/v1/models</code> exposes the upstream catalog with 1,000,000-token context and 128,000-token maximum output metadata. Requests are still subject to the upstream free-tier limits.</p>
        </div>
      </section>

      <section id="s-upstream" role="tabpanel" aria-labelledby="tab-upstream">
        <h2>Upstream</h2>
        <div class="sub" id="up-sub">&mdash;</div>
        <div class="group"><h3>Configuration</h3><div id="up-rows" class="rows"></div></div>
        <div class="group"><h3>Last recorded upstream failure</h3><div id="up-fail"></div></div>
        <div class="group"><h3>Recent failures</h3><div id="up-hist" class="failure-list"></div></div>
      </section>

      <section id="s-endpoints" role="tabpanel" aria-labelledby="tab-endpoints">
        <h2>Endpoints</h2>
        <div class="sub">Public API surface. The proxy key is never displayed by this dashboard.</div>
        <div class="card">
          <h3>Public API base URL</h3>
          <div class="copyline"><code id="ep-base">&mdash;</code><button class="copy" type="button" data-copy-target="ep-base">Copy</button></div>
          <p style="margin-top:7px">OpenAI-compatible clients should use the base URL plus <code>/v1</code>; Anthropic clients should use the bare host and set the key as a bearer token or <code>x-api-key</code>.</p>
        </div>
        <div class="group"><h3>Routes</h3><div id="ep-rows" class="rows"></div></div>
        <div class="card">
          <h3>Claude Code client setup</h3>
          <pre id="ep-snippet"></pre>
          <div class="copyline"><button class="copy" type="button" data-copy-target="ep-snippet">Copy snippet</button></div>
        </div>
        <div class="card">
          <h3>OpenAI-compatible client setup</h3>
          <pre id="ep-snippet-openai"></pre>
          <div class="copyline"><button class="copy" type="button" data-copy-target="ep-snippet-openai">Copy snippet</button></div>
        </div>
      </section>
    </main>
  </div>
  <footer class="foot">
    <span id="ft-uptime">uptime &mdash;</span>
    <span id="ft-refresh">refreshed &mdash;</span>
    <span class="grow"></span>
    <span>auto-refresh 5s</span>
  </footer>
</div>
<div id="toast" class="toast" role="status" aria-live="polite"></div>
<script>
const $ = id => document.getElementById(id);
const state = { data:null, lastSample:0, error:null, tab:'overview', loading:false, timer:null };
const sections = ['overview','traffic','models','upstream','endpoints'];
const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = n => Number(n || 0).toLocaleString('en-US');
const fmtN = n => num(n);
const pct = (n, digits=0) => (Number(n || 0) * 100).toFixed(digits) + '%';
const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
function fmtDur(seconds){
  let s = Math.floor(Number(seconds) || 0);
  if (s < 0) s = 0;
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return d + 'd ' + h + 'h';
  if (h) return h + 'h ' + m + 'm';
  if (m) return m + 'm ' + (s % 60) + 's';
  return s + 's';
}
function fmtAgo(at){
  if (!at || Number(at) <= 0) return 'never';
  const s = Math.max(0, Math.floor(Date.now()/1000 - Number(at)));
  if (s < 10) return 'just now';
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  if (s < 86400) return Math.floor(s/3600) + 'h ago';
  return Math.floor(s/86400) + 'd ago';
}
function fmtDate(at){
  if (!at || Number(at) <= 0) return 'unknown';
  return new Date(Number(at) * 1000).toLocaleString();
}
function row(k, v, mono){
  return '<div class="row"><div class="k">' + esc(k) + '</div><div class="v' + (mono ? ' mono' : '') + '">' + esc(v) + '</div></div>';
}
function metric(label, value, note, tone){
  return '<div class="metric"><div class="label">' + esc(label) + '</div><div class="value' + (tone ? ' ' + tone : '') + '">' + esc(value) + '</div><div class="note">' + esc(note || '') + '</div></div>';
}
function statusTone(code){
  const c = Number(code);
  if (c >= 200 && c < 300) return 'green';
  if (c >= 300 && c < 400) return '';
  if (c >= 400 && c < 500) return 'amber';
  return 'red';
}
function dist(el, obj, total, opts){
  opts = opts || {};
  const entries = Object.entries(obj || {}).filter(([,v]) => Number(v) > 0).sort((a,b) => Number(b[1]) - Number(a[1]));
  if (!entries.length) { el.innerHTML = '<div class="empty">' + esc(opts.empty || 'no data yet') + '</div>'; return; }
  const denom = Number(total) || entries.reduce((sum, [,v]) => sum + Number(v), 0);
  el.innerHTML = entries.map(([k,v]) => {
    const value = Number(v) || 0;
    const share = denom ? clamp(value / denom * 100, 0, 100) : 0;
    const tone = opts.status ? statusTone(k) : (opts.tone || '');
    return '<div class="dist"><div class="dist-head"><span class="dist-k">' + esc(k) + '</span><span class="dist-v">' +
      fmtN(value) + ' &middot; ' + Math.round(share) + '%</span></div><div class="bar ' + tone + '"><i style="width:' + share.toFixed(1) + '%"></i></div></div>';
  }).join('');
}
function publicBase(d){
  const configured = d && d.proxy && d.proxy.public_base_url;
  if (configured) return String(configured).replace(/\\/$/, '');
  const host = location.hostname || '127.0.0.1';
  return location.protocol + '//' + host + ':4014/v1';
}
function setLive(kind, label){
  $('dot').className = 'dot' + (kind ? ' ' + kind : '');
  $('live-label').textContent = label;
}
function toast(text){
  const el = $('toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), 1600);
}
async function copyText(text){
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    const ok = document.execCommand('copy'); ta.remove(); return ok;
  } catch (e) { return false; }
}
function setTab(name, push){
  if (!sections.includes(name)) name = 'overview';
  state.tab = name;
  document.querySelectorAll('nav button[data-s]').forEach(b => {
    const on = b.dataset.s === name;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  document.querySelectorAll('main section').forEach(s => s.classList.toggle('active', s.id === 's-' + name));
  if (push && location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  if (window.innerWidth < 769) window.scrollTo({top:0, behavior:'smooth'});
}
function render(d){
  state.lastSample = Date.now();
  const p = d.proxy || {}, s = d.stats || {};
  const total = Number(s.total_requests || 0);
  const ok = Number(s.ok_requests || 0);
  const errors = Number(s.errors || 0);
  const streamErrors = Number(s.stream_errors || 0);
  const errRate = Number(s.error_rate || 0);
  const success = total ? ok / total : 0;
  const now = Date.now() / 1000;
  const recentError = s.last_error_at && (now - Number(s.last_error_at) < 300);
  const lastFailAt = d.last_upstream_failure && d.last_upstream_failure.at;
  const recentFail = lastFailAt && (now - Number(lastFailAt) < 300);
  const bad = total > 0 && errRate >= 0.15;
  const warn = !bad && (recentError || recentFail);
  const compState = bad ? 'Degraded' : warn ? 'Attention' : total ? 'Operational' : 'Idle';
  const compTone = bad ? 'bad' : warn ? 'warn' : '';
  $('ov-status').textContent = compState;
  $('ov-sub').textContent = 'Anthropic Messages \u2192 ' + (p.openai_base_url || 'upstream');
  setLive(compTone, bad ? 'degraded' : warn ? 'attention' : 'live');
  const chips = [];
  chips.push('<span class="badge ' + (p.wire_api === 'responses' ? 'warn' : 'ok') + '">wire: ' + esc(p.wire_api || '—') + '</span>');
  chips.push('<span class="badge ' + (p.client_key_validation ? 'ok' : 'warn') + '">client key: ' + (p.client_key_validation ? 'enforced' : 'open') + '</span>');
  chips.push('<span class="badge">egress: ' + esc(p.upstream_proxy || 'direct') + '</span>');
  chips.push('<span class="badge">max output: ' + fmtN(p.max_tokens_limit) + '</span>');
  $('ov-chips').innerHTML = chips.join('');
  $('ov-uptime').textContent = fmtDur(s.uptime_secs);
  $('ov-started').textContent = s.started_at ? 'since ' + fmtDate(s.started_at) : '';
  $('ov-metrics').innerHTML =
    metric('Requests', fmtN(total), fmtN(ok) + ' ok · ' + fmtN(errors) + ' failed') +
    metric('Success', total ? pct(success) : '—', total ? 'error rate ' + pct(errRate) : 'no traffic yet', bad ? 'err' : (total ? 'ok' : '')) +
    metric('Uptime', fmtDur(s.uptime_secs), s.started_at ? fmtAgo(s.started_at) + ' start' : '') +
    metric('Streams', fmtN(s.streams), fmtN(s.keepalive_sent) + ' keepalives') +
    metric('Avg accept', fmtN(s.avg_latency_ms) + ' ms', 'HTTP accept latency') +
    metric('TTFT p50', s.ttft_ms ? fmtN(s.ttft_ms.p50_ms) + ' ms' : '—', s.ttft_ms ? 'p95 ' + fmtN(s.ttft_ms.p95_ms) + ' ms' : 'no streams') +
    metric('Tokens', fmtN((s.tokens_in || 0) + (s.tokens_out || 0)), fmtN(s.tokens_in) + ' in · ' + fmtN(s.tokens_out) + ' out') +
    metric('Sessions', fmtN(s.active_sessions), 'in-memory conversations');
  const modelNames = [...new Set([p.models && p.models.big, p.models && p.models.middle, p.models && p.models.small].filter(Boolean))];
  $('ov-rows').innerHTML =
    row('Upstream', p.openai_base_url || '—', true) +
    row('Public API', publicBase(d), true) +
    row('Models', modelNames.join(' / ') || '—', true) +
    row('Request timeout', fmtN(p.request_timeout) + 's') +
    row('Retry budget', fmtN(p.retry_budget_secs) + 's') +
    row('Keepalive', fmtN(p.keepalive_secs) + 's') +
    row('Process', 'pid ' + fmtN(p.pid) + ' · started ' + fmtDate(s.started_at)) +
    row('Cost (list estimate)', '$' + Number(s.total_cost_usd || 0).toFixed(2) + ' · not billed upstream') +
    row('Version', 'claude-code-proxy 1.0.0');
  $('ft-uptime').textContent = 'uptime ' + fmtDur(s.uptime_secs);
  $('ft-refresh').textContent = 'refreshed ' + new Date(state.lastSample).toLocaleTimeString();

  // Traffic
  const cachedPct = s.tokens_in ? (Number(s.tokens_cached || 0) / Number(s.tokens_in) * 100) : 0;
  $('tr-rows').innerHTML =
    row('Final requests', fmtN(total)) +
    row('Successful', fmtN(ok)) +
    row('Failed', fmtN(errors) + (streamErrors ? ' · ' + fmtN(streamErrors) + ' after headers' : '')) +
    row('Error rate', total ? pct(errRate, 1) : '—') +
    row('Avg accept latency', fmtN(s.avg_latency_ms) + ' ms') +
    row('Streams', fmtN(s.streams)) +
    row('Keepalives sent', fmtN(s.keepalive_sent)) +
    row('TTFT avg / p50 / p95', s.ttft_ms ? fmtN(s.ttft_ms.avg_ms) + ' / ' + fmtN(s.ttft_ms.p50_ms) + ' / ' + fmtN(s.ttft_ms.p95_ms) + ' ms (n=' + fmtN(s.ttft_ms.count) + ')' : '—') +
    row('ITL p50 / p95', s.itl_ms ? fmtN(s.itl_ms.p50_ms) + ' / ' + fmtN(s.itl_ms.p95_ms) + ' ms (n=' + fmtN(s.itl_ms.count) + ')' : '—');
  dist($('tr-status'), s.by_status, total, {status:true});
  dist($('tr-ep'), s.by_endpoint, total);
  $('tr-tokens').innerHTML =
    row('Input tokens', fmtN(s.tokens_in)) +
    row('Output tokens', fmtN(s.tokens_out)) +
    row('Cache reads', fmtN(s.tokens_cached) + ' · ' + cachedPct.toFixed(1) + '% of input') +
    row('Cache writes', fmtN(s.tokens_cache_creation) + (s.tokens_cache_creation_1h ? ' · ' + fmtN(s.tokens_cache_creation_1h) + ' at 1h' : '')) +
    row('Estimated list cost', '$' + Number(s.total_cost_usd || 0).toFixed(4)) +
    row('Active conversations', fmtN(s.active_sessions));

  // Models
  $('mo-big').textContent = (p.models && p.models.big) || '—';
  $('mo-middle').textContent = (p.models && p.models.middle) || '—';
  $('mo-small').textContent = (p.models && p.models.small) || '—';
  dist($('mo-rows'), s.by_model, total);

  // Upstream
  $('up-sub').textContent = (p.openai_base_url || '—') + '  ·  wire ' + (p.wire_api || '—') + '  ·  egress ' + (p.upstream_proxy || 'direct');
  $('up-rows').innerHTML =
    row('Base URL', p.openai_base_url || '—', true) +
    row('Wire protocol', p.wire_api || '—') +
    row('Upstream User-Agent', p.upstream_user_agent || '—', true) +
    row('Egress path', p.upstream_proxy || 'direct', true) +
    row('Max output tokens', fmtN(p.max_tokens_limit)) +
    row('Request timeout', fmtN(p.request_timeout) + 's') +
    row('Retry budget', fmtN(p.retry_budget_secs) + 's') +
    row('SSE keepalive', fmtN(p.keepalive_secs) + 's') +
    row('Client key validation', p.client_key_validation ? 'enforced' : 'disabled');
  const f = d.last_upstream_failure;
  if (f && f.status != null) {
    const age = f.at ? (Date.now()/1000 - Number(f.at)) : null;
    const stale = age != null && age > 3600;
    $('up-fail').innerHTML = '<div class="failure"><div class="head">' +
      '<span class="badge ' + (Number(f.status) >= 500 ? 'err' : Number(f.status) === 429 ? 'warn' : '') + '">HTTP ' + esc(f.status) + '</span>' +
      '<span class="when">' + esc(fmtAgo(f.at)) + (stale ? ' · stale' : '') + '</span>' +
      (f.model ? '<span class="badge">' + esc(f.model) + '</span>' : '') +
      '</div>' + (f.request_id ? '<div class="small muted" style="margin-top:5px">request ' + esc(f.request_id) + '</div>' : '') +
      '<pre>' + esc(f.error || '(no error text)') + '</pre></div>';
  } else {
    $('up-fail').innerHTML = '<div class="empty">No upstream failure has been recorded.</div>';
  }
  const hist = Array.isArray(d.failure_history) ? d.failure_history : [];
  if (!hist.length) {
    $('up-hist').innerHTML = '<div class="empty">No archived failures yet. New final failures are archived here automatically (up to 50).</div>';
  } else {
    $('up-hist').innerHTML = hist.map(h => {
      const detail = String(h.error || '').replace(/\\s+/g, ' ').slice(0, 180);
      return '<div class="row"><div class="k"><span class="badge ' + (Number(h.status) >= 500 ? 'err' : Number(h.status) === 429 ? 'warn' : '') + '">' + esc(h.status) + '</span></div>' +
        '<div class="v" style="text-align:left;flex:1;margin-left:0"><div class="small">' + esc(fmtAgo(h.at)) + (h.model ? ' · ' + esc(h.model) : '') + '</div>' +
        '<div class="muted small" style="overflow-wrap:anywhere">' + esc(detail || '(no error text)') + '</div></div></div>';
    }).join('');
  }

  // Endpoints
  const base = publicBase(d);
  $('ep-base').textContent = base;
  $('ep-rows').innerHTML =
    row('POST /v1/messages', 'Claude Messages API (translated)', true) +
    row('POST /v1/messages/count_tokens', 'Token estimation', true) +
    row('POST /v1/chat/completions', 'OpenAI chat passthrough', true) +
    row('POST /v1/responses', 'OpenAI Responses passthrough', true) +
    row('GET /v1/models', 'Upstream model catalog', true) +
    row('GET /health · /test-connection · /api/status', 'Operations', true);
  const claudeBase = base.replace(/\\/v1$/, '');
  $('ep-snippet').textContent = 'export ANTHROPIC_BASE_URL="' + claudeBase + '"\\n' +
    'export ANTHROPIC_AUTH_TOKEN="<proxy key>"\\n' +
    'claude';
  $('ep-snippet-openai').textContent = 'Base URL: ' + base + '\\n' +
    'API key:  <proxy key>\\n' +
    'Model:    ' + ((p.models && p.models.middle) || 'deepseek-v4.1-flash-free');
  $('banner').hidden = true;
}
function updateOffline(){
  if (!state.error) return;
  setLive('bad', 'offline');
  $('ov-status').textContent = state.lastSample ? 'Last sample' : 'Unreachable';
  const when = state.lastSample ? new Date(state.lastSample).toLocaleTimeString() : '—';
  $('banner').hidden = false;
  $('banner').textContent = 'Dashboard API is unreachable. Showing the last sample from ' + when + '. Retrying automatically.';
  $('ft-refresh').textContent = 'last try ' + new Date().toLocaleTimeString();
}
async function load(){
  if (state.loading) return;
  state.loading = true;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 8000);
  try {
    const r = await fetch('/api/status', {cache:'no-store', signal:ctl.signal, headers:{Accept:'application/json'}});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    if (!d || !d.proxy || !d.stats) throw new Error('invalid status payload');
    state.data = d; state.error = null; state.lastSample = Date.now();
    render(d);
  } catch (e) {
    state.error = e;
    updateOffline();
  } finally {
    clearTimeout(timer);
    state.loading = false;
  }
}
document.querySelectorAll('nav button[data-s]').forEach(b => {
  b.addEventListener('click', () => setTab(b.dataset.s, true));
});
document.querySelectorAll('[data-copy-target]').forEach(b => {
  b.addEventListener('click', async () => {
    const target = $(b.dataset.copyTarget);
    const text = target ? (target.textContent || '').trim() : '';
    if (!text) return;
    toast(await copyText(text) ? 'Copied' : 'Copy failed');
  });
});
$('refresh').addEventListener('click', () => load());
window.addEventListener('hashchange', () => setTab((location.hash || '#overview').slice(1), false));
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
setTab((location.hash || '#overview').slice(1), false);
load();
state.timer = setInterval(() => { if (!document.hidden) load(); }, 5000);
</script>
</body>
</html>
"""


def dashboard_response() -> HTMLResponse:
    """Render the status dashboard with no-store/security headers."""
    return HTMLResponse(
        content=PAGE,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Content-Security-Policy": (
                "default-src 'none'; "
                "style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; "
                "img-src 'self' data:; "
                "connect-src 'self'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            ),
        },
    )
