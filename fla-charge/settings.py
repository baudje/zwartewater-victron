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
