#!/usr/bin/env python3
"""Test harness for FLA charge service.

Mocks D-Bus calls so tests run on any machine, not just Venus OS.
Tests cover scheduling logic, safety guards, and state transitions.
"""

import os
import sys
import unittest
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project and shared modules to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'fla-shared'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'fla-shared', 'tests'))

from helpers import dbus_mock_setup, MockMonitor, MockStatus
dbus_mock_setup()

from fla_charge import (
    should_run, read_last_charge, write_last_charge,
    run_charge, LAST_CHARGE_FILE,
)
from dbus_status_service import (
    STATE_IDLE, STATE_PHASE1_SHARED, STATE_STOPPING_DRIVER,
    STATE_DISCONNECTING, STATE_PHASE2_BULK, STATE_PHASE3_ABSORPTION,
    STATE_VOLTAGE_MATCHING, STATE_RECONNECTING,
    STATE_RESTARTING_DRIVER, STATE_ERROR,
)


class MockChargeSettings:
    """Mock Settings for charge service with all configurable properties."""

    def __init__(self, **kwargs):
        self.enabled = kwargs.get('enabled', True)
        self.trojan_soc_trigger = kwargs.get('trojan_soc_trigger', 85)
        self.lfp_soc_transition = kwargs.get('lfp_soc_transition', 95)
        self.lfp_cell_voltage_disconnect = kwargs.get('lfp_cell_voltage_disconnect', 3.50)
        self.current_taper_threshold = kwargs.get('current_taper_threshold', 20.0)
        self.fla_bulk_voltage = kwargs.get('fla_bulk_voltage', 29.64)
        self.fla_absorption_complete_current = kwargs.get('fla_absorption_complete_current', 10.0)
        self.fla_absorption_max_hours = kwargs.get('fla_absorption_max_hours', 4.0)
        self.fla_float_voltage = kwargs.get('fla_float_voltage', 27.0)
        self.voltage_delta_max = kwargs.get('voltage_delta_max', 1.0)
        self.voltage_match_timeout_hours = kwargs.get('voltage_match_timeout_hours', 4.0)
        self.phase1_timeout_hours = kwargs.get('phase1_timeout_hours', 8.0)
        self.run_now = kwargs.get('run_now', False)
        self._cleared_run_now = False

    def clear_run_now(self):
        self._cleared_run_now = True
        self.run_now = False

    def _write(self, key, value):
        setattr(self, key, value)


class TestShouldRun(unittest.TestCase):
    """Test should_run() scheduling logic."""

    def setUp(self):
        self.settings = MockChargeSettings()
        self.monitor = MockMonitor()

    def test_disabled_returns_false(self):
        self.settings.enabled = False
        with patch('fla_charge.is_ac_available', return_value=True):
            self.assertFalse(should_run(self.settings, self.monitor))

    def test_trojan_soc_above_trigger_returns_false(self):
        self.monitor._trojan_soc = 90.0
        with patch('fla_charge.is_ac_available', return_value=True):
            self.assertFalse(should_run(self.settings, self.monitor))

    def test_trojan_soc_below_trigger_with_ac_returns_true(self):
        self.monitor._trojan_soc = 80.0
        with patch('fla_charge.is_ac_available', return_value=True):
            self.assertTrue(should_run(self.settings, self.monitor))

    def test_trojan_soc_none_returns_false(self):
        self.monitor._trojan_soc = None
        with patch('fla_charge.is_ac_available', return_value=True):
            self.assertFalse(should_run(self.settings, self.monitor))

    def test_no_ac_returns_false(self):
        self.monitor._trojan_soc = 80.0
        with patch('fla_charge.is_ac_available', return_value=False):
            self.assertFalse(should_run(self.settings, self.monitor))

    def test_run_now_bypasses_soc_trigger(self):
        self.settings.run_now = True
        self.monitor._trojan_soc = 90.0  # Above trigger
        with patch('fla_charge.is_ac_available', return_value=True):
            self.assertTrue(should_run(self.settings, self.monitor))

    def test_run_now_still_requires_ac(self):
        self.settings.run_now = True
        self.monitor._trojan_soc = 90.0
        with patch('fla_charge.is_ac_available', return_value=False):
            self.assertFalse(should_run(self.settings, self.monitor))

    def test_run_now_not_consumed_when_no_ac(self):
        """RunNow should NOT be cleared if AC is unavailable."""
        self.settings.run_now = True
        self.monitor._trojan_soc = 90.0
        with patch('fla_charge.is_ac_available', return_value=False):
            result = should_run(self.settings, self.monitor)
        self.assertFalse(result)
        self.assertFalse(self.settings._cleared_run_now)

    def test_run_now_clears_flag(self):
        self.settings.run_now = True
        self.monitor._trojan_soc = 90.0
        with patch('fla_charge.is_ac_available', return_value=True):
            should_run(self.settings, self.monitor)
        self.assertTrue(self.settings._cleared_run_now)


