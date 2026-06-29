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


class TestTeardown(unittest.TestCase):
    def setUp(self):
        self._tmp = os.path.join(os.path.dirname(__file__), "_snap_td.json")
        p = patch.object(takeover, "SNAPSHOT_FILE", self._tmp); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: os.path.exists(self._tmp) and os.unlink(self._tmp))
        self.alerting = MagicMock()
        self.status = MockStatus()

    def _make(self, monitor):
        t = takeover.Takeover(monitor, self.status, self.alerting, "fla-equalisation", _states())
        t.temp_service = MagicMock()
        t._aggregate_stopped = True
        t._originals = {"battery_service": "com.victronenergy.battery/277",
                        "bms_instance": -1, "max_charge_voltage": 32.0}
        return t

    @patch('takeover.aggregate_driver')
    @patch('takeover.release_lock')
    def test_relay_closed_restores_from_snapshot_and_releases(self, mrelease, magg):
        monitor = MockMonitor(relay_state=1)
        recorded = {}
        monitor.set_battery_service_setting = MagicMock(side_effect=lambda v: recorded.update(bs=v) or True)
        monitor.set_bms_instance = MagicMock(side_effect=lambda v: recorded.update(bms=v) or True)
        monitor.set_dvcc_max_charge_voltage = MagicMock(side_effect=lambda v: recorded.update(cvl=v) or True)
        t = self._make(monitor)
        takeover.save_originals("com.victronenergy.battery/277", -1, 32.0)
        svc = t.temp_service

        t.teardown()

        self.assertEqual(recorded["bs"], "com.victronenergy.battery/277")  # NOT aggregate
        self.assertEqual(recorded["bms"], -1)
        self.assertEqual(recorded["cvl"], 32.0)  # NOT 28.4
        svc.deregister.assert_called_once()
        magg.start.assert_called_once()
        mrelease.assert_called_once()
        self.assertIsNone(takeover.load_originals())  # snapshot deleted

    @patch('takeover.aggregate_driver')
    @patch('takeover.release_lock')
    def test_relay_open_holds_and_does_not_restore_or_release(self, mrelease, magg):
        monitor = MockMonitor(relay_state=0)
        t = self._make(monitor)
        takeover.save_originals("com.victronenergy.battery/277", -1, 32.0)

        t.teardown()

        t.temp_service.deregister.assert_not_called()
        mrelease.assert_not_called()
        magg.start.assert_not_called()
        self.assertTrue(self.alerting.raise_alarm.called)
        self.assertIsNotNone(takeover.load_originals())  # snapshot KEPT for resume
        self.assertFalse(t._torn_down)

    @patch('takeover.aggregate_driver')
    @patch('takeover.release_lock')
    def test_relay_closed_uses_persisted_snapshot_when_originals_none(self, mrelease, magg):
        # Resume case: self._originals is None; teardown must read the snapshot.
        monitor = MockMonitor(relay_state=1)
        recorded = {}
        monitor.set_battery_service_setting = MagicMock(side_effect=lambda v: recorded.update(bs=v) or True)
        t = self._make(monitor)
        t._originals = None
        takeover.save_originals("com.victronenergy.battery/277", -1, 32.0)

        t.teardown()
        self.assertEqual(recorded["bs"], "com.victronenergy.battery/277")

    @patch('takeover.aggregate_driver')
    @patch('takeover.release_lock')
    def test_does_not_clear_alarm(self, mrelease, magg):
        # teardown must NOT clear the alarm — a failure path raised one before
        # calling teardown, and clearing it here would hide the failure.
        monitor = MockMonitor(relay_state=1)
        t = self._make(monitor)
        takeover.save_originals("com.victronenergy.battery/277", -1, 32.0)
        t.teardown()
        self.alerting.clear_alarm.assert_not_called()

    @patch('takeover.aggregate_driver')
    @patch('takeover.release_lock')
    def test_relay_open_then_closed_restores_on_second_call(self, mrelease, magg):
        # First call: relay open → hold (no restore, _torn_down stays False).
        # Second call: relay now closed → restores from snapshot and releases.
        monitor = MockMonitor(relay_state=0)
        t = self._make(monitor)
        takeover.save_originals("com.victronenergy.battery/277", -1, 32.0)

        t.teardown()

        self.assertFalse(t._torn_down)
        mrelease.assert_not_called()
        self.assertTrue(self.alerting.raise_alarm.called)

        # Flip relay to closed and retry.
        monitor._relay_state = 1
        recorded = {}
        monitor.set_battery_service_setting = MagicMock(side_effect=lambda v: recorded.update(bs=v) or True)
        monitor.set_bms_instance = MagicMock(side_effect=lambda v: recorded.update(bms=v) or True)
        monitor.set_dvcc_max_charge_voltage = MagicMock(side_effect=lambda v: recorded.update(cvl=v) or True)

        t.teardown()

        self.assertEqual(recorded["bs"], "com.victronenergy.battery/277")
        self.assertEqual(recorded["bms"], -1)
        self.assertEqual(recorded["cvl"], 32.0)
        mrelease.assert_called_once()
        self.assertTrue(t._torn_down)

    @patch('takeover.aggregate_driver')
    @patch('takeover.release_lock')
    def test_second_teardown_is_noop(self, mrelease, magg):
        # The service finally re-calls teardown after a completed hand_back;
        # the second call must do nothing (no double release / restore).
        monitor = MockMonitor(relay_state=1)
        t = self._make(monitor)
        takeover.save_originals("com.victronenergy.battery/277", -1, 32.0)
        t.teardown()
        t.teardown()
        mrelease.assert_called_once()
        magg.start.assert_called_once()


