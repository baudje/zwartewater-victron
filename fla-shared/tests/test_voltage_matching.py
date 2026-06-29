"""Tests for voltage_matching.wait_for_match (controlled hold-and-lower reconnect)."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))
from helpers import dbus_mock_setup, MockMonitor, MockStatus

dbus_mock_setup()

from voltage_matching import (
    wait_for_match, POLL_INTERVAL, SETTLE_TIMEOUT, MAX_NONE_CYCLES,
)


class TestWaitForMatch(unittest.TestCase):
    def setUp(self):
        self.temp_service = MagicMock()
        self.alerting_mod = MagicMock()
        self.status = MockStatus()

    # --- Convergence (the only production return) ---

    @patch('voltage_matching.time')
    def test_immediate_convergence_closes(self, mock_time):
        mock_time.time.side_effect = [0, 1]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.3, lfp_voltage=27.0)

        ok, delta = wait_for_match(monitor, self.temp_service, self.status,
                                   self.alerting_mod, float_voltage=27.0)

        self.assertTrue(ok)
        self.assertAlmostEqual(delta, 0.3)
        mock_time.sleep.assert_not_called()

    @patch('voltage_matching.time')
    def test_converges_after_descent(self, mock_time):
        mock_time.time.side_effect = [0, 2, 4, 6]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(lfp_voltage=27.0)
        monitor.get_trojan_voltage = MagicMock(side_effect=[29.0, 28.2, 27.2])

        ok, delta = wait_for_match(monitor, self.temp_service, self.status,
                                   self.alerting_mod, float_voltage=27.0)

        self.assertTrue(ok)
        self.assertAlmostEqual(delta, 0.2)

    # --- Re-pin float every cycle ---

    @patch('voltage_matching.time')
    def test_float_repinned_every_cycle(self, mock_time):
        mock_time.time.side_effect = [0, 2, 4, 6]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(lfp_voltage=27.0)
        monitor.get_trojan_voltage = MagicMock(side_effect=[29.0, 28.2, 27.0])

        wait_for_match(monitor, self.temp_service, self.status,
                       self.alerting_mod, float_voltage=27.0)

        # 3 cycles ran before convergence → float pinned 3 times, all to 27.0
        self.assertEqual(self.temp_service.set_charge_voltage.call_count, 3)
        for call in self.temp_service.set_charge_voltage.call_args_list:
            self.assertAlmostEqual(call.args[0], 27.0)

    @patch('voltage_matching.time')
    def test_no_float_voltage_skips_pin(self, mock_time):
        mock_time.time.side_effect = [0, 1]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.3, lfp_voltage=27.0)

        wait_for_match(monitor, self.temp_service, self.status,
                       self.alerting_mod, float_voltage=None)

        self.temp_service.set_charge_voltage.assert_not_called()

    # --- Never auto-close above threshold ---

    @patch('voltage_matching.time')
    def test_never_closes_above_threshold(self, mock_time):
        # Bus stuck 2V high for the whole bounded run — must never converge.
        mock_time.time.side_effect = [0] + [i * POLL_INTERVAL for i in range(1, 12)]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=29.0, lfp_voltage=27.0)  # delta 2.0

        ok, delta = wait_for_match(monitor, self.temp_service, self.status,
                                   self.alerting_mod, float_voltage=27.0,
                                   max_cycles=8)

        self.assertFalse(ok)              # bounded by max_cycles, never converged
        self.assertAlmostEqual(delta, 2.0)
        # Relay close is the caller's job and only after (True, _) — assert the
        # loop never told the temp service to do anything but hold float.
        self.assertNotIn(1, monitor._relay_set_calls)

    # --- SETTLE_TIMEOUT debounce ---

    @patch('voltage_matching.time')
    def test_transient_undershoot_does_not_alarm(self, mock_time):
        # Non-converged but still inside the settle window → no alarm yet.
        mock_time.time.side_effect = [0, SETTLE_TIMEOUT - 10, SETTLE_TIMEOUT - 8]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=29.0, lfp_voltage=27.0)

        wait_for_match(monitor, self.temp_service, self.status,
                       self.alerting_mod, float_voltage=27.0, max_cycles=2)

        self.alerting_mod.raise_alarm.assert_not_called()

    @patch('voltage_matching.time')
    def test_sustained_nonconvergence_enters_safe_hold(self, mock_time):
        # Past SETTLE_TIMEOUT and still 2V high → safe-hold + alarm, float still pinned.
        mock_time.time.side_effect = [0, SETTLE_TIMEOUT + 1, SETTLE_TIMEOUT + 3]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=29.0, lfp_voltage=27.0)

        ok, _ = wait_for_match(monitor, self.temp_service, self.status,
                               self.alerting_mod, float_voltage=27.0, max_cycles=2)

        self.assertFalse(ok)
        self.alerting_mod.raise_alarm.assert_called()      # safe-hold alarm
        self.temp_service.set_charge_voltage.assert_called_with(27.0)  # still holding
        self.assertNotIn(1, monitor._relay_set_calls)

    # --- None-shunt safe-hold ---

    @patch('voltage_matching.time')
    def test_lfp_none_never_closes_blind(self, mock_time):
        mock_time.time.side_effect = [0] + [i * POLL_INTERVAL for i in range(1, 6)]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.0, lfp_voltage=None)

        ok, _ = wait_for_match(monitor, self.temp_service, self.status,
                               self.alerting_mod, float_voltage=27.0, max_cycles=4)

        self.assertFalse(ok)
        self.assertNotIn(1, monitor._relay_set_calls)

    @patch('voltage_matching.time')
    def test_sustained_none_enters_safe_hold(self, mock_time):
        mock_time.time.side_effect = [0] + [i * POLL_INTERVAL for i in range(1, MAX_NONE_CYCLES + 3)]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.0, lfp_voltage=None)

        wait_for_match(monitor, self.temp_service, self.status,
                       self.alerting_mod, float_voltage=27.0,
                       max_cycles=MAX_NONE_CYCLES + 1)

        self.alerting_mod.raise_alarm.assert_called()

    @patch('voltage_matching.time')
    def test_transient_none_resets_and_converges(self, mock_time):
        mock_time.time.side_effect = [0, 2, 4, 6, 8]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.0)
        monitor.get_lfp_voltage = MagicMock(side_effect=[None, None, None, 27.0])

        ok, _ = wait_for_match(monitor, self.temp_service, self.status,
                               self.alerting_mod, float_voltage=27.0)

        self.assertTrue(ok)
        self.alerting_mod.raise_alarm.assert_not_called()

    # --- Status / cache plumbing preserved ---

    @patch('voltage_matching.time')
    def test_status_and_cache_updated(self, mock_time):
        mock_time.time.side_effect = [0, 1]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.3, lfp_voltage=27.0)
        cb = MagicMock()

        wait_for_match(monitor, self.temp_service, self.status,
                       self.alerting_mod, float_voltage=27.0, cache_callback=cb)

        self.assertTrue(self.status.updates)
        last = self.status.updates[-1]
        self.assertAlmostEqual(last['trojan_v'], 27.3)
        self.assertAlmostEqual(last['lfp_v'], 27.0)
        cb.assert_called_once()
        self.assertAlmostEqual(cb.call_args[1]['voltage_delta'], 0.3)

    @patch('voltage_matching.time')
    def test_timeout_hours_is_ignored(self, mock_time):
        # Back-compat shim: callers still pass timeout_hours; it must be accepted.
        mock_time.time.side_effect = [0, 1]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.2, lfp_voltage=27.0)

        ok, _ = wait_for_match(monitor, self.temp_service, self.status,
                               self.alerting_mod, float_voltage=27.0, timeout_hours=4.0)

        self.assertTrue(ok)


if __name__ == '__main__':
    unittest.main()