class TestLastCharge(unittest.TestCase):
    """Test read/write of last charge timestamp."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
        self.tmpfile.close()

    def tearDown(self):
        try:
            os.unlink(self.tmpfile.name)
        except OSError:
            pass

    def test_read_nonexistent(self):
        with patch('fla_charge.LAST_CHARGE_FILE', '/nonexistent/file'):
            self.assertIsNone(read_last_charge())

    def test_write_and_read(self):
        with patch('fla_charge.LAST_CHARGE_FILE', self.tmpfile.name):
            write_last_charge()
            result = read_last_charge()
            self.assertIsNotNone(result)
            self.assertAlmostEqual(
                result.timestamp(), datetime.now().timestamp(), delta=5
            )

    def test_corrupt_file_returns_none(self):
        Path(self.tmpfile.name).write_text("not-a-date")
        with patch('fla_charge.LAST_CHARGE_FILE', self.tmpfile.name):
            self.assertIsNone(read_last_charge())


class TestRunChargeSafety(unittest.TestCase):
    """Test safety guards in run_charge()."""

    def _make_mocks(self, **monitor_kwargs):
        settings = MockChargeSettings()
        monitor = MockMonitor(**monitor_kwargs)
        status = MockStatus()
        return settings, monitor, status

    @patch('fla_charge.release_lock')
    @patch('fla_charge.acquire_lock', return_value=False)
    def test_lock_held_returns_false(self, mock_lock, mock_unlock):
        settings, monitor, status = self._make_mocks()
        result = run_charge(settings, monitor, status)
        self.assertFalse(result)

    @patch('fla_charge.release_lock')
    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.stop_aggregate', return_value=False)
    @patch('fla_charge.start_aggregate', return_value=True)
    @patch('fla_charge.time')
    def test_aggregate_stop_failure_aborts(self, mock_time, mock_start, mock_stop,
                                           mock_cell_v, mock_ac, mock_lock, mock_unlock):
        """Phase 2: stop_aggregate failure should abort with error."""
        mock_time.time.side_effect = [0, 5]  # phase1_start, elapsed
        mock_time.sleep = MagicMock()
        settings, monitor, status = self._make_mocks(lfp_soc=96.0)  # Trigger P1→P2
        with patch('fla_charge.TempBatteryService') as MockTBS:
            MockTBS.return_value = MagicMock()
            with patch('fla_charge.update_cache'):
                result = run_charge(settings, monitor, status)
        self.assertFalse(result)
        self.assertIn(STATE_ERROR, status.states)

    @patch('fla_charge.release_lock')
    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.stop_aggregate', return_value=True)
    @patch('fla_charge.start_aggregate', return_value=True)
    @patch('fla_charge.open_relay', return_value=False)
    @patch('fla_charge.time')
    def test_relay_open_failure_aborts(self, mock_time, mock_open, mock_start, mock_stop,
                                       mock_cell_v, mock_ac, mock_lock, mock_unlock):
        mock_time.time.side_effect = [0, 5]
        mock_time.sleep = MagicMock()
        settings, monitor, status = self._make_mocks(lfp_soc=96.0)
        with patch('fla_charge.TempBatteryService') as MockTBS:
            MockTBS.return_value = MagicMock()
            with patch('fla_charge.update_cache'):
                result = run_charge(settings, monitor, status)
        self.assertFalse(result)
        self.assertIn(STATE_ERROR, status.states)

    @patch('fla_charge.release_lock')
    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.stop_aggregate', return_value=True)
    @patch('fla_charge.start_aggregate', return_value=True)
    @patch('fla_charge.open_relay', return_value=True)
    @patch('fla_charge.verify_relay_open', return_value=False)
    @patch('fla_charge.time')
    def test_relay_verify_failure_aborts(self, mock_time, mock_verify, mock_open,
                                         mock_start, mock_stop, mock_cell_v, mock_ac,
                                         mock_lock, mock_unlock):
        mock_time.time.side_effect = [0, 5]
        mock_time.sleep = MagicMock()
        settings, monitor, status = self._make_mocks(lfp_soc=96.0)
        with patch('fla_charge.TempBatteryService') as MockTBS:
            MockTBS.return_value = MagicMock()
            with patch('fla_charge.update_cache'):
                result = run_charge(settings, monitor, status)
        self.assertFalse(result)
        self.assertIn(STATE_ERROR, status.states)

    @patch('fla_charge.release_lock')
    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.stop_aggregate', return_value=True)
    @patch('fla_charge.start_aggregate', return_value=True)
    @patch('fla_charge.open_relay', return_value=True)
    @patch('fla_charge.verify_relay_open', return_value=True)
    @patch('fla_charge.verify_relay_still_open', return_value=True)
    @patch('fla_charge.time')
    def test_trojan_unresponsive_during_absorption_aborts(self, mock_time, mock_relay_check,
                                                          mock_verify, mock_open, mock_start,
                                                          mock_stop, mock_cell_v, mock_ac,
                                                          mock_lock, mock_unlock):
        """SmartShunt Trojan returning None during absorption should abort."""
        mock_time.time.side_effect = [0, 5, 10, 15, 20]  # phase1 + absorption
        mock_time.sleep = MagicMock()
        settings, monitor, status = self._make_mocks(lfp_soc=96.0, trojan_voltage=None)
        # Phase 1 needs trojan_voltage for logging, but absorption gets None
        # We need trojan_voltage valid in Phase 1 then None in Phase 2
        phase1_call = [0]
        original_trojan_v = 27.5
        def trojan_v_side_effect():
            phase1_call[0] += 1
            if phase1_call[0] <= 2:
                return original_trojan_v
            return None  # Goes None in absorption
        monitor.get_trojan_voltage = trojan_v_side_effect

        with patch('fla_charge.TempBatteryService') as MockTBS:
            MockTBS.return_value = MagicMock()
            with patch('fla_charge.update_cache'):
                result = run_charge(settings, monitor, status)
        self.assertFalse(result)
        self.assertIn(STATE_ERROR, status.states)

    @patch('fla_charge.release_lock')
    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.stop_aggregate', return_value=True)
    @patch('fla_charge.start_aggregate', return_value=True)
    @patch('fla_charge.open_relay', return_value=True)
    @patch('fla_charge.verify_relay_open', return_value=True)
    @patch('fla_charge.verify_relay_still_open', return_value=False)
    @patch('fla_charge.time')
    def test_relay_closed_externally_during_absorption_aborts(self, mock_time, mock_relay_check,
                                                              mock_verify, mock_open, mock_start,
                                                              mock_stop, mock_cell_v, mock_ac,
                                                              mock_lock, mock_unlock):
        """CRITICAL: External relay close during absorption should abort immediately."""
        mock_time.time.side_effect = [0, 5, 10, 15]
        mock_time.sleep = MagicMock()
        settings, monitor, status = self._make_mocks(lfp_soc=96.0)
        with patch('fla_charge.TempBatteryService') as MockTBS:
            MockTBS.return_value = MagicMock()
            with patch('fla_charge.update_cache'):
                result = run_charge(settings, monitor, status)
        self.assertFalse(result)
        self.assertIn(STATE_ERROR, status.states)


class TestRunChargeFinally(unittest.TestCase):
    """Test the finally block restores state on failure."""

    @patch('fla_charge.release_lock')
    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.stop_aggregate', return_value=True)
    @patch('fla_charge.start_aggregate', return_value=True)
    @patch('fla_charge.open_relay', return_value=False)
    @patch('fla_charge.close_relay_delta_aware')
    @patch('fla_charge.time')
    def test_lock_released_on_failure(self, mock_time, mock_close, mock_open,
                                      mock_start, mock_stop, mock_cell_v, mock_ac,
                                      mock_lock, mock_unlock):
        mock_time.time.side_effect = [0, 5]
        mock_time.sleep = MagicMock()
        settings = MockChargeSettings()
        monitor = MockMonitor(lfp_soc=96.0)
        status = MockStatus()
        with patch('fla_charge.TempBatteryService') as MockTBS:
            MockTBS.return_value = MagicMock()
            with patch('fla_charge.update_cache'):
                run_charge(settings, monitor, status)
        mock_unlock.assert_called_once()

    @patch('fla_charge.release_lock')
    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.stop_aggregate', return_value=True)
    @patch('fla_charge.start_aggregate')
    @patch('fla_charge.open_relay', return_value=False)
    @patch('fla_charge.close_relay_delta_aware')
    @patch('fla_charge.time')
    def test_aggregate_restarted_on_failure(self, mock_time, mock_close, mock_open,
                                            mock_start, mock_stop, mock_cell_v, mock_ac,
                                            mock_lock, mock_unlock):
        """If aggregate was stopped, finally block should restart it."""
        mock_time.time.side_effect = [0, 5]
        mock_time.sleep = MagicMock()
        settings = MockChargeSettings()
        monitor = MockMonitor(lfp_soc=96.0)
        status = MockStatus()
        with patch('fla_charge.TempBatteryService') as MockTBS:
            MockTBS.return_value = MagicMock()
            with patch('fla_charge.update_cache'):
                run_charge(settings, monitor, status)
        # Aggregate was stopped (stop_aggregate returned True), so start should be called in finally
        mock_start.assert_called()

    @patch('fla_charge.release_lock')
    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.stop_aggregate', return_value=True)
    @patch('fla_charge.start_aggregate', return_value=True)
    @patch('fla_charge.open_relay', return_value=False)
    @patch('fla_charge.close_relay_delta_aware')
    @patch('fla_charge.time')
    def test_close_relay_delta_aware_called_in_finally(self, mock_time, mock_close, mock_open,
                                                       mock_start, mock_stop, mock_cell_v,
                                                       mock_ac, mock_lock, mock_unlock):
        """Finally block should attempt delta-aware relay close."""
        mock_time.time.side_effect = [0, 5]
        mock_time.sleep = MagicMock()
        settings = MockChargeSettings()
        monitor = MockMonitor(lfp_soc=96.0)
        status = MockStatus()
        with patch('fla_charge.TempBatteryService') as MockTBS:
            MockTBS.return_value = MagicMock()
            with patch('fla_charge.update_cache'):
                run_charge(settings, monitor, status)
        mock_close.assert_called_once()


class TestPhase1Transitions(unittest.TestCase):
    """Test Phase 1 shared charging transition triggers."""

    @patch('fla_charge.release_lock')
    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.time')
    def test_phase1_timeout_returns_false(self, mock_time, mock_cell_v, mock_ac,
                                          mock_lock, mock_unlock):
        """Phase 1 should abort if timeout exceeded."""
        mock_time.time.side_effect = [0, 30000]  # way past 8hr default
        mock_time.sleep = MagicMock()
        settings = MockChargeSettings()
        # lfp_current=50 (above taper threshold 20), lfp_soc=50 (below transition 95),
        # cell_voltage=3.40 (below disconnect 3.50) — no transition triggers
        monitor = MockMonitor(lfp_soc=50.0, trojan_soc=70.0, lfp_current=50.0)
        status = MockStatus()
        with patch('fla_charge.update_cache'):
            result = run_charge(settings, monitor, status)
        self.assertFalse(result)
        # Should have entered Phase 1 but not Phase 2
        self.assertIn(STATE_PHASE1_SHARED, status.states)
        self.assertNotIn(STATE_STOPPING_DRIVER, status.states)
        self.assertIn(STATE_ERROR, status.states)

    @patch('fla_charge.release_lock')
    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available')
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.time')
    def test_phase1_ac_loss_returns_false(self, mock_time, mock_cell_v, mock_ac,
                                          mock_lock, mock_unlock):
        """AC input lost during Phase 1 should abort with STATE_ERROR."""
        mock_time.time.side_effect = [0, 5]
        mock_time.sleep = MagicMock()
        mock_ac.return_value = False  # AC lost in loop
        settings = MockChargeSettings()
        monitor = MockMonitor(lfp_soc=50.0, trojan_soc=70.0, lfp_current=50.0)
        status = MockStatus()
        with patch('fla_charge.update_cache'):
            result = run_charge(settings, monitor, status)
        self.assertFalse(result)
        self.assertIn(STATE_ERROR, status.states)


    @patch('fla_charge.release_lock')
    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.time')
    def test_phase1_discharge_current_does_not_trigger_taper(self, mock_time, mock_cell_v,
                                                              mock_ac, mock_lock, mock_unlock):
        """Small discharge current should NOT trigger Phase 1 → 2 taper transition."""
        mock_time.time.side_effect = [0, 30000]  # will hit timeout
        mock_time.sleep = MagicMock()
        settings = MockChargeSettings()
        # lfp_current=-5.0 (discharge), lfp_soc=50 (below transition), cell=3.40 (below disconnect)
        # abs(-5)=5 < 20 threshold — old code would wrongly trigger taper
        monitor = MockMonitor(lfp_soc=50.0, trojan_soc=70.0, lfp_current=-5.0)
        status = MockStatus()
        with patch('fla_charge.update_cache'):
            result = run_charge(settings, monitor, status)
        self.assertFalse(result)  # Timeout, not Phase 2 transition
        self.assertIn(STATE_PHASE1_SHARED, status.states)
        self.assertNotIn(STATE_STOPPING_DRIVER, status.states)


if __name__ == '__main__':
    unittest.main()
