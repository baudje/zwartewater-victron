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
    """Get the shared system bus — required for DVCC to discover this service."""
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
        """Remove the temporary service from D-Bus and release the bus name."""
        if not self._registered:
            return
        try:
            self._service["/Connected"] = 0
        except Exception as e:
            log.warning("Error setting Connected=0: %s", e)
        try:
            # Release the D-Bus name so DVCC stops seeing this service
            self._bus.release_name("com.victronenergy.battery.fla_equalisation")
        except Exception as e:
            log.warning("Error releasing D-Bus name: %s", e)
        self._service = None
        self._registered = False
        log.info("Temporary battery service deregistered")
