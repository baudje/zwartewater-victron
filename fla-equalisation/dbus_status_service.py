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
            "com.victronenergy.genset.fla_equalisation",
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
        self._service.add_path(
            "/Alarms/Equalisation", 0, writeable=True,
            gettextcallback=lambda a, x: {0: "OK", 1: "Warning", 2: "Alarm"}.get(x, "Unknown"),
        )
        self._service.add_path(
            "/InrushCurrent", None, writeable=True,
            gettextcallback=lambda a, x: "{:.1f}A".format(x) if x is not None else "---",
        )
        self._service.add_path(
            "/ReconnectDelta", None, writeable=True,
            gettextcallback=lambda a, x: "{:.2f}V".format(x) if x is not None else "---",
        )

        self._service.register()
        self._registered = True
        log.info("Status service registered on D-Bus")

    def update(self, state=None, time_remaining=None, trojan_v=None, lfp_v=None,
               inrush_current=None, reconnect_delta=None):
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
            if inrush_current is not None:
                svc["/InrushCurrent"] = inrush_current
            if reconnect_delta is not None:
                svc["/ReconnectDelta"] = reconnect_delta

    def set_alarm(self, level=2):
        """Set alarm level: 0=OK, 1=Warning, 2=Alarm."""
        if not self._registered:
            return
        self._service["/Alarms/Equalisation"] = level

    def clear_alarm_path(self):
        """Clear alarm on D-Bus."""
        if not self._registered:
            return
        self._service["/Alarms/Equalisation"] = 0

    def deregister(self):
        """Remove the status service from D-Bus."""
        if not self._registered:
            return
        try:
            self._service["/Connected"] = 0
        except Exception as e:
            log.warning("Error deregistering status service: %s", e)
        self._registered = False
        log.info("Status service deregistered")
