"""Tests for the Takeover module (DVCC handoff / reconnect orchestration)."""

import os
import sys
import json
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))
from helpers import dbus_mock_setup, MockMonitor, MockStatus

dbus_mock_setup()

import takeover


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        # Redirect the snapshot file to a temp path per test.
        self._tmp = os.path.join(os.path.dirname(__file__), "_snap_test.json")
        self._patch = patch.object(takeover, "SNAPSHOT_FILE", self._tmp)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(lambda: os.path.exists(self._tmp) and os.unlink(self._tmp))

    def test_save_then_load_roundtrip(self):
        ok = takeover.save_originals("com.victronenergy.battery/277", -1, 32.0)
        self.assertTrue(ok)
        got = takeover.load_originals()
        self.assertEqual(got["battery_service"], "com.victronenergy.battery/277")
        self.assertEqual(got["bms_instance"], -1)
        self.assertEqual(got["max_charge_voltage"], 32.0)

    def test_load_missing_returns_none(self):
        self.assertIsNone(takeover.load_originals())

    def test_load_corrupt_returns_none(self):
        with open(self._tmp, "w") as f:
            f.write("{not json")
        self.assertIsNone(takeover.load_originals())

    def test_delete_removes_file(self):
        takeover.save_originals("com.victronenergy.battery/277", -1, 32.0)
        takeover.delete_originals()
        self.assertFalse(os.path.exists(self._tmp))
        takeover.delete_originals()  # idempotent — no raise


def _states():
    return takeover.TakeoverStates("STOP", "DISC", "VM", "RECON", "RESTART")


class TestHandOffIn(unittest.TestCase):
    def setUp(self):
        self._tmp = os.path.join(os.path.dirname(__file__), "_snap_hoi.json")
        p = patch.object(takeover, "SNAPSHOT_FILE", self._tmp); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: os.path.exists(self._tmp) and os.unlink(self._tmp))
        self.alerting = MagicMock()
        self.status = MockStatus()

    def _make(self, monitor):
        return takeover.Takeover(monitor, self.status, self.alerting, "fla-equalisation", _states())

    @patch('takeover.aggregate_driver')
    @patch('takeover.relay_control')
    @patch('takeover.TempBatteryService')
    @patch('takeover.time')
    def test_success_opens_relay_only_after_bms_confirmed(self, mtime, MockTBS, mrelay, magg):
        mtime.sleep = MagicMock()
        magg.stop.return_value = True
        mrelay.open_relay.return_value = True
        mrelay.verify_relay_open.return_value = True
        MockTBS.return_value = MagicMock(**{"register.return_value": True})
        monitor = MockMonitor(battery_service="com.victronenergy.battery/277", bms_instance=-1)
        # Record call order across the monitor + relay to prove ordering.
        order = []
        monitor.wait_for_bms_selection = MagicMock(side_effect=lambda *a, **k: order.append("bms") or True)
        mrelay.open_relay.side_effect = lambda *a, **k: order.append("open") or True

        t = self._make(monitor)
        ok = t.hand_off_in(safe_voltage=28.4, target_voltage=31.5)

        self.assertTrue(ok)
        # The relay must open only AFTER the BMS selection is confirmed.
        self.assertEqual(order, ["bms", "open"])

    @patch('takeover.aggregate_driver')
    @patch('takeover.relay_control')
    @patch('takeover.TempBatteryService')
    @patch('takeover.time')
    def test_success_persists_snapshot_of_real_originals(self, mtime, MockTBS, mrelay, magg):
        mtime.sleep = MagicMock()
        magg.stop.return_value = True
        mrelay.open_relay.return_value = True
        mrelay.verify_relay_open.return_value = True
        MockTBS.return_value = MagicMock(**{"register.return_value": True})
        monitor = MockMonitor(battery_service="com.victronenergy.battery/277", bms_instance=-1)
        monitor.get_dvcc_max_charge_voltage = MagicMock(return_value=32.0)

        t = self._make(monitor)
        t.hand_off_in(safe_voltage=28.4, target_voltage=31.5)

        snap = takeover.load_originals()
        self.assertEqual(snap["battery_service"], "com.victronenergy.battery/277")
        self.assertEqual(snap["bms_instance"], -1)
        self.assertEqual(snap["max_charge_voltage"], 32.0)

    @patch('takeover.aggregate_driver')
    @patch('takeover.relay_control')
    @patch('takeover.TempBatteryService')
    @patch('takeover.time')
    def test_aggregate_stop_failure_tears_down_and_returns_false(self, mtime, MockTBS, mrelay, magg):
        mtime.sleep = MagicMock()
        magg.stop.return_value = False  # aggregate stop fails
        MockTBS.return_value = MagicMock(**{"register.return_value": True})
        monitor = MockMonitor()
        t = self._make(monitor)
        ok = t.hand_off_in(safe_voltage=28.4, target_voltage=31.5)
        self.assertFalse(ok)
        self.assertTrue(self.alerting.raise_alarm.called)

    @patch('takeover.aggregate_driver')
    @patch('takeover.relay_control')
    @patch('takeover.TempBatteryService')
    @patch('takeover.time')
    def test_temp_register_failure_returns_false_before_stopping_aggregate(self, mtime, MockTBS, mrelay, magg):
        mtime.sleep = MagicMock()
        MockTBS.return_value = MagicMock(**{"register.return_value": False})
        monitor = MockMonitor()
        t = self._make(monitor)
        ok = t.hand_off_in(safe_voltage=28.4, target_voltage=31.5)
        self.assertFalse(ok)
        magg.stop.assert_not_called()


if __name__ == '__main__':
    unittest.main()
