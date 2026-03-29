# FLA Equalisation Script Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone Python script for Cerbo GX that automates periodic Trojan L16H-AC FLA equalisation with safe LFP disconnect/reconnect, temporary D-Bus battery service for DVCC control, voltage matching, and GUI-visible settings/status.

**Architecture:** The script runs hourly via cron, checks scheduling conditions, then orchestrates a multi-step equalisation sequence: stop aggregate driver → register temporary battery service → open relay → equalise → voltage match → close relay → restart aggregate driver. All state is visible on Cerbo GUI via D-Bus, all settings configurable via Venus OS settings.

**Tech Stack:** Python 3.8+ (Venus OS), dbus-python, GLib mainloop, Victron velib_python (VeDbusService, SettingsDevice, VeDbusItemImport)

---

## File Structure

```
fla-equalisation/
├── fla_equalisation.py          # Entry point: scheduling checks, orchestrates the full sequence
├── dbus_battery_service.py      # Temporary D-Bus battery service for DVCC CVL control
├── dbus_monitor.py              # Reads SmartShunt + relay + SoC values from D-Bus
├── dbus_status_service.py       # D-Bus status service for Cerbo GUI visibility
├── settings.py                  # Registers and reads Venus OS settings (GUI-accessible)
├── alerting.py                  # Buzzer, D-Bus alarm, logging
├── ext/velib_python/            # Symlink or copy of Victron D-Bus library
└── install.sh                   # Installation script for Cerbo GX
```

**Responsibilities:**
- `fla_equalisation.py` — main loop, scheduling logic, state machine, orchestration
- `dbus_battery_service.py` — register/deregister temporary `com.victronenergy.battery` service with configurable CVL
- `dbus_monitor.py` — read SmartShunt LFP (277) voltage/current, SmartShunt Trojan (279) voltage/current, LFP SoC from aggregate driver or serialbattery, relay state
- `settings.py` — register all equalisation parameters in `com.victronenergy.settings`, expose `RunNow` and `Enabled` flags
- `dbus_status_service.py` — register `com.victronenergy.fla_equalisation` with State/TimeRemaining/TrojanVoltage/LfpVoltage/VoltageDelta for Cerbo GUI
- `alerting.py` — activate Cerbo buzzer, set D-Bus alarm paths, write to log file
- `install.sh` — copy files, set permissions, install cron, symlink velib_python

---

### Task 1: Project Scaffolding and velib_python Setup

**Files:**
- Create: `fla-equalisation/install.sh`
- Create: `fla-equalisation/ext/` (symlink)

- [ ] **Step 1: Create install.sh**

```bash
#!/bin/bash
# Install FLA equalisation script on Venus OS
set -e

INSTALL_DIR="/data/apps/fla-equalisation"
LOG_DIR="/data/log"
CRON_FILE="/etc/cron.d/fla-equalisation"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing FLA equalisation script to ${INSTALL_DIR}..."

# Create directories
mkdir -p "${INSTALL_DIR}"
mkdir -p "${LOG_DIR}"

# Copy files
cp "${SCRIPT_DIR}/fla_equalisation.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/dbus_battery_service.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/dbus_monitor.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/settings.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/dbus_status_service.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/alerting.py" "${INSTALL_DIR}/"

# Make executable
chmod +x "${INSTALL_DIR}/fla_equalisation.py"

# Symlink velib_python from aggregate batteries if available, else copy
if [ -d "/data/apps/dbus-aggregate-batteries/ext/velib_python" ]; then
    ln -sfn /data/apps/dbus-aggregate-batteries/ext/velib_python "${INSTALL_DIR}/ext"
    echo "Linked velib_python from dbus-aggregate-batteries"
else
    echo "ERROR: velib_python not found at /data/apps/dbus-aggregate-batteries/ext/velib_python"
    echo "Please install dbus-aggregate-batteries first."
    exit 1
fi

# Install cron job (runs every hour)
cat > "${CRON_FILE}" << 'EOF'
0 * * * * root /usr/bin/python3 /data/apps/fla-equalisation/fla_equalisation.py >> /data/log/fla-equalisation.log 2>&1
EOF

echo "Installation complete."
echo "Configure settings via Cerbo GX GUI or VRM remote console."
```

- [ ] **Step 2: Commit**

```bash
git add fla-equalisation/install.sh
git commit -m "feat: add install script for FLA equalisation on Venus OS"
```

---

### Task 2: D-Bus Monitor — Read SmartShunt and System Values

**Files:**
- Create: `fla-equalisation/dbus_monitor.py`

- [ ] **Step 1: Write dbus_monitor.py**

