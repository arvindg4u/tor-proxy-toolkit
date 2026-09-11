"""Native-style status dashboard served at GET / (no cards, no frameworks)."""

from fastapi.responses import HTMLResponse

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Zen Proxy</title>
<style>
:root{
  --bg:#f5f5f7; --panel:#ffffff; --sidebar:rgba(246,246,248,.86);
  --text:#1d1d1f; --sub:#6e6e73; --hair:rgba(0,0,0,.1);
  --accent:#0071e3; --green:#1d8127; --amber:#b25e09; --red:#c8102e;
  --mono:ui-monospace,'SF Mono',SFMono-Regular,Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',Inter,Helvetica,Arial,sans-serif;
  --header-h:52px; --tap:44px;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#1e1e1e; --panel:#2c2c2e; --sidebar:rgba(40,40,42,.86);
    --text:#f5f5f7; --sub:#a1a1a6; --hair:rgba(255,255,255,.14);
    --accent:#2997ff; --green:#30d158; --amber:#ff9f0a; --red:#ff453a;
  }
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--sans);background:var(--bg);color:var(--text);font-size:14px;line-height:1.45;
  -webkit-font-smoothing:antialiased;-webkit-text-size-adjust:100%}
.window{max-width:1060px;margin:0 auto;border:0;background:var(--panel);min-height:100dvh}
/* Mobile base: single column, nav = sticky horizontal strip under titlebar */
.titlebar{position:sticky;top:0;z-index:100;display:flex;align-items:center;gap:12px;
  padding:0 16px;height:var(--header-h);border-bottom:1px solid var(--hair);
  background:var(--panel)}
.lights{display:none}
.titlebar h1{font-size:15px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.titlebar .live{margin-left:auto;font-size:12px;color:var(--sub);display:flex;align-items:center;
  gap:6px;flex:none}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);display:inline-block}
.dot.bad{background:var(--red)}.dot.warn{background:var(--amber)}
.body{display:block}
nav{position:sticky;top:var(--header-h);z-index:90;display:flex;gap:4px;overflow-x:auto;
  background:var(--sidebar);border-bottom:1px solid var(--hair);padding:8px 10px;
  backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);scrollbar-width:none}
nav::-webkit-scrollbar{display:none}
nav button{display:flex;flex:none;gap:8px;align-items:center;border:0;background:transparent;
  color:var(--text);font:inherit;font-size:13px;min-height:var(--tap);padding:8px 14px;
  border-radius:8px;cursor:pointer;text-align:left;-webkit-tap-highlight-color:transparent}
nav button .ic{width:20px;text-align:center;opacity:.75}
nav button.active{background:rgba(128,128,128,.22)}
nav button:hover:not(.active){background:rgba(128,128,128,.12)}
main{padding:16px;min-width:0}
section{display:none}
section.active{display:block}
h2{font-size:20px;font-weight:700;margin-bottom:2px}
.sub{color:var(--sub);font-size:13px;margin-bottom:16px}
.group{border-top:1px solid var(--hair);margin:18px 0 6px}
.group h3{font-size:12px;font-weight:600;color:var(--sub);text-transform:uppercase;letter-spacing:.04em;
  padding:10px 0 2px}
.row{display:flex;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid var(--hair)}
.row:last-child{border-bottom:0}
.row .k{flex:none;width:130px;color:var(--sub)}
.row .v{margin-left:auto;text-align:right;font-variant-numeric:tabular-nums;overflow-wrap:anywhere;
  min-width:0}