class TestHandBack(unittest.TestCase):
    def setUp(self):
        self._tmp = os.path.join(os.path.dirname(__file__), "_snap_hb.json")
        p = patch.object(takeover, "SNAPSHOT_FILE", self._tmp); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: os.path.exists(self._tmp) and os.unlink(self._tmp))
        self.alerting = MagicMock()
        self.status = MockStatus()

    def _make(self, monitor):
        t = takeover.Takeover(monitor, self.status, self.alerting, "fla-equalisation", _states())
        t.temp_service = MagicMock()
        t._aggregate_stopped = True
        t._originals = {"battery_service": "com.victronenergy.battery/277",
                        "bms_instance": -1, "max_charge_voltage": 32.0}
        return t

    @patch('takeover.aggregate_driver')
    @patch('takeover.release_lock')
    @patch('takeover.relay_control')
    @patch('takeover.voltage_matching')
    def test_converged_closes_and_tears_down(self, mvm, mrelay, mrelease, magg):
        mvm.wait_for_match.return_value = (True, 0.2)
        mrelay.close_relay_verified.side_effect = lambda m: setattr(m, '_relay_state', 1) or True
        monitor = MockMonitor(relay_state=0)  # relay starts open; relay_control is fully mocked so the value is inert
        t = self._make(monitor)
        matched, delta = t.hand_back(float_voltage=27.0, voltage_delta_max=1.0)
        self.assertTrue(matched)
        mrelay.close_relay_verified.assert_called_once()
        mrelease.assert_called_once()  # teardown ran (relay closed)

    @patch('takeover.aggregate_driver')
    @patch('takeover.release_lock')
    @patch('takeover.relay_control')
    @patch('takeover.voltage_matching')
    def test_restores_ceiling_from_snapshot_before_matching(self, mvm, mrelay, mrelease, magg):
        # side_effect asserts the ceiling is already in recorded when wait_for_match runs,
        # proving the restore happens BEFORE matching (relay still open — safe).
        mvm.wait_for_match.side_effect = lambda *a, **k: (self.assertIn(32.0, recorded), (True, 0.2))[1]
        mrelay.close_relay_verified.return_value = True
        monitor = MockMonitor(relay_state=1)
        recorded = []
        monitor.set_dvcc_max_charge_voltage = MagicMock(side_effect=lambda v: recorded.append(v) or True)
        t = self._make(monitor)
        t.hand_back(float_voltage=27.0, voltage_delta_max=1.0)
        self.assertIn(32.0, recorded)  # ceiling restored to the snapshot value

    @patch('takeover.aggregate_driver')
    @patch('takeover.release_lock')
    @patch('takeover.relay_control')
    @patch('takeover.voltage_matching')
    def test_not_matched_does_not_close_or_teardown(self, mvm, mrelay, mrelease, magg):
        mvm.wait_for_match.return_value = (False, 2.0)  # bounded non-convergence (test only)
        monitor = MockMonitor(relay_state=0)
        t = self._make(monitor)
        matched, delta = t.hand_back(float_voltage=27.0, voltage_delta_max=1.0)
        self.assertFalse(matched)
        mrelay.close_relay_verified.assert_not_called()
        mrelease.assert_not_called()

    @patch('takeover.aggregate_driver')
    @patch('takeover.release_lock')
    @patch('takeover.relay_control')
    @patch('takeover.voltage_matching')
    def test_close_failure_raises_alarm_and_no_teardown(self, mvm, mrelay, mrelease, magg):
        mvm.wait_for_match.return_value = (True, 0.3)
        mrelay.close_relay_verified.return_value = False   # close fails
        monitor = MockMonitor(relay_state=0)
        t = self._make(monitor)
        matched, delta = t.hand_back(float_voltage=27.0, voltage_delta_max=1.0)
        self.assertFalse(matched)
        self.alerting.raise_alarm.assert_called_once()
        mrelease.assert_not_called()   # teardown must not run


if __name__ == '__main__':
    unittest.main()