```python
"""Read SmartShunt and system values from Venus OS D-Bus."""

import dbus
import logging
import os

log = logging.getLogger(__name__)

# D-Bus paths
SMARTSHUNT_VOLTAGE = "/Dc/0/Voltage"
SMARTSHUNT_CURRENT = "/Dc/0/Current"
BATTERY_SOC = "/Soc"
RELAY_STATE_PATH = "/Relay/1/State"  # Relay 2 (0-indexed)
SYSTEM_SERVICE = "com.victronenergy.system"


def get_bus():
    """Return the shared system bus connection."""
    if "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        return dbus.SessionBus()
    return dbus.SystemBus()


def _get_dbus_value(bus, service, path):
    """Read a single value from D-Bus. Returns None on failure."""
    try:
        obj = bus.get_object(service, path)
        iface = dbus.Interface(obj, "com.victronenergy.BusItem")
        value = iface.GetValue()
        # Unwrap dbus types
        if isinstance(value, dbus.Double):
            return float(value)
        if isinstance(value, (dbus.Int32, dbus.Int16, dbus.UInt32, dbus.Byte)):
            return int(value)
        if isinstance(value, dbus.String):
            return str(value)
        return value
    except dbus.exceptions.DBusException as e:
        log.warning("Failed to read %s %s: %s", service, path, e)
        return None


def _find_service(bus, prefix, instance):
    """Find a D-Bus service by prefix and device instance."""
    for name in bus.list_names():
        if not str(name).startswith(prefix):
            continue
        dev_instance = _get_dbus_value(bus, name, "/DeviceInstance")
        if dev_instance == instance:
            return str(name)
    return None


class DbusMonitor:
    """Reads SmartShunt and system values from Venus OS D-Bus."""

    def __init__(self, lfp_instance=277, trojan_instance=279):
        self.bus = get_bus()
        self.lfp_instance = lfp_instance
        self.trojan_instance = trojan_instance
        self._lfp_service = None
        self._trojan_service = None

    def _ensure_services(self):
        """Discover SmartShunt services if not yet found."""
        if self._lfp_service is None:
            self._lfp_service = _find_service(
                self.bus, "com.victronenergy.battery", self.lfp_instance
            )
            if self._lfp_service is None:
                # Try dcload (SmartShunt in DC meter mode)
                self._lfp_service = _find_service(
                    self.bus, "com.victronenergy.dcload", self.lfp_instance
                )
            if self._lfp_service:
                log.info("Found LFP SmartShunt: %s", self._lfp_service)

        if self._trojan_service is None:
            self._trojan_service = _find_service(
                self.bus, "com.victronenergy.battery", self.trojan_instance
            )
            if self._trojan_service is None:
                self._trojan_service = _find_service(
                    self.bus, "com.victronenergy.dcload", self.trojan_instance
                )
            if self._trojan_service:
                log.info("Found Trojan SmartShunt: %s", self._trojan_service)

    def get_lfp_voltage(self):
        """Read LFP bank voltage from SmartShunt LFP (277)."""
        self._ensure_services()
        if self._lfp_service is None:
            return None
        return _get_dbus_value(self.bus, self._lfp_service, SMARTSHUNT_VOLTAGE)

    def get_lfp_current(self):
        """Read LFP bank current from SmartShunt LFP (277)."""
        self._ensure_services()
        if self._lfp_service is None:
            return None
        return _get_dbus_value(self.bus, self._lfp_service, SMARTSHUNT_CURRENT)

    def get_trojan_voltage(self):
        """Read Trojan bank voltage from SmartShunt Trojan (279)."""
        self._ensure_services()
        if self._trojan_service is None:
            return None
        return _get_dbus_value(self.bus, self._trojan_service, SMARTSHUNT_VOLTAGE)

    def get_trojan_current(self):
        """Read Trojan bank current from SmartShunt Trojan (279)."""
        self._ensure_services()
        if self._trojan_service is None:
            return None
        return _get_dbus_value(self.bus, self._trojan_service, SMARTSHUNT_CURRENT)

    def get_lfp_soc(self):
        """Read LFP SoC from aggregate battery driver or serialbattery."""
        # Try aggregate driver first
        soc = _get_dbus_value(
            self.bus, "com.victronenergy.battery.aggregate", BATTERY_SOC
        )
        if soc is not None:
            return float(soc)
        # Fallback: try finding any serialbattery service
        for name in self.bus.list_names():
            if "com.victronenergy.battery" in str(name) and "aggregate" not in str(name):
                product = _get_dbus_value(self.bus, name, "/ProductName")
                if product and "SerialBattery" in str(product):
                    soc = _get_dbus_value(self.bus, name, BATTERY_SOC)
                    if soc is not None:
                        return float(soc)
        return None

    def set_relay(self, state):
        """Set Cerbo relay 2: 1=closed (normal), 0=open (disconnect LFP).
        Returns True on success."""
        try:
            obj = self.bus.get_object(SYSTEM_SERVICE, RELAY_STATE_PATH)
            iface = dbus.Interface(obj, "com.victronenergy.BusItem")
            iface.SetValue(dbus.Int32(state))
            log.info("Relay 2 set to %d", state)
            return True
        except dbus.exceptions.DBusException as e:
            log.error("Failed to set relay: %s", e)
            return False

    def get_relay_state(self):
        """Read Cerbo relay 2 state. Returns 0 (open) or 1 (closed), or None."""
        return _get_dbus_value(self.bus, SYSTEM_SERVICE, RELAY_STATE_PATH)

    def invalidate_services(self):
        """Force re-discovery of SmartShunt services."""
        self._lfp_service = None
        self._trojan_service = None
```

