"""Tests for orphan temp-battery recovery.

After the 2026-06 Venus OS v3.80 upgrade restarted dbus-daemon mid-handoff,
a temp_battery_process.py was left running with no operation lock. Its
half-dead com.victronenergy.battery.fla_temp registration hung every Victron
dbusmonitor scan (systemcalc, the aggregate driver), taking the DVCC chain
down. recover_orphan_temp_battery() detects and clears that on startup.
"""

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

    def test_skips_when_operation_lock_held(self, mock_lock, mock_subproc):
        # A real equalisation/charge run owns the temp battery — never touch it.
        mock_lock.is_locked.return_value = True

        self.assertFalse(temp_battery.recover_orphan_temp_battery())
        mock_subproc.run.assert_not_called()

    def test_no_op_when_no_temp_process_running(self, mock_lock, mock_subproc):
        mock_lock.is_locked.return_value = False
        mock_subproc.run.return_value = MagicMock(returncode=1, stdout=b"")  # pgrep: none

        self.assertFalse(temp_battery.recover_orphan_temp_battery())
        # only the pgrep probe ran, never a kill
        self.assertEqual(mock_subproc.run.call_count, 1)

    def test_kills_orphan_when_running_with_no_lock(self, mock_lock, mock_subproc):
        mock_lock.is_locked.return_value = False
        mock_subproc.run.side_effect = [
            MagicMock(returncode=0, stdout=b"7487\n"),  # pgrep finds it
            MagicMock(returncode=0, stdout=b""),        # pkill
        ]

        self.assertTrue(temp_battery.recover_orphan_temp_battery())
        kill_cmd = mock_subproc.run.call_args_list[1].args[0]
        self.assertIn("pkill", kill_cmd)
        # The matcher must be specific to our spawned interpreter+script, not the
        # bare filename, so an editor/tail/grep on the path is never SIGKILLed.
        matcher = kill_cmd[-1]
        self.assertTrue(matcher.startswith("python3 "))
        self.assertIn("temp_battery_process.py", matcher)


if __name__ == '__main__':
    unittest.main()
