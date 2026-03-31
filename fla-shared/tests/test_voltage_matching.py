"""Tests for voltage_matching.wait_for_match."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))
from helpers import dbus_mock_setup, MockMonitor, MockStatus

dbus_mock_setup()

from voltage_matching import wait_for_match


class TestWaitForMatch(unittest.TestCase):
    """Tests for the voltage matching loop."""

    def setUp(self):
        self.temp_service = MagicMock()
        self.alerting_mod = MagicMock()
        self.status = MockStatus()

    # 1. Immediate convergence
    @patch('voltage_matching.time')
    def test_immediate_convergence(self, mock_time):
        mock_time.time.side_effect = [0, 5]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.3, lfp_voltage=27.0)

        ok, delta = wait_for_match(monitor, self.temp_service, self.status,
                                   self.alerting_mod)

        self.assertTrue(ok)
        self.assertAlmostEqual(delta, 0.3)
        mock_time.sleep.assert_not_called()

    # 2. Converges after iterations
    @patch('voltage_matching.time')
    def test_converges_after_iterations(self, mock_time):
        mock_time.time.side_effect = [0, 35, 70, 105]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor()
        monitor.get_trojan_voltage = MagicMock(
            side_effect=[29.0, 28.2, 27.2])
        monitor.get_lfp_voltage = MagicMock(
            side_effect=[27.0, 27.0, 27.0])

        ok, delta = wait_for_match(monitor, self.temp_service, self.status,
                                   self.alerting_mod)

        self.assertTrue(ok)
        self.assertAlmostEqual(delta, 0.2)

    # 3. Timeout returns false
    @patch('voltage_matching.time')
    def test_timeout_returns_false(self, mock_time):
        # First call: match_start. Second call: elapsed check inside loop.
        # The elapsed (14401) > match_timeout (14400), but delta (2.0) >= 1.0,
        # so it hits the timeout branch.
        mock_time.time.side_effect = [0, 14401]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=28.0, lfp_voltage=26.0)

        ok, delta = wait_for_match(monitor, self.temp_service, self.status,
                                   self.alerting_mod)

        self.assertFalse(ok)
        self.assertAlmostEqual(delta, 2.0)

    # 4. Trojan None returns false
    @patch('voltage_matching.time')
    def test_trojan_none_returns_false(self, mock_time):
        mock_time.time.side_effect = [0, 5]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=None)

        ok, delta = wait_for_match(monitor, self.temp_service, self.status,
                                   self.alerting_mod)

        self.assertFalse(ok)
        self.assertIsNone(delta)

    # 5. Float voltage set
    @patch('voltage_matching.time')
    def test_float_voltage_set(self, mock_time):
        mock_time.time.side_effect = [0, 5]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.3, lfp_voltage=27.0)

        wait_for_match(monitor, self.temp_service, self.status,
                       self.alerting_mod, float_voltage=27.0)

        self.temp_service.set_charge_voltage.assert_called_once_with(27.0)

    # 6. No float voltage skips set_charge_voltage
    @patch('voltage_matching.time')
    def test_no_float_voltage_skips(self, mock_time):
        mock_time.time.side_effect = [0, 5]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.3, lfp_voltage=27.0)

        wait_for_match(monitor, self.temp_service, self.status,
                       self.alerting_mod, float_voltage=None)

        self.temp_service.set_charge_voltage.assert_not_called()

    # 7. Status updated
    @patch('voltage_matching.time')
    def test_status_updated(self, mock_time):
        mock_time.time.side_effect = [0, 5]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.3, lfp_voltage=27.0)

        wait_for_match(monitor, self.temp_service, self.status,
                       self.alerting_mod)

        self.assertTrue(len(self.status.updates) > 0)
        update = self.status.updates[-1]
        self.assertIn('time_remaining', update)
        self.assertIn('trojan_v', update)
        self.assertIn('lfp_v', update)
        self.assertAlmostEqual(update['trojan_v'], 27.3)
        self.assertAlmostEqual(update['lfp_v'], 27.0)

    # 8. Cache callback called
    @patch('voltage_matching.time')
    def test_cache_callback_called(self, mock_time):
        mock_time.time.side_effect = [0, 5]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.3, lfp_voltage=27.0)
        callback = MagicMock()

        wait_for_match(monitor, self.temp_service, self.status,
                       self.alerting_mod, cache_callback=callback)

        callback.assert_called_once()
        kwargs = callback.call_args[1]
        self.assertAlmostEqual(kwargs['trojan_v'], 27.3)
        self.assertAlmostEqual(kwargs['lfp_v'], 27.0)
        self.assertAlmostEqual(kwargs['voltage_delta'], 0.3)
        self.assertIn('time_remaining', kwargs)

    # 9. Alarm on timeout
    @patch('voltage_matching.time')
    def test_alarm_on_timeout(self, mock_time):
        mock_time.time.side_effect = [0, 14401]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=28.0, lfp_voltage=26.0)

        wait_for_match(monitor, self.temp_service, self.status,
                       self.alerting_mod)

        self.alerting_mod.raise_alarm.assert_called_once()

    # 10. Alarm on SmartShunt unresponsive
    @patch('voltage_matching.time')
    def test_alarm_on_smartshunt_unresponsive(self, mock_time):
        mock_time.time.side_effect = [0, 5]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=None)

        wait_for_match(monitor, self.temp_service, self.status,
                       self.alerting_mod)

        self.alerting_mod.raise_alarm.assert_called_once()


    # 11. Persistent LFP None raises alarm after 10 iterations
    @patch('voltage_matching.time')
    def test_lfp_none_persistent_raises_alarm(self, mock_time):
        """Persistent LFP voltage=None should alarm after 10 reads, not wait full timeout."""
        # 12 time.time() calls: 1 for match_start + 11 for iterations (triggers at count=10)
        mock_time.time.side_effect = [0] + [i * 35 for i in range(1, 13)]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.0, lfp_voltage=None)

        matched, delta = wait_for_match(
            monitor, self.temp_service, self.status, self.alerting_mod,
            voltage_delta_max=1.0, timeout_hours=4.0,
        )
        self.assertFalse(matched)
        self.alerting_mod.raise_alarm.assert_called_once()

    # 12. Transient LFP None resets counter
    @patch('voltage_matching.time')
    def test_lfp_none_transient_resets_counter(self, mock_time):
        """LFP returning after a few None reads should reset the counter and converge."""
        # Need enough time values: 1 for match_start + 2 per iteration (elapsed check + sleep)
        # 3 None iterations + 1 convergence = 4 iterations, ~8 time.time() calls
        mock_time.time.side_effect = [0] + [i * 35 for i in range(1, 10)]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.0)
        # 3 None reads then a valid read that converges
        lfp_values = [None, None, None, 27.0]
        call_count = [0]
        def lfp_side_effect():
            idx = min(call_count[0], len(lfp_values) - 1)
            call_count[0] += 1
            return lfp_values[idx]
        monitor.get_lfp_voltage = lfp_side_effect

        matched, delta = wait_for_match(
            monitor, self.temp_service, self.status, self.alerting_mod,
            voltage_delta_max=1.0, timeout_hours=4.0,
        )
        self.assertTrue(matched)
        self.alerting_mod.raise_alarm.assert_not_called()


if __name__ == '__main__':
    unittest.main()