- [ ] **Step 2: Commit**

```bash
git add fla-equalisation/dbus_monitor.py
git commit -m "feat: add D-Bus monitor for SmartShunt and relay readings"
```

---

### Task 3: Venus OS Settings Integration

**Files:**
- Create: `fla-equalisation/settings.py`

- [ ] **Step 1: Write settings.py**

```python
"""Register and read FLA equalisation settings via Venus OS D-Bus.

Settings are accessible from Cerbo GX GUI, VRM remote console, and GUI v2.
Uses com.victronenergy.settings service (localsettings).
"""

import dbus
import logging
import os

log = logging.getLogger(__name__)

# Setting definitions: (path, default, min, max)
SETTINGS_DEFS = {
    "eq_voltage": ("/Settings/FlaEqualisation/EqualisationVoltage", 31.2, 28.0, 33.0),
    "eq_current_complete": ("/Settings/FlaEqualisation/EqualisationCurrentComplete", 8.0, 1.0, 50.0),
    "eq_timeout_hours": ("/Settings/FlaEqualisation/EqualisationTimeoutHours", 2.5, 0.5, 8.0),
    "float_voltage": ("/Settings/FlaEqualisation/FloatVoltage", 27.6, 24.0, 30.0),
    "voltage_delta_max": ("/Settings/FlaEqualisation/VoltageDeltaMax", 1.0, 0.1, 5.0),
    "voltage_match_timeout_hours": ("/Settings/FlaEqualisation/VoltageMatchTimeoutHours", 4.0, 0.5, 12.0),
    "days_between": ("/Settings/FlaEqualisation/DaysBetweenEqualisation", 90, 7, 365),
    "start_hour": ("/Settings/FlaEqualisation/AfternoonStartHour", 14, 0, 23),
    "end_hour": ("/Settings/FlaEqualisation/AfternoonEndHour", 17, 0, 23),
    "lfp_soc_min": ("/Settings/FlaEqualisation/LfpSocMin", 95, 50, 100),
    "enabled": ("/Settings/FlaEqualisation/Enabled", 1, 0, 1),
    "run_now": ("/Settings/FlaEqualisation/RunNow", 0, 0, 1),
}


def _get_bus():
    if "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        return dbus.SessionBus()
    return dbus.SystemBus()


class Settings:
    """Manages FLA equalisation settings in Venus OS localsettings."""

    def __init__(self):
        self.bus = _get_bus()
        self._settings_service = "com.victronenergy.settings"
        self._ensure_settings()

    def _ensure_settings(self):
        """Register all settings if they don't exist yet."""
        try:
            settings_obj = self.bus.get_object(
                self._settings_service, "/Settings"
            )
            settings_iface = dbus.Interface(
                settings_obj, "com.victronenergy.BusItem"
            )
        except dbus.exceptions.DBusException:
            log.error("com.victronenergy.settings not available")
            return

        for key, (path, default, minimum, maximum) in SETTINGS_DEFS.items():
            try:
                # Check if setting already exists
                obj = self.bus.get_object(self._settings_service, path)
                iface = dbus.Interface(obj, "com.victronenergy.BusItem")
                iface.GetValue()
                log.debug("Setting %s exists", path)
            except dbus.exceptions.DBusException:
                # Setting doesn't exist, create it
                setting_path = path.replace("/Settings/", "", 1)
                if isinstance(default, int):
                    item_type = "i"
                elif isinstance(default, float):
                    item_type = "f"
                else:
                    item_type = "s"
                try:
                    settings_iface.AddSetting(
                        "", setting_path, default, item_type, minimum, maximum
                    )
                    log.info("Created setting %s = %s", path, default)
                except dbus.exceptions.DBusException as e:
                    log.error("Failed to create setting %s: %s", path, e)

    def _read(self, key):
        """Read a setting value from D-Bus."""
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
            return SETTINGS_DEFS[key][1]  # Return default

    def _write(self, key, value):
        """Write a setting value to D-Bus."""
        path = SETTINGS_DEFS[key][0]
        try:
            obj = self.bus.get_object(self._settings_service, path)
            iface = dbus.Interface(obj, "com.victronenergy.BusItem")
            iface.SetValue(value)
        except dbus.exceptions.DBusException as e:
            log.error("Failed to write setting %s: %s", path, e)

    # Properties for each setting
    @property
    def eq_voltage(self):
        return float(self._read("eq_voltage"))

    @property
    def eq_current_complete(self):
        return float(self._read("eq_current_complete"))

    @property
    def eq_timeout_hours(self):
        return float(self._read("eq_timeout_hours"))

    @property
    def float_voltage(self):
        return float(self._read("float_voltage"))

    @property
    def voltage_delta_max(self):
        return float(self._read("voltage_delta_max"))

    @property
    def voltage_match_timeout_hours(self):
        return float(self._read("voltage_match_timeout_hours"))

    @property
    def days_between(self):
        return int(self._read("days_between"))

    @property
    def start_hour(self):
        return int(self._read("start_hour"))

    @property
    def end_hour(self):
        return int(self._read("end_hour"))

    @property
    def lfp_soc_min(self):
        return int(self._read("lfp_soc_min"))

    @property
    def enabled(self):
        return bool(self._read("enabled"))

    @property
    def run_now(self):
        return bool(self._read("run_now"))

    def clear_run_now(self):
        """Reset the RunNow flag after triggering."""
        self._write("run_now", 0)
```

