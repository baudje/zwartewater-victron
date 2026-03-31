"""Tests for relay_control.py — relay open/close with safety verification."""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))
from helpers import MockMonitor, MockStatus

import relay_control


@patch('relay_control.time')
class TestOpenRelay(unittest.TestCase):

    def test_success(self, mock_time):
        monitor = MockMonitor(relay_state=1)
        result = relay_control.open_relay(monitor)
        self.assertTrue(result)
        self.assertIn(0, monitor._relay_set_calls)

    def test_failure(self, mock_time):
        monitor = MockMonitor(relay_state=1)
        monitor.set_relay = lambda state: False
        result = relay_control.open_relay(monitor)
        self.assertFalse(result)


@patch('relay_control.time')
class TestVerifyRelayOpen(unittest.TestCase):

    def test_low_current_verifies(self, mock_time):
        monitor = MockMonitor(lfp_current=2.0)
        result = relay_control.verify_relay_open(monitor)
        self.assertTrue(result)

    def test_high_current_fails(self, mock_time):
        monitor = MockMonitor(lfp_current=20.0)
        result = relay_control.verify_relay_open(monitor)
        self.assertFalse(result)

    def test_none_current_passes(self, mock_time):
        monitor = MockMonitor(lfp_current=None)
        result = relay_control.verify_relay_open(monitor)
        self.assertTrue(result)

    def test_sleep_called(self, mock_time):
        monitor = MockMonitor(lfp_current=0.0)
        relay_control.verify_relay_open(monitor, wait_seconds=15)
        mock_time.sleep.assert_called_with(15)


@patch('relay_control.time')
class TestCloseRelayVerified(unittest.TestCase):

    def test_success_readback(self, mock_time):
        monitor = MockMonitor(relay_state=0)
        result = relay_control.close_relay_verified(monitor)
        self.assertTrue(result)
        self.assertIn(1, monitor._relay_set_calls)

    def test_command_fails(self, mock_time):
        monitor = MockMonitor(relay_state=0)
        monitor.set_relay = lambda state: False
        result = relay_control.close_relay_verified(monitor)
        self.assertFalse(result)

    def test_readback_fails(self, mock_time):
        monitor = MockMonitor(relay_state=0)
        # Override get_relay_state to always return 0 (open) even after set_relay(1)
        original_set_relay = monitor.set_relay
        def set_relay_no_state_change(state):
            monitor._relay_set_calls.append(state)
            return True  # Command succeeds but state doesn't change
        monitor.set_relay = set_relay_no_state_change
        result = relay_control.close_relay_verified(monitor)
        self.assertFalse(result)

    def test_sleep_called(self, mock_time):
        monitor = MockMonitor(relay_state=0)
        relay_control.close_relay_verified(monitor)
        mock_time.sleep.assert_called_with(2)


@patch('relay_control.time')
class TestCloseRelayDeltaAware(unittest.TestCase):

    def test_already_closed_no_action(self, mock_time):
        monitor = MockMonitor(relay_state=1)
        relay_control.close_relay_delta_aware(monitor)
        self.assertEqual(monitor._relay_set_calls, [])

    def test_safe_delta_closes(self, mock_time):
        monitor = MockMonitor(relay_state=0, trojan_voltage=27.5, lfp_voltage=27.2)
        relay_control.close_relay_delta_aware(monitor)
        self.assertIn(1, monitor._relay_set_calls)

    def test_unsafe_delta_does_not_close(self, mock_time):
        monitor = MockMonitor(relay_state=0, trojan_voltage=28.5, lfp_voltage=27.0)
        relay_control.close_relay_delta_aware(monitor)
        self.assertEqual(monitor._relay_set_calls, [])

    def test_unsafe_delta_raises_alarm(self, mock_time):
        monitor = MockMonitor(relay_state=0, trojan_voltage=28.5, lfp_voltage=27.0)
        alerting_mod = MagicMock()
        status = MockStatus()
        relay_control.close_relay_delta_aware(monitor, alerting_mod=alerting_mod, status=status)
        alerting_mod.raise_alarm.assert_called_once()

    def test_exact_boundary_closes(self, mock_time):
        # delta = 1.0 exactly, NOT > 1.0, so relay closes
        monitor = MockMonitor(relay_state=0, trojan_voltage=28.0, lfp_voltage=27.0)
        relay_control.close_relay_delta_aware(monitor)
        self.assertIn(1, monitor._relay_set_calls)

    def test_unreadable_voltages_raises_alarm_and_does_not_close(self, mock_time):
        """Unreadable voltages: do NOT close relay, raise alarm."""
        monitor = MockMonitor(relay_state=0, trojan_voltage=None, lfp_voltage=27.0)
        alerting_mod = MagicMock()
        status = MockStatus()
        relay_control.close_relay_delta_aware(monitor, alerting_mod=alerting_mod, status=status)
        self.assertEqual(monitor._relay_set_calls, [])
        alerting_mod.raise_alarm.assert_called_once()

    def test_one_voltage_none_raises_alarm_and_does_not_close(self, mock_time):
        """One voltage unreadable: do NOT close relay, raise alarm."""
        monitor = MockMonitor(relay_state=0, trojan_voltage=27.5, lfp_voltage=None)
        alerting_mod = MagicMock()
        status = MockStatus()
        relay_control.close_relay_delta_aware(monitor, alerting_mod=alerting_mod, status=status)
        self.assertEqual(monitor._relay_set_calls, [])
        alerting_mod.raise_alarm.assert_called_once()

    def test_no_alerting_mod_no_crash(self, mock_time):
        monitor = MockMonitor(relay_state=0, trojan_voltage=28.5, lfp_voltage=27.0)
        # Should not raise even without alerting_mod/status
        relay_control.close_relay_delta_aware(monitor, alerting_mod=None, status=None)
        self.assertEqual(monitor._relay_set_calls, [])


