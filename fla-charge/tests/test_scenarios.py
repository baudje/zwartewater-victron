#!/usr/bin/env python3
"""Scenario tests for the FLA charge service against the simulated boat.

See fla-equalisation/tests/test_scenarios.py for the why. The first scenario is
the 2026-10-04 Run Now charge that never got past the isolation check.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'fla-shared'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'fla-shared', 'tests'))

from helpers import dbus_mock_setup
dbus_mock_setup()

import fla_charge
from scenario_case import ScenarioCase


class Settings:
    enabled = True
    trojan_soc_trigger = 85
    lfp_soc_transition = 95
    lfp_cell_voltage_disconnect = 3.50
    current_taper_threshold = 20.0
    fla_bulk_voltage = 29.64
    fla_absorption_complete_current = 10.0
    fla_absorption_max_hours = 4.0
    fla_float_voltage = 27.0
    voltage_delta_max = 1.0
    phase1_timeout_hours = 8.0
    run_now = True   # the operator pressed Run Now

    def clear_run_now(self):
        self.run_now = False

    def _write(self, key, value):
        setattr(self, key, value)


class ChargeCase(ScenarioCase):
    module = fla_charge
    service_class = "FlaChargeService"
    settings_class = Settings
    last_run_attr = "LAST_CHARGE_FILE"

    def extra_patches(self):
        return [(fla_charge, "is_ac_available", lambda monitor: self.sim.ac),
                (fla_charge, "get_max_lfp_cell_voltage", lambda monitor: 3.37)]


class TestChargeScenarios(ChargeCase):
    def test_run_now_charge_on_a_full_bank(self):
        """2026-10-04: with ESS on keep-charged the charger held the Trojans at
        the bus voltage after relay 2 opened, the two banks never diverged, and
        the isolation check failed the run with the relay open."""
        sim = self.sim
        self.tick()
        self.assert_back_to_normal()
        self.assertGreaterEqual(sim.peak_v_trojan, 29.5, "reached the absorption voltage")
        self.assertLess(sim.peak_v_trojan, 30.2, "and not beyond it")
        self.assertEqual(sim.isolated_discharge_s, 0)
        self.assertEqual(sim.bms_lost_s, 0)
        self.assertTrue(os.path.exists(self.last_run), "charge recorded")
        self.assertEqual(self.alarms, [])

    def test_shore_power_lost_during_absorption(self):
        sim = self.sim

        def lose_shore_power(s):
            if s.relay == 0 and s.v_trojan >= 29.5:
                s.ac = False
        sim.events.append(lose_shore_power)
        self.tick()
        self.assert_back_to_normal()
        self.assertLessEqual(sim.isolated_discharge_s, 300, "reconnected within minutes")
        self.assertFalse(os.path.exists(self.last_run), "not recorded as a charge")
        self.assertTrue(self.alarms)


if __name__ == '__main__':
    unittest.main()