- [ ] **Step 2: Commit**

```bash
git add fla-equalisation/settings.py
git commit -m "feat: add Venus OS settings integration for equalisation parameters"
```

---

### Task 4: Temporary D-Bus Battery Service

**Files:**
- Create: `fla-equalisation/dbus_battery_service.py`

- [ ] **Step 1: Write dbus_battery_service.py**

```python
"""Temporary D-Bus battery service for DVCC CVL control during FLA equalisation.

Registers as com.victronenergy.battery with a configurable CVL so the Quattro
charges the Trojan FLAs at equalisation voltage. Feeds live SmartShunt Trojan
readings for voltage/current.
"""

import logging
import os
import platform
import sys
import dbus

log = logging.getLogger(__name__)

# Add velib_python to path
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext", "velib_python"))
from vedbus import VeDbusService


def get_bus():
    if "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        return dbus.SessionBus()
    return dbus.SystemBus()


class TempBatteryService:
    """Temporary D-Bus battery service for DVCC control during equalisation."""

    def __init__(self, device_instance=100):
        self._bus = get_bus()
        self._service = VeDbusService(
            "com.victronenergy.battery.fla_equalisation",
            self._bus,
            register=False,
        )
        self._device_instance = device_instance
        self._registered = False

    def register(self, charge_voltage, charge_current, discharge_current=0):
        """Register the temporary battery service on D-Bus with given CVL."""
        # Management paths
        self._service.add_path("/Mgmt/ProcessName", __file__)
        self._service.add_path(
            "/Mgmt/ProcessVersion", "Python " + platform.python_version()
        )
        self._service.add_path("/Mgmt/Connection", "FLA Equalisation")

        # Mandatory paths
        self._service.add_path("/DeviceInstance", self._device_instance)
        self._service.add_path("/ProductId", 0xFE01)
        self._service.add_path("/ProductName", "FLA Equalisation")
        self._service.add_path("/FirmwareVersion", "1.0")
        self._service.add_path("/HardwareVersion", "1.0")
        self._service.add_path("/Connected", 1)

        # DC measurements (updated live from SmartShunt Trojan)
        self._service.add_path(
            "/Dc/0/Voltage", None, writeable=True,
            gettextcallback=lambda a, x: "{:.2f}V".format(x) if x is not None else "---",
        )
        self._service.add_path(
            "/Dc/0/Current", None, writeable=True,
            gettextcallback=lambda a, x: "{:.2f}A".format(x) if x is not None else "---",
        )
        self._service.add_path(
            "/Dc/0/Power", None, writeable=True,
            gettextcallback=lambda a, x: "{:.0f}W".format(x) if x is not None else "---",
        )

        # SoC (not critical, set to None)
        self._service.add_path("/Soc", None, writeable=True)

        # Charge/discharge control — this is what DVCC reads
        self._service.add_path(
            "/Info/MaxChargeVoltage", charge_voltage, writeable=True,
            gettextcallback=lambda a, x: "{:.2f}V".format(x),
        )
        self._service.add_path(
            "/Info/MaxChargeCurrent", charge_current, writeable=True,
            gettextcallback=lambda a, x: "{:.1f}A".format(x),
        )
        self._service.add_path(
            "/Info/MaxDischargeCurrent", discharge_current, writeable=True,
            gettextcallback=lambda a, x: "{:.1f}A".format(x),
        )

        # Allow charge/discharge flags
        self._service.add_path("/Io/AllowToCharge", 1, writeable=True)
        self._service.add_path("/Io/AllowToDischarge", 0, writeable=True)

        # Register on D-Bus
        self._service.register()
        self._registered = True
        log.info(
            "Temporary battery service registered: CVL=%.1fV, CCL=%.1fA",
            charge_voltage, charge_current,
        )

    def update_voltage_current(self, voltage, current):
        """Update live voltage and current from SmartShunt Trojan."""
        if not self._registered:
            return
        with self._service as svc:
            svc["/Dc/0/Voltage"] = voltage
            svc["/Dc/0/Current"] = current
            if voltage is not None and current is not None:
                svc["/Dc/0/Power"] = round(voltage * current, 0)

    def set_charge_voltage(self, voltage):
        """Update the CVL (e.g., reduce from equalisation to float)."""
        if not self._registered:
            return
        self._service["/Info/MaxChargeVoltage"] = voltage
        log.info("CVL updated to %.2fV", voltage)

    def deregister(self):
        """Remove the temporary service from D-Bus."""
        if not self._registered:
            return
        try:
            self._service.__del__()
        except Exception as e:
            log.warning("Error deregistering service: %s", e)
        self._registered = False
        log.info("Temporary battery service deregistered")
```

