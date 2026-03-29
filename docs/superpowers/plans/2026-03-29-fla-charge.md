# FLA Charge Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an FLA charge service that detects undercharged Trojans and runs a full bulk+absorption charge cycle by temporarily disconnecting LFPs, reusing shared safety infrastructure with the existing FLA equalisation service.

**Architecture:** Extract common relay/DVCC/safety code from the equalisation service into `fla-shared/`. Both equalisation and charge services import from shared. A file-based lock prevents simultaneous operations. The charge service has its own daemontools service, D-Bus status, web UI (port 8089), and Venus OS settings.

**Tech Stack:** Python 3.8+ (Venus OS), dbus-python, GLib mainloop, Victron velib_python

---

## File Structure

### Phase 1: Shared Module Extraction

```
fla-shared/
├── relay_control.py         # Open/close relay, verify, delta-aware safety, startup check
├── voltage_matching.py      # Voltage matching loop with SmartShunt watchdog
├── aggregate_driver.py      # Stop/start dbus-aggregate-batteries
├── lock.py                  # File-based operation lock
├── temp_battery.py          # TempBatteryService (moved from fla-equalisation/dbus_battery_service.py)
├── dbus_monitor.py          # Moved from fla-equalisation/
├── alerting.py              # Moved from fla-equalisation/
└── __init__.py              # Empty, makes it importable
```

### Phase 2: FLA Charge Service

```
fla-charge/
├── fla_charge.py            # Main service: trigger logic, Phase 1-4 orchestration
├── dbus_status_service.py   # D-Bus status (device instance 201, own state constants)
├── settings.py              # Venus OS settings at /Settings/FlaCharge/*
├── web_server.py            # Web UI at port 8089
├── service/
│   └── run                  # Daemontools runner
└── install.sh               # Installer
```

### Modified: FLA Equalisation Service

```
fla-equalisation/
├── fla_equalisation.py      # Refactored to import from fla-shared
├── dbus_status_service.py   # Unchanged
├── settings.py              # Unchanged
├── web_server.py            # Add link to charge UI
├── service/run              # Unchanged
└── install.sh               # Updated to install fla-shared
```

**Responsibilities per module:**
- `relay_control.py` — `open_relay(monitor, status)`, `close_relay_safe(monitor, status, delta_max=2.0)`, `verify_relay_open(monitor)`, `startup_safety_check(monitor, status)`
- `voltage_matching.py` — `wait_for_voltage_match(monitor, temp_service, status, settings)` returns True/False
- `aggregate_driver.py` — `stop()`, `start()` with logging
- `lock.py` — `acquire(service_name)`, `release()`, `is_locked()`, `holder()` using file lock
- `temp_battery.py` — `TempBatteryService` class (existing code, moved)

---

### Task 1: Create fla-shared Directory and Extract Core Modules

**Files:**
- Create: `fla-shared/__init__.py`
- Create: `fla-shared/aggregate_driver.py`
- Create: `fla-shared/lock.py`
- Move: `fla-equalisation/dbus_monitor.py` → `fla-shared/dbus_monitor.py`
- Move: `fla-equalisation/alerting.py` → `fla-shared/alerting.py`
- Move: `fla-equalisation/dbus_battery_service.py` → `fla-shared/temp_battery.py`

- [ ] **Step 1: Create fla-shared directory with __init__.py**

```bash
mkdir -p fla-shared
touch fla-shared/__init__.py
```

- [ ] **Step 2: Create aggregate_driver.py**

Extract `stop_aggregate_driver()` and `start_aggregate_driver()` from `fla_equalisation.py` lines 64-84:

```python
"""Start/stop the dbus-aggregate-batteries service."""

import logging
import subprocess
import time

log = logging.getLogger(__name__)

AGG_SERVICE_PATH = "/service/dbus-aggregate-batteries"


def stop():
    """Stop dbus-aggregate-batteries. Returns True on success."""
    log.info("Stopping dbus-aggregate-batteries...")
    result = subprocess.run(["svc", "-d", AGG_SERVICE_PATH], capture_output=True)
    if result.returncode != 0:
        log.error("Failed to stop aggregate driver: %s", result.stderr.decode())
        return False
    time.sleep(5)
    log.info("Aggregate driver stopped")
    return True


def start():
    """Start dbus-aggregate-batteries. Returns True on success."""
    log.info("Starting dbus-aggregate-batteries...")
    result = subprocess.run(["svc", "-u", AGG_SERVICE_PATH], capture_output=True)
    if result.returncode != 0:
        log.error("Failed to start aggregate driver: %s", result.stderr.decode())
        return False
    log.info("Aggregate driver started")
    return True
```

- [ ] **Step 3: Create lock.py**

```python
"""File-based operation lock for FLA services.

Prevents equalisation and charge from running simultaneously.
Lock file at /data/apps/fla-shared/operation.lock
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

LOCK_FILE = "/data/apps/fla-shared/operation.lock"


def acquire(service_name):
    """Acquire the operation lock. Returns True if acquired, False if held by another."""
    if is_locked():
        holder_info = holder()
        log.debug("Lock held by %s since %s", holder_info.get("service"), holder_info.get("started"))
        return False
    try:
        Path(LOCK_FILE).write_text(json.dumps({
            "service": service_name,
            "started": datetime.now().isoformat(),
            "pid": os.getpid(),
        }))
        log.info("Operation lock acquired by %s", service_name)
        return True
    except OSError as e:
        log.error("Failed to acquire lock: %s", e)
        return False


def release():
    """Release the operation lock."""
    try:
        Path(LOCK_FILE).unlink(missing_ok=True)
        log.info("Operation lock released")
    except OSError as e:
        log.warning("Failed to release lock: %s", e)


def is_locked():
    """Check if the lock is held. Also cleans stale locks (PID no longer running)."""
    if not Path(LOCK_FILE).exists():
        return False
    try:
        info = json.loads(Path(LOCK_FILE).read_text())
        pid = info.get("pid")
        if pid and not _pid_exists(pid):
            log.warning("Stale lock from PID %s — clearing", pid)
            release()
            return False
        return True
    except (json.JSONDecodeError, OSError):
        return False


def holder():
    """Return info about the lock holder, or empty dict."""
    try:
        return json.loads(Path(LOCK_FILE).read_text())
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return {}


def _pid_exists(pid):
    """Check if a process with given PID exists."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False
```

- [ ] **Step 4: Move dbus_monitor.py, alerting.py, and dbus_battery_service.py**

```bash
cp fla-equalisation/dbus_monitor.py fla-shared/dbus_monitor.py
cp fla-equalisation/alerting.py fla-shared/alerting.py
cp fla-equalisation/dbus_battery_service.py fla-shared/temp_battery.py
```

Keep the originals in `fla-equalisation/` temporarily — they become thin wrappers in Task 3.

- [ ] **Step 5: Commit**

```bash
git add fla-shared/
git commit -m "feat: create fla-shared module — aggregate driver, lock, move monitor/alerting/temp_battery"
```

---

### Task 2: Extract Relay Control and Voltage Matching

**Files:**
- Create: `fla-shared/relay_control.py`
- Create: `fla-shared/voltage_matching.py`

- [ ] **Step 1: Create relay_control.py**

Extract relay safety logic from `fla_equalisation.py` (lines 156-171 verify open, 296-308 verify close, 355-374 delta-aware finally, 392-411 startup check):

