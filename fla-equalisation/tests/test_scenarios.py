#!/usr/bin/env python3
"""Scenario tests: the real equalisation service tick, Takeover, relay control
and reconnect hold, run end to end against a simulated boat (SimBoat).

These exist because of 2026-10-03: every unit passed its own test, and the
failures were in the combination — a reboot mid-run, ESS behaviour, and a second
run stacked on the first. The first scenario is that incident.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'fla-shared'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'fla-shared', 'tests'))

from helpers import dbus_mock_setup
dbus_mock_setup()

import fla_equalisation
from scenario_case import ScenarioCase
from sim_boat import SimReboot
from test_fla_equalisation import MockSettings


class EqualisationCase(ScenarioCase):
    module = fla_equalisation
    service_class = "FlaEqualisationService"
    # due now, time window wide open
    settings_class = staticmethod(lambda: MockSettings(start_hour=0, end_hour=24))
    last_run_attr = "LAST_EQ_FILE"


class TestEqualisationScenarios(EqualisationCase):
    def test_normal_equalisation_on_shore_power(self):
        sim = self.sim
        self.tick()
        self.assert_back_to_normal()
        self.assertGreaterEqual(sim.peak_v_trojan, 31.4, "reached the EQ voltage")
        self.assertEqual(sim.isolated_discharge_s, 0, "Trojans never discharged while isolated")
        self.assertEqual(sim.bms_lost_s, 0)
        self.assertTrue(os.path.exists(self.last_run), "equalisation recorded")
        self.assertEqual(self.alarms, [])
        open_before = sim.relay_open_s
        self.tick(3)
        self.assertEqual(sim.relay_open_s, open_before, "no second run: the interval advanced")

    def test_incident_2026_10_03_reboot_with_relay_open(self):
        """EQ starts, the Cerbo reboots just after relay 2 opened and the CVL was
        raised. On 2026-10-03 this ended with the Trojans at 20.45V and DVCC
        restored to a dead temp battery."""
        sim = self.sim

        def cut_power_at_eq_voltage(s):
            if s.relay == 0 and s.temp_cvl is not None and s.temp_cvl > 31:
                raise SimReboot()
        sim.events.append(cut_power_at_eq_voltage)
        with self.assertRaises(SimReboot):
            self.tick()
        self.assertEqual(sim.bms_instance, 100, "the reboot hit mid-takeover")
        self.power_cut()

        self.tick()                      # first tick after boot: recover
        self.assertEqual(sim.bms_instance, 99, "DVCC back on the aggregate after one tick")
        self.assertEqual(sim.ess_state, 10)
        self.assertLessEqual(sim.bms_lost_s, 120, "BMS lost only between boot and the first tick")

        self.tick(2)                     # the equalisation is still due: it runs again
        self.assert_back_to_normal()
        self.assertGreaterEqual(sim.peak_v_trojan, 31.4)
        self.assertTrue(os.path.exists(self.last_run))
        self.assertEqual(sim.isolated_discharge_s, 0, "Trojans never discharged while isolated")
        self.assertGreater(sim.min_v_trojan, 26.0)
        self.assertLessEqual(sim.bms_lost_s, 120)
        self.assertEqual(self.alarms, [])

    def test_shore_power_lost_during_equalisation(self):
        sim = self.sim

        def lose_shore_power(s):
            if s.relay == 0 and s.v_trojan >= 31.4:
                s.ac = False
        sim.events.append(lose_shore_power)
        self.tick()
        self.assert_back_to_normal()
        self.assertLessEqual(sim.isolated_discharge_s, 300, "reconnected within minutes")
        self.assertGreater(sim.min_v_trojan, 26.0)
        self.assertFalse(os.path.exists(self.last_run), "not recorded as an equalisation")
        self.assertTrue(any("discharging" in a for a in self.alarms), self.alarms)
        open_before = sim.relay_open_s
        self.tick(5)
        self.assertEqual(sim.relay_open_s, open_before, "no immediate retry")

    def test_relay_stays_closed_when_ess_cannot_be_switched(self):
        sim = self.sim
        sim.ess_writable = False
        self.tick()
        self.assertEqual(sim.relay_open_s, 0, "LFP bank never isolated")
        self.assertEqual(sim.relay, 1)
        self.assertEqual(sim.bms_instance, 99)
        self.assertFalse(os.path.exists(self.last_run))
        self.assertTrue(self.alarms)


class TestEqualisationWithLfpInAbsorption(EqualisationCase):
    bus_voltage = 28.4

    def test_isolation_is_proven_without_headroom_below_lfp_safe(self):
        sim = self.sim
        self.tick()
        self.assert_back_to_normal()
        self.assertGreaterEqual(sim.peak_v_trojan, 31.4)
        self.assertTrue(os.path.exists(self.last_run))
        self.assertEqual(self.alarms, [])


if __name__ == '__main__':
    unittest.main()