@patch('relay_control.time')
class TestVerifyRelayStillOpen(unittest.TestCase):

    def test_relay_open_returns_true(self, mock_time):
        monitor = MockMonitor(relay_state=0)
        result = relay_control.verify_relay_still_open(monitor, current_cvl=31.5)
        self.assertTrue(result)

    def test_relay_closed_high_cvl_false(self, mock_time):
        monitor = MockMonitor(relay_state=1)
        result = relay_control.verify_relay_still_open(monitor, current_cvl=31.5)
        self.assertFalse(result)

    def test_relay_closed_safe_cvl_true(self, mock_time):
        monitor = MockMonitor(relay_state=1)
        result = relay_control.verify_relay_still_open(monitor, current_cvl=27.0)
        self.assertTrue(result)

    def test_relay_closed_boundary_cvl_true(self, mock_time):
        # cvl=28.4 is NOT > 28.4, so returns True
        monitor = MockMonitor(relay_state=1)
        result = relay_control.verify_relay_still_open(monitor, current_cvl=28.4)
        self.assertTrue(result)

    def test_relay_closed_above_boundary_false(self, mock_time):
        monitor = MockMonitor(relay_state=1)
        result = relay_control.verify_relay_still_open(monitor, current_cvl=28.5)
        self.assertFalse(result)


@patch('relay_control.time')
class TestStartupSafetyCheck(unittest.TestCase):

    def test_relay_closed_no_action(self, mock_time):
        monitor = MockMonitor(relay_state=1)
        relay_control.startup_safety_check(monitor)
        self.assertEqual(monitor._relay_set_calls, [])

    def test_open_safe_delta_closes(self, mock_time):
        monitor = MockMonitor(relay_state=0, trojan_voltage=27.5, lfp_voltage=27.2)
        relay_control.startup_safety_check(monitor)
        self.assertIn(1, monitor._relay_set_calls)

    def test_open_unsafe_delta_raises_alarm(self, mock_time):
        monitor = MockMonitor(relay_state=0, trojan_voltage=29.0, lfp_voltage=27.0)
        alerting_mod = MagicMock()
        status = MockStatus()
        relay_control.startup_safety_check(monitor, status=status, alerting_mod=alerting_mod)
        alerting_mod.raise_alarm.assert_called_once()
        self.assertEqual(monitor._relay_set_calls, [])

    def test_unreadable_voltages_raises_alarm(self, mock_time):
        monitor = MockMonitor(relay_state=0, trojan_voltage=None, lfp_voltage=27.0)
        alerting_mod = MagicMock()
        status = MockStatus()
        relay_control.startup_safety_check(monitor, status=status, alerting_mod=alerting_mod)
        alerting_mod.raise_alarm.assert_called_once()

    def test_no_alerting_no_crash(self, mock_time):
        monitor = MockMonitor(relay_state=0, trojan_voltage=29.0, lfp_voltage=27.0)
        # Should not raise even without alerting_mod/status
        relay_control.startup_safety_check(monitor, status=None, alerting_mod=None)

    def test_sleep_after_close(self, mock_time):
        monitor = MockMonitor(relay_state=0, trojan_voltage=27.5, lfp_voltage=27.2)
        relay_control.startup_safety_check(monitor)
        mock_time.sleep.assert_called_with(3)


if __name__ == '__main__':
    unittest.main()