```python
"""Relay 2 control with safety verification.

Shared between FLA equalisation and FLA charge services.
All operations verify hardware state via read-back.
"""

import logging
import time

log = logging.getLogger(__name__)

RELAY_CLOSE_DELTA_MAX = 2.0  # Max voltage delta for auto-close (V)


def open_relay(monitor):
    """Open relay 2 to disconnect LFP direct path. Returns True on success."""
    if not monitor.set_relay(0):
        log.error("Failed to send relay open command")
        return False
    log.info("Relay 2 opened — LFP direct path disconnected, Orion activating")
    return True


def verify_relay_open(monitor, wait_seconds=10):
    """Verify LFP is disconnected after relay open. Returns True if verified."""
    time.sleep(wait_seconds)
    lfp_current = monitor.get_lfp_current()
    if lfp_current is not None and abs(lfp_current) > 5.0:
        log.error("LFP current still %.1fA after relay open — relay may not have opened", abs(lfp_current))
        return False
    return True


def close_relay_verified(monitor):
    """Close relay 2 and verify via read-back. Returns True on success."""
    if not monitor.set_relay(1):
        log.error("Failed to send relay close command")
        return False
    time.sleep(2)
    if monitor.get_relay_state() != 1:
        log.error("Relay 2 failed to close — read-back shows still open")
        return False
    log.info("Relay 2 closed and verified — LFP direct path restored")
    return True


def close_relay_delta_aware(monitor, alerting_mod=None, status=None):
    """Close relay only if voltage delta is safe. For use in finally/cleanup blocks.

    If delta > RELAY_CLOSE_DELTA_MAX: raises alarm, does NOT close.
    If delta <= max or unreadable with low risk: closes relay.
    """
    relay_state = monitor.get_relay_state()
    if relay_state != 0:
        return  # Relay not open, nothing to do

    v_t = monitor.get_trojan_voltage()
    v_l = monitor.get_lfp_voltage()

    if v_t is not None and v_l is not None:
        delta = abs(v_t - v_l)
        if delta > RELAY_CLOSE_DELTA_MAX:
            log.error(
                "SAFETY: Relay open with delta=%.1fV — too large to auto-close. "
                "LFPs remain on Orion. Manual intervention required.", delta
            )
            if alerting_mod and status:
                alerting_mod.raise_alarm(
                    "Relay open with %.1fV delta — manual close required" % delta,
                    status_service=status,
                )
            return  # Do NOT close
        log.warning("Safety: closing relay (delta=%.1fV)", delta)
    else:
        log.warning("Safety: closing relay (voltages unreadable — assuming safe)")

    monitor.set_relay(1)
    time.sleep(2)


def startup_safety_check(monitor, status=None, alerting_mod=None):
    """Check relay state on service startup. Recovers from interrupted operations."""
    relay_state = monitor.get_relay_state()
    if relay_state != 0:
        return  # Relay closed, nothing to recover

    log.warning("STARTUP: Relay 2 is open — possible interrupted operation")
    v_t = monitor.get_trojan_voltage()
    v_l = monitor.get_lfp_voltage()

    if v_t is not None and v_l is not None:
        delta = abs(v_t - v_l)
        if delta < RELAY_CLOSE_DELTA_MAX:
            log.info("STARTUP: Delta=%.1fV safe — closing relay", delta)
            monitor.set_relay(1)
            time.sleep(3)
        else:
            log.error("STARTUP: Delta=%.1fV too high — leaving relay open, alarm raised", delta)
            if status:
                from fla_shared.dbus_status_service import STATE_ERROR
                status.update(state=STATE_ERROR)
            if alerting_mod and status:
                alerting_mod.raise_alarm(
                    "Startup: relay open with %.1fV delta — manual close required" % delta,
                    status_service=status,
                )
    else:
        log.error("STARTUP: Cannot read voltages — leaving relay open for safety")
        if alerting_mod and status:
            alerting_mod.raise_alarm(
                "Startup: relay open, cannot read voltages — manual check required",
                status_service=status,
            )
```

- [ ] **Step 2: Create voltage_matching.py**

Extract voltage matching loop from `fla_equalisation.py` lines 250-294:

```python
"""Voltage matching loop — waits for Trojan/LFP delta to converge before reconnect.

Shared between FLA equalisation and FLA charge services.
"""

import logging
import time

log = logging.getLogger(__name__)


def wait_for_match(monitor, temp_service, status, alerting_mod,
                   voltage_delta_max=1.0, timeout_hours=4.0,
                   float_voltage=None):
    """Wait for |V_trojan - V_lfp| < voltage_delta_max.

    If float_voltage is provided, reduces CVL to that voltage first.
    Returns (True, delta) on success, (False, delta) on timeout/error.
    """
    if float_voltage is not None:
        log.info("Reducing CVL to float voltage %.1fV", float_voltage)
        temp_service.set_charge_voltage(float_voltage)

    log.info("Waiting for voltage convergence (delta < %.1fV)", voltage_delta_max)
    match_start = time.time()
    match_timeout = timeout_hours * 3600
    delta = None

    while True:
        elapsed = time.time() - match_start
        v_trojan = monitor.get_trojan_voltage()
        v_lfp = monitor.get_lfp_voltage()

        # SmartShunt Trojan responsive check
        if v_trojan is None:
            log.error("SmartShunt Trojan unresponsive during voltage matching")
            if alerting_mod and status:
                alerting_mod.raise_alarm(
                    "SmartShunt Trojan unresponsive during voltage matching",
                    status_service=status,
                )
            return False, delta

        if v_trojan is not None and v_lfp is not None:
            delta = abs(v_trojan - v_lfp)
            remaining = max(0, match_timeout - elapsed)
            status.update(time_remaining=remaining, trojan_v=v_trojan, lfp_v=v_lfp)
            temp_service.update_voltage_current(v_trojan, monitor.get_trojan_current())

            if delta < voltage_delta_max:
                log.info("Voltage converged: Trojan=%.2fV, LFP=%.2fV, delta=%.2fV",
                         v_trojan, v_lfp, delta)
                return True, delta

            if int(elapsed) % 300 < 30:
                log.info("Voltage matching: Trojan=%.2fV, LFP=%.2fV, delta=%.2fV (%.0f min)",
                         v_trojan, v_lfp, delta, elapsed / 60)

        if elapsed > match_timeout:
            log.error("Voltage delta did not converge after %.0f hours", elapsed / 3600)
            if alerting_mod and status:
                alerting_mod.raise_alarm(
                    "Voltage delta did not converge after %.0f hours. "
                    "Trojan=%.2fV, LFP=%.2fV, delta=%.2fV. "
                    "LFPs remain disconnected — manual intervention required."
                    % (elapsed / 3600, v_trojan or 0, v_lfp or 0, delta or 0),
                    status_service=status,
                )
            return False, delta

        time.sleep(30)
```

- [ ] **Step 3: Commit**

```bash
git add fla-shared/relay_control.py fla-shared/voltage_matching.py
git commit -m "feat: extract relay_control and voltage_matching into shared modules"
```

---

### Task 3: Refactor FLA Equalisation to Use Shared Modules

**Files:**
- Modify: `fla-equalisation/fla_equalisation.py` (major refactor)
- Modify: `fla-equalisation/install.sh`
- Replace: `fla-equalisation/dbus_monitor.py` → thin import wrapper
- Replace: `fla-equalisation/alerting.py` → thin import wrapper

