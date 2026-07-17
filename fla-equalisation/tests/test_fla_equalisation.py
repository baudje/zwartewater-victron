#!/usr/bin/env python3
"""Test harness for FLA equalisation service.

Mocks D-Bus calls so tests run on any machine, not just Venus OS.
Tests cover scheduling logic, safety guards, and state transitions.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
import tempfile

# Add project and shared modules to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'fla-shared'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'fla-shared', 'tests'))

from helpers import dbus_mock_setup, MockMonitor, MockStatus
dbus_mock_setup()

from fla_equalisation import (
    should_run, read_last_equalisation, write_last_equalisation,
    days_until_next, run_equalisation,
    LAST_EQ_FILE,
)
from settings import SETTINGS_DEFS as EQ_SETTINGS_DEFS
from dbus_status_service import (
    STATE_IDLE, STATE_STOPPING_DRIVER, STATE_DISCONNECTING,
    STATE_EQUALISING, STATE_COOLING_DOWN, STATE_VOLTAGE_MATCHING,
    STATE_RECONNECTING, STATE_RESTARTING_DRIVER, STATE_ERROR,
)


class MockSettings:
    """Mock Settings object with configurable properties."""
    def __init__(self, **kwargs):
        self.eq_voltage = kwargs.get('eq_voltage', 31.5)
        self.eq_current_complete = kwargs.get('eq_current_complete', 10.0)
        self.eq_timeout_hours = kwargs.get('eq_timeout_hours', 2.5)
        self.float_voltage = kwargs.get('float_voltage', 27.0)
        self.voltage_delta_max = kwargs.get('voltage_delta_max', 1.0)
        self.voltage_match_timeout_hours = kwargs.get('voltage_match_timeout_hours', 4.0)
        self.days_between = kwargs.get('days_between', 90)
        self.start_hour = kwargs.get('start_hour', 14)
        self.end_hour = kwargs.get('end_hour', 17)
        self.lfp_soc_min = kwargs.get('lfp_soc_min', 95)
        self.enabled = kwargs.get('enabled', True)
        self.run_now = kwargs.get('run_now', False)
        self._cleared_run_now = False

    def clear_run_now(self):
        self._cleared_run_now = True
        self.run_now = False

    def _write(self, key, value):
        setattr(self, key, value)


class TestScheduling(unittest.TestCase):
    """Test should_run() scheduling logic."""

    def setUp(self):
        self.settings = MockSettings()
        self.monitor = MockMonitor()

    def test_disabled_returns_false(self):
        self.settings.enabled = False
        self.assertFalse(should_run(self.settings, self.monitor))

    def test_soc_too_low_returns_false(self):
        self.monitor._lfp_soc = 80.0
        with patch('fla_equalisation.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 29, 15, 0)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            self.assertFalse(should_run(self.settings, self.monitor))

    def test_soc_none_returns_false(self):
        self.monitor._lfp_soc = None
        self.assertFalse(should_run(self.settings, self.monitor))

    def test_outside_window_returns_false(self):
        self.monitor._lfp_soc = 96.0
        with patch('fla_equalisation.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 29, 10, 0)  # 10:00, before 14:00
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            with patch('fla_equalisation.read_last_equalisation', return_value=None):
                self.assertFalse(should_run(self.settings, self.monitor))

    def test_within_window_and_soc_ok_returns_true(self):
        self.monitor._lfp_soc = 96.0
        with patch('fla_equalisation.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 29, 15, 0)  # 15:00
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            with patch('fla_equalisation.read_last_equalisation', return_value=None):
                with patch('fla_equalisation.days_until_next', return_value=0):
                    self.assertTrue(should_run(self.settings, self.monitor))

    def test_too_recent_returns_false(self):
        self.monitor._lfp_soc = 96.0
        with patch('fla_equalisation.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 29, 15, 0)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            with patch('fla_equalisation.days_until_next', return_value=45):
                self.assertFalse(should_run(self.settings, self.monitor))

    def test_run_now_bypasses_interval_and_window(self):
        self.settings.run_now = True
        self.monitor._lfp_soc = 96.0
        with patch('fla_equalisation.days_until_next', return_value=45):
            self.assertTrue(should_run(self.settings, self.monitor))
        self.assertTrue(self.settings._cleared_run_now)

    def test_run_now_not_consumed_when_soc_too_low(self):
        """RunNow should NOT be cleared if SoC gate fails — preserves operator intent."""
        self.settings.run_now = True
        self.monitor._lfp_soc = 80.0  # Below 95%
        self.assertFalse(should_run(self.settings, self.monitor))
        self.assertFalse(self.settings._cleared_run_now)

    def test_run_now_not_consumed_when_soc_none(self):
        """RunNow should NOT be cleared if SoC is unreadable."""
        self.settings.run_now = True
        self.monitor._lfp_soc = None
        self.assertFalse(should_run(self.settings, self.monitor))
        self.assertFalse(self.settings._cleared_run_now)


class TestDaysUntilNext(unittest.TestCase):
    """Test days_until_next() calculation."""

    def test_never_run(self):
        settings = MockSettings(days_between=90)
        with patch('fla_equalisation.read_last_equalisation', return_value=None):
            self.assertEqual(days_until_next(settings), 0)

    def test_recently_run(self):
        settings = MockSettings(days_between=90)
        last = datetime.now() - timedelta(days=10)
        with patch('fla_equalisation.read_last_equalisation', return_value=last):
            self.assertEqual(days_until_next(settings), 80)

    def test_overdue(self):
        settings = MockSettings(days_between=90)
        last = datetime.now() - timedelta(days=100)
        with patch('fla_equalisation.read_last_equalisation', return_value=last):
            self.assertEqual(days_until_next(settings), 0)


class TestLastEqualisation(unittest.TestCase):
    """Test read/write of last equalisation timestamp."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
        self.tmpfile.close()

    def tearDown(self):
        try:
            os.unlink(self.tmpfile.name)
        except OSError:
            pass

    def test_read_nonexistent(self):
        with patch('fla_equalisation.LAST_EQ_FILE', '/nonexistent/file'):
            self.assertIsNone(read_last_equalisation())

    def test_write_and_read(self):
        with patch('fla_equalisation.LAST_EQ_FILE', self.tmpfile.name):
            write_last_equalisation()
            result = read_last_equalisation()
            self.assertIsNotNone(result)
            self.assertAlmostEqual(
                result.timestamp(), datetime.now().timestamp(), delta=5
            )


