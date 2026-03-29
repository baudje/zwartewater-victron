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
    """Private bus for status service — avoids root path conflict with temp battery service."""
    if "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        return dbus.SessionBus(private=True)
    return dbus.SystemBus(private=True)


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
