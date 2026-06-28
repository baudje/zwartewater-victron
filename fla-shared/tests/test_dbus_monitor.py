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


@patch('dbus_monitor.subprocess')
@patch('dbus_monitor.time')
class TestRestartSystemcalcWaitsForSystem(unittest.TestCase):
    """After restarting systemcalc, the call must block until
    com.victronenergy.system re-registers (it owns the relays the caller
    opens next), not return after a blind sleep."""

    def _monitor(self, name_has_owner):
        m = dbus_monitor.DbusMonitor()
        m.bus = MagicMock()
        m.bus.name_has_owner = name_has_owner
        return m

    def test_returns_true_once_system_name_reappears(self, mock_time, mock_subproc):
        mock_subproc.run.return_value = MagicMock(returncode=0)
        mock_time.time.return_value = 0          # deadline never passes
        mock_time.sleep = MagicMock()
        m = self._monitor(MagicMock(side_effect=[False, False, True]))

        self.assertTrue(m.restart_systemcalc(system_timeout=120, system_poll=0.1))
        self.assertEqual(m.bus.name_has_owner.call_count, 3)

    def test_returns_false_if_system_name_never_reappears(self, mock_time, mock_subproc):
        mock_subproc.run.return_value = MagicMock(returncode=0)
        mock_time.time.side_effect = [0, 0, 5]   # second poll is past the deadline
        mock_time.sleep = MagicMock()
        m = self._monitor(MagicMock(return_value=False))

        self.assertFalse(m.restart_systemcalc(system_timeout=1, system_poll=0.1))

    def test_returns_false_if_svc_up_fails(self, mock_time, mock_subproc):
        # svc -d ok, svc -u fails -> never reach the wait
        mock_subproc.run.side_effect = [MagicMock(returncode=0),
                                        MagicMock(returncode=1, stderr=b"boom")]
        mock_time.sleep = MagicMock()
        m = self._monitor(MagicMock(return_value=True))

        self.assertFalse(m.restart_systemcalc())
        m.bus.name_has_owner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