- [ ] **Step 1: Create thin wrappers for moved modules**

`fla-equalisation/dbus_monitor.py`:
```python
"""Thin wrapper — imports from fla-shared."""
import sys, os
sys.path.insert(0, "/data/apps/fla-shared")
from dbus_monitor import *
```

`fla-equalisation/alerting.py`:
```python
"""Thin wrapper — imports from fla-shared."""
import sys, os
sys.path.insert(0, "/data/apps/fla-shared")
from alerting import *
```

Remove `fla-equalisation/dbus_battery_service.py` — replaced by `fla-shared/temp_battery.py`.

- [ ] **Step 2: Refactor fla_equalisation.py**

Replace all inline relay, voltage matching, and aggregate driver code with calls to shared modules. The key changes:

1. Add `sys.path.insert(0, "/data/apps/fla-shared")` at top
2. Import: `from relay_control import open_relay, verify_relay_open, close_relay_verified, close_relay_delta_aware, startup_safety_check`
3. Import: `from voltage_matching import wait_for_match`
4. Import: `from aggregate_driver import stop as stop_aggregate_driver, start as start_aggregate_driver`
5. Import: `from temp_battery import TempBatteryService`
6. Import: `from lock import acquire as acquire_lock, release as release_lock`
7. Replace inline `stop_aggregate_driver()` / `start_aggregate_driver()` functions with imports
8. Replace relay open/close/verify blocks with shared function calls
9. Replace voltage matching loop with `wait_for_match()` call
10. Replace finally block relay logic with `close_relay_delta_aware()`
11. Replace startup check with `startup_safety_check()`
12. Add lock acquire/release around `run_equalisation()`

The `run_equalisation()` function should become approximately:
```python
def run_equalisation(settings, monitor, status):
    temp_service = None
    aggregate_stopped = False

    if not acquire_lock("fla-equalisation"):
        log.info("Operation lock held — skipping")
        return False

    try:
        # Step 1: Register temp service at safe voltage
        temp_service = TempBatteryService(device_instance=100)
        temp_service.register(charge_voltage=28.4, charge_current=120.0, discharge_current=0)

        # Step 2: Stop aggregate driver
        status.update(state=STATE_STOPPING_DRIVER)
        if not stop_aggregate_driver():
            status.update(state=STATE_ERROR)
            raise_alarm("Failed to stop aggregate driver", status_service=status)
            return False
        aggregate_stopped = True

        # Step 3: Open relay
        status.update(state=STATE_DISCONNECTING)
        if not open_relay(monitor):
            status.update(state=STATE_ERROR)
            raise_alarm("Failed to open relay 2", status_service=status)
            return False

        # Step 4: Verify relay open
        if not verify_relay_open(monitor):
            status.update(state=STATE_ERROR)
            raise_alarm("LFP not disconnected after relay open", status_service=status)
            return False

        # Record LFP voltage for Orion failure detection
        lfp_voltage_at_disconnect = monitor.get_lfp_voltage()

        # Step 5: Raise CVL to equalisation voltage
        temp_service.set_charge_voltage(settings.eq_voltage)

        # Step 6: Equalisation loop (service-specific — not shared)
        status.update(state=STATE_EQUALISING)
        # ... equalisation monitoring loop stays inline ...

        # Step 7-8: Voltage matching (shared)
        status.update(state=STATE_VOLTAGE_MATCHING)
        matched, delta = wait_for_match(
            monitor, temp_service, status, alerting,
            voltage_delta_max=settings.voltage_delta_max,
            timeout_hours=settings.voltage_match_timeout_hours,
            float_voltage=settings.float_voltage,
        )
        if not matched:
            return False

        # Step 9: Reconnect
        status.update(state=STATE_RECONNECTING)
        if not close_relay_verified(monitor):
            raise_alarm("Failed to close relay 2", status_service=status)
            return False

        # Measure inrush
        time.sleep(1)
        inrush = monitor.get_lfp_current()
        status.update(inrush_current=abs(inrush) if inrush else None, reconnect_delta=delta)

        # Step 10: Cleanup
        temp_service.deregister()
        temp_service = None
        status.update(state=STATE_RESTARTING_DRIVER)
        if not start_aggregate_driver():
            raise_alarm("Failed to restart aggregate driver", status_service=status)
            return False
        monitor.invalidate_services()

        write_last_equalisation()
        status.update(state=STATE_IDLE, time_remaining=0)
        clear_alarm(status_service=status)
        return True

    except Exception as e:
        log.exception("Unexpected error: %s", e)
        raise_alarm("Equalisation error: %s" % e, status_service=status)
        return False

    finally:
        if temp_service is not None:
            try: temp_service.deregister()
            except: pass
        close_relay_delta_aware(monitor, alerting, status)
        if aggregate_stopped:
            try: start_aggregate_driver()
            except: log.error("CRITICAL: Failed to restart aggregate driver")
        release_lock()
```

- [ ] **Step 3: Update install.sh to install fla-shared**

Add to `fla-equalisation/install.sh`:
```bash
# Install shared modules
SHARED_DIR="/data/apps/fla-shared"
mkdir -p "${SHARED_DIR}"
for f in __init__.py relay_control.py voltage_matching.py aggregate_driver.py lock.py dbus_monitor.py alerting.py temp_battery.py; do
    cp "${SCRIPT_DIR}/../fla-shared/${f}" "${SHARED_DIR}/"
done
mkdir -p "${SHARED_DIR}/ext"
ln -sfn /data/apps/dbus-aggregate-batteries/ext/velib_python "${SHARED_DIR}/ext/velib_python"
```

- [ ] **Step 4: Run existing tests**

```bash
python3 -m unittest fla-equalisation/tests/test_fla_equalisation.py -v
```

Expected: All 26 tests pass (some may need path updates for moved modules)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: FLA equalisation uses shared modules — relay, voltage matching, lock, aggregate driver"
```

---

### Task 4: Create FLA Charge Settings

**Files:**
- Create: `fla-charge/settings.py`

- [ ] **Step 1: Create settings.py**

```python
"""FLA charge service settings — Venus OS D-Bus integration.

Settings at /Settings/FlaCharge/* accessible from Cerbo GUI and web UI.
"""

import dbus
import logging
import os
import sys

sys.path.insert(0, "/data/apps/fla-shared")

log = logging.getLogger(__name__)

SETTINGS_DEFS = {
    "enabled": ("/Settings/FlaCharge/Enabled", 1, 0, 1),
    "trojan_soc_trigger": ("/Settings/FlaCharge/TrojanSocTrigger", 85, 50, 100),
    "lfp_soc_transition": ("/Settings/FlaCharge/LfpSocTransition", 95, 50, 100),
    "lfp_cell_voltage_disconnect": ("/Settings/FlaCharge/LfpCellVoltageDisconnect", 3.50, 3.30, 3.65),
    "current_taper_threshold": ("/Settings/FlaCharge/CurrentTaperThreshold", 20.0, 5.0, 60.0),
    "fla_bulk_voltage": ("/Settings/FlaCharge/FlaBulkVoltage", 29.64, 28.0, 32.0),
    "fla_absorption_complete_current": ("/Settings/FlaCharge/FlaAbsorptionCompleteCurrent", 10.0, 2.0, 50.0),
    "fla_absorption_max_hours": ("/Settings/FlaCharge/FlaAbsorptionMaxHours", 4.0, 0.5, 12.0),
    "fla_float_voltage": ("/Settings/FlaCharge/FlaFloatVoltage", 27.0, 24.0, 30.0),
    "voltage_delta_max": ("/Settings/FlaCharge/VoltageDeltaMax", 1.0, 0.1, 5.0),
    "voltage_match_timeout_hours": ("/Settings/FlaCharge/VoltageMatchTimeoutHours", 4.0, 0.5, 12.0),
    "phase1_timeout_hours": ("/Settings/FlaCharge/Phase1TimeoutHours", 8.0, 1.0, 24.0),
    "run_now": ("/Settings/FlaCharge/RunNow", 0, 0, 1),
}


