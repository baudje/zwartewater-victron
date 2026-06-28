"""Read SmartShunt and system values from Venus OS D-Bus."""

import dbus
import logging
import os
import subprocess
import time

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
        # Victron publishes an empty array/dict variant to signal an invalid or
        # not-yet-populated value (e.g. a service that is on the bus but still
        # initialising). dbus.Array/Dictionary subclass list/dict, so check the
        # builtins; an empty one means "no value" and must become None rather
        # than flow through to float()/int() and raise TypeError.
        if isinstance(value, (list, tuple, dict)) and len(value) == 0:
            return None
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

    def get_battery_temperature(self):
        """Read battery temperature from serialbattery (JK BMS).

        Returns the average of both BMS readings, or a single reading if only
        one is available. Used for Trojan FLA temperature compensation — the LFP
        cells are in the same engine room as the FLA bank.
        """
        temps = []
        for name in self.bus.list_names():
            name = str(name)
            if "com.victronenergy.battery" not in name:
                continue
            if "aggregate" in name or "fla" in name:
                continue
            product = _get_dbus_value(self.bus, name, "/ProductName")
            if product and "SerialBattery" in str(product):
                temp = _get_dbus_value(self.bus, name, "/Dc/0/Temperature")
                if temp is not None:
                    temps.append(float(temp))
        if not temps:
            return None
        return sum(temps) / len(temps)

    def get_lfp_soc(self):
        """Read LFP SoC from aggregate battery driver or serialbattery."""
        # Try aggregate driver first
        soc = _get_dbus_value(
            self.bus, "com.victronenergy.battery.aggregate", BATTERY_SOC
        )
        if soc is not None:
            return float(soc)
        # Fallback: try finding any serialbattery service. Skip our temp battery
        # service (registered by fla-equalisation/fla-charge during the handoff)
        # since it doesn't carry an LFP SoC.
        for name in self.bus.list_names():
            if "fla_temp" in str(name) or "fla_equalisation" in str(name):
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

    def get_battery_service_setting(self):
        """Read the Venus OS BatteryService setting (which battery the system uses for display/ESS)."""
        return _get_dbus_value(
            self.bus, "com.victronenergy.settings",
            "/Settings/SystemSetup/BatteryService",
        )

    def set_battery_service_setting(self, value):
        """Set the Venus OS BatteryService setting. Returns True on success."""
        try:
            obj = self.bus.get_object(
                "com.victronenergy.settings",
                "/Settings/SystemSetup/BatteryService",
            )
            iface = dbus.Interface(obj, "com.victronenergy.BusItem")
            iface.SetValue(str(value))
            log.info("BatteryService set to %s", value)
            return True
        except dbus.exceptions.DBusException as e:
            log.error("Failed to set BatteryService: %s", e)
            return False

    def get_bms_instance(self):
        """Read the BmsInstance setting (which BMS DVCC reads CVL/CCL/DCL from).
        -1 = auto-select, -255 = no BMS, other = explicit device instance."""
        return _get_dbus_value(
            self.bus, "com.victronenergy.settings",
            "/Settings/SystemSetup/BmsInstance",
        )

    def set_bms_instance(self, instance):
        """Set the BmsInstance setting. Returns True on success."""
        try:
            obj = self.bus.get_object(
                "com.victronenergy.settings",
                "/Settings/SystemSetup/BmsInstance",
            )
            iface = dbus.Interface(obj, "com.victronenergy.BusItem")
            iface.SetValue(dbus.Int32(instance))
            log.info("BmsInstance set to %d", instance)
            return True
        except dbus.exceptions.DBusException as e:
            log.error("Failed to set BmsInstance: %s", e)
            return False

    def restart_systemcalc(self, system_timeout=120, system_poll=1.0):
        """Restart dbus-systemcalc-py so it discovers newly registered battery
        services, then block until com.victronenergy.system re-registers.

        systemcalc doesn't detect services registered after boot — restart
        forces a rescan. But systemcalc also owns com.victronenergy.system,
        which in turn owns the GX relays the caller opens immediately
        afterwards. After a restart that name can take tens of seconds to
        reappear (longer when another service is blocking systemcalc's dbus
        scan), so a blind sleep raced ahead and relay operations failed with
        ServiceUnknown. Returns False if the name does not return in time."""
        try:
            down = subprocess.run(["svc", "-d", "/service/dbus-systemcalc-py"], capture_output=True)
            if down.returncode != 0:
                log.error("Failed to stop systemcalc: %s", down.stderr.decode())
                return False
            time.sleep(2)
            up = subprocess.run(["svc", "-u", "/service/dbus-systemcalc-py"], capture_output=True)
            if up.returncode != 0:
                log.error("Failed to start systemcalc: %s", up.stderr.decode())
                return False
            if not self.wait_for_system_service(timeout_seconds=system_timeout,
                                                poll_interval=system_poll):
                log.error("systemcalc restarted but com.victronenergy.system did "
                          "not re-register within %ds", system_timeout)
                return False
            log.info("systemcalc restarted and com.victronenergy.system re-registered")
            return True
        except Exception as e:
            log.error("Failed to restart systemcalc: %s", e)
            return False

    def wait_for_system_service(self, timeout_seconds=120, poll_interval=1.0):
        """Wait until com.victronenergy.system re-claims its D-Bus name.

        This name owns the GX relays; relay reads/writes fail with
        ServiceUnknown until it is back. Returns True once present, False on
        timeout."""
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                if self.bus.name_has_owner(SYSTEM_SERVICE):
                    return True
            except Exception:
                pass  # bus hiccup during the restart window — keep polling
            time.sleep(poll_interval)
        # The name may have re-registered during the final sleep that crossed
        # the deadline — check once more before giving up, so we don't abort an
        # operation that was a fraction of a second from being ready.
        try:
            return bool(self.bus.name_has_owner(SYSTEM_SERVICE))
        except Exception:
            return False

    def wait_for_service_instance(self, instance, prefix="com.victronenergy.battery",
                                  timeout_seconds=10, poll_interval=0.5):
        """Wait until a D-Bus service with the given instance becomes visible."""
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            service = _find_service(self.bus, prefix, instance)
            if service:
                connected = _get_dbus_value(self.bus, service, "/Connected")
                if connected in (None, 1):
                    return service
            time.sleep(poll_interval)
        log.error("Timed out waiting for %s instance %s", prefix, instance)
        return None

    def wait_for_bms_selection(self, battery_service, bms_instance,
                               timeout_seconds=5, poll_interval=0.5):
        """Wait until BatteryService and BmsInstance reflect the requested handoff."""
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if (self.get_battery_service_setting() == battery_service
                    and self.get_bms_instance() == bms_instance):
                return True
            time.sleep(poll_interval)
        log.error(
            "Timed out waiting for BatteryService=%s and BmsInstance=%s",
            battery_service, bms_instance,
        )
        return False

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
