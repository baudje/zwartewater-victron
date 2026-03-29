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