- [ ] **Step 2: Commit**

```bash
git add fla-equalisation/dbus_battery_service.py
git commit -m "feat: add temporary D-Bus battery service for DVCC CVL control"
```

---

### Task 5: D-Bus Status Service for Cerbo GUI

**Files:**
- Create: `fla-equalisation/dbus_status_service.py`

- [ ] **Step 1: Write dbus_status_service.py**

```python
"""D-Bus status service for FLA equalisation — visible on Cerbo GUI Device List."""

import logging
import os
import platform
import sys

import dbus

log = logging.getLogger(__name__)

sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext", "velib_python"))
from vedbus import VeDbusService

# State constants
STATE_IDLE = 0
STATE_STOPPING_DRIVER = 1
STATE_DISCONNECTING = 2
STATE_EQUALISING = 3
STATE_COOLING_DOWN = 4
STATE_VOLTAGE_MATCHING = 5
STATE_RECONNECTING = 6
STATE_RESTARTING_DRIVER = 7
STATE_ERROR = 8

STATE_NAMES = {
    STATE_IDLE: "Idle",
    STATE_STOPPING_DRIVER: "Stopping aggregate driver",
    STATE_DISCONNECTING: "Disconnecting LFP",
    STATE_EQUALISING: "Equalising FLA",
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
    """D-Bus service exposing equalisation state on Cerbo GUI."""

    def __init__(self):
        self._bus = get_bus()
        self._service = VeDbusService(
            "com.victronenergy.fla_equalisation",
            self._bus,
            register=False,
        )
        self._registered = False

    def register(self):
        """Register the status service on D-Bus."""
        self._service.add_path("/Mgmt/ProcessName", __file__)
        self._service.add_path("/Mgmt/ProcessVersion", "Python " + platform.python_version())
        self._service.add_path("/Mgmt/Connection", "FLA Equalisation Status")

        self._service.add_path("/DeviceInstance", 200)
        self._service.add_path("/ProductId", 0xFE02)
        self._service.add_path("/ProductName", "FLA Equalisation")
        self._service.add_path("/FirmwareVersion", "1.0")
        self._service.add_path("/Connected", 1)

        self._service.add_path(
            "/State", STATE_IDLE, writeable=True,
            gettextcallback=lambda a, x: STATE_NAMES.get(x, "Unknown"),
        )
        self._service.add_path(
            "/TimeRemaining", 0, writeable=True,
            gettextcallback=lambda a, x: "{:.0f}s".format(x) if x else "---",
        )
        self._service.add_path(
            "/TrojanVoltage", None, writeable=True,
            gettextcallback=lambda a, x: "{:.2f}V".format(x) if x is not None else "---",
        )
        self._service.add_path(
            "/LfpVoltage", None, writeable=True,
            gettextcallback=lambda a, x: "{:.2f}V".format(x) if x is not None else "---",
        )
        self._service.add_path(
            "/VoltageDelta", None, writeable=True,
            gettextcallback=lambda a, x: "{:.2f}V".format(x) if x is not None else "---",
        )

        self._service.register()
        self._registered = True
        log.info("Status service registered on D-Bus")

    def update(self, state=None, time_remaining=None, trojan_v=None, lfp_v=None):
        """Update status values on D-Bus."""
        if not self._registered:
            return
        with self._service as svc:
            if state is not None:
                svc["/State"] = state
            if time_remaining is not None:
                svc["/TimeRemaining"] = time_remaining
            if trojan_v is not None:
                svc["/TrojanVoltage"] = trojan_v
            if lfp_v is not None:
                svc["/LfpVoltage"] = lfp_v
            if trojan_v is not None and lfp_v is not None:
                svc["/VoltageDelta"] = round(abs(trojan_v - lfp_v), 2)

    def deregister(self):
        """Remove the status service from D-Bus."""
        if not self._registered:
            return
        try:
            self._service.__del__()
        except Exception as e:
            log.warning("Error deregistering status service: %s", e)
        self._registered = False
        log.info("Status service deregistered")
```

- [ ] **Step 2: Commit**

```bash
git add fla-equalisation/dbus_status_service.py
git commit -m "feat: add D-Bus status service for Cerbo GUI visibility"
```

---

### Task 6: Alerting module

**Files:**
- Create: `fla-equalisation/alerting.py`

- [ ] **Step 1: Write alerting.py**

