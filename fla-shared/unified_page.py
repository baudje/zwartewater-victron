"""The unified FLA dashboard page (issue #24).

ONE self-contained HTML/JS asset served identically by both services'
engines. It is fully data-driven: at load it fetches /api/config from every
dashboard port (injected as __PORTS__) and builds one panel per operation;
every 5s it polls /api/status from both. Controls POST to whichever service
owns the panel — the engine's origin-validated CORS makes that work from
either port.

Strictly self-contained: no external hosts (the vessel may be offline).
All URLs are built from location.hostname plus the injected ports.
"""

import json

UNIFIED_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>FLA Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, sans-serif; background: #0d1b2a; color: #e0e0e0;
         padding: 12px; max-width: 980px; margin: 0 auto; font-size: 14px; }
  h1 { color: #f0f4fc; margin-bottom: 12px; font-size: 1.2em; }
  h2.panel-title { color: #f0f4fc; font-size: 1.05em; margin-bottom: 8px; }
  .card { background: #1b2838; border-radius: 8px; padding: 12px; margin-bottom: 10px; }
  .card h2 { color: #8bb4d9; font-size: 0.85em; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 1px; }
  .row { display: flex; justify-content: space-between; align-items: center;
         padding: 5px 0; border-bottom: 1px solid #253546; }
  .row:last-child { border-bottom: none; }
  .label { color: #8899aa; font-size: 0.9em; }
  .value { color: #f0f4fc; font-weight: 600; font-size: 0.9em; }
  .value.idle { color: #4caf50; }
  .value.active { color: #ff9800; }
  .value.error { color: #f44336; }
  .btn { display: inline-block; background: #152b4e; color: #f0f4fc; border: 1px solid #2a4a7a;
         padding: 8px 20px; border-radius: 6px; cursor: pointer; font-size: 0.9em; margin-top: 4px; }
  .btn:hover { background: #1e3a66; }
  .btn:active { background: #0d1b2a; }
  .btn.abort { background: #4a1010; border-color: #8b2020; }
  input.si { background: #0d1b2a; border: 1px solid #2a4a7a; color: #f0f4fc;
    padding: 3px 6px; border-radius: 4px; width: 60px; font-size: 0.9em; text-align: right; }
  input.si:focus { border-color: #4a8ad9; outline: none; }
  select.si { background: #0d1b2a; border: 1px solid #2a4a7a; color: #f0f4fc;
    padding: 3px 6px; border-radius: 4px; width: 70px; font-size: 0.9em; }
  .ok { color: #4caf50; font-size: 0.8em; margin-left: 4px; }
  .unit { color: #667; font-size: 0.85em; margin-left: 2px; }
  .updated { color: #556677; font-size: 0.75em; margin-top: 8px; text-align: center; }
  .panels { display: grid; grid-template-columns: 1fr; gap: 10px; align-items: start; }
  @media (min-width: 760px) { .panels { grid-template-columns: 1fr 1fr; } }
  .panel { border: 1px solid #253546; border-radius: 10px; padding: 10px; background: #14202e; }
  .panel.error-state { border-color: #f44336; }
  .panel.unreachable { opacity: 0.65; border-style: dashed; }
  .badge { font-size: 0.75em; padding: 2px 8px; border-radius: 10px; }
  .badge.down { background: #4a1010; color: #ff9d9d; }
  .panel-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
  .msg { margin-left: 8px; color: #8899aa; font-size: 0.85em; }
  details.logbox summary { color: #8bb4d9; font-size: 0.85em; text-transform: uppercase;
    letter-spacing: 1px; cursor: pointer; }
  pre.log { font-size: 0.72em; line-height: 1.5; white-space: pre-wrap; word-break: break-all;
    color: #9fb3c8; max-height: 260px; overflow-y: auto; margin-top: 8px; }
  table.runs { width: 100%; border-collapse: collapse; font-size: 0.78em; }
  table.runs th { color: #8899aa; text-align: left; font-weight: 400; padding: 3px 4px;
    border-bottom: 1px solid #253546; }
  table.runs td { padding: 3px 4px; border-bottom: 1px solid #1e2c3c; color: #cfd9e4; }
  td.outcome-success { color: #4caf50; }
  td.outcome-aborted { color: #ff9800; }
  td.outcome-failed { color: #f44336; }
</style>
</head>
<body>
<h1>FLA Dashboard</h1>

<div class="card">
  <h2>System</h2>
  <div class="row"><span class="label">Trojan FLA</span><span class="value" id="sys_trojan_voltage">-</span></div>
  <div class="row"><span class="label">EVE LFP</span><span class="value" id="sys_lfp_voltage">-</span></div>
  <div class="row"><span class="label">Delta</span><span class="value" id="sys_voltage_delta">-</span></div>
  <div class="row"><span class="label">Trojan SoC</span><span class="value" id="sys_trojan_soc">-</span></div>
  <div class="row"><span class="label">LFP SoC</span><span class="value" id="sys_lfp_soc">-</span></div>
</div>

<div class="panels" id="panels"></div>

<div class="updated" id="updated">Loading…</div>

<script>
var PORTS = __PORTS__;
var SVC = {};   // port -> {cfg, status, reachable, built}
var initialLoad = true;
PORTS.forEach(function(p){ SVC[p] = {cfg:null, status:null, reachable:false, built:false}; });

function base(port) { return location.protocol + "//" + location.hostname + ":" + port; }
function el(id) { return document.getElementById(id); }

var FMT = {
  v:    function(x){ return x==null ? "-" : parseFloat(x).toFixed(2)+" V"; },
  a:    function(x){ return x==null ? "-" : parseFloat(x).toFixed(1)+" A"; },
  pct:  function(x){ return x==null ? "-" : parseFloat(x).toFixed(0)+" %"; },
  days_or_due: function(x){ return x==null ? "Due now" : x+" days"; },
  text: function(x){ return x==null || x==="" ? "Never" : String(x); }
};
function fmtT(s) { if(!s||s<=0) return "-"; var m=Math.floor(s/60),h=Math.floor(m/60); return h>0?h+"h "+(m%60)+"m":m+"m"; }
function si(id,v) { var e=el(id); if(e&&(initialLoad||document.activeElement!==e)) e.value=v!=null?v:""; }

function buildPanel(port) {
  var cfg = SVC[port].cfg;
  var d = document.createElement("div");
  d.className = "panel"; d.id = "panel_"+port;
  var h = '<div class="panel-head"><h2 class="panel-title">'+cfg.title+'</h2>' +
          '<span class="badge down" id="down_'+port+'" style="display:none">unreachable</span></div>';
  h += '<div class="card"><h2>Status</h2>' +
       '<div class="row"><span class="label">State</span><span class="value" id="state_'+port+'">-</span></div>' +
       '<div class="row"><span class="label">Time remaining</span><span class="value" id="time_'+port+'">-</span></div>';
  cfg.panel_fields.forEach(function(f){
    h += '<div class="row"><span class="label">'+f.label+'</span><span class="value" id="pf_'+port+'_'+f.key+'">-</span></div>';
  });
  h += '</div><div class="card"><h2>Settings</h2>';
  cfg.settings_rows.forEach(function(r){
    var input;
    if (r.type === "b") {
      input = '<select class="si" id="s_'+port+'_'+r.key+'"><option value="1">Yes</option><option value="0">No</option></select>';
    } else {
      input = '<input class="si" id="s_'+port+'_'+r.key+'">';
    }
    h += '<div class="row"><span class="label">'+r.label+'</span><span>'+input+
         '<span class="unit">'+(r.unit||"")+'</span><span class="ok" id="ok_'+port+'_'+r.key+'"></span></span></div>';
  });
  h += '</div><div class="card"><h2>Control</h2>' +
       '<button class="btn" id="run_'+port+'">Run Now</button> ' +
       '<button class="btn abort" id="abort_'+port+'" style="display:none">Abort</button>' +
       '<span class="msg" id="msg_'+port+'"></span></div>';
  h += '<div class="card"><h2>Recent runs</h2><table class="runs">' +
       '<thead><tr><th>Start</th><th>Outcome</th><th>Peak V</th><th>At target</th><th>Delta</th></tr></thead>' +
       '<tbody id="runs_'+port+'"><tr><td colspan="5">-</td></tr></tbody></table></div>';
  h += '<div class="card"><details class="logbox" id="logbox_'+port+'">' +
       '<summary>Log</summary><pre class="log" id="log_'+port+'">-</pre></details></div>';
  d.innerHTML = h;
  el("panels").appendChild(d);

  cfg.settings_rows.forEach(function(r){
    var e = el("s_"+port+"_"+r.key);
    e.addEventListener("change", function(){ save(port, r.key, e.value, r.type); });
  });
  el("run_"+port).addEventListener("click", function(){ runNow(port); });
  el("abort_"+port).addEventListener("click", function(){ doAbort(port); });
  SVC[port].built = true;
}

function loadConfig(port) {
  return fetch(base(port)+"/api/config").then(function(r){ return r.json(); })
    .then(function(cfg){ SVC[port].cfg = cfg; if(!SVC[port].built) buildPanel(port); });
}

function renderPanel(port) {
  var s = SVC[port], cfg = s.cfg, d = s.status;
  var panel = el("panel_"+port);
  if (!panel) return;
  el("down_"+port).style.display = s.reachable ? "none" : "inline-block";
  panel.className = "panel" + (s.reachable ? "" : " unreachable");
  if (!d) return;
  var stateEl = el("state_"+port);
  stateEl.textContent = cfg.states[String(d.state)] || (d.state==null ? "-" : "Unknown");
  var cls = d.state===0 ? "idle" : d.state===cfg.error_state ? "error" : "active";
  stateEl.className = "value " + (d.state==null ? "" : cls);
  if (d.state === cfg.error_state) panel.className += " error-state";
  el("time_"+port).textContent = fmtT(d.time_remaining);
  cfg.panel_fields.forEach(function(f){
    el("pf_"+port+"_"+f.key).textContent = FMT[f.format](d[f.key]);
  });
  if (d.settings) {
    cfg.settings_rows.forEach(function(r){
      if (r.type === "b") {
        var sel = el("s_"+port+"_"+r.key);
        if (sel && (initialLoad || document.activeElement !== sel)) sel.value = d.settings[r.key] ? "1" : "0";
      } else {
        si("s_"+port+"_"+r.key, d.settings[r.key]);
      }
    });
  }
  el("abort_"+port).style.display = (d.state>0 && d.state<cfg.error_state) ? "inline-block" : "none";
}

function esc(t) { var d=document.createElement("span"); d.textContent=t==null?"-":String(t); return d.innerHTML; }

function refreshRuns(port) {
  if (!SVC[port].reachable) return;
  fetch(base(port)+"/api/runs?limit=8").then(function(r){ return r.json(); })
    .then(function(d){
      var tb = el("runs_"+port);
      if (!tb) return;
      if (!d.runs.length) { tb.innerHTML = '<tr><td colspan="5">No runs recorded yet</td></tr>'; return; }
      tb.innerHTML = d.runs.map(function(r){
        return '<tr><td>'+esc(r.start ? r.start.replace("T"," ") : "-")+'</td>' +
               '<td class="outcome-'+esc(r.outcome)+'">'+esc(r.outcome)+'</td>' +
               '<td>'+(r.peak_trojan_voltage!=null ? esc(parseFloat(r.peak_trojan_voltage).toFixed(2))+" V" : "-")+'</td>' +
               '<td>'+(r.minutes_at_target!=null ? esc(r.minutes_at_target)+" min" : "-")+'</td>' +
               '<td>'+(r.reconnect_delta!=null ? esc(parseFloat(r.reconnect_delta).toFixed(2))+" V" : "-")+'</td></tr>';
      }).join("");
    }).catch(function(){});
}

function refreshLog(port) {
  // Only fetch when the operator has the log card open — no idle cost.
  var box = el("logbox_"+port);
  if (!box || !box.open || !SVC[port].reachable) return;
  fetch(base(port)+"/api/log?lines=50").then(function(r){ return r.json(); })
    .then(function(d){ el("log_"+port).textContent = d.lines.length ? d.lines.join("\\n") : "(log empty)"; })
    .catch(function(){});
}

function renderHeader() {
  ["trojan_voltage","lfp_voltage","voltage_delta","trojan_soc","lfp_soc"].forEach(function(key){
    var val = null;
    PORTS.forEach(function(p){
      var d = SVC[p].status;
      if (val==null && d && d[key]!=null) val = d[key];
    });
    var f = (key==="trojan_soc"||key==="lfp_soc") ? FMT.pct : FMT.v;
    el("sys_"+key).textContent = f(val);
  });
}

function save(port, key, value, type) {
  var v = (type === "f") ? parseFloat(value) : parseInt(value);
  if (isNaN(v)) return;
  fetch(base(port)+"/api/setting", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({key:key, value:v})}).then(function(r){ return r.json(); }).then(function(){
    var ok = el("ok_"+port+"_"+key);
    if (ok) { ok.textContent="saved"; setTimeout(function(){ ok.textContent=""; }, 2000); }
  }).catch(function(){});
}

function fillConfirm(tpl, settings) {
  return tpl.replace(/\\{(\\w+)\\}/g, function(_, k){
    return settings && settings[k] != null ? settings[k] : "?";
  });
}

function runNow(port) {
  var cfg = SVC[port].cfg, d = SVC[port].status || {};
  var text = cfg.run_now_confirm ? fillConfirm(cfg.run_now_confirm, d.settings)
                                 : "Start " + cfg.title + " now?";
  if (!confirm(text)) return;
  fetch(base(port)+"/api/run-now", {method:"POST"}).then(function(r){ return r.json(); })
    .then(function(m){ el("msg_"+port).textContent = m.message; })
    .catch(function(){ el("msg_"+port).textContent = "request failed"; });
}

function doAbort(port) {
  var cfg = SVC[port].cfg;
  if (!confirm("Abort " + cfg.title + "?\\nRelay will only close if voltage delta <= 1V.")) return;
  fetch(base(port)+"/api/abort", {method:"POST"}).then(function(r){ return r.json(); })
    .then(function(m){ el("msg_"+port).textContent = m.message; })
    .catch(function(){ el("msg_"+port).textContent = "request failed"; });
}

var cycle = 0;
function refresh() {
  var pending = PORTS.map(function(port){
    var chain = SVC[port].cfg ? Promise.resolve() : loadConfig(port);
    return chain.then(function(){
      return fetch(base(port)+"/api/status").then(function(r){ return r.json(); })
        .then(function(d){ SVC[port].status = d; SVC[port].reachable = true; });
    }).catch(function(){ SVC[port].reachable = false; })
      .then(function(){
        renderPanel(port); refreshLog(port);
        if (cycle % 12 === 0) refreshRuns(port);
      });
  });
  Promise.all(pending).then(function(){
    renderHeader();
    var anyUp = PORTS.some(function(p){ return SVC[p].reachable; });
    el("updated").textContent = anyUp
      ? "Updated " + new Date().toLocaleTimeString()
      : "Both services unreachable — retrying…";
    initialLoad = false;
    cycle++;
  });
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


def render_unified_page(ports):
    """Bake the dashboard port list into the shared page. Everything else
    is fetched from each service's /api/config at runtime, so the page is
    byte-identical no matter which service serves it."""
    return UNIFIED_TEMPLATE.replace("__PORTS__", json.dumps(list(ports)))
