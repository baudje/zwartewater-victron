"""Tests for relay-aware orphan recovery and temp-battery attach mode."""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import dbus_mock_setup
dbus_mock_setup()
import temp_battery


@patch('temp_battery.subprocess')
@patch('temp_battery.lock')
class TestRecoverOrphanTempBattery(unittest.TestCase):

    def test_relay_open_never_kills(self, mock_lock, mock_subproc):
        # Relay open → temp battery is a live hold, not an orphan. Never probe/kill.
        mock_lock.is_locked.return_value = False
        self.assertFalse(temp_battery.recover_orphan_temp_battery(relay_state=0))
        mock_subproc.run.assert_not_called()

    def test_skips_when_operation_lock_held(self, mock_lock, mock_subproc):
        mock_lock.is_locked.return_value = True
        self.assertFalse(temp_battery.recover_orphan_temp_battery(relay_state=1))
        mock_subproc.run.assert_not_called()

    def test_no_op_when_no_temp_process_running(self, mock_lock, mock_subproc):
        mock_lock.is_locked.return_value = False
        mock_subproc.run.return_value = MagicMock(returncode=1, stdout=b"")
        self.assertFalse(temp_battery.recover_orphan_temp_battery(relay_state=1))
        self.assertEqual(mock_subproc.run.call_count, 1)

    def test_kills_orphan_when_relay_closed_and_no_lock(self, mock_lock, mock_subproc):
        mock_lock.is_locked.return_value = False
        mock_subproc.run.side_effect = [
            MagicMock(returncode=0, stdout=b"7487\n"),  # pgrep finds it
            MagicMock(returncode=0, stdout=b""),        # pkill
        ]
        self.assertTrue(temp_battery.recover_orphan_temp_battery(relay_state=1))
        kill_cmd = mock_subproc.run.call_args_list[1].args[0]
        self.assertIn("pkill", kill_cmd)
        matcher = kill_cmd[-1]
        self.assertTrue(matcher.startswith("python3 "))
        self.assertIn("temp_battery_process.py", matcher)

    def test_default_relay_state_none_does_not_kill(self, mock_lock, mock_subproc):
        # Relay state unknown (no arg → None) → cannot confirm relay closed.
        # Must NOT kill — same guard as relay open. It's the relay guard (not the
        # lock) that fires here, so we set is_locked=False to prove it.
        mock_lock.is_locked.return_value = False
        self.assertFalse(temp_battery.recover_orphan_temp_battery())
        mock_subproc.run.assert_not_called()

    def test_relay_none_never_kills(self, mock_lock, mock_subproc):
        # Explicit relay_state=None also must not probe or kill.
        mock_lock.is_locked.return_value = False
        self.assertFalse(temp_battery.recover_orphan_temp_battery(relay_state=None))
        mock_subproc.run.assert_not_called()


@patch('temp_battery.subprocess')
class TestIsTempBatteryRunning(unittest.TestCase):

    def test_true_when_pgrep_finds_process(self, mock_subproc):
        mock_subproc.run.return_value = MagicMock(returncode=0, stdout=b"7487\n")
        self.assertTrue(temp_battery.is_temp_battery_running())

    def test_false_when_none(self, mock_subproc):
        mock_subproc.run.return_value = MagicMock(returncode=1, stdout=b"")
        self.assertFalse(temp_battery.is_temp_battery_running())


@patch('temp_battery.subprocess')
class TestAttachMode(unittest.TestCase):

    def test_attach_marks_registered_without_popen(self, mock_subproc):
        svc = temp_battery.TempBatteryService(device_instance=100)
        self.assertTrue(svc.attach())
        self.assertTrue(svc._registered)
        self.assertTrue(svc._attached)
        self.assertIsNone(svc._process)
        mock_subproc.Popen.assert_not_called()

    def test_attached_set_charge_voltage_writes_file(self, mock_subproc):
        svc = temp_battery.TempBatteryService(device_instance=100)
        svc.attach()
        m = MagicMock()
        with patch('builtins.open', m):
            svc.set_charge_voltage(27.0)
        m.assert_called_once_with(temp_battery.CVL_FILE, "w")

    def test_attached_deregister_pkills(self, mock_subproc):
        svc = temp_battery.TempBatteryService(device_instance=100)
        svc.attach()
        with patch('temp_battery.os.unlink'):
            svc.deregister()
        pkill_cmd = mock_subproc.run.call_args[0][0]
        self.assertIn("pkill", pkill_cmd)
        self.assertEqual(pkill_cmd[-1], temp_battery.PROCESS_MATCH)
        self.assertFalse(svc._registered)
        self.assertFalse(svc._attached)


if __name__ == '__main__':
    unittest.main()
