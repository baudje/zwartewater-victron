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

    def get_trojan_soc(self):
        """Read Trojan SoC from SmartShunt Trojan (279)."""
        self._ensure_services()
        if self._trojan_service is None:
            return None
        soc = _get_dbus_value(self.bus, self._trojan_service, "/Soc")
        return float(soc) if soc is not None else None

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
            if "fla_equalisation" in str(name):
                continue
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

    def _get_active_battery_service(self):
        """Read the active battery service from DVCC."""
        return _get_dbus_value(
            self.bus, "com.victronenergy.system", "/ActiveBatteryService",
        )

    def get_dvcc_max_charge_voltage(self):
        """Read the DVCC system MaxChargeVoltage setting."""
        return _get_dbus_value(
            self.bus, "com.victronenergy.settings",
            "/Settings/SystemSetup/MaxChargeVoltage",
        )

    def set_dvcc_max_charge_voltage(self, voltage):
        """Set the DVCC system MaxChargeVoltage setting. Returns True on success."""
        try:
            obj = self.bus.get_object(
                "com.victronenergy.settings",
                "/Settings/SystemSetup/MaxChargeVoltage",
            )
            iface = dbus.Interface(obj, "com.victronenergy.BusItem")
            iface.SetValue(dbus.Double(voltage))
            log.info("DVCC MaxChargeVoltage set to %.1fV", voltage)
            return True
        except dbus.exceptions.DBusException as e:
            log.error("Failed to set DVCC MaxChargeVoltage: %s", e)
            return False

    def invalidate_services(self):
        """Force re-discovery of SmartShunt services."""
        self._lfp_service = None
        self._trojan_service = None
