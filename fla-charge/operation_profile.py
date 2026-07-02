"""Operation profile for the FLA charge dashboard (ADR-0002).

The single declarative card that distinguishes this service's web UI from
fla-equalisation's. The shared engine (fla-shared/web_engine.py) is closed
and configured by this card — the plumbing cannot drift; only what is
declared here may differ. States and settings come from this service's own
maps so the page can never disagree with the state machine or the settings
schema.
"""

import os
import sys

sys.path.insert(1, os.path.join(os.path.dirname(__file__), "..", "fla-shared"))

from web_engine import OperationProfile
from settings import SETTINGS_DEFS
from dbus_status_service import STATE_NAMES, STATE_ERROR

HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>__TITLE__</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, sans-serif; background: #0d1b2a; color: #e0e0e0;
         padding: 12px; max-width: 480px; margin: 0 auto; font-size: 14px; }
  h1 { color: #f0f4fc; margin-bottom: 12px; font-size: 1.2em; }
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
  input.si { background: #0d1b2a; border: 1px solid #2a4a7a; color: #f0f4fc;
    padding: 3px 6px; border-radius: 4px; width: 60px; font-size: 0.9em; text-align: right; }
  input.si:focus { border-color: #4a8ad9; outline: none; }
  select.si { background: #0d1b2a; border: 1px solid #2a4a7a; color: #f0f4fc;
    padding: 3px 6px; border-radius: 4px; width: 70px; font-size: 0.9em; }
  .ok { color: #4caf50; font-size: 0.8em; margin-left: 4px; }
  .unit { color: #667; font-size: 0.85em; margin-left: 2px; }
  .updated { color: #556677; font-size: 0.75em; margin-top: 8px; text-align: center; }
  .nav { color: #556677; font-size: 0.8em; margin-top: 10px; text-align: center; }
  .nav a { color: #4a8ad9; text-decoration: none; }
  .nav a:hover { text-decoration: underline; }
</style>
</head>
<body>
<h1>__TITLE__</h1>

<div class="card">
  <h2>Status</h2>
  <div class="row"><span class="label">State</span><span class="value" id="state">-</span></div>
  <div class="row"><span class="label">Time remaining</span><span class="value" id="time">-</span></div>
  <div class="row"><span class="label">Last charge</span><span class="value" id="last">-</span></div>
  <div class="row"><span class="label">Trojan SoC</span><span class="value" id="trojan_soc">-</span></div>
  <div class="row"><span class="label">LFP SoC</span><span class="value" id="lfp_soc">-</span></div>
  <div class="row"><span class="label">Trojan current</span><span class="value" id="trojan_current">-</span></div>
</div>

<div class="card">
  <h2>Voltages</h2>
  <div class="row"><span class="label">Trojan FLA</span><span class="value" id="vtrojan">-</span></div>
  <div class="row"><span class="label">EVE LFP</span><span class="value" id="vlfp">-</span></div>
  <div class="row"><span class="label">Delta</span><span class="value" id="delta">-</span></div>
</div>

<div class="card">
  <h2>Settings</h2>
  <div class="row"><span class="label">Enabled</span><span><select class="si" id="s_enabled" data-key="enabled" data-type="i"><option value="1">Yes</option><option value="0">No</option></select><span class="ok" id="ok_enabled"></span></span></div>
  <div class="row"><span class="label">Trojan SoC trigger</span><span><input class="si" id="s_trojan_soc_trigger" data-key="trojan_soc_trigger" data-type="i"><span class="unit">%</span><span class="ok" id="ok_trojan_soc_trigger"></span></span></div>
  <div class="row"><span class="label">LFP SoC transition</span><span><input class="si" id="s_lfp_soc_transition" data-key="lfp_soc_transition" data-type="i"><span class="unit">%</span><span class="ok" id="ok_lfp_soc_transition"></span></span></div>
  <div class="row"><span class="label">LFP cell V disconnect</span><span><input class="si" id="s_lfp_cell_voltage_disconnect" data-key="lfp_cell_voltage_disconnect" data-type="f"><span class="unit">V</span><span class="ok" id="ok_lfp_cell_voltage_disconnect"></span></span></div>
  <div class="row"><span class="label">Current taper threshold</span><span><input class="si" id="s_current_taper_threshold" data-key="current_taper_threshold" data-type="f"><span class="unit">A</span><span class="ok" id="ok_current_taper_threshold"></span></span></div>
  <div class="row"><span class="label">FLA bulk voltage</span><span><input class="si" id="s_fla_bulk_voltage" data-key="fla_bulk_voltage" data-type="f"><span class="unit">V</span><span class="ok" id="ok_fla_bulk_voltage"></span></span></div>
  <div class="row"><span class="label">Absorption complete I</span><span><input class="si" id="s_fla_absorption_complete_current" data-key="fla_absorption_complete_current" data-type="f"><span class="unit">A</span><span class="ok" id="ok_fla_absorption_complete_current"></span></span></div>
  <div class="row"><span class="label">Absorption max hours</span><span><input class="si" id="s_fla_absorption_max_hours" data-key="fla_absorption_max_hours" data-type="f"><span class="unit">hrs</span><span class="ok" id="ok_fla_absorption_max_hours"></span></span></div>
  <div class="row"><span class="label">FLA float voltage</span><span><input class="si" id="s_fla_float_voltage" data-key="fla_float_voltage" data-type="f"><span class="unit">V</span><span class="ok" id="ok_fla_float_voltage"></span></span></div>
  <div class="row"><span class="label">Max reconnect delta</span><span><input class="si" id="s_voltage_delta_max" data-key="voltage_delta_max" data-type="f"><span class="unit">V</span><span class="ok" id="ok_voltage_delta_max"></span></span></div>
  <div class="row"><span class="label">Voltage match timeout</span><span><input class="si" id="s_voltage_match_timeout_hours" data-key="voltage_match_timeout_hours" data-type="f"><span class="unit">hrs</span><span class="ok" id="ok_voltage_match_timeout_hours"></span></span></div>
  <div class="row"><span class="label">Phase 1 timeout</span><span><input class="si" id="s_phase1_timeout_hours" data-key="phase1_timeout_hours" data-type="f"><span class="unit">hrs</span><span class="ok" id="ok_phase1_timeout_hours"></span></span></div>
</div>

<div class="card">
  <h2>Control</h2>
  <button class="btn" onclick="runNow()">Run Charge Now</button>
  <button class="btn" id="abortBtn" onclick="abort()" style="background:#4a1010; border-color:#8b2020; display:none;">Abort</button>
  <span id="run_msg" style="margin-left: 8px; color: #8899aa; font-size:0.85em;"></span>
</div>

<div class="updated" id="updated"></div>
<div class="nav">Also see: <a href="http://venus.local:8088">FLA Equalisation</a></div>

<script>
var STATES = __STATES__;
var ERROR_STATE = __ERROR_STATE__;
var initialLoad = true;

function sc(s) { return s===0?"idle":s===ERROR_STATE?"error":"active"; }
function fmt(v,u,d) { return (v==null||v==undefined)? "-" : parseFloat(v).toFixed(d||2)+" "+(u||""); }
function fmtT(s) { if(!s||s<=0) return "-"; var m=Math.floor(s/60),h=Math.floor(m/60); return h>0?h+"h "+(m%60)+"m":m+"m"; }
function si(id,v) { var e=document.getElementById(id); if(e&&(initialLoad||document.activeElement!==e)) e.value=v!=null?v:""; }

function save(key,value,type) {
  var v = type==="i" ? parseInt(value) : parseFloat(value);
  if (isNaN(v)) return;
  fetch("/api/setting",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({key:key,value:v})}).then(function(r){return r.json()}).then(function(){
    var ok=document.getElementById("ok_"+key);
    if(ok){ok.textContent="saved";setTimeout(function(){ok.textContent="";},2000);}
  });
}

document.addEventListener("DOMContentLoaded",function(){
  document.querySelectorAll(".si").forEach(function(el){
    el.addEventListener("change",function(){save(el.dataset.key,el.value,el.dataset.type);});
  });
});

function refresh() {
  fetch("/api/status").then(function(r){return r.json()}).then(function(d) {
    var el=document.getElementById("state");
    el.textContent=STATES[d.state]||"Unknown"; el.className="value "+sc(d.state);
    document.getElementById("time").textContent=fmtT(d.time_remaining);
    document.getElementById("last").textContent=d.last_charge||"Never";
    document.getElementById("trojan_soc").textContent=fmt(d.trojan_soc,"%",0);
    document.getElementById("lfp_soc").textContent=fmt(d.lfp_soc,"%",0);
    document.getElementById("trojan_current").textContent=fmt(d.trojan_current,"A",1);
    document.getElementById("vtrojan").textContent=fmt(d.trojan_voltage,"V");
    document.getElementById("vlfp").textContent=fmt(d.lfp_voltage,"V");
    document.getElementById("delta").textContent=fmt(d.voltage_delta,"V");
    if(d.settings){
      var sel=document.getElementById("s_enabled"); if(sel) sel.value=d.settings.enabled?"1":"0";
      si("s_trojan_soc_trigger",d.settings.trojan_soc_trigger);
      si("s_lfp_soc_transition",d.settings.lfp_soc_transition);
      si("s_lfp_cell_voltage_disconnect",d.settings.lfp_cell_voltage_disconnect);
      si("s_current_taper_threshold",d.settings.current_taper_threshold);
      si("s_fla_bulk_voltage",d.settings.fla_bulk_voltage);
      si("s_fla_absorption_complete_current",d.settings.fla_absorption_complete_current);
      si("s_fla_absorption_max_hours",d.settings.fla_absorption_max_hours);
      si("s_fla_float_voltage",d.settings.fla_float_voltage);
      si("s_voltage_delta_max",d.settings.voltage_delta_max);
      si("s_voltage_match_timeout_hours",d.settings.voltage_match_timeout_hours);
      si("s_phase1_timeout_hours",d.settings.phase1_timeout_hours);
    }
    var ab=document.getElementById("abortBtn");
    if(ab) ab.style.display=(d.state>0&&d.state<ERROR_STATE)?"inline-block":"none";
    document.getElementById("updated").textContent="Updated "+new Date().toLocaleTimeString();
    initialLoad=false;
  }).catch(function(e){
    document.getElementById("updated").textContent="Error: "+e;
  });
}

function runNow() {
  if(!confirm("Start FLA charge now?")) return;
  fetch("/api/run-now",{method:"POST"}).then(function(r){return r.json()}).then(function(d){
    document.getElementById("run_msg").textContent=d.message;
    setTimeout(refresh,2000);
  });
}

function abort() {
  if(!confirm("Abort charge?\\nRelay will only close if voltage delta <= 1V.")) return;
  fetch("/api/abort",{method:"POST"}).then(function(r){return r.json()}).then(function(d){
    document.getElementById("run_msg").textContent=d.message;
    setTimeout(refresh,2000);
  });
}

refresh();
setInterval(refresh,5000);
</script>
</body>
</html>"""

PROFILE = OperationProfile(
    name="fla-charge",
    title="FLA Charge",
    port=8089,
    states=STATE_NAMES,
    error_state=STATE_ERROR,
    settings_keys=list(SETTINGS_DEFS),
    cache_fields={
        "state": None,
        "time_remaining": 0,
        "trojan_voltage": None,
        "lfp_voltage": None,
        "voltage_delta": None,
        "trojan_soc": None,
        "lfp_soc": None,
        "trojan_current": None,
        "last_charge": None,
        "settings": {},
    },
    html_template=HTML_TEMPLATE,
)