def _get_bus():
    if "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        return dbus.SessionBus()
    return dbus.SystemBus()


class Settings:
    """Manages FLA charge settings in Venus OS localsettings."""

    def __init__(self):
        self.bus = _get_bus()
        self._settings_service = "com.victronenergy.settings"
        self._ensure_settings()

    def _ensure_settings(self):
        """Register all settings if they don't exist yet."""
        try:
            settings_obj = self.bus.get_object(self._settings_service, "/Settings")
            settings_iface = dbus.Interface(settings_obj, "com.victronenergy.Settings")
        except dbus.exceptions.DBusException:
            log.error("com.victronenergy.settings not available")
            return

        for key, (path, default, minimum, maximum) in SETTINGS_DEFS.items():
            try:
                obj = self.bus.get_object(self._settings_service, path)
                iface = dbus.Interface(obj, "com.victronenergy.BusItem")
                iface.GetValue()
                log.debug("Setting %s exists", path)
            except dbus.exceptions.DBusException:
                setting_path = path.replace("/Settings/", "", 1)
                if isinstance(default, int):
                    item_type = "i"
                elif isinstance(default, float):
                    item_type = "f"
                else:
                    item_type = "s"
                try:
                    settings_iface.AddSetting("", setting_path, default, item_type, minimum, maximum)
                    log.info("Created setting %s = %s", path, default)
                except dbus.exceptions.DBusException as e:
                    log.error("Failed to create setting %s: %s", path, e)

    def _read(self, key):
        path = SETTINGS_DEFS[key][0]
        try:
            obj = self.bus.get_object(self._settings_service, path)
            iface = dbus.Interface(obj, "com.victronenergy.BusItem")
            value = iface.GetValue()
            if isinstance(value, dbus.Double):
                return float(value)
            if isinstance(value, (dbus.Int32, dbus.Int16, dbus.UInt32)):
                return int(value)
            return value
        except dbus.exceptions.DBusException:
            return SETTINGS_DEFS[key][1]

    def _write(self, key, value):
        path = SETTINGS_DEFS[key][0]
        try:
            obj = self.bus.get_object(self._settings_service, path)
            iface = dbus.Interface(obj, "com.victronenergy.BusItem")
            iface.SetValue(value)
        except dbus.exceptions.DBusException as e:
            log.error("Failed to write setting %s: %s", path, e)

    @property
    def enabled(self): return bool(self._read("enabled"))
    @property
    def trojan_soc_trigger(self): return int(self._read("trojan_soc_trigger"))
    @property
    def lfp_soc_transition(self): return int(self._read("lfp_soc_transition"))
    @property
    def lfp_cell_voltage_disconnect(self): return float(self._read("lfp_cell_voltage_disconnect"))
    @property
    def current_taper_threshold(self): return float(self._read("current_taper_threshold"))
    @property
    def fla_bulk_voltage(self): return float(self._read("fla_bulk_voltage"))
    @property
    def fla_absorption_complete_current(self): return float(self._read("fla_absorption_complete_current"))
    @property
    def fla_absorption_max_hours(self): return float(self._read("fla_absorption_max_hours"))
    @property
    def fla_float_voltage(self): return float(self._read("fla_float_voltage"))
    @property
    def voltage_delta_max(self): return float(self._read("voltage_delta_max"))
    @property
    def voltage_match_timeout_hours(self): return float(self._read("voltage_match_timeout_hours"))
    @property
    def phase1_timeout_hours(self): return float(self._read("phase1_timeout_hours"))
    @property
    def run_now(self): return bool(self._read("run_now"))

    def clear_run_now(self):
        self._write("run_now", 0)
```

- [ ] **Step 2: Commit**

```bash
git add fla-charge/settings.py
git commit -m "feat: add FLA charge settings — 13 configurable parameters via Venus OS D-Bus"
```

---

### Task 5: Create FLA Charge Status Service

**Files:**
- Create: `fla-charge/dbus_status_service.py`

- [ ] **Step 1: Create dbus_status_service.py**

Same pattern as equalisation but with charge-specific states and device instance 201:

```python
"""D-Bus status service for FLA charge — visible on Cerbo GUI."""

import logging
import os
import platform
import sys

import dbus

log = logging.getLogger(__name__)

sys.path.insert(0, "/data/apps/fla-shared")
sys.path.insert(1, os.path.join("/data/apps/fla-shared", "ext", "velib_python"))
from vedbus import VeDbusService

STATE_IDLE = 0
STATE_PHASE1_SHARED = 1
STATE_STOPPING_DRIVER = 2
STATE_DISCONNECTING = 3
STATE_PHASE2_BULK = 4
STATE_PHASE3_ABSORPTION = 5
STATE_COOLING_DOWN = 6
STATE_VOLTAGE_MATCHING = 7
STATE_RECONNECTING = 8
STATE_RESTARTING_DRIVER = 9
STATE_ERROR = 10

STATE_NAMES = {
    STATE_IDLE: "Idle",
    STATE_PHASE1_SHARED: "Phase 1: Shared charging",
    STATE_STOPPING_DRIVER: "Stopping aggregate driver",
    STATE_DISCONNECTING: "Disconnecting LFP",
    STATE_PHASE2_BULK: "Phase 2: FLA bulk charge",
    STATE_PHASE3_ABSORPTION: "Phase 3: FLA absorption",
    STATE_COOLING_DOWN: "Cooling down",
    STATE_VOLTAGE_MATCHING: "Voltage matching",
    STATE_RECONNECTING: "Reconnecting LFP",
    STATE_RESTARTING_DRIVER: "Restarting aggregate driver",
    STATE_ERROR: "Error — manual intervention",
}


def get_bus():
    if "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        return dbus.SessionBus()
    return dbus.SystemBus()