```python
"""Alerting for FLA equalisation — buzzer, D-Bus alarm, logging."""

import dbus
import logging
import os

log = logging.getLogger(__name__)

BUZZER_PATH = "/Buzzer/State"
SYSTEM_SERVICE = "com.victronenergy.system"


def _get_bus():
    if "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        return dbus.SessionBus()
    return dbus.SystemBus()


def activate_buzzer():
    """Activate Cerbo GX buzzer."""
    try:
        bus = _get_bus()
        obj = bus.get_object(SYSTEM_SERVICE, BUZZER_PATH)
        iface = dbus.Interface(obj, "com.victronenergy.BusItem")
        iface.SetValue(dbus.Int32(1))
        log.info("Buzzer activated")
    except dbus.exceptions.DBusException as e:
        log.warning("Failed to activate buzzer: %s", e)


def deactivate_buzzer():
    """Deactivate Cerbo GX buzzer."""
    try:
        bus = _get_bus()
        obj = bus.get_object(SYSTEM_SERVICE, BUZZER_PATH)
        iface = dbus.Interface(obj, "com.victronenergy.BusItem")
        iface.SetValue(dbus.Int32(0))
    except dbus.exceptions.DBusException:
        pass


def raise_alarm(message):
    """Raise alarm: buzzer + log. VRM picks up D-Bus alarms automatically."""
    log.error("ALARM: %s", message)
    activate_buzzer()


def clear_alarm():
    """Clear alarm state."""
    deactivate_buzzer()
```

- [ ] **Step 2: Commit**

```bash
git add fla-equalisation/alerting.py
git commit -m "feat: add alerting module — buzzer, alarm, logging"
```

---

### Task 7: Main Equalisation Script — Scheduling and Pre-checks

**Files:**
- Create: `fla-equalisation/fla_equalisation.py`

- [ ] **Step 1: Write the scheduling and pre-check logic**

