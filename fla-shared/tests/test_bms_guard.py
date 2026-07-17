"""Tests for the idle DVCC-BMS-selection guard (takeover.verify_idle_bms_selection).

Regression guard for the 2026-05-28 event: DVCC's controlling BMS silently
drifted off the aggregate (instance 99, CCL 120A) onto a single JK pack (60A),
halving LFP charge for six weeks. While no FLA op holds the lock, BmsInstance
must be the aggregate; anything else raises an alarm.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from helpers import dbus_mock_setup, MockMonitor, MockStatus

dbus_mock_setup()
import takeover


class TestIdleBmsGuard(unittest.TestCase):
    def setUp(self):
        self.alerting = MagicMock()
        self.status = MockStatus()
        # Reset the per-episode dedup flag between tests.
        takeover._idle_bms_alarm_active = False
        # Default: no FLA operation active.
        p = patch.object(takeover, "lock_is_locked", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def test_aggregate_is_instance_99(self):
        self.assertEqual(takeover.AGGREGATE_INSTANCE, 99)

    def test_idle_off_aggregate_raises_alarm(self):
        monitor = MockMonitor(bms_instance=3)  # a single JK pack
        result = takeover.verify_idle_bms_selection(monitor, self.alerting, self.status)
        self.assertFalse(result)
        self.assertTrue(self.alerting.raise_alarm.called)

    def test_idle_on_aggregate_does_not_alarm(self):
        monitor = MockMonitor(bms_instance=99)
        result = takeover.verify_idle_bms_selection(monitor, self.alerting, self.status)
        self.assertTrue(result)
        self.alerting.raise_alarm.assert_not_called()

    def test_op_active_skips_check_and_never_alarms(self):
        # Lock held => an FLA op legitimately points DVCC at the temp battery (100).
        with patch.object(takeover, "lock_is_locked", return_value=True):
            monitor = MockMonitor(bms_instance=100)
            result = takeover.verify_idle_bms_selection(monitor, self.alerting, self.status)
        self.assertIsNone(result)
        self.alerting.raise_alarm.assert_not_called()

    def test_does_not_re_alarm_while_still_wrong(self):
        monitor = MockMonitor(bms_instance=4)
        takeover.verify_idle_bms_selection(monitor, self.alerting, self.status)
        takeover.verify_idle_bms_selection(monitor, self.alerting, self.status)
        self.assertEqual(self.alerting.raise_alarm.call_count, 1)

    def test_re_arms_after_recovery(self):
        wrong = MockMonitor(bms_instance=3)
        good = MockMonitor(bms_instance=99)
        takeover.verify_idle_bms_selection(wrong, self.alerting, self.status)   # alarm 1
        takeover.verify_idle_bms_selection(good, self.alerting, self.status)    # recover
        takeover.verify_idle_bms_selection(wrong, self.alerting, self.status)   # alarm 2
        self.assertEqual(self.alerting.raise_alarm.call_count, 2)


if __name__ == "__main__":
    unittest.main()
