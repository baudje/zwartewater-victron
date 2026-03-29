#!/usr/bin/env python3
"""Temp battery service as a standalone process.

Launched as a subprocess to avoid D-Bus root path conflicts.
Reads CVL/CCL from command-line args, publishes to D-Bus, runs until killed.

Usage: python3 temp_battery_process.py <charge_voltage> <charge_current> [trojan_instance]
"""

import dbus
import logging
import os
import platform
import signal
import sys
import time

sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext", "velib_python"))

from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
from vedbus import VeDbusService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _find_service_by_instance(bus, instance):
    """Find a SmartShunt D-Bus service by device instance number."""
    for prefix in ("com.victronenergy.battery", "com.victronenergy.dcload"):
        for name in bus.list_names():
            name = str(name)
            if not name.startswith(prefix):
                continue
            try:
                obj = bus.get_object(name, "/DeviceInstance")
                iface = dbus.Interface(obj, "com.victronenergy.BusItem")
                if int(iface.GetValue()) == instance:
                    return name
            except Exception:
                continue
    return None


def main():
    if len(sys.argv) < 3:
        print("Usage: temp_battery_process.py <charge_voltage> <charge_current> [trojan_instance]")
        sys.exit(1)

    charge_voltage = float(sys.argv[1])
    charge_current = float(sys.argv[2])
    trojan_instance = int(sys.argv[3]) if len(sys.argv) > 3 else 279

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    svc = VeDbusService("com.victronenergy.battery.fla_equalisation", bus, register=False)

    # Management paths
    svc.add_path("/Mgmt/ProcessName", __file__)
    svc.add_path("/Mgmt/ProcessVersion", "Python " + platform.python_version())
    svc.add_path("/Mgmt/Connection", "FLA Equalisation")

    # Mandatory paths
    svc.add_path("/DeviceInstance", 100)
    svc.add_path("/ProductId", 0xFE01)
    svc.add_path("/ProductName", "FLA Equalisation")
    svc.add_path("/FirmwareVersion", "1.0")
    svc.add_path("/HardwareVersion", "1.0")
    svc.add_path("/Connected", 1)

    # DC measurements (updated via stdin or file)
    svc.add_path("/Dc/0/Voltage", None, writeable=True,
        gettextcallback=lambda a, x: "{:.2f}V".format(x) if x is not None else "---")
    svc.add_path("/Dc/0/Current", None, writeable=True,
        gettextcallback=lambda a, x: "{:.2f}A".format(x) if x is not None else "---")
    svc.add_path("/Dc/0/Power", None, writeable=True,
        gettextcallback=lambda a, x: "{:.0f}W".format(x) if x is not None else "---")

    svc.add_path("/Soc", None, writeable=True)
    svc.add_path("/Capacity", 435, writeable=True)
    svc.add_path("/InstalledCapacity", 435)

    # Charge/discharge control
    svc.add_path("/Info/MaxChargeVoltage", charge_voltage, writeable=True,
        gettextcallback=lambda a, x: "{:.2f}V".format(x))
    svc.add_path("/Info/MaxChargeCurrent", charge_current, writeable=True,
        gettextcallback=lambda a, x: "{:.1f}A".format(x))
    svc.add_path("/Info/MaxDischargeCurrent", 0.0, writeable=True,
        gettextcallback=lambda a, x: "{:.1f}A".format(x))

    svc.add_path("/Io/AllowToCharge", 1, writeable=True)
    svc.add_path("/Io/AllowToDischarge", 0, writeable=True)

    svc.register()
    log.info("Temp battery service registered: CVL=%.1fV, CCL=%.1fA", charge_voltage, charge_current)

    # Discover SmartShunt Trojan by instance number
    trojan_service = _find_service_by_instance(bus, trojan_instance)
    if trojan_service:
        log.info("Found Trojan SmartShunt: %s (instance %d)", trojan_service, trojan_instance)
    else:
        log.warning("Trojan SmartShunt instance %d not found — voltage/current won't update", trojan_instance)

    # Watch for CVL update file — parent process writes new voltage here
    cvl_file = "/tmp/fla_eq_cvl"

    def update_from_file():
        """Check for CVL update from parent process."""
        nonlocal trojan_service
        try:
            if os.path.exists(cvl_file):
                new_cvl = float(open(cvl_file).read().strip())
                if new_cvl != svc["/Info/MaxChargeVoltage"]:
                    svc["/Info/MaxChargeVoltage"] = new_cvl
                    log.info("CVL updated to %.2fV", new_cvl)
        except (ValueError, OSError):
            pass

        # Retry discovery if not found yet
        if trojan_service is None:
            trojan_service = _find_service_by_instance(bus, trojan_instance)
            if trojan_service:
                log.info("Found Trojan SmartShunt: %s", trojan_service)

        # Update voltage/current from SmartShunt Trojan
        if trojan_service:
            v = None
            try:
                obj = bus.get_object(trojan_service, "/Dc/0/Voltage")
                iface = dbus.Interface(obj, "com.victronenergy.BusItem")
                v = float(iface.GetValue())
                svc["/Dc/0/Voltage"] = v
            except Exception:
                pass
            try:
                obj = bus.get_object(trojan_service, "/Dc/0/Current")
                iface = dbus.Interface(obj, "com.victronenergy.BusItem")
                i = float(iface.GetValue())
                svc["/Dc/0/Current"] = i
                if v and i:
                    svc["/Dc/0/Power"] = round(v * i, 0)
            except Exception:
                pass

        return True  # Keep timer running

    GLib.timeout_add_seconds(2, update_from_file)

    # Clean shutdown on SIGTERM
    def on_sigterm(signum, frame):
        log.info("SIGTERM received, shutting down")
        try:
            svc["/Connected"] = 0
        except Exception:
            pass
        try:
            os.unlink(cvl_file)
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_sigterm)
    signal.signal(signal.SIGINT, on_sigterm)

    log.info("Entering GLib main loop")
    mainloop = GLib.MainLoop()
    mainloop.run()


if __name__ == "__main__":
    main()