```python
#!/usr/bin/env python3
"""FLA Equalisation Script for Venus OS.

Automates periodic Trojan L16H-AC equalisation on vessel Zwartewater.
Runs hourly via cron, checks conditions, orchestrates the full sequence.
"""

import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Add velib_python to path
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext", "velib_python"))

from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

from dbus_battery_service import TempBatteryService
from dbus_monitor import DbusMonitor
from dbus_status_service import (
    StatusService, STATE_IDLE, STATE_STOPPING_DRIVER, STATE_DISCONNECTING,
    STATE_EQUALISING, STATE_COOLING_DOWN, STATE_VOLTAGE_MATCHING,
    STATE_RECONNECTING, STATE_RESTARTING_DRIVER, STATE_ERROR,
)
from settings import Settings
from alerting import raise_alarm, clear_alarm

# Logging setup
LOG_FILE = "/data/log/fla-equalisation.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

LAST_EQ_FILE = "/data/apps/fla-equalisation/last_equalisation"
AGG_SERVICE_PATH = "/service/dbus-aggregate-batteries"


def read_last_equalisation():
    """Read timestamp of last successful equalisation."""
    try:
        return datetime.fromisoformat(Path(LAST_EQ_FILE).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def write_last_equalisation():
    """Record current time as last successful equalisation."""
    Path(LAST_EQ_FILE).write_text(datetime.now().isoformat())


def stop_aggregate_driver():
    """Stop dbus-aggregate-batteries service."""
    log.info("Stopping dbus-aggregate-batteries...")
    result = subprocess.run(["svc", "-d", AGG_SERVICE_PATH], capture_output=True)
    if result.returncode != 0:
        log.error("Failed to stop aggregate driver: %s", result.stderr.decode())
        return False
    # Wait for service to stop and D-Bus service to disappear
    time.sleep(5)
    log.info("Aggregate driver stopped")
    return True


def start_aggregate_driver():
    """Start dbus-aggregate-batteries service."""
    log.info("Starting dbus-aggregate-batteries...")
    result = subprocess.run(["svc", "-u", AGG_SERVICE_PATH], capture_output=True)
    if result.returncode != 0:
        log.error("Failed to start aggregate driver: %s", result.stderr.decode())
        return False
    log.info("Aggregate driver started")
    return True


def should_run(settings, monitor):
    """Check if all scheduling conditions are met."""
    # Check enabled
    if not settings.enabled:
        log.debug("Equalisation disabled")
        return False

    # Check RunNow override
    if settings.run_now:
        log.info("RunNow flag set — bypassing schedule checks")
        settings.clear_run_now()
        return True

    # Check interval
    last = read_last_equalisation()
    if last is not None:
        days_since = (datetime.now() - last).days
        if days_since < settings.days_between:
            log.debug("Only %d days since last equalisation (need %d)", days_since, settings.days_between)
            return False

    # Check afternoon window
    now = datetime.now()
    if not (settings.start_hour <= now.hour < settings.end_hour):
        log.debug("Outside afternoon window (%d:00-%d:00)", settings.start_hour, settings.end_hour)
        return False

    # Check LFP SoC
    soc = monitor.get_lfp_soc()
    if soc is None:
        log.warning("Cannot read LFP SoC")
        return False
    if soc < settings.lfp_soc_min:
        log.debug("LFP SoC %.1f%% < %d%% minimum", soc, settings.lfp_soc_min)
        return False

    log.info("All conditions met: SoC=%.1f%%, time=%s", soc, now.strftime("%H:%M"))
    return True


def run_equalisation(settings, monitor, status):
    """Execute the full equalisation sequence. Returns True on success."""
    temp_service = None
    aggregate_stopped = False

    try:
        # Step 1: Stop aggregate driver
        status.update(state=STATE_STOPPING_DRIVER)
        if not stop_aggregate_driver():
            status.update(state=STATE_ERROR)
            raise_alarm("Failed to stop aggregate driver")
            return False
        aggregate_stopped = True

        # Step 2: Register temporary battery service
        temp_service = TempBatteryService(device_instance=100)
        temp_service.register(
            charge_voltage=settings.eq_voltage,
            charge_current=120.0,  # Quattro max
            discharge_current=0,
        )

        # Step 3: Open relay 2 (disconnect LFP direct path)
        status.update(state=STATE_DISCONNECTING)
        if not monitor.set_relay(0):
            status.update(state=STATE_ERROR)
            raise_alarm("Failed to open relay 2")
            return False
        log.info("Relay 2 opened — LFP direct path disconnected, Orion activating")

        # Step 4: Verify LFP disconnected (current drops to ~0A)
        time.sleep(10)  # Wait for relay + Orion to settle
        lfp_current = monitor.get_lfp_current()
        if lfp_current is not None and abs(lfp_current) > 5.0:
            log.warning("LFP current still %.1fA after relay open — Orion taking over", lfp_current)
            # Not a hard failure — Orion may be drawing current

        # Step 5: Equalisation — monitor Trojan current
        status.update(state=STATE_EQUALISING)
        log.info("Starting equalisation at %.1fV", settings.eq_voltage)
        eq_start = time.time()
        eq_timeout = settings.eq_timeout_hours * 3600

        while True:
            elapsed = time.time() - eq_start

            # Read Trojan values and update temp service
            v_trojan = monitor.get_trojan_voltage()
            i_trojan = monitor.get_trojan_current()
            if v_trojan is not None and i_trojan is not None:
                temp_service.update_voltage_current(v_trojan, i_trojan)

            # Update status
            v_lfp = monitor.get_lfp_voltage()
            remaining = max(0, eq_timeout - elapsed)
            status.update(
                time_remaining=remaining, trojan_v=v_trojan, lfp_v=v_lfp,
            )

            # Check SmartShunt Trojan responsive
            if v_trojan is None:
                status.update(state=STATE_ERROR)
                raise_alarm("SmartShunt Trojan (279) unresponsive during equalisation")
                return False

            # Check completion: current below threshold
            if i_trojan is not None and abs(i_trojan) < settings.eq_current_complete:
                log.info(
                    "Equalisation complete: current %.1fA < %.1fA threshold (%.0f min)",
                    abs(i_trojan), settings.eq_current_complete, elapsed / 60,
                )
                break

            # Check timeout
            if elapsed > eq_timeout:
                log.warning(
                    "Equalisation timeout after %.0f min, current %.1fA",
                    elapsed / 60, abs(i_trojan) if i_trojan else 0,
                )
                break

            # Log progress every 5 minutes
            if int(elapsed) % 300 < 30:
                log.info(
                    "Equalising: %.0f min, V=%.1fV, I=%.1fA",
                    elapsed / 60, v_trojan or 0, i_trojan or 0,
                )

            time.sleep(30)

        # Step 6: Reduce CVL to float
        status.update(state=STATE_COOLING_DOWN)
        log.info("Reducing CVL to float voltage %.1fV", settings.float_voltage)
        temp_service.set_charge_voltage(settings.float_voltage)

        # Step 7: Voltage matching
        status.update(state=STATE_VOLTAGE_MATCHING)
        log.info("Waiting for voltage convergence (delta < %.1fV)", settings.voltage_delta_max)
        match_start = time.time()
        match_timeout = settings.voltage_match_timeout_hours * 3600

        while True:
            elapsed = time.time() - match_start

            v_trojan = monitor.get_trojan_voltage()
            v_lfp = monitor.get_lfp_voltage()

            if v_trojan is not None and v_lfp is not None:
                delta = abs(v_trojan - v_lfp)
                remaining = max(0, match_timeout - elapsed)
                status.update(
                    time_remaining=remaining, trojan_v=v_trojan, lfp_v=v_lfp,
                )
                temp_service.update_voltage_current(v_trojan, monitor.get_trojan_current())

                if delta < settings.voltage_delta_max:
                    log.info(
                        "Voltage converged: Trojan=%.2fV, LFP=%.2fV, delta=%.2fV",
                        v_trojan, v_lfp, delta,
                    )
                    break

                # Log every 5 minutes
                if int(elapsed) % 300 < 30:
                    log.info(
                        "Voltage matching: Trojan=%.2fV, LFP=%.2fV, delta=%.2fV (%.0f min)",
                        v_trojan, v_lfp, delta, elapsed / 60,
                    )

            # Check timeout
            if elapsed > match_timeout:
                status.update(state=STATE_ERROR)
                raise_alarm(
                    "Voltage delta did not converge after %.0f hours. "
                    "Trojan=%.2fV, LFP=%.2fV, delta=%.2fV. "
                    "LFPs remain disconnected — manual intervention required."
                    % (elapsed / 3600, v_trojan or 0, v_lfp or 0, delta if v_trojan and v_lfp else 0)
                )
                return False

            time.sleep(30)

        # Step 8: Close relay 2
        status.update(state=STATE_RECONNECTING)
        if not monitor.set_relay(1):
            raise_alarm("Failed to close relay 2")
            return False
        log.info("Relay 2 closed — LFP direct path restored")
        time.sleep(5)

        # Step 9: Deregister temp service
        temp_service.deregister()
        temp_service = None

        # Step 10: Restart aggregate driver
        status.update(state=STATE_RESTARTING_DRIVER)
        if not start_aggregate_driver():
            raise_alarm("Failed to restart aggregate driver")
            return False

        # Step 11: Record success
        write_last_equalisation()
        status.update(state=STATE_IDLE, time_remaining=0)
        log.info("Equalisation completed successfully")
        clear_alarm()
        return True

    except Exception as e:
        log.exception("Unexpected error during equalisation: %s", e)
        raise_alarm("Equalisation script error: %s" % e)
        return False

    finally:
        # Safety cleanup: always try to restore safe state
        if temp_service is not None:
            try:
                temp_service.deregister()
            except Exception:
                pass

        # If relay was opened and we're in a failure path, close it
        relay_state = monitor.get_relay_state()
        if relay_state == 0:
            log.warning("Safety: relay still open in cleanup — closing")
            monitor.set_relay(1)
            time.sleep(2)

        # If aggregate driver was stopped, restart it
        if aggregate_stopped:
            try:
                start_aggregate_driver()
            except Exception:
                log.error("CRITICAL: Failed to restart aggregate driver in cleanup")


def main():
    """Entry point: check conditions and run equalisation if needed."""
    DBusGMainLoop(set_as_default=True)

    log.info("FLA equalisation check starting")

    try:
        settings = Settings()
        monitor = DbusMonitor(
            lfp_instance=277,
            trojan_instance=279,
        )
        status = StatusService()
        status.register()

        if should_run(settings, monitor):
            success = run_equalisation(settings, monitor, status)
            if success:
                log.info("Equalisation run completed successfully")
            else:
                log.error("Equalisation run failed — check alarms")
        else:
            log.debug("Conditions not met, exiting")

        status.deregister()

    except Exception as e:
        log.exception("Fatal error: %s", e)
        raise_alarm("FLA equalisation fatal error: %s" % e)

    log.info("FLA equalisation check finished")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add fla-equalisation/fla_equalisation.py
git commit -m "feat: add main equalisation script with scheduling, state machine, and safety cleanup"
```