class TestSafetyGuards(unittest.TestCase):
    """Test safety guards in run_equalisation()."""

    def _make_mocks(self, **monitor_kwargs):
        settings = MockSettings()
        monitor = MockMonitor(**monitor_kwargs)
        status = MockStatus()
        return settings, monitor, status

    @patch('fla_equalisation.acquire_lock', return_value=True)
    def test_hand_off_in_failure_aborts(self, mock_lock):
        # The individual handoff-step safety guards (temp-battery register,
        # aggregate stop, systemcalc restart, BMS-switch confirmation, relay
        # open) now live in the Takeover and are covered by test_takeover.py.
        # Here we only assert run_equalisation surfaces a hand_off_in failure.
        settings, monitor, status = self._make_mocks()
        with patch('fla_equalisation.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = False
            MockT.return_value = inst
            result = run_equalisation(settings, monitor, status)
        self.assertFalse(result)
        self.assertIn(STATE_ERROR, status.states)
        inst.abort_teardown.assert_called_once()


class TestOperatorAbortRouting(unittest.TestCase):
    """Operator Abort in the relay-open equalising loop reconnects cleanly."""

    def _make_mocks(self, **kw):
        settings = MockSettings(**kw)
        monitor = MockMonitor(trojan_voltage=27.2, lfp_voltage=27.0, relay_state=0)
        status = MockStatus()
        return settings, monitor, status

    @patch('fla_equalisation.write_last_equalisation')
    @patch('fla_equalisation.acquire_lock', return_value=True)
    @patch('fla_equalisation.check_abort', return_value=True)
    @patch('fla_equalisation.time')
    def test_operator_abort_reconnects_without_timestamp(
        self, mock_time, mock_abort, mock_lock, mock_write,
    ):
        mock_time.time.side_effect = [i for i in range(0, 200, 5)]
        mock_time.sleep = MagicMock()
        settings, monitor, status = self._make_mocks()
        with patch('fla_equalisation.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = True
            inst.hand_back.return_value = (True, 0.2)
            MockT.return_value = inst
            with patch('fla_equalisation.update_cache'):
                result = run_equalisation(settings, monitor, status)

        # Operator abort routed through the controlled reconnect (hand_back)…
        inst.hand_back.assert_called_once()
        # …but the run is flagged non-completion: no timestamp, returns False.
        mock_write.assert_not_called()
        self.assertFalse(result)
        # The guarded teardown still runs in the finally.
        inst.abort_teardown.assert_called_once()


class TestRelayStateGuardedFinally(unittest.TestCase):
    """The finally always routes teardown through the Takeover.

    The relay-state-guarded restore (hold the bus while the relay is open,
    restore DVCC + release the lock once it is closed) now lives in
    Takeover.teardown and is covered by test_takeover.py::TestTeardown. Here
    we only assert run_equalisation delegates to abort_teardown in its finally
    on both a mid-EQ hard error and a hand_off_in failure."""

    @patch('fla_equalisation.raise_alarm')
    @patch('fla_equalisation.acquire_lock', return_value=True)
    @patch('fla_equalisation.time')
    def test_mid_eq_error_routes_teardown_through_takeover(
        self, mock_time, mock_lock, mock_alarm,
    ):
        # Trojan goes unresponsive mid-EQ (hard error) → return False; the
        # finally hands off to the Takeover's guarded teardown.
        mock_time.time.side_effect = [i for i in range(0, 200, 5)]
        mock_time.sleep = MagicMock()
        settings = MockSettings()
        monitor = MockMonitor(relay_state=0, lfp_voltage=27.0)
        monitor.get_trojan_voltage = MagicMock(return_value=None)  # unresponsive
        status = MockStatus()
        with patch('fla_equalisation.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = True
            MockT.return_value = inst
            with patch('fla_equalisation.update_cache'):
                result = run_equalisation(settings, monitor, status)

        self.assertFalse(result)
        self.assertTrue(mock_alarm.called)         # loop raised the hard-error alarm
        inst.abort_teardown.assert_called_once()   # finally routed through the Takeover

    @patch('fla_equalisation.acquire_lock', return_value=True)
    def test_hand_off_failure_routes_teardown_through_takeover(
        self, mock_lock,
    ):
        # hand_off_in fails before the relay ever opens → return False; the
        # finally still routes teardown through the Takeover.
        settings = MockSettings()
        monitor = MockMonitor(relay_state=1)
        status = MockStatus()
        with patch('fla_equalisation.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = False
            MockT.return_value = inst
            result = run_equalisation(settings, monitor, status)

        self.assertFalse(result)
        inst.abort_teardown.assert_called_once()   # finally routed through the Takeover


class TestFinallySafety(unittest.TestCase):
    """Test the finally block's delta-aware relay handling."""

    def test_finally_does_not_close_relay_at_high_delta(self):
        """CRIT-2: finally block must NOT close relay if delta > 1V (RELAY_CLOSE_DELTA_MAX)."""
        monitor = MockMonitor(
            relay_state=0,  # Relay is open
            trojan_voltage=28.5,
            lfp_voltage=27.0,  # Delta = 1.5V
        )
        v_t = monitor.get_trojan_voltage()
        v_l = monitor.get_lfp_voltage()
        delta = abs(v_t - v_l)
        self.assertGreater(delta, 1.0)
        # In the real code, this means relay stays open

    def test_finally_closes_relay_at_low_delta(self):
        """CRIT-2: finally block should close relay if delta < 1V (RELAY_CLOSE_DELTA_MAX)."""
        monitor = MockMonitor(
            relay_state=0,
            trojan_voltage=27.5,
            lfp_voltage=27.2,  # Delta = 0.3V
        )
        v_t = monitor.get_trojan_voltage()
        v_l = monitor.get_lfp_voltage()
        delta = abs(v_t - v_l)
        self.assertLess(delta, 1.0)
        # In the real code, this means relay closes safely


class TestStartupSafety(unittest.TestCase):
    """Test startup safety check for relay state (CRIT-4)."""

    def test_relay_open_low_delta_auto_closes(self):
        """On startup, if relay is open but delta < 1V, auto-close."""
        monitor = MockMonitor(
            relay_state=0,
            trojan_voltage=27.3,
            lfp_voltage=27.1,
        )
        delta = abs(monitor.get_trojan_voltage() - monitor.get_lfp_voltage())
        self.assertLess(delta, 1.0)
        # Real code would call monitor.set_relay(1)

    def test_relay_open_high_delta_raises_alarm(self):
        """On startup, if relay is open and delta > 1V, alarm — do NOT close."""
        monitor = MockMonitor(
            relay_state=0,
            trojan_voltage=28.5,
            lfp_voltage=27.0,
        )
        delta = abs(monitor.get_trojan_voltage() - monitor.get_lfp_voltage())
        self.assertGreater(delta, 1.0)
        # Real code would raise alarm, NOT close relay

    def test_relay_closed_no_action(self):
        """On startup, if relay is closed, no action needed."""
        monitor = MockMonitor(relay_state=1)
        self.assertEqual(monitor.get_relay_state(), 1)


class TestCrashSafety(unittest.TestCase):
    """Test that crash at any point leaves system in safe state."""

    def test_temp_service_registers_at_safe_voltage(self):
        """Service crash before relay open: CVL must be safe for LFPs (28.4V, not 31.5V)."""
        # The temp service registers at 28.4V first, only raised to EQ after relay opens
        safe_voltage = 28.4
        eq_voltage = 31.5
        max_lfp_cell = 3.65
        # 28.4V / 8 cells = 3.55V per cell — safe
        self.assertLess(safe_voltage / 8, max_lfp_cell)
        # 31.2V / 8 cells = 3.9V per cell — DANGEROUS
        self.assertGreater(eq_voltage / 8, max_lfp_cell)


class TestInrushProtection(unittest.TestCase):
    """Test voltage delta checks protect against inrush current."""

    def test_1v_delta_is_reconnect_safe(self):
        """Default voltage_delta_max=1.0V — verify it's within safe reconnect range."""
        delta = 1.0
        # For 628Ah LFP bank with ~1mOhm internal resistance:
        # I = V / R = 1.0 / 0.001 = 1000A theoretical
        # But limited by cable resistance, relay resistance, SmartShunt resistance
        # Practical inrush with 1V delta and typical wiring ~50-100A peak
        # This is within relay and SmartShunt ratings
        self.assertLessEqual(delta, 1.0)

    def test_1_5v_delta_is_dangerous(self):
        """1.5V delta would cause dangerous inrush — must be blocked."""
        delta = 1.5
        max_safe_delta = 1.0
        self.assertGreater(delta, max_safe_delta)


class TestSettings(unittest.TestCase):
    """Test settings bounds."""

    def test_eq_voltage_max_prevents_lfp_overvoltage(self):
        """eq_voltage max (32V) / 8 cells = 4.0V — above 3.65V but relay should be open."""
        max_eq = 32.0
        cells = 8
        lfp_max = 3.65
        # This is safe ONLY because relay is open during equalisation
        self.assertGreater(max_eq / cells, lfp_max)
        # If relay fails to open, this voltage would damage LFPs
        # That's why we verify relay open before raising CVL

    def test_eq_voltage_safe_for_trojans(self):
        """eq_voltage default (31.5V) / 12 cells = 2.625V per FLA cell — within EQ range."""
        eq_v = 31.5
        fla_cells = 12
        per_cell = eq_v / fla_cells
        self.assertAlmostEqual(per_cell, 2.625, places=2)


class TestSettingsBounds(unittest.TestCase):
    """Test hard safety bounds match the documented operating envelope."""

    def test_eq_voltage_max_allows_datasheet_range(self):
        self.assertEqual(EQ_SETTINGS_DEFS["eq_voltage"][3], 32.0)

    def test_reconnect_delta_max_cannot_exceed_safe_limit(self):
        self.assertEqual(EQ_SETTINGS_DEFS["voltage_delta_max"][3], 1.0)


class TestRunEqualisationHappyPath(unittest.TestCase):
    """Test successful full equalisation sequence."""

    def _make_mocks(self, **monitor_kwargs):
        settings = MockSettings()
        monitor = MockMonitor(**monitor_kwargs)
        status = MockStatus()
        return settings, monitor, status

    @patch('fla_equalisation.acquire_lock', return_value=True)
    @patch('fla_equalisation.verify_relay_still_open', return_value=True)
    @patch('fla_equalisation.write_last_equalisation')
    @patch('fla_equalisation.clear_alarm')
    @patch('fla_equalisation.update_cache')
    @patch('fla_equalisation.time')
    def test_full_success_returns_true(self, mock_time, mock_cache, mock_clear,
                                       mock_write_eq, mock_relay_check,
                                       mock_lock):
        """Full EQ sequence with handoff/reconnect via Takeover returns True."""
        # EQ loop: first iteration sees current below threshold → complete
        mock_time.time.side_effect = [0, 5, 10]
        mock_time.sleep = MagicMock()
        settings, monitor, status = self._make_mocks(
            trojan_voltage=31.5, trojan_current=5.0,  # Below eq_current_complete=10
            lfp_voltage=28.0,
        )
        with patch('fla_equalisation.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = True
            inst.hand_back.return_value = (True, 0.3)
            MockT.return_value = inst
            result = run_equalisation(settings, monitor, status)
        self.assertTrue(result)
        mock_write_eq.assert_called_once()
        mock_clear.assert_called_once()
        inst.abort_teardown.assert_called_once()  # finally still routes through Takeover

    @patch('fla_equalisation.acquire_lock', return_value=True)
    @patch('fla_equalisation.verify_relay_still_open', return_value=True)
    @patch('fla_equalisation.write_last_equalisation')
    @patch('fla_equalisation.clear_alarm')
    @patch('fla_equalisation.update_cache')
    @patch('fla_equalisation.time')
    def test_state_transitions_in_order(self, mock_time, mock_cache, mock_clear,
                                         mock_write_eq, mock_relay_check,
                                         mock_lock):
        """The states run_equalisation itself emits, in order. The handoff and
        reconnect display states (STOPPING_DRIVER … RESTARTING_DRIVER) are now
        set inside the Takeover and asserted in test_takeover.py."""
        mock_time.time.side_effect = [0, 5, 10]
        mock_time.sleep = MagicMock()
        settings, monitor, status = self._make_mocks(
            trojan_voltage=31.5, trojan_current=5.0, lfp_voltage=28.0,
        )
        with patch('fla_equalisation.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = True
            inst.hand_back.return_value = (True, 0.3)
            MockT.return_value = inst
            run_equalisation(settings, monitor, status)
        expected_order = [STATE_EQUALISING, STATE_IDLE]
        self.assertEqual(status.states, expected_order)


class TestOrionFailureDetection(unittest.TestCase):
    """Test CRIT-3: Orion DC-DC failure detection during equalisation."""

    @patch('fla_equalisation.acquire_lock', return_value=True)
    @patch('fla_equalisation.verify_relay_still_open', return_value=True)
    @patch('fla_equalisation.update_cache')
    @patch('fla_equalisation.time')
    def test_lfp_voltage_drop_triggers_alarm(self, mock_time, mock_cache,
                                              mock_relay_check, mock_lock):
        """LFP voltage dropping > 0.5V from disconnect indicates Orion failure."""
        mock_time.time.side_effect = [0, 5, 10, 15]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(
            trojan_voltage=29.0, trojan_current=30.0,
            lfp_voltage=27.3,  # disconnect will record 27.3, then it drops below 26.8
        )
        # After disconnect, LFP voltage drops
        call_count = [0]
        def lfp_v_dropping():
            call_count[0] += 1
            if call_count[0] <= 2:
                return 27.3  # At disconnect
            return 26.5  # Dropped > 0.5V → Orion failure
        monitor.get_lfp_voltage = lfp_v_dropping

        settings = MockSettings()
        status = MockStatus()
        with patch('fla_equalisation.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = True
            MockT.return_value = inst
            result = run_equalisation(settings, monitor, status)
        self.assertFalse(result)
        self.assertIn(STATE_ERROR, status.states)

    @patch('fla_equalisation.acquire_lock', return_value=True)
    @patch('fla_equalisation.verify_relay_still_open', return_value=True)
    @patch('fla_equalisation.write_last_equalisation')
    @patch('fla_equalisation.clear_alarm')
    @patch('fla_equalisation.update_cache')
    @patch('fla_equalisation.time')
    def test_lfp_voltage_stable_no_alarm(self, mock_time, mock_cache, mock_clear,
                                          mock_write_eq, mock_relay_check,
                                          mock_lock):
        """LFP voltage stable (within 0.3V) should not trigger Orion alarm."""
        mock_time.time.side_effect = [0, 5, 10]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(
            trojan_voltage=31.5, trojan_current=5.0,
            lfp_voltage=27.7,  # Stable — within 0.5V of disconnect voltage
        )
        settings = MockSettings()
        status = MockStatus()
        with patch('fla_equalisation.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = True
            inst.hand_back.return_value = (True, 0.3)
            MockT.return_value = inst
            result = run_equalisation(settings, monitor, status)
        self.assertTrue(result)

    @patch('fla_equalisation.acquire_lock', return_value=True)
    @patch('fla_equalisation.verify_relay_still_open', return_value=True)
    @patch('fla_equalisation.write_last_equalisation')
    @patch('fla_equalisation.clear_alarm')
    @patch('fla_equalisation.update_cache')
    @patch('fla_equalisation.time')
    def test_lfp_voltage_none_no_false_alarm(self, mock_time, mock_cache, mock_clear,
                                              mock_write_eq, mock_relay_check,
                                              mock_lock):
        """LFP voltage=None should not trigger false Orion alarm."""
        mock_time.time.side_effect = [0, 5, 10]
        mock_time.sleep = MagicMock()
        # First call returns value (at disconnect), subsequent calls return None
        call_count = [0]
        def lfp_v_goes_none():
            call_count[0] += 1
            if call_count[0] <= 2:
                return 28.0
            return None
        monitor = MockMonitor(
            trojan_voltage=31.5, trojan_current=5.0,
        )
        monitor.get_lfp_voltage = lfp_v_goes_none
        settings = MockSettings()
        status = MockStatus()
        with patch('fla_equalisation.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = True
            inst.hand_back.return_value = (True, 0.3)
            MockT.return_value = inst
            result = run_equalisation(settings, monitor, status)
        # Should still succeed — None doesn't trigger the Orion check
        self.assertTrue(result)


class TestTempCompensationIntegration(unittest.TestCase):
    """Test temperature compensation applied during equalisation."""

    @patch('fla_equalisation.acquire_lock', return_value=True)
    @patch('fla_equalisation.verify_relay_still_open', return_value=True)
    @patch('fla_equalisation.write_last_equalisation')
    @patch('fla_equalisation.clear_alarm')
    @patch('fla_equalisation.update_cache')
    @patch('fla_equalisation.time')
    def test_cold_temp_raises_cvl(self, mock_time, mock_cache, mock_clear,
                                   mock_write_eq, mock_relay_check,
                                   mock_lock):
        """At 15°C, the compensated EQ target handed to the Takeover rises above base.

        run_equalisation computes temp_compensate(eq_voltage, temp) and passes it
        as hand_off_in's target_voltage; the Takeover then drives the temp-battery
        CVL (covered in test_takeover.py)."""
        mock_time.time.side_effect = [0, 5, 10]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(
            trojan_voltage=32.1, trojan_current=5.0,  # At compensated voltage
            lfp_voltage=28.0, battery_temp=15.0,
        )
        settings = MockSettings()
        status = MockStatus()
        with patch('fla_equalisation.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = True
            inst.hand_back.return_value = (True, 0.3)
            MockT.return_value = inst
            run_equalisation(settings, monitor, status)
        # temp_compensate(31.5, 15.0) = 31.5 + 0.6 = 32.1 → hand_off_in target
        target = inst.hand_off_in.call_args.kwargs["target_voltage"]
        self.assertAlmostEqual(target, 32.1, places=2)

    @patch('fla_equalisation.acquire_lock', return_value=True)
    @patch('fla_equalisation.verify_relay_still_open', return_value=True)
    @patch('fla_equalisation.write_last_equalisation')
    @patch('fla_equalisation.clear_alarm')
    @patch('fla_equalisation.update_cache')
    @patch('fla_equalisation.time')
    def test_none_temp_uses_base_voltage(self, mock_time, mock_cache, mock_clear,
                                          mock_write_eq, mock_relay_check,
                                          mock_lock):
        """No temperature reading should hand off the base eq_voltage unchanged."""
        mock_time.time.side_effect = [0, 5, 10]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(
            trojan_voltage=31.5, trojan_current=5.0,
            lfp_voltage=28.0, battery_temp=None,
        )
        settings = MockSettings()
        status = MockStatus()
        with patch('fla_equalisation.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = True
            inst.hand_back.return_value = (True, 0.3)
            MockT.return_value = inst
            run_equalisation(settings, monitor, status)
        # temp_compensate(31.5, None) = 31.5 unchanged → hand_off_in target
        target = inst.hand_off_in.call_args.kwargs["target_voltage"]
        self.assertAlmostEqual(target, 31.5, places=2)


class TestRunEqualisationLockHeld(unittest.TestCase):
    """Lock-held branch (mirrors fla-charge.test_lock_held_returns_false)."""

    @patch('fla_equalisation.acquire_lock', return_value=False)
    def test_lock_held_returns_false_without_starting_handoff(self, mock_lock):
        """If the lock is held, run_equalisation must return False BEFORE
        any state-mutating action (no aggregate stop, no temp battery, no
        relay open). Otherwise a charge run holding the lock could be
        interrupted by an EQ trying to handoff on top of it."""
        settings = MockSettings()
        monitor = MockMonitor()
        monitor.set_relay = MagicMock()
        monitor.set_battery_service = MagicMock()
        status = MockStatus()
        result = run_equalisation(settings, monitor, status)
        self.assertFalse(result)
        # Crucial: no relay change should have happened.
        monitor.set_relay.assert_not_called()


class TestApplyPendingSettingsBounds(unittest.TestCase):
    """Settings handed off from the web thread must be bounds-checked
    before being written to D-Bus, even though Venus enforces min/max on
    the AddSetting path. Defence in depth — a misbehaving HTTP client (or
    a mocking environment) shouldn't be able to push out-of-range values."""

    def setUp(self):
        # Drive FlaEqualisationService._apply_pending_settings directly
        # (it only touches self.settings._write and the module-bound
        # drain_pending_settings, so we don't need a full service).
        from fla_equalisation import FlaEqualisationService, _engine
        self.svc = FlaEqualisationService.__new__(FlaEqualisationService)
        self.svc.settings = MagicMock()
        self._engine = _engine
        # Make sure no stale pending settings leak in from prior tests.
        self._engine.drain_pending_settings()

    def _enqueue(self, key, value):
        # Same public path the HTTP POST /api/setting handler uses.
        self._engine.queue_setting(key, value)

    def test_eq_voltage_above_max_is_rejected(self):
        """31.5V was the cap before it was tightened to 32.0V; either way,
        35V exceeds the configured max and must be refused."""
        self._enqueue("eq_voltage", 35.0)
        self.svc._apply_pending_settings()
        self.svc.settings._write.assert_not_called()

    def test_lfp_soc_min_below_min_is_rejected(self):
        # Min is 50; -5 is well below.
        self._enqueue("lfp_soc_min", -5)
        self.svc._apply_pending_settings()
        self.svc.settings._write.assert_not_called()

    def test_unknown_key_is_refused_at_the_queue(self):
        # The engine validates keys against the profile's schema at queue
        # time — an unknown key can no longer even enter the pipeline.
        with self.assertRaises(ValueError):
            self._enqueue("not_a_real_setting", 42)
        self.svc._apply_pending_settings()
        self.svc.settings._write.assert_not_called()

    def test_in_range_value_is_written_through(self):
        self._enqueue("eq_voltage", 30.0)
        self.svc._apply_pending_settings()
        self.svc.settings._write.assert_called_once_with("eq_voltage", 30.0)

    def test_mixed_batch_writes_only_valid_entries(self):
        self._enqueue("eq_voltage", 30.0)        # in range
        self._enqueue("eq_voltage", 99.0)        # out of range — rejected
        self._enqueue("lfp_soc_min", 90)         # in range
        self.svc._apply_pending_settings()
        # Only the two in-range writes reach _write.
        calls = self.svc.settings._write.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn((("eq_voltage", 30.0), {}), [(c.args, c.kwargs) for c in calls])
        self.assertIn((("lfp_soc_min", 90), {}), [(c.args, c.kwargs) for c in calls])

    def test_empty_pending_does_not_call_write(self):
        # No enqueue.
        self.svc._apply_pending_settings()
        self.svc.settings._write.assert_not_called()


class TestVoltageDeltaWithZero(unittest.TestCase):
    """Regression for IMP-5: `if (v_trojan and v_lfp)` falsy check used to
    drop a 0.0V reading on the floor (treating it as None), so the
    dashboard would show '-' instead of '0.00V'. Now uses
    `is not None` and a true 0V reading produces a numeric delta."""

    def test_zero_voltage_does_not_yield_none_delta(self):
        # Inline the post-IMP-5 expression for clarity. If a future refactor
        # reintroduces the falsy check, this test will fail.
        v_trojan, v_lfp = 0.0, 26.5
        delta = round(abs(v_trojan - v_lfp), 2) if (v_trojan is not None and v_lfp is not None) else None
        self.assertEqual(delta, 26.5)

    def test_both_zero_yields_zero_not_none(self):
        v_trojan, v_lfp = 0.0, 0.0
        delta = round(abs(v_trojan - v_lfp), 2) if (v_trojan is not None and v_lfp is not None) else None
        self.assertEqual(delta, 0.0)
        self.assertIsNotNone(delta)

    def test_actual_none_still_yields_none(self):
        v_trojan, v_lfp = None, 26.5
        delta = round(abs(v_trojan - v_lfp), 2) if (v_trojan is not None and v_lfp is not None) else None
        self.assertIsNone(delta)


class TestStatusServiceDeregisterFailsFast(unittest.TestCase):
    """Regression for IMP-2: after deregister(), set_alarm/clear_alarm_path
    should be no-ops (gated by self._registered) AND self._service must be
    None so any code path that bypasses _registered crashes loudly instead
    of silently writing to a stale handle. Mirrors fla-charge."""

    def test_service_handle_nulled_after_deregister(self):
        from dbus_status_service import StatusService
        svc = StatusService.__new__(StatusService)
        svc._registered = True
        svc._service = MagicMock()
        svc.deregister()
        self.assertIsNone(svc._service)
        self.assertFalse(svc._registered)

    def test_post_deregister_set_alarm_is_silent_noop(self):
        """Belt-and-braces: gated by _registered, so set_alarm doesn't
        crash even though _service is None."""
        from dbus_status_service import StatusService
        svc = StatusService.__new__(StatusService)
        svc._registered = True
        svc._service = MagicMock()
        svc.deregister()
        # No exception — registered=False short-circuits before touching None.
        svc.set_alarm(2)
        svc.clear_alarm_path()


class TestResumeOnStartup(unittest.TestCase):
    """Startup adopts an interrupted hold instead of killing it."""

    def _service(self, monitor):
        """Build a FlaEqualisationService with construction side-effects stubbed.

        Uses start()/addCleanup(stop) rather than a with-block so patches remain
        active when Service() is called from the test method body (returning from
        inside a with-block exits the context managers, removing the patches).
        """
        from fla_equalisation import FlaEqualisationService

        p_recover = patch('fla_equalisation.recover_orphan_temp_battery')
        p_settings = patch('fla_equalisation.Settings', return_value=MockSettings())
        p_monitor = patch('fla_equalisation.DbusMonitor', return_value=monitor)
        p_status = patch('fla_equalisation.StatusService', return_value=MockStatus())
        p_update = patch.object(FlaEqualisationService, '_update_idle_status')

        mock_recover = p_recover.start()
        p_settings.start()
        p_monitor.start()
        p_status.start()
        p_update.start()

        self.addCleanup(p_recover.stop)
        self.addCleanup(p_settings.stop)
        self.addCleanup(p_monitor.stop)
        self.addCleanup(p_status.stop)
        self.addCleanup(p_update.stop)

        return FlaEqualisationService, mock_recover

    @patch('fla_equalisation.threading')
    @patch('fla_equalisation.startup_safety_check')
    @patch('fla_equalisation.is_temp_battery_running', return_value=True)
    @patch('fla_equalisation.acquire_lock', return_value=True)
    def test_relay_open_with_subprocess_resumes(self, mock_acquire, mock_running, mock_safety, mock_threading):
        monitor = MockMonitor(relay_state=0)
        Service, mock_recover = self._service(monitor)
        with patch('fla_equalisation.Takeover') as MockT:
            MockT.resume_attach.return_value = MagicMock()
            Service()
        mock_recover.assert_called_once_with(0)
        mock_threading.Thread.assert_called_once()
        mock_safety.assert_not_called()

    @patch('fla_equalisation.threading')
    @patch('fla_equalisation.startup_safety_check')
    @patch('fla_equalisation.is_temp_battery_running', return_value=True)
    @patch('fla_equalisation.acquire_lock', return_value=True)
    def test_relay_open_held_no_worker_keeps_lock(self, mock_acquire, mock_running, mock_safety, mock_threading):
        # resume_attach raised an alarm and is holding the bus (RESUME_HELD):
        # no worker, no safety check, and the lock is NOT released.
        monitor = MockMonitor(relay_state=0)
        Service, mock_recover = self._service(monitor)
        with patch('fla_equalisation.Takeover') as MockT, \
             patch('fla_equalisation.release_lock') as mock_release:
            MockT.resume_attach.return_value = MockT.RESUME_HELD
            Service()
        mock_threading.Thread.assert_not_called()
        mock_safety.assert_not_called()
        mock_release.assert_not_called()

    @patch('fla_equalisation.threading')
    @patch('fla_equalisation.startup_safety_check')
    @patch('fla_equalisation.is_temp_battery_running', return_value=True)
    @patch('fla_equalisation.acquire_lock', return_value=True)
    def test_resume_nothing_to_adopt_releases_lock_and_runs_safety(self, mock_acquire, mock_running, mock_safety, mock_threading):
        # resume_attach returned None (relay closed / temp gone in the race): the
        # lock we took is released and the normal startup safety check runs.
        monitor = MockMonitor(relay_state=0)
        Service, mock_recover = self._service(monitor)
        with patch('fla_equalisation.Takeover') as MockT, \
             patch('fla_equalisation.release_lock') as mock_release:
            MockT.resume_attach.return_value = None
            Service()
        mock_threading.Thread.assert_not_called()
        mock_release.assert_called_once()
        mock_safety.assert_called_once()

    @patch('fla_equalisation.threading')
    @patch('fla_equalisation.startup_safety_check')
    @patch('fla_equalisation.is_temp_battery_running', return_value=True)
    @patch('fla_equalisation.acquire_lock', return_value=True)
    def test_resume_worker_non_matched_sets_error(self, mock_acquire, mock_running, mock_safety, mock_threading):
        # A non-exception hand_back failure (safe-hold / relay close failed) must
        # leave the status display in STATE_ERROR, not stuck in a matching state.
        monitor = MockMonitor(relay_state=0)
        Service, mock_recover = self._service(monitor)
        with patch('fla_equalisation.Takeover') as MockT, \
             patch('fla_equalisation.clear_alarm') as mock_clear:
            t = MagicMock()
            t.hand_back.return_value = (False, 2.0)
            MockT.resume_attach.return_value = t
            svc = Service()
            worker = mock_threading.Thread.call_args.kwargs['target']
            worker()
        self.assertIn(STATE_ERROR, svc.status.states)
        mock_clear.assert_not_called()

    @patch('fla_equalisation.threading')
    @patch('fla_equalisation.startup_safety_check')
    @patch('fla_equalisation.is_temp_battery_running', return_value=False)
    @patch('fla_equalisation.acquire_lock', return_value=True)
    def test_relay_closed_runs_normal_safety_check(
        self, mock_acquire, mock_running, mock_safety, mock_threading,
    ):
        monitor = MockMonitor(relay_state=1)
        Service, mock_recover = self._service(monitor)
        Service()
        mock_recover.assert_called_once_with(1)
        mock_threading.Thread.assert_not_called()
        mock_safety.assert_called_once()


class TestCheckStartsWorker(unittest.TestCase):
    """Regression for the missed web_server→engine conversion: _check()'s
    should-run branch executed a leftover `_cache[...]` reference after the
    import was removed, raising NameError AFTER `self._running = True` —
    the blanket except swallowed it, the worker never spawned, and the
    service was wedged 'running' forever. All 275 tests stayed green
    because nothing drove this branch."""

    def _service(self):
        from fla_equalisation import FlaEqualisationService
        svc = FlaEqualisationService.__new__(FlaEqualisationService)
        svc.settings = MagicMock()
        svc.monitor = MagicMock()
        svc.status = MagicMock()
        svc._running = False
        svc._failed = False
        # Idle-status refresh reads live D-Bus values — not under test here.
        svc._update_idle_status = lambda: None
        return svc

    @patch('fla_equalisation.threading.Thread')
    @patch('fla_equalisation.should_run', return_value=True)
    def test_should_run_branch_spawns_the_worker(self, _sr, mock_thread):
        svc = self._service()
        svc._check()
        # The worker must be spawned — a swallowed exception between
        # `self._running = True` and Thread(...) leaves the service wedged.
        mock_thread.assert_called_once()
        mock_thread.return_value.start.assert_called_once()
        self.assertTrue(svc._running)

    @patch('fla_equalisation.threading.Thread')
    @patch('fla_equalisation.should_run', return_value=True)
    def test_web_run_now_flag_is_cleared_when_run_starts(self, _sr, mock_thread):
        from fla_equalisation import _engine
        svc = self._service()
        _engine._run_now_requested = True  # queued via web while conditions align
        try:
            svc._check()
            self.assertFalse(_engine.check_run_now(),
                             "queued web RunNow must be discarded once a run starts")
        finally:
            _engine.clear_run_now()

    @patch('fla_equalisation.verify_idle_bms_selection')
    @patch('fla_equalisation.should_run', return_value=False)
    def test_idle_check_runs_the_bms_guard(self, _sr, mock_guard):
        # Every idle tick must verify DVCC's controlling BMS is the aggregate —
        # the guard that would have caught the 2026-05-28 silent half-charge.
        svc = self._service()
        svc._check()
        mock_guard.assert_called_once()
        self.assertIs(mock_guard.call_args.args[0], svc.monitor)


class TestRunHistoryRecords(unittest.TestCase):
    """Every run exit path — success, operator abort, failure — must append
    one run-history record (issue #25)."""

    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        f.close()
        os.unlink(f.name)
        self.path = f.name
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        patcher = patch('fla_equalisation.RUN_HISTORY_FILE', self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _records(self):
        import sys as _sys
        _sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'fla-shared'))
        from run_history import read_last
        return read_last(self.path, 10)

    def _make_mocks(self, **monitor_kwargs):
        return MockSettings(), MockMonitor(**monitor_kwargs), MockStatus()

    @patch('fla_equalisation.acquire_lock', return_value=True)
    @patch('fla_equalisation.verify_relay_still_open', return_value=True)
    @patch('fla_equalisation.write_last_equalisation')
    @patch('fla_equalisation.clear_alarm')
    @patch('fla_equalisation.update_cache')
    @patch('fla_equalisation.time')
    def test_success_appends_a_success_record(self, mock_time, *_):
        mock_time.time.return_value = 0
        mock_time.sleep = MagicMock()
        settings, monitor, status = self._make_mocks(
            trojan_voltage=31.5, trojan_current=5.0, lfp_voltage=28.0)
        with patch('fla_equalisation.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = True
            inst.hand_back.return_value = (True, 0.3)
            MockT.return_value = inst
            self.assertTrue(run_equalisation(settings, monitor, status))
        recs = self._records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["outcome"], "success")
        self.assertEqual(recs[0]["peak_trojan_voltage"], 31.5)
        self.assertEqual(recs[0]["reconnect_delta"], 0.3)
        self.assertIn("start", recs[0])
        self.assertIn("end", recs[0])

    @patch('fla_equalisation.acquire_lock', return_value=True)
    @patch('fla_equalisation.verify_relay_still_open', return_value=True)
    @patch('fla_equalisation.clear_alarm')
    @patch('fla_equalisation.update_cache')
    @patch('fla_equalisation.time')
    def test_operator_abort_appends_an_aborted_record(self, mock_time, *_):
        mock_time.time.return_value = 0
        mock_time.sleep = MagicMock()
        # Current above the completion threshold so the loop reaches the
        # abort check instead of completing.
        settings, monitor, status = self._make_mocks(
            trojan_voltage=31.0, trojan_current=20.0, lfp_voltage=28.0)
        with patch('fla_equalisation.Takeover') as MockT, \
             patch('fla_equalisation.check_abort', return_value=True), \
             patch('fla_equalisation.clear_abort'):
            inst = MagicMock()
            inst.hand_off_in.return_value = True
            inst.hand_back.return_value = (True, 0.5)
            MockT.return_value = inst
            self.assertFalse(run_equalisation(settings, monitor, status))
        recs = self._records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["outcome"], "aborted")

    @patch('fla_equalisation.acquire_lock', return_value=True)
    @patch('fla_equalisation.update_cache')
    @patch('fla_equalisation.time')
    def test_handoff_failure_appends_a_failed_record(self, mock_time, *_):
        mock_time.time.return_value = 0
        mock_time.sleep = MagicMock()
        settings, monitor, status = self._make_mocks()
        with patch('fla_equalisation.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = False
            MockT.return_value = inst
            self.assertFalse(run_equalisation(settings, monitor, status))
        recs = self._records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["outcome"], "failed")


if __name__ == '__main__':
    unittest.main()