class StatusService:
    def __init__(self):
        self._bus = get_bus()
        self._service = VeDbusService(
            "com.victronenergy.fla_charge", self._bus, register=False)
        self._registered = False

    def register(self):
        self._service.add_path("/Mgmt/ProcessName", __file__)
        self._service.add_path("/Mgmt/ProcessVersion", "Python " + platform.python_version())
        self._service.add_path("/Mgmt/Connection", "FLA Charge Status")
        self._service.add_path("/DeviceInstance", 201)
        self._service.add_path("/ProductId", 0xFE03)
        self._service.add_path("/ProductName", "FLA Charge")
        self._service.add_path("/FirmwareVersion", "1.0")
        self._service.add_path("/Connected", 1)

        self._service.add_path("/State", STATE_IDLE, writeable=True,
            gettextcallback=lambda a, x: STATE_NAMES.get(x, "Unknown"))
        self._service.add_path("/TimeRemaining", 0, writeable=True,
            gettextcallback=lambda a, x: "{:.0f}s".format(x) if x else "---")
        self._service.add_path("/TrojanVoltage", None, writeable=True,
            gettextcallback=lambda a, x: "{:.2f}V".format(x) if x is not None else "---")
        self._service.add_path("/TrojanCurrent", None, writeable=True,
            gettextcallback=lambda a, x: "{:.1f}A".format(x) if x is not None else "---")
        self._service.add_path("/TrojanSoc", None, writeable=True,
            gettextcallback=lambda a, x: "{:.0f}%".format(x) if x is not None else "---")
        self._service.add_path("/LfpVoltage", None, writeable=True,
            gettextcallback=lambda a, x: "{:.2f}V".format(x) if x is not None else "---")
        self._service.add_path("/LfpSoc", None, writeable=True,
            gettextcallback=lambda a, x: "{:.0f}%".format(x) if x is not None else "---")
        self._service.add_path("/VoltageDelta", None, writeable=True,
            gettextcallback=lambda a, x: "{:.2f}V".format(x) if x is not None else "---")
        self._service.add_path("/InrushCurrent", None, writeable=True,
            gettextcallback=lambda a, x: "{:.1f}A".format(x) if x is not None else "---")
        self._service.add_path("/ReconnectDelta", None, writeable=True,
            gettextcallback=lambda a, x: "{:.2f}V".format(x) if x is not None else "---")
        self._service.add_path("/Alarms/Charge", 0, writeable=True,
            gettextcallback=lambda a, x: {0: "OK", 1: "Warning", 2: "Alarm"}.get(x, "Unknown"))

        self._service.register()
        self._registered = True
        log.info("FLA charge status service registered on D-Bus")

    def update(self, state=None, time_remaining=None, trojan_v=None, trojan_i=None,
               trojan_soc=None, lfp_v=None, lfp_soc=None,
               inrush_current=None, reconnect_delta=None):
        if not self._registered:
            return
        with self._service as svc:
            if state is not None: svc["/State"] = state
            if time_remaining is not None: svc["/TimeRemaining"] = time_remaining
            if trojan_v is not None: svc["/TrojanVoltage"] = trojan_v
            if trojan_i is not None: svc["/TrojanCurrent"] = trojan_i
            if trojan_soc is not None: svc["/TrojanSoc"] = trojan_soc
            if lfp_v is not None: svc["/LfpVoltage"] = lfp_v
            if lfp_soc is not None: svc["/LfpSoc"] = lfp_soc
            if trojan_v is not None and lfp_v is not None:
                svc["/VoltageDelta"] = round(abs(trojan_v - lfp_v), 2)
            if inrush_current is not None: svc["/InrushCurrent"] = inrush_current
            if reconnect_delta is not None: svc["/ReconnectDelta"] = reconnect_delta

    def set_alarm(self, level=2):
        if self._registered: self._service["/Alarms/Charge"] = level

    def clear_alarm_path(self):
        if self._registered: self._service["/Alarms/Charge"] = 0

    def deregister(self):
        if not self._registered: return
        try: self._service["/Connected"] = 0
        except Exception as e: log.warning("Error deregistering: %s", e)
        self._service = None
        self._registered = False
        log.info("FLA charge status service deregistered")
```

- [ ] **Step 2: Commit**

```bash
git add fla-charge/dbus_status_service.py
git commit -m "feat: add FLA charge D-Bus status service — device instance 201"
```

---

### Task 6: Create FLA Charge Main Script

**Files:**
- Create: `fla-charge/fla_charge.py`

- [ ] **Step 1: Create fla_charge.py**

This is the core of the new service. It follows the same persistent-service pattern as the equalisation but with the Phase 1-4 charging sequence:

```python
#!/usr/bin/env python3
"""FLA Charge Service for Venus OS.

Detects undercharged Trojan FLA batteries and runs a full bulk+absorption
charge cycle by temporarily disconnecting LFPs.
Runs as a persistent daemontools service with GLib main loop.
"""

import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/data/apps/fla-shared")
sys.path.insert(1, os.path.join("/data/apps/fla-shared", "ext", "velib_python"))

from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

from dbus_monitor import DbusMonitor
from temp_battery import TempBatteryService
from relay_control import (
    open_relay, verify_relay_open, close_relay_verified,
    close_relay_delta_aware, startup_safety_check,
)
from voltage_matching import wait_for_match
from aggregate_driver import stop as stop_aggregate, start as start_aggregate
from lock import acquire as acquire_lock, release as release_lock
import alerting

from dbus_status_service import (
    StatusService, STATE_IDLE, STATE_PHASE1_SHARED, STATE_STOPPING_DRIVER,
    STATE_DISCONNECTING, STATE_PHASE2_BULK, STATE_PHASE3_ABSORPTION,
    STATE_COOLING_DOWN, STATE_VOLTAGE_MATCHING, STATE_RECONNECTING,
    STATE_RESTARTING_DRIVER, STATE_ERROR,
)
from settings import Settings
from web_server import start_web_server, update_cache, check_run_now, _cache

LOG_FILE = "/data/log/fla-charge.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

LAST_CHARGE_FILE = "/data/apps/fla-charge/last_charge"
CHECK_INTERVAL_SEC = 60


def read_last_charge():
    try: return datetime.fromisoformat(Path(LAST_CHARGE_FILE).read_text().strip())
    except (FileNotFoundError, ValueError): return None


def write_last_charge():
    Path(LAST_CHARGE_FILE).write_text(datetime.now().isoformat())


def is_ac_available(monitor):
    """Check if AC input is active (shore power or generator)."""
    try:
        import dbus
        bus = monitor.bus
        obj = bus.get_object("com.victronenergy.vebus.ttyS4", "/Ac/ActiveIn/ActiveInput")
        iface = dbus.Interface(obj, "com.victronenergy.BusItem")
        value = iface.GetValue()
        # 0 = AC-in-1, 1 = AC-in-2, 240 = disconnected
        return int(value) in (0, 1)
    except Exception:
        return False


def get_max_lfp_cell_voltage(monitor):
    """Read max LFP cell voltage from serialbattery instances."""
    try:
        import dbus
        bus = monitor.bus
        max_v = 0
        for name in bus.list_names():
            name = str(name)
            if "com.victronenergy.battery" not in name or "aggregate" in name or "fla" in name:
                continue
            try:
                obj = bus.get_object(name, "/ProductName")
                iface = dbus.Interface(obj, "com.victronenergy.BusItem")
                product = str(iface.GetValue())
                if "SerialBattery" not in product:
                    continue
            except Exception:
                continue
            # Read cell voltages
            for i in range(1, 9):
                try:
                    obj = bus.get_object(name, "/Voltages/Cell%d" % i)
                    iface = dbus.Interface(obj, "com.victronenergy.BusItem")
                    v = float(iface.GetValue())
                    if v > max_v:
                        max_v = v
                except Exception:
                    continue
        return max_v if max_v > 0 else None
    except Exception:
        return None