---

### Task 8: Integration Test on Local Machine

This task verifies all modules import correctly and the code is syntactically valid. Full D-Bus testing requires Venus OS.

**Files:**
- No new files

- [ ] **Step 1: Verify all Python files parse correctly**

```bash
cd /Users/jochembaud/claude/apps/zwartewater-victron
python3 -c "import ast; [ast.parse(open(f).read()) for f in ['fla-equalisation/settings.py', 'fla-equalisation/dbus_monitor.py', 'fla-equalisation/dbus_battery_service.py', 'fla-equalisation/dbus_status_service.py', 'fla-equalisation/alerting.py', 'fla-equalisation/fla_equalisation.py']]; print('All files parse OK')"
```

Expected: `All files parse OK`

- [ ] **Step 2: Verify install.sh is valid bash**

```bash
bash -n fla-equalisation/install.sh && echo "install.sh syntax OK"
```

Expected: `install.sh syntax OK`

- [ ] **Step 3: Commit if any fixes were needed**

```bash
git add -A
git commit -m "fix: resolve any syntax issues found during verification"
```

---

### Task 9: Deployment and First Run

This task is performed on the Cerbo GX directly.

**Files:**
- No new files (deployment of existing files)

- [ ] **Step 1: Copy files to Cerbo GX**

```bash
scp -r fla-equalisation/ root@venus.local:/tmp/fla-equalisation/
ssh root@venus.local "bash /tmp/fla-equalisation/install.sh"
```

- [ ] **Step 2: Verify settings registered on D-Bus**

```bash
ssh root@venus.local "dbus -y com.victronenergy.settings /Settings/FlaEqualisation/EqualisationVoltage GetValue"
```

Expected: `31.2` (or similar dbus-wrapped value)

- [ ] **Step 3: Test a dry run (conditions won't be met, should exit cleanly)**

```bash
ssh root@venus.local "python3 /data/apps/fla-equalisation/fla_equalisation.py"
ssh root@venus.local "tail -20 /data/log/fla-equalisation.log"
```

Expected: Log shows "Conditions not met, exiting" or "FLA equalisation check finished"

- [ ] **Step 4: Test manual trigger via RunNow flag**

```bash
ssh root@venus.local "dbus -y com.victronenergy.settings /Settings/FlaEqualisation/RunNow SetValue 1"
ssh root@venus.local "python3 /data/apps/fla-equalisation/fla_equalisation.py"
```

Expected: Equalisation sequence starts (monitor closely via log)

- [ ] **Step 5: Commit any deployment fixes**

```bash
git add -A
git commit -m "fix: deployment adjustments from first Cerbo GX test"
git push origin main
```
