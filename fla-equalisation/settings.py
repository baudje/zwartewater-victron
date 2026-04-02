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
    "eq_voltage": ("/Settings/FlaEqualisation/EqualisationVoltage", 31.5, 28.0, 31.5),
    "eq_current_complete": ("/Settings/FlaEqualisation/EqualisationCurrentComplete", 10.0, 1.0, 50.0),
    "eq_timeout_hours": ("/Settings/FlaEqualisation/EqualisationTimeoutHours", 2.5, 0.5, 8.0),
    "float_voltage": ("/Settings/FlaEqualisation/FloatVoltage", 27.0, 24.0, 30.0),
    "voltage_delta_max": ("/Settings/FlaEqualisation/VoltageDeltaMax", 1.0, 0.1, 1.0),
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
                settings_obj, "com.victronenergy.Settings"
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
