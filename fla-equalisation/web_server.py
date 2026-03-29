"""Simple web UI for FLA equalisation status and control.

Serves a single-page dashboard at port 8088 on the Cerbo GX.
Access via http://venus.local:8088
"""

import json
import logging
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

import dbus

log = logging.getLogger(__name__)

PORT = 8088

HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FLA Equalisation — Zwartewater</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, sans-serif; background: #0d1b2a; color: #e0e0e0; padding: 20px; }
  h1 { color: #f0f4fc; margin-bottom: 20px; font-size: 1.4em; }
  .card { background: #1b2838; border-radius: 8px; padding: 16px; margin-bottom: 12px; }
  .card h2 { color: #8bb4d9; font-size: 1em; margin-bottom: 10px; text-transform: uppercase; letter-spacing: 1px; }
  .row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #253546; }
  .row:last-child { border-bottom: none; }
  .label { color: #8899aa; }
  .value { color: #f0f4fc; font-weight: 600; }
  .value.idle { color: #4caf50; }
  .value.active { color: #ff9800; }
  .value.error { color: #f44336; }
  .btn { display: inline-block; background: #152b4e; color: #f0f4fc; border: 1px solid #2a4a7a;
         padding: 10px 24px; border-radius: 6px; cursor: pointer; font-size: 1em; margin-top: 8px; }
  .btn:hover { background: #1e3a66; }
  .btn:active { background: #0d1b2a; }
  .btn.danger { border-color: #ef4035; color: #ef4035; }
  .btn.danger:hover { background: #2a1515; }
  .btn.small { padding: 4px 12px; font-size: 0.85em; margin-left: 8px; }
  .settings-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 16px; }
  input.setting-input { background: #0d1b2a; border: 1px solid #2a4a7a; color: #f0f4fc;
    padding: 4px 8px; border-radius: 4px; width: 70px; font-size: 0.95em; text-align: right; }
  input.setting-input:focus { border-color: #4a8ad9; outline: none; }
  .save-ok { color: #4caf50; font-size: 0.85em; margin-left: 6px; }
  .updated { color: #556677; font-size: 0.8em; margin-top: 12px; text-align: center; }
</style>
</head>
<body>
<h1>FLA Equalisation — Zwartewater</h1>

<div class="card">
  <h2>Status</h2>
  <div class="row"><span class="label">State</span><span class="value" id="state">—</span></div>
  <div class="row"><span class="label">Time remaining</span><span class="value" id="time">—</span></div>
  <div class="row"><span class="label">Last equalisation</span><span class="value" id="last">—</span></div>
  <div class="row"><span class="label">Next due in</span><span class="value" id="next">—</span></div>
</div>

<div class="card">
  <h2>Voltages</h2>
  <div class="row"><span class="label">Trojan FLA</span><span class="value" id="vtrojan">—</span></div>
  <div class="row"><span class="label">EVE LFP</span><span class="value" id="vlfp">—</span></div>
  <div class="row"><span class="label">Delta</span><span class="value" id="delta">—</span></div>
</div>

<div class="card">
  <h2>Settings</h2>
  <div class="row"><span class="label">Equalisation voltage</span><span class="value"><input class="setting-input" id="s_eqv" data-key="eq_voltage" data-type="f"> V<span class="save-ok" id="ok_eq_voltage"></span></span></div>
  <div class="row"><span class="label">Completion current</span><span class="value"><input class="setting-input" id="s_eqi" data-key="eq_current_complete" data-type="f"> A<span class="save-ok" id="ok_eq_current_complete"></span></span></div>
  <div class="row"><span class="label">Max duration</span><span class="value"><input class="setting-input" id="s_timeout" data-key="eq_timeout_hours" data-type="f"> hrs<span class="save-ok" id="ok_eq_timeout_hours"></span></span></div>
  <div class="row"><span class="label">Float voltage</span><span class="value"><input class="setting-input" id="s_float" data-key="float_voltage" data-type="f"> V<span class="save-ok" id="ok_float_voltage"></span></span></div>
  <div class="row"><span class="label">Max delta for reconnect</span><span class="value"><input class="setting-input" id="s_delta" data-key="voltage_delta_max" data-type="f"> V<span class="save-ok" id="ok_voltage_delta_max"></span></span></div>
  <div class="row"><span class="label">Interval</span><span class="value"><input class="setting-input" id="s_days" data-key="days_between" data-type="i"> days<span class="save-ok" id="ok_days_between"></span></span></div>
  <div class="row"><span class="label">Start hour</span><span class="value"><input class="setting-input" id="s_start" data-key="start_hour" data-type="i"> :00<span class="save-ok" id="ok_start_hour"></span></span></div>
  <div class="row"><span class="label">End hour</span><span class="value"><input class="setting-input" id="s_end" data-key="end_hour" data-type="i"> :00<span class="save-ok" id="ok_end_hour"></span></span></div>
  <div class="row"><span class="label">Min LFP SoC</span><span class="value"><input class="setting-input" id="s_soc" data-key="lfp_soc_min" data-type="i"> %<span class="save-ok" id="ok_lfp_soc_min"></span></span></div>
  <div class="row"><span class="label">Enabled</span><span class="value"><select class="setting-input" id="s_enabled" data-key="enabled" data-type="i" style="width:80px"><option value="1">Yes</option><option value="0">No</option></select><span class="save-ok" id="ok_enabled"></span></span></div>
</div>

<div class="card">
  <h2>Control</h2>
  <button class="btn" onclick="runNow()">Run Equalisation Now</button>
  <span id="run_msg" style="margin-left: 12px; color: #8899aa;"></span>
</div>

<div class="updated" id="updated"></div>

<script>
const STATES = {0:"Idle", 1:"Stopping aggregate driver", 2:"Disconnecting LFP",
  3:"Equalising FLA", 4:"Cooling down", 5:"Voltage matching",
  6:"Reconnecting LFP", 7:"Restarting aggregate driver", 8:"Error — manual intervention"};

function stateClass(s) {
  if (s === 0) return "idle";
  if (s === 8) return "error";
  return "active";
}

function fmt(v, unit, decimals) {
  if (v === null || v === undefined) return "—";
  return parseFloat(v).toFixed(decimals || 2) + " " + (unit || "");
}

function fmtTime(s) {
  if (!s || s <= 0) return "—";
  var m = Math.floor(s / 60), h = Math.floor(m / 60);
  if (h > 0) return h + "h " + (m % 60) + "m";
  return m + "m " + Math.floor(s % 60) + "s";
}

var initialLoad = true;
function setInput(id, val) {
  var el = document.getElementById(id);
  if (el && (initialLoad || document.activeElement !== el)) {
    el.value = val != null ? val : "";
  }
}

function saveSetting(key, value, type) {
  var v = type === "i" ? parseInt(value) : parseFloat(value);
  if (isNaN(v)) return;
  fetch("/api/setting", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({key: key, value: v})
  }).then(r => r.json()).then(d => {
    var ok = document.getElementById("ok_" + key);
    if (ok) { ok.textContent = "saved"; setTimeout(function(){ ok.textContent = ""; }, 2000); }
  });
}

// Attach change handlers to all setting inputs
document.addEventListener("DOMContentLoaded", function() {
  document.querySelectorAll(".setting-input").forEach(function(el) {
    el.addEventListener("change", function() {
      saveSetting(el.dataset.key, el.value, el.dataset.type);
    });
  });
});

function refresh() {
  fetch("/api/status").then(r => r.json()).then(d => {
    var el = document.getElementById("state");
    el.textContent = STATES[d.state] || "Unknown";
    el.className = "value " + stateClass(d.state);
    document.getElementById("time").textContent = fmtTime(d.time_remaining);
    document.getElementById("last").textContent = d.last_equalisation || "Never";
    document.getElementById("next").textContent = d.days_until_next !== null ? d.days_until_next + " days" : "Due now";
    document.getElementById("vtrojan").textContent = fmt(d.trojan_voltage, "V");
    document.getElementById("vlfp").textContent = fmt(d.lfp_voltage, "V");
    document.getElementById("delta").textContent = fmt(d.voltage_delta, "V");
    setInput("s_eqv", d.settings.eq_voltage);
    setInput("s_eqi", d.settings.eq_current_complete);
    setInput("s_timeout", d.settings.eq_timeout_hours);
    setInput("s_float", d.settings.float_voltage);
    setInput("s_delta", d.settings.voltage_delta_max);
    setInput("s_days", d.settings.days_between);
    setInput("s_start", d.settings.start_hour);
    setInput("s_end", d.settings.end_hour);
    setInput("s_soc", d.settings.lfp_soc_min);
    var sel = document.getElementById("s_enabled");
    sel.value = d.settings.enabled ? "1" : "0";
    document.getElementById("updated").textContent = "Updated " + new Date().toLocaleTimeString();
  }).catch(e => {
    document.getElementById("updated").textContent = "Error: " + e;
  });
}

function runNow() {
  if (!confirm("Start FLA equalisation now? (LFP SoC must be >= 95%)")) return;
  fetch("/api/run-now", {method:"POST"}).then(r => r.json()).then(d => {
    document.getElementById("run_msg").textContent = d.message;
    setTimeout(refresh, 2000);
  });
}

refresh();
initialLoad = false;
setInterval(refresh, 5000);
</script>
</body>
</html>"""


def _get_dbus_value(bus, service, path):
    """Read a D-Bus value, return None on failure."""
    try:
        obj = bus.get_object(service, path)
        iface = dbus.Interface(obj, "com.victronenergy.BusItem")
        value = iface.GetValue()
        if isinstance(value, dbus.Double):
            return float(value)
        if isinstance(value, (dbus.Int32, dbus.Int16, dbus.UInt32, dbus.Byte)):
            return int(value)
        return value
    except Exception:
        return None


def _get_status_data():
    """Gather all status data from D-Bus."""
    bus = dbus.SystemBus()
    status_svc = "com.victronenergy.fla_equalisation"
    settings_svc = "com.victronenergy.settings"

    # Read last equalisation from file
    last_eq = None
    days_until = None
    try:
        from pathlib import Path
        from datetime import datetime
        last_str = Path("/data/apps/fla-equalisation/last_equalisation").read_text().strip()
        last_dt = datetime.fromisoformat(last_str)
        last_eq = last_dt.strftime("%Y-%m-%d %H:%M")
        days_between = _get_dbus_value(bus, settings_svc,
            "/Settings/FlaEqualisation/DaysBetweenEqualisation") or 90
        days_since = (datetime.now() - last_dt).days
        days_until = max(0, days_between - days_since)
    except Exception:
        pass

    return {
        "state": _get_dbus_value(bus, status_svc, "/State"),
        "time_remaining": _get_dbus_value(bus, status_svc, "/TimeRemaining"),
        "trojan_voltage": _get_dbus_value(bus, status_svc, "/TrojanVoltage"),
        "lfp_voltage": _get_dbus_value(bus, status_svc, "/LfpVoltage"),
        "voltage_delta": _get_dbus_value(bus, status_svc, "/VoltageDelta"),
        "last_equalisation": last_eq,
        "days_until_next": days_until,
        "settings": {
            "eq_voltage": _get_dbus_value(bus, settings_svc,
                "/Settings/FlaEqualisation/EqualisationVoltage"),
            "eq_current_complete": _get_dbus_value(bus, settings_svc,
                "/Settings/FlaEqualisation/EqualisationCurrentComplete"),
            "eq_timeout_hours": _get_dbus_value(bus, settings_svc,
                "/Settings/FlaEqualisation/EqualisationTimeoutHours"),
            "float_voltage": _get_dbus_value(bus, settings_svc,
                "/Settings/FlaEqualisation/FloatVoltage"),
            "voltage_delta_max": _get_dbus_value(bus, settings_svc,
                "/Settings/FlaEqualisation/VoltageDeltaMax"),
            "days_between": _get_dbus_value(bus, settings_svc,
                "/Settings/FlaEqualisation/DaysBetweenEqualisation"),
            "start_hour": _get_dbus_value(bus, settings_svc,
                "/Settings/FlaEqualisation/AfternoonStartHour"),
            "end_hour": _get_dbus_value(bus, settings_svc,
                "/Settings/FlaEqualisation/AfternoonEndHour"),
            "lfp_soc_min": _get_dbus_value(bus, settings_svc,
                "/Settings/FlaEqualisation/LfpSocMin"),
            "enabled": bool(_get_dbus_value(bus, settings_svc,
                "/Settings/FlaEqualisation/Enabled")),
        },
    }


class RequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the web UI."""

    def log_message(self, format, *args):
        pass  # Suppress default HTTP logging

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode())
        elif self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            data = _get_status_data()
            self.wfile.write(json.dumps(data).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/run-now":
            try:
                bus = dbus.SystemBus()
                obj = bus.get_object("com.victronenergy.settings",
                    "/Settings/FlaEqualisation/RunNow")
                iface = dbus.Interface(obj, "com.victronenergy.BusItem")
                iface.SetValue(1)
                msg = {"message": "RunNow flag set — equalisation will start at next check (SoC must be >= 95%)"}
            except Exception as e:
                msg = {"message": "Error: %s" % e}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(msg).encode())
        elif self.path == "/api/setting":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                key = data["key"]
                value = data["value"]
                # Map key to D-Bus path
                key_to_path = {
                    "eq_voltage": "EqualisationVoltage",
                    "eq_current_complete": "EqualisationCurrentComplete",
                    "eq_timeout_hours": "EqualisationTimeoutHours",
                    "float_voltage": "FloatVoltage",
                    "voltage_delta_max": "VoltageDeltaMax",
                    "days_between": "DaysBetweenEqualisation",
                    "start_hour": "AfternoonStartHour",
                    "end_hour": "AfternoonEndHour",
                    "lfp_soc_min": "LfpSocMin",
                    "enabled": "Enabled",
                }
                if key not in key_to_path:
                    raise ValueError("Unknown setting: %s" % key)
                path = "/Settings/FlaEqualisation/" + key_to_path[key]
                bus = dbus.SystemBus()
                obj = bus.get_object("com.victronenergy.settings", path)
                iface = dbus.Interface(obj, "com.victronenergy.BusItem")
                iface.SetValue(value)
                msg = {"ok": True, "key": key, "value": value}
            except Exception as e:
                msg = {"ok": False, "error": str(e)}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(msg).encode())
        else:
            self.send_response(404)
            self.end_headers()


def start_web_server():
    """Start the web server in a background thread."""
    server = HTTPServer(("0.0.0.0", PORT), RequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Web UI started at http://0.0.0.0:%d", PORT)
    return server
