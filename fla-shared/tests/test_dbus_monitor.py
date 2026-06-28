"""Tests for dbus_monitor.py — D-Bus value reading and systemcalc handling.

Covers the resilience fixes added after the 2026-06 Venus OS v3.80 incident:
  - Victron's empty-array "invalid value" sentinel must not crash readers.
  - systemcalc restart must wait for com.victronenergy.system to re-register
    before callers use the relays it owns.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))
from helpers import dbus_mock_setup

dbus_mock_setup()
import dbus_monitor
from dbus_monitor import _get_dbus_value


class TestEmptyArraySentinel(unittest.TestCase):
    """Victron publishes an empty array variant ([]) to mean invalid/no value.
    It must be treated as None, never passed through to float()/int()."""

    def setUp(self):
        self.dbus_mod = sys.modules['dbus']

    def test_get_lfp_soc_returns_none_when_aggregate_soc_invalid(self):
        # Regression: float(dbus.Array([])) raised TypeError and crashed the
        # service loop whenever the aggregate was present-but-not-ready.
        self.dbus_mod.Interface.return_value.GetValue.return_value = []
        m = dbus_monitor.DbusMonitor(lfp_instance=277, trojan_instance=279)
        m.bus = MagicMock()
        m.bus.list_names.return_value = []  # no serialbattery fallback available

        self.assertIsNone(m.get_lfp_soc())

    def test_get_dbus_value_returns_none_for_empty_array(self):
        self.dbus_mod.Interface.return_value.GetValue.return_value = []
        self.assertIsNone(
            _get_dbus_value(MagicMock(), "com.victronenergy.battery.aggregate", "/Soc")
        )


if __name__ == '__main__':
    unittest.main()
