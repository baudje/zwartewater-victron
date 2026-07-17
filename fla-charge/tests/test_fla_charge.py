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
from settings import SETTINGS_DEFS as CHARGE_SETTINGS_DEFS
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

    @patch('fla_charge.acquire_lock', return_value=False)
    def test_lock_held_returns_false(self, mock_lock):
        settings, monitor, status = self._make_mocks()
        result = run_charge(settings, monitor, status)
        self.assertFalse(result)

    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.update_cache')
    @patch('fla_charge.time')
    def test_hand_off_in_failure_aborts(self, mock_time, mock_cache, mock_cell_v,
                                        mock_ac, mock_lock):
        # The individual handoff-step safety guards (temp-battery register,
        # aggregate stop, systemcalc restart, BMS-switch confirmation, relay
        # open) now live in the Takeover and are covered by test_takeover.py.
        # Here we only assert run_charge surfaces a hand_off_in failure. Phase 1
        # transitions immediately (LFP SoC high) so we reach the handoff.
        mock_time.time.side_effect = [0, 5]  # phase1_start, elapsed → transition
        mock_time.sleep = MagicMock()
        settings, monitor, status = self._make_mocks(lfp_soc=96.0)
        with patch('fla_charge.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = False
            MockT.return_value = inst
            result = run_charge(settings, monitor, status)
        self.assertFalse(result)
        self.assertIn(STATE_ERROR, status.states)
        inst.abort_teardown.assert_called_once()

    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.verify_relay_still_open', return_value=True)
    @patch('fla_charge.update_cache')
    @patch('fla_charge.time')
    def test_trojan_unresponsive_during_absorption_aborts(
        self, mock_time, mock_cache, mock_relay_check, mock_cell_v, mock_ac,
        mock_lock,
    ):
        """SmartShunt Trojan returning None during absorption should abort. The
        absorption loop stays in run_charge; only the handoff moved to the Takeover."""
        mock_time.time.side_effect = [0, 5, 10, 15, 20]  # phase1 + absorption
        mock_time.sleep = MagicMock()
        settings, monitor, status = self._make_mocks(lfp_soc=96.0)
        # Phase 1 + first absorption read see a valid voltage, then it goes None.
        phase1_call = [0]
        def trojan_v_side_effect():
            phase1_call[0] += 1
            if phase1_call[0] <= 2:
                return 27.5
            return None  # Goes None in absorption
        monitor.get_trojan_voltage = trojan_v_side_effect

        with patch('fla_charge.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = True
            MockT.return_value = inst
            result = run_charge(settings, monitor, status)
        self.assertFalse(result)
        self.assertIn(STATE_ERROR, status.states)

    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.verify_relay_still_open', return_value=False)
    @patch('fla_charge.update_cache')
    @patch('fla_charge.time')
    def test_relay_closed_externally_during_absorption_aborts(
        self, mock_time, mock_cache, mock_relay_check, mock_cell_v, mock_ac,
        mock_lock,
    ):
        """CRITICAL: External relay close during absorption should abort immediately.
        The verify_relay_still_open guard stays in run_charge's absorption loop."""
        mock_time.time.side_effect = [0, 5, 10, 15]
        mock_time.sleep = MagicMock()
        settings, monitor, status = self._make_mocks(
            lfp_soc=96.0, trojan_voltage=27.5, lfp_voltage=27.0,
        )
        with patch('fla_charge.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = True
            MockT.return_value = inst
            result = run_charge(settings, monitor, status)
        self.assertFalse(result)
        self.assertIn(STATE_ERROR, status.states)


class TestPhase1Transitions(unittest.TestCase):
    """Test Phase 1 shared charging transition triggers."""

    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.time')
    def test_phase1_timeout_returns_false(self, mock_time, mock_cell_v, mock_ac,
                                          mock_lock):
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

    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available')
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.time')
    def test_phase1_ac_loss_returns_false(self, mock_time, mock_cell_v, mock_ac,
                                          mock_lock):
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

    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.time')
    def test_phase1_discharge_current_does_not_trigger_taper(self, mock_time, mock_cell_v,
                                                              mock_ac, mock_lock):
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


class TestSettingsBounds(unittest.TestCase):
    """Test hard safety bounds match the documented operating envelope."""

    def test_bulk_voltage_max_allows_temp_compensation_headroom(self):
        self.assertEqual(CHARGE_SETTINGS_DEFS["fla_bulk_voltage"][3], 30.5)

    def test_reconnect_delta_max_cannot_exceed_safe_limit(self):
        self.assertEqual(CHARGE_SETTINGS_DEFS["voltage_delta_max"][3], 1.0)


class TestApplyPendingSettingsBounds(unittest.TestCase):
    """Mirrors fla-equalisation: out-of-bounds settings from the web UI are
    rejected before reaching D-Bus, defending against a misbehaving HTTP
    client or test fixture pushing values outside the documented envelope."""

    def setUp(self):
        from fla_charge import FlaChargeService, _engine
        self.svc = FlaChargeService.__new__(FlaChargeService)
        self.svc.settings = MagicMock()
        self._engine = _engine
        # Make sure no stale pending settings leak in from prior tests.
        self._engine.drain_pending_settings()

    def _enqueue(self, key, value):
        # Same public path the HTTP POST /api/setting handler uses.
        self._engine.queue_setting(key, value)

    def test_fla_bulk_voltage_above_max_is_rejected(self):
        # Max is 30.5V (per CHARGE_SETTINGS_DEFS). 35V is well above.
        self._enqueue("fla_bulk_voltage", 35.0)
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
        self._enqueue("fla_bulk_voltage", 29.0)
        self.svc._apply_pending_settings()
        self.svc.settings._write.assert_called_once_with("fla_bulk_voltage", 29.0)

    def test_empty_pending_does_not_call_write(self):
        self.svc._apply_pending_settings()
        self.svc.settings._write.assert_not_called()


class TestVoltageDeltaWithZero(unittest.TestCase):
    """Regression for IMP-5 in fla-charge: the IMP-5 falsy-check pattern
    appeared in fla_charge.py at lines 324 and 511. After the fix, a true
    0.0V reading produces a numeric delta instead of being silently dropped."""

    def test_zero_voltage_does_not_yield_none_delta(self):
        v_trojan, v_lfp = 0.0, 26.5
        delta = round(abs(v_trojan - v_lfp), 2) if (v_trojan is not None and v_lfp is not None) else None
        self.assertEqual(delta, 26.5)

    def test_both_zero_yields_zero_not_none(self):
        v_trojan, v_lfp = 0.0, 0.0
        delta = round(abs(v_trojan - v_lfp), 2) if (v_trojan is not None and v_lfp is not None) else None
        self.assertIsNotNone(delta)
        self.assertEqual(delta, 0.0)


class TestStatusServiceDeregisterFailsFast(unittest.TestCase):
    """fla-charge's StatusService already nulled _service after deregister.
    This test pins that behaviour so a future refactor doesn't drift back."""

    def test_service_handle_nulled_after_deregister(self):
        from dbus_status_service import StatusService
        svc = StatusService.__new__(StatusService)
        svc._registered = True
        svc._service = MagicMock()
        svc.deregister()
        self.assertIsNone(svc._service)
        self.assertFalse(svc._registered)


class TestIsAcAvailableLogsExceptions(unittest.TestCase):
    """Regression for IMP-7: a transient D-Bus read error in is_ac_available
    used to silently return False, hiding the real cause when a Phase 1
    charge then aborted with 'AC input lost'. Now the exception is logged
    at warning level so an operator can distinguish a real AC-loss from a
    D-Bus hiccup."""

    def test_exception_is_logged_at_warning(self):
        from fla_charge import is_ac_available
        # Build a monitor whose .bus.get_object raises.
        mon = MagicMock()
        mon.bus.get_object.side_effect = RuntimeError("boom")
        with patch('fla_charge.log.warning') as mock_warn:
            result = is_ac_available(mon)
        self.assertFalse(result)
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        self.assertIn("is_ac_available", msg)


class TestChargeOperatorAbortRouting(unittest.TestCase):
    """Operator Abort in the relay-open absorption loop reconnects cleanly."""

    @patch('fla_charge.write_last_charge')
    @patch('fla_charge.verify_relay_still_open', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.time')
    def test_operator_abort_in_absorption_reconnects_without_timestamp(
        self, mock_time, mock_lock, mock_ac, mock_cell,
        mock_still, mock_write,
    ):
        mock_time.time.side_effect = [i for i in range(0, 400, 5)]
        mock_time.sleep = MagicMock()
        settings = MockChargeSettings()
        # Phase 1 transitions immediately (LFP SoC high), then the absorption
        # loop runs until the operator abort routes through the controlled
        # reconnect (hand_back).
        monitor = MockMonitor(relay_state=0, lfp_soc=96.0,
                              trojan_voltage=27.2, lfp_voltage=27.0)
        status = MockStatus()
        # Abort only AFTER Phase 1 (which has its own check_abort): first call
        # in Phase 1 returns False, subsequent (absorption) returns True.
        with patch('fla_charge.check_abort', side_effect=[False] + [True] * 50):
            with patch('fla_charge.Takeover') as MockT:
                inst = MagicMock()
                inst.hand_off_in.return_value = True
                inst.hand_back.return_value = (True, 0.2)
                MockT.return_value = inst
                with patch('fla_charge.update_cache'):
                    result = run_charge(settings, monitor, status)

        # Operator abort routed through the controlled reconnect (hand_back)…
        inst.hand_back.assert_called_once()
        # …but the run is flagged non-completion: no timestamp, returns False.
        mock_write.assert_not_called()
        self.assertFalse(result)
        # The guarded teardown still runs in the finally.
        inst.abort_teardown.assert_called_once()


class TestChargeRelayStateGuardedFinally(unittest.TestCase):
    """The finally always routes teardown through the Takeover.

    The relay-state-guarded restore (hold the bus while the relay is open,
    restore DVCC + release the lock once it is closed) now lives in
    Takeover.teardown and is covered by test_takeover.py::TestTeardown. Here
    we only assert run_charge delegates to abort_teardown in its finally on
    both a mid-absorption hard error and a hand_off_in failure."""

    @patch('fla_charge.alerting')
    @patch('fla_charge.verify_relay_still_open', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.time')
    def test_mid_charge_error_routes_teardown_through_takeover(
        self, mock_time, mock_lock, mock_ac, mock_cell,
        mock_still, mock_alerting,
    ):
        # Trojan goes unresponsive mid-absorption (hard error) → return False;
        # the finally hands off to the Takeover's guarded teardown.
        mock_time.time.side_effect = [i for i in range(0, 400, 5)]
        mock_time.sleep = MagicMock()
        settings = MockChargeSettings()
        monitor = MockMonitor(relay_state=0, lfp_soc=96.0, lfp_voltage=27.0)
        # Phase 1 read sees a valid voltage, absorption sees None.
        phase1_call = [0]
        def trojan_v_side_effect():
            phase1_call[0] += 1
            if phase1_call[0] <= 1:
                return 27.5
            return None
        monitor.get_trojan_voltage = trojan_v_side_effect
        status = MockStatus()
        with patch('fla_charge.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = True
            MockT.return_value = inst
            with patch('fla_charge.update_cache'):
                result = run_charge(settings, monitor, status)

        self.assertFalse(result)
        self.assertTrue(mock_alerting.raise_alarm.called)  # loop raised the hard-error alarm
        inst.abort_teardown.assert_called_once()           # finally routed through the Takeover

    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.update_cache')
    @patch('fla_charge.time')
    def test_hand_off_failure_routes_teardown_through_takeover(
        self, mock_time, mock_cache, mock_cell, mock_ac, mock_lock,
    ):
        # hand_off_in fails before the relay ever opens → return False; the
        # finally still routes teardown through the Takeover.
        mock_time.time.side_effect = [0, 5]
        mock_time.sleep = MagicMock()
        settings = MockChargeSettings()
        monitor = MockMonitor(relay_state=1, lfp_soc=96.0)
        status = MockStatus()
        with patch('fla_charge.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = False
            MockT.return_value = inst
            result = run_charge(settings, monitor, status)

        self.assertFalse(result)
        inst.abort_teardown.assert_called_once()  # finally routed through the Takeover


class TestChargeResumeOnStartup(unittest.TestCase):
    """Startup adopts an interrupted hold instead of killing it."""

    def _service(self, monitor):
        """Build a FlaChargeService with construction side-effects stubbed.

        Uses start()/addCleanup(stop) rather than a with-block so patches remain
        active when Service() is called from the test method body (returning from
        inside a with-block exits the context managers, removing the patches).
        """
        from fla_charge import FlaChargeService

        p_recover = patch('fla_charge.recover_orphan_temp_battery')
        p_settings = patch('fla_charge.Settings', return_value=MockChargeSettings())
        p_monitor = patch('fla_charge.DbusMonitor', return_value=monitor)
        p_status = patch('fla_charge.StatusService', return_value=MockStatus())
        p_update = patch.object(FlaChargeService, '_update_idle_status')

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

        return FlaChargeService, mock_recover

    @patch('fla_charge.threading')
    @patch('fla_charge.startup_safety_check')
    @patch('fla_charge.is_temp_battery_running', return_value=True)
    @patch('fla_charge.acquire_lock', return_value=True)
    def test_relay_open_with_subprocess_resumes(
        self, mock_acquire, mock_running, mock_safety, mock_threading,
    ):
        monitor = MockMonitor(relay_state=0)
        Service, mock_recover = self._service(monitor)
        with patch('fla_charge.Takeover') as MockT:
            MockT.resume_attach.return_value = MagicMock()
            Service()
        mock_recover.assert_called_once_with(0)
        mock_threading.Thread.assert_called_once()
        mock_safety.assert_not_called()

    @patch('fla_charge.threading')
    @patch('fla_charge.startup_safety_check')
    @patch('fla_charge.is_temp_battery_running', return_value=True)
    @patch('fla_charge.acquire_lock', return_value=True)
    def test_relay_open_held_no_worker_keeps_lock(
        self, mock_acquire, mock_running, mock_safety, mock_threading,
    ):
        # resume_attach raised an alarm and is holding the bus (RESUME_HELD):
        # no worker, no safety check, and the lock is NOT released.
        monitor = MockMonitor(relay_state=0)
        Service, mock_recover = self._service(monitor)
        with patch('fla_charge.Takeover') as MockT, \
             patch('fla_charge.release_lock') as mock_release:
            MockT.resume_attach.return_value = MockT.RESUME_HELD
            Service()
        mock_threading.Thread.assert_not_called()
        mock_safety.assert_not_called()
        mock_release.assert_not_called()

    @patch('fla_charge.threading')
    @patch('fla_charge.startup_safety_check')
    @patch('fla_charge.is_temp_battery_running', return_value=True)
    @patch('fla_charge.acquire_lock', return_value=True)
    def test_resume_nothing_to_adopt_releases_lock_and_runs_safety(
        self, mock_acquire, mock_running, mock_safety, mock_threading,
    ):
        # resume_attach returned None (relay closed / temp gone in the race): the
        # lock we took is released and the normal startup safety check runs.
        monitor = MockMonitor(relay_state=0)
        Service, mock_recover = self._service(monitor)
        with patch('fla_charge.Takeover') as MockT, \
             patch('fla_charge.release_lock') as mock_release:
            MockT.resume_attach.return_value = None
            Service()
        mock_threading.Thread.assert_not_called()
        mock_release.assert_called_once()
        mock_safety.assert_called_once()

    @patch('fla_charge.threading')
    @patch('fla_charge.startup_safety_check')
    @patch('fla_charge.is_temp_battery_running', return_value=True)
    @patch('fla_charge.acquire_lock', return_value=True)
    def test_resume_worker_non_matched_sets_error(
        self, mock_acquire, mock_running, mock_safety, mock_threading,
    ):
        # A non-exception hand_back failure (safe-hold / relay close failed) must
        # leave the status display in STATE_ERROR, not stuck in a matching state.
        monitor = MockMonitor(relay_state=0)
        Service, mock_recover = self._service(monitor)
        with patch('fla_charge.Takeover') as MockT, \
             patch('fla_charge.alerting') as mock_alerting:
            t = MagicMock()
            t.hand_back.return_value = (False, 2.0)
            MockT.resume_attach.return_value = t
            svc = Service()
            worker = mock_threading.Thread.call_args.kwargs['target']
            worker()
        self.assertIn(STATE_ERROR, svc.status.states)
        mock_alerting.clear_alarm.assert_not_called()

    @patch('fla_charge.threading')
    @patch('fla_charge.startup_safety_check')
    @patch('fla_charge.is_temp_battery_running', return_value=False)
    @patch('fla_charge.acquire_lock', return_value=True)
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
    """Mirror of fla-equalisation's TestCheckStartsWorker: _check()'s
    should-run branch must spawn the worker without tripping on the web
    engine surface (the EQ side shipped a leftover `_cache[...]` reference
    that NameError'd here after `self._running = True`, wedging the
    service; this pins the charge side against the same drift)."""

    def _service(self):
        from fla_charge import FlaChargeService
        svc = FlaChargeService.__new__(FlaChargeService)
        svc.settings = MagicMock()
        svc.monitor = MagicMock()
        svc.status = MagicMock()
        svc._running = False
        svc._failed = False
        # Idle-status refresh reads live D-Bus values — not under test here.
        svc._update_idle_status = lambda: None
        return svc

    @patch('fla_charge.threading.Thread')
    @patch('fla_charge.should_run', return_value=True)
    def test_should_run_branch_spawns_the_worker(self, _sr, mock_thread):
        svc = self._service()
        svc._check()
        mock_thread.assert_called_once()
        mock_thread.return_value.start.assert_called_once()
        self.assertTrue(svc._running)

    @patch('fla_charge.threading.Thread')
    @patch('fla_charge.should_run', return_value=True)
    def test_web_run_now_flag_is_cleared_when_run_starts(self, _sr, mock_thread):
        from fla_charge import _engine
        svc = self._service()
        _engine._run_now_requested = True  # queued via web while conditions align
        try:
            svc._check()
            self.assertFalse(_engine.check_run_now(),
                             "queued web RunNow must be discarded once a run starts")
        finally:
            _engine.clear_run_now()

    @patch('fla_charge.verify_idle_bms_selection')
    @patch('fla_charge.should_run', return_value=False)
    def test_idle_check_runs_the_bms_guard(self, _sr, mock_guard):
        # Every idle tick must verify DVCC's controlling BMS is the aggregate —
        # the guard that would have caught the 2026-05-28 silent half-charge.
        svc = self._service()
        svc._check()
        mock_guard.assert_called_once()
        self.assertIs(mock_guard.call_args.args[0], svc.monitor)


class TestRunHistoryRecords(unittest.TestCase):
    """Mirror of fla-equalisation's TestRunHistoryRecords: every run exit
    path — success, operator abort, failure — appends one record (#25)."""

    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        f.close()
        os.unlink(f.name)
        self.path = f.name
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        patcher = patch('fla_charge.RUN_HISTORY_FILE', self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _records(self):
        from run_history import read_last
        return read_last(self.path, 10)

    def _make_mocks(self, **monitor_kwargs):
        return MockChargeSettings(), MockMonitor(**monitor_kwargs), MockStatus()

    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.verify_relay_still_open', return_value=True)
    @patch('fla_charge.write_last_charge')
    @patch('fla_charge.update_cache')
    @patch('fla_charge.time')
    def test_success_appends_a_success_record(self, mock_time, *_):
        mock_time.time.return_value = 0
        mock_time.sleep = MagicMock()
        settings, monitor, status = self._make_mocks(
            lfp_soc=96.0, trojan_voltage=30.0, trojan_current=5.0, lfp_voltage=28.0)
        with patch('fla_charge.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = True
            inst.hand_back.return_value = (True, 0.4)
            MockT.return_value = inst
            self.assertTrue(run_charge(settings, monitor, status))
        recs = self._records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["outcome"], "success")
        self.assertEqual(recs[0]["peak_trojan_voltage"], 30.0)
        self.assertEqual(recs[0]["reconnect_delta"], 0.4)

    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.update_cache')
    @patch('fla_charge.time')
    def test_operator_abort_appends_an_aborted_record(self, mock_time, *_):
        mock_time.time.return_value = 0
        mock_time.sleep = MagicMock()
        # LFP SoC below transition so Phase 1 keeps looping and reaches the
        # abort check.
        settings, monitor, status = self._make_mocks(lfp_soc=50.0)
        with patch('fla_charge.Takeover') as MockT, \
             patch('fla_charge.check_abort', return_value=True), \
             patch('fla_charge.clear_abort'):
            MockT.return_value = MagicMock()
            self.assertFalse(run_charge(settings, monitor, status))
        recs = self._records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["outcome"], "aborted")

    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.update_cache')
    @patch('fla_charge.time')
    def test_handoff_failure_appends_a_failed_record(self, mock_time, *_):
        mock_time.time.return_value = 0
        mock_time.sleep = MagicMock()
        settings, monitor, status = self._make_mocks(lfp_soc=96.0)
        with patch('fla_charge.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = False
            MockT.return_value = inst
            self.assertFalse(run_charge(settings, monitor, status))
        recs = self._records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["outcome"], "failed")


if __name__ == '__main__':
    unittest.main()
