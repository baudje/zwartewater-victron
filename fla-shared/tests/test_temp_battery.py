#!/usr/bin/env python3
"""Tests for the temp battery subprocess and its parent wrapper.

These tests don't actually launch a D-Bus service — they verify the
contract pieces (service name, file paths, argv shape) that both
fla-charge and fla-equalisation depend on. The contract drifted in the
past (IMP-1 in the full-repo review): the subprocess hard-coded an
fla_equalisation-specific service name even though fla-charge launches
the same subprocess. These tests guard against that regression.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tests'))

from helpers import dbus_mock_setup
dbus_mock_setup()


class TestNeutralServiceName(unittest.TestCase):
    """The temp battery service name must NOT bake in either consumer's
    name. Both fla-charge and fla-equalisation share this subprocess
    (lock-protected so they never run concurrently); a service-specific
    name would mislead operators looking at logs/dashboards during a
    charge run."""

    def _read(self, path):
        with open(path) as f:
            return f.read()

    def test_subprocess_registers_under_neutral_name(self):
        src = self._read(os.path.join(os.path.dirname(__file__), '..', 'temp_battery_process.py'))
        # The exact registration line must use the neutral fla_temp name.
        self.assertIn(
            'com.victronenergy.battery.fla_temp',
            src,
            "temp_battery_process must register under com.victronenergy.battery.fla_temp",
        )
        # No legacy name should remain anywhere in the file.
        self.assertNotIn(
            'com.victronenergy.battery.fla_equalisation',
            src,
            "Legacy EQ-specific service name must be gone (IMP-1)",
        )

    def test_management_strings_are_neutral(self):
        src = self._read(os.path.join(os.path.dirname(__file__), '..', 'temp_battery_process.py'))
        # /Mgmt/Connection and /ProductName surface in VRM and the GUI.
        self.assertNotIn('"FLA Equalisation"', src,
                         "Mgmt strings must not bake in 'FLA Equalisation' (mixes consumers)")

    def test_cvl_file_path_is_neutral(self):
        # Both the wrapper and the subprocess must agree on the same neutral
        # file path. If they diverge, set_charge_voltage() in the wrapper
        # writes to one path while the subprocess polls a different one and
        # the CVL update is silently lost.
        wrapper = self._read(os.path.join(os.path.dirname(__file__), '..', 'temp_battery.py'))
        proc = self._read(os.path.join(os.path.dirname(__file__), '..', 'temp_battery_process.py'))
        self.assertIn('/tmp/fla_temp_cvl', wrapper)
        self.assertIn('/tmp/fla_temp_cvl', proc)
        self.assertNotIn('/tmp/fla_eq_cvl', wrapper)
        self.assertNotIn('/tmp/fla_eq_cvl', proc)


class TestDbusMonitorSkipsTempService(unittest.TestCase):
    """get_lfp_soc fallback iterates D-Bus battery services looking for a
    serialbattery. It must skip our temp battery (which has no LFP SoC),
    or the fallback would return the temp battery's None and miss the
    real serialbattery sitting next to it."""

    def test_filter_excludes_fla_temp_name(self):
        path = os.path.join(os.path.dirname(__file__), '..', 'dbus_monitor.py')
        with open(path) as f:
            src = f.read()
        # The filter must catch the new neutral name.
        self.assertIn('"fla_temp"', src,
                      "dbus_monitor.get_lfp_soc must skip fla_temp service")


if __name__ == '__main__':
    unittest.main()