def should_run(settings, monitor):
    """Check if FLA charge conditions are met."""
    if not settings.enabled:
        return False

    if settings.run_now:
        log.info("RunNow flag set — bypassing schedule checks (AC still enforced)")
        settings.clear_run_now()
        # Still check AC and lock below

    elif monitor.get_trojan_soc() is not None:
        trojan_soc = monitor.get_trojan_soc()
        if trojan_soc >= settings.trojan_soc_trigger:
            return False
    else:
        log.warning("Cannot read Trojan SoC")
        return False

    if not is_ac_available(monitor):
        log.debug("No AC input available")
        return False

    trojan_soc = monitor.get_trojan_soc()
    log.info("FLA charge conditions met: Trojan SoC=%.1f%%, AC available", trojan_soc or 0)
    return True


def run_charge(settings, monitor, status):
    """Execute the full FLA charge sequence. Returns True on success."""
    temp_service = None
    aggregate_stopped = False

    if not acquire_lock("fla-charge"):
        log.info("Operation lock held — skipping")
        return False

    try:
        # === PHASE 1: Shared charging ===
        status.update(state=STATE_PHASE1_SHARED)
        log.info("Phase 1: Shared charging — both banks on bus")
        phase1_start = time.time()
        phase1_timeout = settings.phase1_timeout_hours * 3600

        while True:
            elapsed = time.time() - phase1_start
            remaining = max(0, phase1_timeout - elapsed)

            lfp_soc = monitor.get_lfp_soc()
            trojan_soc = monitor.get_trojan_soc()
            v_trojan = monitor.get_trojan_voltage()
            v_lfp = monitor.get_lfp_voltage()
            i_trojan = monitor.get_trojan_current()
            charge_current = monitor.get_lfp_current()  # Total through LFP shunt
            max_cell_v = get_max_lfp_cell_voltage(monitor)

            status.update(
                time_remaining=remaining,
                trojan_v=v_trojan, trojan_i=i_trojan, trojan_soc=trojan_soc,
                lfp_v=v_lfp, lfp_soc=lfp_soc,
            )
            update_cache(
                state=STATE_PHASE1_SHARED, time_remaining=remaining,
                trojan_v=v_trojan, trojan_soc=trojan_soc,
                lfp_v=v_lfp, lfp_soc=lfp_soc,
            )

            # Check AC still available
            if not is_ac_available(monitor):
                log.warning("AC input lost during Phase 1 — aborting")
                return False

            # Transition triggers (any)
            transition_reason = None
            if lfp_soc is not None and lfp_soc >= settings.lfp_soc_transition:
                transition_reason = "LFP SoC %.1f%% >= %d%%" % (lfp_soc, settings.lfp_soc_transition)
            elif charge_current is not None and abs(charge_current) < settings.current_taper_threshold:
                transition_reason = "Charge current %.1fA < %.1fA" % (abs(charge_current), settings.current_taper_threshold)
            elif max_cell_v is not None and max_cell_v >= settings.lfp_cell_voltage_disconnect:
                transition_reason = "LFP cell voltage %.3fV >= %.3fV" % (max_cell_v, settings.lfp_cell_voltage_disconnect)

            if transition_reason:
                log.info("Phase 1 → 2 transition: %s", transition_reason)
                break

            if elapsed > phase1_timeout:
                log.warning("Phase 1 timeout after %.0f hours — aborting", elapsed / 3600)
                return False

            if int(elapsed) % 300 < 30:
                log.info("Phase 1: %.0f min, LFP SoC=%.0f%%, Trojan SoC=%.0f%%, I=%.0fA, max cell=%.3fV",
                         elapsed / 60, lfp_soc or 0, trojan_soc or 0, abs(charge_current or 0), max_cell_v or 0)

            time.sleep(30)

        # === PHASE 2: FLA-only bulk ===
        # Crash-safe: register at current bus voltage first
        current_voltage = v_lfp or v_trojan or 28.0
        temp_service = TempBatteryService(device_instance=100)
        temp_service.register(
            charge_voltage=current_voltage,
            charge_current=120.0,
            discharge_current=0,
        )

        status.update(state=STATE_STOPPING_DRIVER)
        if not stop_aggregate():
            status.update(state=STATE_ERROR)
            alerting.raise_alarm("Failed to stop aggregate driver", status_service=status)
            return False
        aggregate_stopped = True

        status.update(state=STATE_DISCONNECTING)
        if not open_relay(monitor):
            status.update(state=STATE_ERROR)
            alerting.raise_alarm("Failed to open relay 2", status_service=status)
            return False

        if not verify_relay_open(monitor):
            status.update(state=STATE_ERROR)
            alerting.raise_alarm("LFP not disconnected after relay open", status_service=status)
            return False

        lfp_voltage_at_disconnect = monitor.get_lfp_voltage()
        if lfp_voltage_at_disconnect:
            log.info("LFP voltage at disconnect: %.2fV", lfp_voltage_at_disconnect)

        # NOW safe to raise CVL
        temp_service.set_charge_voltage(settings.fla_bulk_voltage)
        log.info("CVL raised to FLA bulk voltage: %.2fV", settings.fla_bulk_voltage)

        # === PHASE 3: FLA absorption ===
        status.update(state=STATE_PHASE2_BULK)
        log.info("Phase 2-3: FLA bulk/absorption at %.2fV", settings.fla_bulk_voltage)
        abs_start = time.time()
        abs_timeout = settings.fla_absorption_max_hours * 3600
        i_trojan_none_count = 0

        while True:
            elapsed = time.time() - abs_start
            remaining = max(0, abs_timeout - elapsed)

            v_trojan = monitor.get_trojan_voltage()
            i_trojan = monitor.get_trojan_current()
            v_lfp = monitor.get_lfp_voltage()

            if v_trojan is not None and i_trojan is not None:
                temp_service.update_voltage_current(v_trojan, i_trojan)

            status.update(
                state=STATE_PHASE3_ABSORPTION if elapsed > 300 else STATE_PHASE2_BULK,
                time_remaining=remaining,
                trojan_v=v_trojan, trojan_i=i_trojan,
                lfp_v=v_lfp,
            )

            if v_trojan is None:
                status.update(state=STATE_ERROR)
                alerting.raise_alarm("SmartShunt Trojan unresponsive during absorption", status_service=status)
                return False

            # Orion failure detection
            if (lfp_voltage_at_disconnect is not None and v_lfp is not None
                    and v_lfp < lfp_voltage_at_disconnect - 0.5):
                alerting.raise_alarm("LFP voltage dropping — Orion may have failed", status_service=status)
                return False

            # AC loss detection
            if not is_ac_available(monitor) and i_trojan is not None and abs(i_trojan) < 2:
                log.warning("AC input lost during absorption — proceeding to voltage matching")
                break

            # Current lost
            if i_trojan is None:
                i_trojan_none_count += 1
                if i_trojan_none_count >= 10:
                    log.warning("Trojan current unreadable for 5 min — proceeding to voltage matching")
                    break
            else:
                i_trojan_none_count = 0

            # High current warning
            if i_trojan is not None and abs(i_trojan) > 60:
                log.warning("High Trojan charge current: %.1fA (dynamo/MPPT?)", abs(i_trojan))

            # Absorption complete
            if i_trojan is not None and abs(i_trojan) < settings.fla_absorption_complete_current:
                log.info("Absorption complete: current %.1fA < %.1fA threshold (%.0f min)",
                         abs(i_trojan), settings.fla_absorption_complete_current, elapsed / 60)
                break

            if elapsed > abs_timeout:
                log.warning("Absorption timeout after %.0f min", elapsed / 60)
                break

            if int(elapsed) % 300 < 30:
                log.info("Absorption: %.0f min, V=%.1fV, I=%.1fA",
                         elapsed / 60, v_trojan or 0, i_trojan or 0)

            time.sleep(30)

        # === PHASE 4: Voltage matching + reconnect ===
        status.update(state=STATE_VOLTAGE_MATCHING)
        matched, delta = wait_for_match(
            monitor, temp_service, status, alerting,
            voltage_delta_max=settings.voltage_delta_max,
            timeout_hours=settings.voltage_match_timeout_hours,
            float_voltage=settings.fla_float_voltage,
        )
        if not matched:
            return False

        status.update(state=STATE_RECONNECTING)
        if not close_relay_verified(monitor):
            alerting.raise_alarm("Failed to close relay 2", status_service=status)
            return False

        # Measure inrush
        time.sleep(1)
        inrush = monitor.get_lfp_current()
        status.update(
            inrush_current=abs(inrush) if inrush else None,
            reconnect_delta=delta,
        )
        log.info("Reconnect: inrush=%.1fA, delta=%.2fV", abs(inrush) if inrush else 0, delta or 0)
        time.sleep(2)

        # Cleanup
        temp_service.deregister()
        temp_service = None

        status.update(state=STATE_RESTARTING_DRIVER)
        if not start_aggregate():
            alerting.raise_alarm("Failed to restart aggregate driver", status_service=status)
            return False
        monitor.invalidate_services()

        write_last_charge()
        status.update(state=STATE_IDLE, time_remaining=0)
        alerting.clear_alarm(status_service=status)
        log.info("FLA charge completed successfully")
        return True

    except Exception as e:
        log.exception("Unexpected error: %s", e)
        alerting.raise_alarm("FLA charge error: %s" % e, status_service=status)
        return False

    finally:
        if temp_service is not None:
            try: temp_service.deregister()
            except: pass
        close_relay_delta_aware(monitor, alerting, status)
        if aggregate_stopped:
            try: start_aggregate()
            except: log.error("CRITICAL: Failed to restart aggregate driver")
        release_lock()