.row .v.mono{font-family:var(--mono);font-size:12.5px}
.big{font-size:30px;font-weight:700;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.badge{display:inline-block;font-size:12px;font-weight:600;padding:4px 12px;border-radius:999px;
  border:1px solid var(--hair);color:var(--sub);margin:0 6px 6px 0}
.badge.ok{color:var(--green);border-color:currentColor}
.badge.warn{color:var(--amber);border-color:currentColor}
.badge.err{color:var(--red);border-color:currentColor}
.bar{height:6px;border-radius:3px;background:rgba(128,128,128,.25);overflow:hidden;margin-top:4px}
.bar i{display:block;height:100%;background:var(--accent);border-radius:3px}
pre{background:rgba(128,128,128,.12);border:1px solid var(--hair);border-radius:8px;padding:12px;
  font-family:var(--mono);font-size:12px;overflow:auto;max-height:240px;white-space:pre-wrap}
.foot{padding:10px 16px;border-top:1px solid var(--hair);color:var(--sub);font-size:12px;
  display:flex;gap:16px;flex-wrap:wrap}
/* Desktop: centered window, traffic lights, vertical sidebar */
@media (min-width:769px){
  :root{--header-h:60px}
  .window{margin:28px auto;border:1px solid var(--hair);border-radius:12px;overflow:hidden;
    min-height:0}
  .lights{display:flex;gap:8px}
  .lights span{width:12px;height:12px;border-radius:50%;display:block}
  .lights .r{background:#ff5f57}.lights .y{background:#febc2e}.lights .g{background:#28c840}
  .body{display:flex;min-height:560px}
  nav{width:210px;flex:none;flex-direction:column;border-right:1px solid var(--hair);
    border-bottom:0;padding:14px 10px;top:0;overflow:visible}
  nav button{width:100%}
  main{flex:1;padding:20px 24px}
  .big{font-size:34px}
  .row .k{width:190px}
  .badge{margin-bottom:0}
}
</style>
</head>
<body>
<div class="window">
  <div class="titlebar">
    <div class="lights"><span class="r"></span><span class="y"></span><span class="g"></span></div>
    <h1>Claude Zen Proxy</h1>
    <div class="live"><span class="dot" id="dot"></span><span id="live-label">connecting&hellip;</span></div>
  </div>
  <div class="body">
    <nav id="nav">
      <button data-s="overview" class="active"><span class="ic">&#9673;</span>Overview</button>
      <button data-s="traffic"><span class="ic">&#8646;</span>Traffic</button>
      <button data-s="models"><span class="ic">&#9783;</span>Models</button>
      <button data-s="upstream"><span class="ic">&#9729;</span>Upstream</button>
      <button data-s="endpoints"><span class="ic">&#8982;</span>Endpoints</button>
    </nav>
    <main>
      <section id="s-overview" class="active">
        <h2>Overview</h2>
        <div class="sub" id="ov-sub">Anthropic API &rarr; OpenAI-compatible upstream</div>
        <div class="big" id="ov-status">—</div>
        <div style="margin-top:6px"><span class="badge" id="ov-wire">wire: —</span>
        <span class="badge" id="ov-key">client key: —</span></div>
        <div class="group"><h3>Service</h3><div id="ov-rows"></div></div>
      </section>
      <section id="s-traffic">
        <h2>Traffic</h2>
        <div class="sub">Since process start. Token counts cover non-streaming responses only.</div>
        <div class="group"><h3>Totals</h3><div id="tr-rows"></div></div>
        <div class="group"><h3>By status</h3><div id="tr-status"></div></div>
        <div class="group"><h3>By endpoint</h3><div id="tr-ep"></div></div>
      </section>
      <section id="s-models">
        <h2>Models</h2>
        <div class="sub">Claude names map: haiku&rarr;small, sonnet&rarr;middle, opus&rarr;big</div>
        <div class="group"><h3>Mapping</h3><div id="mo-map"></div></div>
        <div class="group"><h3>Requests by model</h3><div id="mo-rows"></div></div>
      </section>
      <section id="s-upstream">
        <h2>Upstream</h2>
        <div class="sub" id="up-sub">—</div>
        <div class="group"><h3>Last failure</h3><div id="up-fail"></div></div>
        <div class="group"><h3>Handled rotations</h3><div id="up-hist"></div></div>
      </section>
      <section id="s-endpoints">
        <h2>Endpoints</h2>
        <div class="sub">Served by this proxy on :4013</div>
        <div class="group"><h3>API</h3><div id="ep-rows"></div></div>
      </section>
    </main>
  </div>
  <div class="foot"><span id="ft-uptime">uptime —</span><span id="ft-refresh">refresh —</span></div>
</div>
<script>
const $=id=>document.getElementById(id);
document.querySelectorAll('#nav button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('#nav button').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('main section').forEach(x=>x.classList.remove('active'));
  b.classList.add('active'); $('s-'+b.dataset.s).classList.add('active');
  b.scrollIntoView({block:'nearest',inline:'center',behavior:'smooth'}); // keep active tab visible on mobile
  if(window.innerWidth<769)window.scrollTo({top:0,behavior:'smooth'});
});
const row=(k,v,mono)=>'<div class="row"><div class="k">'+k+'</div><div class="v'+(mono?' mono':'')+'">'+v+'</div></div>';
const fmtN=n=>Number(n||0).toLocaleString('en-US');
const fmtT=s=>{s=Math.floor(s||0);const h=Math.floor(s/3600),m=Math.floor(s%3600/60);
  return (h?h+'h ':'')+m+'m '+(s%60)+'s';};
function dist(el,obj,total){
  const ks=Object.keys(obj||{}).sort((a,b)=>obj[b]-obj[a]);
  el.innerHTML=ks.length?ks.map(k=>{const p=total?Math.round(obj[k]/total*100):0;
    return '<div class="row"><div class="k">'+k+'</div><div style="flex:1"><div class="v">'+fmtN(obj[k])+
    ' &middot; '+p+'%</div><div class="bar"><i style="width:'+p+'%"></i></div></div></div>';}).join('')
    :row('—','no data yet');
}
async function tick(){
  let d;
  try{const r=await fetch('/api/status');d=await r.json();}
  catch(e){$('dot').className='dot bad';$('live-label').textContent='unreachable';return;}
  const p=d.proxy,s=d.stats;
  const errAge=s.last_error_at?((Date.now()/1000)-s.last_error_at):null; // secs since last failed request
  const failAge=d.last_upstream_failure&&d.last_upstream_failure.at?((Date.now()/1000)-d.last_upstream_failure.at):null;
  const recentErr=errAge!==null&&errAge<300, recentFail=failAge!==null&&failAge<300; // 5-min window
  const bad=s.error_rate>=0.1&&s.total_requests>0, warn=!bad&&(recentErr||recentFail);
  $('dot').className='dot'+(bad?' bad':warn?' warn':'');
  $('live-label').textContent=bad?'degraded':warn?'attention':'live';
  $('ov-status').textContent='Running';
  const wb=$('ov-wire');wb.textContent='wire: '+p.wire_api;wb.className='badge '+(p.wire_api==='responses'?'warn':'ok');
  const kb=$('ov-key');kb.textContent='client key: '+(p.client_key_validation?'enforced':'open');
  $('ov-sub').textContent='Anthropic API → '+p.openai_base_url;
  $('ov-rows').innerHTML=row('Upstream host','<span>'+p.openai_base_url+'</span>',1)
    +row('User-Agent sent upstream',p.upstream_user_agent,1)
    +row('Max tokens limit',fmtN(p.max_tokens_limit))
    +row('Request timeout',p.request_timeout+'s')
    +row('Avg latency',s.avg_latency_ms+' ms')
    +row('Tokens in / out',fmtN(s.tokens_in)+' / '+fmtN(s.tokens_out));
  $('tr-rows').innerHTML=row('Total requests',fmtN(s.total_requests))
    +row('OK',fmtN(s.ok_requests))+row('Errors',fmtN(s.errors))
    +row('Error rate',(s.error_rate*100).toFixed(1)+'%')
    +row('Avg latency',s.avg_latency_ms+' ms');
  dist($('tr-status'),s.by_status,s.total_requests);
  dist($('tr-ep'),s.by_endpoint,s.total_requests);
  $('mo-map').innerHTML=row('big (opus)',p.models.big,1)+row('middle (sonnet)',p.models.middle,1)+row('small (haiku)',p.models.small,1);
  dist($('mo-rows'),s.by_model,s.total_requests);
  $('up-sub').textContent=p.openai_base_url+'  ·  retry budget '+p.retry_budget_secs+'s  ·  keepalive '+p.keepalive_secs+'s';
  const f=d.last_upstream_failure;
  const esc=x=>String(x).replace(/</g,'&lt;');
  const ago=t=>{const s=Math.max(0,Math.floor(Date.now()/1000-t));return s<60?s+'s ago':Math.floor(s/60)+'m ago';};
  $('up-fail').innerHTML=f?(
      row('status',f.status)+row('error',(f.error||'(no error text)'),1)
      +row('model',esc(f.model||'—'),1)+row('when',ago(f.at))
    ):'<div class="row"><div class="k">—</div><div class="v">no failures recorded</div></div>';
  const hist=d.failure_history||[];
  $('up-hist').innerHTML=hist.length?hist.map(h=>
    '<div class="row"><div class="k">'+h.status+' &middot; '+ago(h.at)+'</div>'+
    '<div class="v mono">'+esc((h.error||'').slice(0,120))+'</div></div>').join('')
    :'<div class="row"><div class="k">—</div><div class="v">no handled rotations yet</div></div>';
  $('ep-rows').innerHTML=['POST /v1/messages|Claude Messages API (translated)',
    'POST /v1/chat/completions|OpenAI passthrough','POST /v1/responses|Responses passthrough',
    'POST /v1/messages/count_tokens|estimate','GET /health · /test-connection · /api/status|ops']
    .map(x=>{const[a,b]=x.split('|');return row(a,b,1);}).join('');
  $('ft-uptime').textContent='uptime '+fmtT(s.uptime_secs);
  $('ft-refresh').textContent='refresh '+new Date().toLocaleTimeString();
}
tick();setInterval(()=>{if(!document.hidden)tick();},5000);
</script>
</body>
</html>
"""


def dashboard_response() -> HTMLResponse:
    """Render the status dashboard."""
    return HTMLResponse(content=PAGE)
