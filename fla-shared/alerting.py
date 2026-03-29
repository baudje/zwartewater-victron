"""Alerting for FLA services — buzzer, D-Bus alarm, logging."""

import dbus
import logging

from dbus_monitor import get_bus

log = logging.getLogger(__name__)

BUZZER_PATH = "/Buzzer/State"
SYSTEM_SERVICE = "com.victronenergy.system"


def activate_buzzer():
    """Activate Cerbo GX buzzer."""
    try:
        bus = get_bus()
        obj = bus.get_object(SYSTEM_SERVICE, BUZZER_PATH)
        iface = dbus.Interface(obj, "com.victronenergy.BusItem")
        iface.SetValue(dbus.Int32(1))
        log.info("Buzzer activated")
    except dbus.exceptions.DBusException as e:
        log.warning("Failed to activate buzzer: %s", e)


def deactivate_buzzer():
    """Deactivate Cerbo GX buzzer."""
    try:
        bus = get_bus()
        obj = bus.get_object(SYSTEM_SERVICE, BUZZER_PATH)
        iface = dbus.Interface(obj, "com.victronenergy.BusItem")
        iface.SetValue(dbus.Int32(0))
    except dbus.exceptions.DBusException:
        pass


def raise_alarm(message, status_service=None):
    """Raise alarm: buzzer + D-Bus alarm path + log."""
    log.error("ALARM: %s", message)
    activate_buzzer()
    if status_service is not None:
        status_service.set_alarm(2)


def clear_alarm(status_service=None):
    """Clear alarm state."""
    deactivate_buzzer()
    if status_service is not None:
        status_service.clear_alarm_path()