class FlaChargeService:
    """Persistent service that checks conditions and runs FLA charge."""

    def __init__(self):
        self.settings = Settings()
        self.monitor = DbusMonitor(lfp_instance=277, trojan_instance=279)
        self.status = StatusService()
        self.status.register()
        self._running = False
        startup_safety_check(self.monitor, self.status, alerting)
        self._update_idle_status()
        log.info("FLA charge service started — checking every %ds", CHECK_INTERVAL_SEC)

    def _update_idle_status(self):
        v_trojan = self.monitor.get_trojan_voltage()
        v_lfp = self.monitor.get_lfp_voltage()
        trojan_soc = self.monitor.get_trojan_soc()
        lfp_soc = self.monitor.get_lfp_soc()
        delta = round(abs(v_trojan - v_lfp), 2) if (v_trojan and v_lfp) else None
        self.status.update(
            state=STATE_IDLE, trojan_v=v_trojan, lfp_v=v_lfp,
            trojan_soc=trojan_soc, lfp_soc=lfp_soc,
        )
        last = read_last_charge()
        update_cache(
            state=STATE_IDLE, time_remaining=0,
            trojan_v=v_trojan, trojan_soc=trojan_soc,
            lfp_v=v_lfp, lfp_soc=lfp_soc,
            voltage_delta=delta,
            last_charge=last.strftime("%Y-%m-%d %H:%M") if last else None,
            settings={
                "enabled": self.settings.enabled,
                "trojan_soc_trigger": self.settings.trojan_soc_trigger,
                "lfp_soc_transition": self.settings.lfp_soc_transition,
                "lfp_cell_voltage_disconnect": self.settings.lfp_cell_voltage_disconnect,
                "current_taper_threshold": self.settings.current_taper_threshold,
                "fla_bulk_voltage": self.settings.fla_bulk_voltage,
                "fla_absorption_complete_current": self.settings.fla_absorption_complete_current,
                "fla_absorption_max_hours": self.settings.fla_absorption_max_hours,
                "fla_float_voltage": self.settings.fla_float_voltage,
                "voltage_delta_max": self.settings.voltage_delta_max,
                "voltage_match_timeout_hours": self.settings.voltage_match_timeout_hours,
                "phase1_timeout_hours": self.settings.phase1_timeout_hours,
            },
        )

    def _apply_pending_settings(self):
        from settings import SETTINGS_DEFS
        pending = _cache.pop("pending_settings", None)
        if not pending: return
        for key, value in pending.items():
            if key in SETTINGS_DEFS:
                _, _, minimum, maximum = SETTINGS_DEFS[key]
                if value < minimum or value > maximum:
                    log.warning("Setting %s value %s out of bounds — rejected", key, value)
                    continue
            self.settings._write(key, value)
            log.info("Setting %s updated to %s via web UI", key, value)

    def _check(self):
        if self._running: return True
        try:
            self._apply_pending_settings()
            if check_run_now():
                self.settings._write("run_now", 1)
            self._update_idle_status()
            if should_run(self.settings, self.monitor):
                self._running = True
                _cache["run_now_requested"] = False
                try:
                    success = run_charge(self.settings, self.monitor, self.status)
                    if success:
                        log.info("FLA charge completed successfully")
                    else:
                        log.error("FLA charge failed — check alarms")
                finally:
                    self._running = False
                    self._update_idle_status()
        except Exception as e:
            log.exception("Error in periodic check: %s", e)
        return True


def main():
    DBusGMainLoop(set_as_default=True)
    log.info("FLA charge service starting")
    try:
        service = FlaChargeService()
        start_web_server()
        GLib.timeout_add_seconds(CHECK_INTERVAL_SEC, service._check)
        log.info("Entering GLib main loop")
        GLib.MainLoop().run()
    except Exception as e:
        log.exception("Fatal error: %s", e)
        alerting.raise_alarm("FLA charge fatal error: %s" % e)


if __name__ == "__main__":
    main()
```

Note: `monitor.get_trojan_soc()` needs to be added to `dbus_monitor.py` — it reads from SmartShunt Trojan (279) `/Soc`.

- [ ] **Step 2: Add get_trojan_soc() to dbus_monitor.py**

Add to `fla-shared/dbus_monitor.py` in the `DbusMonitor` class:

```python
def get_trojan_soc(self):
    """Read Trojan SoC from SmartShunt Trojan (279)."""
    self._ensure_services()
    if self._trojan_service is None:
        return None
    soc = _get_dbus_value(self.bus, self._trojan_service, "/Soc")
    return float(soc) if soc is not None else None
```

- [ ] **Step 3: Commit**

```bash
git add fla-charge/fla_charge.py fla-shared/dbus_monitor.py
git commit -m "feat: add FLA charge main script with Phase 1-4 orchestration"
```

---

### Task 7: Create FLA Charge Web Server and Install Script

**Files:**
- Create: `fla-charge/web_server.py`
- Create: `fla-charge/service/run`
- Create: `fla-charge/install.sh`

- [ ] **Step 1: Create web_server.py**

Same pattern as equalisation web server but on port 8089 with charge-specific fields (Trojan SoC, Phase 1 status, cell voltages). Include link to equalisation UI.

The HTML should show:
- Status: phase, time remaining, last charge
- Voltages: Trojan V/I/SoC, LFP V/SoC, delta
- Settings: all 12 charge settings editable inline
- Control: "Run Charge Now" button
- Link to "FLA Equalisation" at port 8088

Use the same shared cache pattern as the equalisation web server (no cross-thread D-Bus calls). Port 8089. Cache keys include `trojan_soc`, `lfp_soc`, `last_charge`, and all charge settings.

- [ ] **Step 2: Create service/run**

```bash
mkdir -p fla-charge/service
cat > fla-charge/service/run << 'EOF'
#!/bin/sh
exec 2>&1
exec python3 /data/apps/fla-charge/fla_charge.py
EOF
chmod +x fla-charge/service/run
```

- [ ] **Step 3: Create install.sh**

```bash
#!/bin/bash
# Install FLA charge service on Venus OS
set -e

