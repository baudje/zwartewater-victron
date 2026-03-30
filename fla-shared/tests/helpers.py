"""Shared test infrastructure for FLA services.

Provides D-Bus mock setup and reusable mock objects for tests
that run on macOS without Venus OS.
"""

import logging
import sys
from unittest.mock import MagicMock


def dbus_mock_setup():
    """Mock all D-Bus and GLib modules so imports work on any platform."""
    for mod in [
        'dbus', 'dbus.mainloop.glib', 'dbus.exceptions', 'dbus.service',
        'gi', 'gi.repository', 'vedbus',
    ]:
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()

    # Redirect FileHandler to stderr (some modules create log files at import time)
    logging.FileHandler = lambda *a, **kw: logging.StreamHandler()


class MockMonitor:
    """Mock DbusMonitor with configurable return values."""

    def __init__(self, **kwargs):
        self._lfp_voltage = kwargs.get('lfp_voltage', 28.0)
        self._lfp_current = kwargs.get('lfp_current', 0.0)
        self._trojan_voltage = kwargs.get('trojan_voltage', 27.5)
        self._trojan_current = kwargs.get('trojan_current', 20.0)
        self._lfp_soc = kwargs.get('lfp_soc', 96.0)
        self._trojan_soc = kwargs.get('trojan_soc', 85.0)
        self._relay_state = kwargs.get('relay_state', 1)
        self._battery_temp = kwargs.get('battery_temp', 25.0)
        self._relay_set_calls = []
        self._invalidated = False

    def get_lfp_voltage(self):
        return self._lfp_voltage

    def get_lfp_current(self):
        return self._lfp_current

    def get_trojan_voltage(self):
        return self._trojan_voltage

    def get_trojan_current(self):
        return self._trojan_current

    def get_lfp_soc(self):
        return self._lfp_soc

    def get_trojan_soc(self):
        return self._trojan_soc

    def get_relay_state(self):
        return self._relay_state

    def set_relay(self, state):
        self._relay_set_calls.append(state)
        self._relay_state = state
        return True

    def get_battery_service_setting(self):
        return "com.victronenergy.battery.aggregate"

    def set_battery_service_setting(self, value):
        return True

    def get_dvcc_max_charge_voltage(self):
        return 28.4

    def set_dvcc_max_charge_voltage(self, voltage):
        return True

    def get_battery_temperature(self):
        return self._battery_temp

    def invalidate_services(self):
        self._invalidated = True


class MockStatus:
    """Mock StatusService that records state transitions."""

    def __init__(self):
        self.states = []
        self.updates = []

    def update(self, **kwargs):
        self.updates.append(kwargs)
        if 'state' in kwargs:
            self.states.append(kwargs['state'])

    def register(self):
        pass

    def deregister(self):
        pass

    def set_alarm(self, level=2):
        pass

    def clear_alarm_path(self):
        pass