INSTALL_DIR="/data/apps/fla-charge"
SHARED_DIR="/data/apps/fla-shared"
SERVICE_DIR="${INSTALL_DIR}/service"
LOG_DIR="/data/log"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing FLA charge service..."

svc -d /service/fla-charge 2>/dev/null || true
sleep 2

# Install shared modules (if not already present)
mkdir -p "${SHARED_DIR}"
for f in __init__.py relay_control.py voltage_matching.py aggregate_driver.py lock.py dbus_monitor.py alerting.py temp_battery.py; do
    cp "${SCRIPT_DIR}/../fla-shared/${f}" "${SHARED_DIR}/" 2>/dev/null || true
done
mkdir -p "${SHARED_DIR}/ext"
ln -sfn /data/apps/dbus-aggregate-batteries/ext/velib_python "${SHARED_DIR}/ext/velib_python"

# Install charge service
mkdir -p "${INSTALL_DIR}" "${SERVICE_DIR}" "${LOG_DIR}"
cp "${SCRIPT_DIR}/fla_charge.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/dbus_status_service.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/settings.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/web_server.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/service/run" "${SERVICE_DIR}/run"
chmod +x "${INSTALL_DIR}/fla_charge.py" "${SERVICE_DIR}/run"

rm -f /etc/cron.d/fla-charge
ln -sfn "${SERVICE_DIR}" /service/fla-charge

if ! grep -q "fla-charge" /data/rc.local 2>/dev/null; then
    echo "" >> /data/rc.local
    echo "# FLA Charge service" >> /data/rc.local
    echo "ln -sfn /data/apps/fla-charge/service /service/fla-charge" >> /data/rc.local
fi

sleep 3
echo "Installation complete."
echo "Service status: $(svstat /service/fla-charge 2>/dev/null || echo 'starting...')"
echo "Web UI: http://venus.local:8089"
```

- [ ] **Step 4: Commit**

```bash
git add fla-charge/
git commit -m "feat: add FLA charge web UI, service runner, and installer"
```

---

### Task 8: Update FLA Equalisation to Add Cross-Links

**Files:**
- Modify: `fla-equalisation/web_server.py` (add link to charge UI)

- [ ] **Step 1: Add link to charge UI in equalisation web server HTML**

In the HTML_PAGE string, after the Control card, add:

```html
<div class="card">
  <h2>Related</h2>
  <div class="row"><a href="http://venus.local:8089" style="color:#8bb4d9">FLA Charge Service →</a></div>
</div>
```

- [ ] **Step 2: Commit**

```bash
git add fla-equalisation/web_server.py
git commit -m "feat: add cross-link to FLA charge UI from equalisation dashboard"
```

---

### Task 9: Update Tests

**Files:**
- Modify: `fla-equalisation/tests/test_fla_equalisation.py`
- Create: `fla-charge/tests/test_fla_charge.py`
- Create: `fla-shared/tests/test_lock.py`

- [ ] **Step 1: Create test_lock.py**

```python
import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import lock


class TestLock(unittest.TestCase):
    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix='.lock', delete=False)
        self.tmpfile.close()
        lock.LOCK_FILE = self.tmpfile.name
        # Ensure clean state
        try: os.unlink(self.tmpfile.name)
        except: pass

    def tearDown(self):
        try: os.unlink(self.tmpfile.name)
        except: pass

    def test_acquire_and_release(self):
        self.assertTrue(lock.acquire("test-service"))
        self.assertTrue(lock.is_locked())
        self.assertEqual(lock.holder()["service"], "test-service")
        lock.release()
        self.assertFalse(lock.is_locked())

    def test_double_acquire_fails(self):
        self.assertTrue(lock.acquire("service-1"))
        self.assertFalse(lock.acquire("service-2"))
        lock.release()

    def test_stale_lock_cleared(self):
        # Write lock with non-existent PID
        from pathlib import Path
        Path(self.tmpfile.name).write_text(json.dumps({
            "service": "dead", "pid": 99999999, "started": "2020-01-01"
        }))
        self.assertFalse(lock.is_locked())  # Should detect stale and clear

    def test_not_locked_initially(self):
        self.assertFalse(lock.is_locked())

    def test_holder_empty_when_unlocked(self):
        self.assertEqual(lock.holder(), {})
```

- [ ] **Step 2: Update existing equalisation tests for shared module imports**

Update `fla-equalisation/tests/test_fla_equalisation.py` to handle the refactored import paths. The main changes are mocking the shared module imports.

- [ ] **Step 3: Create basic charge test**

```python
# fla-charge/tests/test_fla_charge.py
# Same mock pattern as equalisation tests
# Test should_run(), is_ac_available(), phase 1 transition logic
```

- [ ] **Step 4: Run all tests**

```bash
python3 -m unittest fla-shared/tests/test_lock.py -v
python3 -m unittest fla-equalisation/tests/test_fla_equalisation.py -v
python3 -m unittest fla-charge/tests/test_fla_charge.py -v
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test: add lock tests, update equalisation tests for shared modules, add charge tests"
```

---

### Task 10: Deploy and Verify

**Files:** No new files — deployment of existing

- [ ] **Step 1: Deploy shared modules**

```bash
scp -r fla-shared/ root@venus.local:/tmp/fla-shared/
ssh root@venus.local 'mkdir -p /data/apps/fla-shared && cp /tmp/fla-shared/*.py /data/apps/fla-shared/ && mkdir -p /data/apps/fla-shared/ext && ln -sfn /data/apps/dbus-aggregate-batteries/ext/velib_python /data/apps/fla-shared/ext/velib_python'
```

- [ ] **Step 2: Redeploy equalisation with shared imports**

```bash
scp fla-equalisation/*.py root@venus.local:/data/apps/fla-equalisation/
ssh root@venus.local 'kill $(svstat /service/fla-equalisation | grep -o "pid [0-9]*" | grep -o "[0-9]*"); sleep 4; svstat /service/fla-equalisation'
```

Verify: http://venus.local:8088 still works

- [ ] **Step 3: Deploy charge service**

```bash
scp -r fla-charge/ root@venus.local:/tmp/fla-charge/
ssh root@venus.local 'bash /tmp/fla-charge/install.sh'
```

Verify: http://venus.local:8089 shows charge dashboard

- [ ] **Step 4: Verify both services coexist**

```bash
ssh root@venus.local 'svstat /service/fla-equalisation /service/fla-charge; dbus -y | grep fla'
```

Expected: Both services up, both D-Bus services registered

- [ ] **Step 5: Commit deployment fixes if any**

```bash
git add -A
git commit -m "fix: deployment adjustments"
git push origin main
```
