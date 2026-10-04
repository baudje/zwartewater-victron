#!/usr/bin/env python3
"""Scenario tests: the real equalisation service tick, Takeover, relay control
and reconnect hold, run end to end against a simulated boat (SimBoat).

These exist because of 2026-10-03: every unit passed its own test, and the
failures were in the combination — a reboot mid-run, ESS behaviour, and a second
run stacked on the first. The first scenario is that incident.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'fla-shared'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'fla-shared', 'tests'))

from helpers import dbus_mock_setup, MockStatus
dbus_mock_setup()

import alerting
import lock
import takeover
import fla_equalisation
from sim_boat import SimBoat, SimReboot


class Settings:
    """The equalisation settings, due now and with the time window wide open."""
    eq_voltage = 31.5
    eq_current_complete = 10.0
    eq_timeout_hours = 2.5
    float_voltage = 27.0
    voltage_delta_max = 1.0
    days_between = 90
    start_hour, end_hour = 0, 24
    lfp_soc_min = 95
    enabled = True
    run_now = False

    def clear_run_now(self):
        self.run_now = False

    def _write(self, key, value):
        setattr(self, key, value)


class InlineThread:
    """threading.Thread stand-in: the worker runs to completion inside start()."""
    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class ScenarioCase(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="simboat-")   # stands in for /data
        self.addCleanup(shutil.rmtree, self.data, True)
        self.sim = sim = SimBoat(self.data)
        self.alarms = []
        self.last_eq = os.path.join(self.data, "last_equalisation")

        def raise_alarm(message, status_service=None):
            self.alarms.append(message)

        def clear_alarm(status_service=None):
            self.alarms.clear()

        for target, attr, value in [
            ("time.time", None, sim.now), ("time.monotonic", None, sim.now),
            ("time.sleep", None, sim.sleep),
            (lock, "LOCK_FILE", os.path.join(self.data, "operation.lock")),
            # /proc is not there on a dev machine; liveness is the PID check alone.
            (lock, "_pid_matches_service", lambda pid, service: True),
            (takeover, "SNAPSHOT_FILE", os.path.join(self.data, "dvcc_originals.json")),
            (takeover, "TempBatteryService", sim.temp_battery_class()),
            (takeover, "is_temp_battery_running", lambda: sim.temp_cvl is not None),
            (takeover, "aggregate_driver", sim.aggregate_driver()),
            (alerting, "raise_alarm", raise_alarm), (alerting, "clear_alarm", clear_alarm),
            (alerting, "activate_buzzer", lambda *a, **k: None),
            (fla_equalisation, "raise_alarm", raise_alarm),
            (fla_equalisation, "clear_alarm", clear_alarm),
            (fla_equalisation, "LAST_EQ_FILE", self.last_eq),
            (fla_equalisation, "RUN_HISTORY_FILE", os.path.join(self.data, "run-history.jsonl")),
            (fla_equalisation.threading, "Thread", InlineThread),
        ]:
            p = patch(target, value) if attr is None else patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        takeover._idle_bms_alarm_active = False
        self.service = self.start_service()

    def start_service(self):
        """A fresh service process (in-memory state such as the backoff is new)."""
        svc = fla_equalisation.FlaEqualisationService.__new__(
            fla_equalisation.FlaEqualisationService)
        svc.settings = Settings()
        svc.monitor = self.sim
        svc.status = MockStatus()
        svc._running = False
        svc._failed = False
        svc._update_idle_status = lambda: None
        return svc

    def tick(self, times=1):
        """One 60s service tick; a run started by the tick completes inside it."""
        for _ in range(times):
            self.sim.sleep(60)
            self.service._check()

    def power_cut(self):
        """Reboot the Cerbo: the lock's owner is gone, the service starts fresh."""
        self.sim.reboot()
        self.alarms.clear()
        lock_file = lock.LOCK_FILE
        if os.path.exists(lock_file):
            with open(lock_file) as f:
                info = json.load(f)
            info["pid"] = 2 ** 30   # a PID that does not exist after the reboot
            with open(lock_file, "w") as f:
                json.dump(info, f)
        self.service = self.start_service()

    def assert_back_to_normal(self):
        sim = self.sim
        self.assertEqual(sim.relay, 1, "relay 2 closed")
        self.assertEqual(sim.bms_instance, 99, "DVCC on the aggregate")
        self.assertEqual(sim.battery_service, "com.victronenergy.battery/277")
        self.assertEqual(sim.ess_state, 10, "ESS mode restored")
        self.assertEqual(sim.max_charge_voltage, 32.0)
        self.assertIsNone(sim.temp_cvl, "temp battery stopped")
        self.assertTrue(sim.aggregate_running)
        self.assertFalse(os.path.exists(lock.LOCK_FILE), "operation lock released")
        self.assertIsNone(takeover.load_originals(), "DVCC snapshot consumed")
        self.assertEqual(sim.lfp_overvoltage_s, 0, "LFP never saw more than 28.4V")


class TestEqualisationScenarios(ScenarioCase):
    def test_normal_equalisation_on_shore_power(self):
        sim = self.sim
        self.tick()
        self.assert_back_to_normal()
        self.assertGreaterEqual(sim.peak_v_trojan, 31.4, "reached the EQ voltage")
        self.assertEqual(sim.isolated_discharge_s, 0, "Trojans never discharged while isolated")
        self.assertEqual(sim.bms_lost_s, 0)
        self.assertTrue(os.path.exists(self.last_eq), "equalisation recorded")
        self.assertEqual(self.alarms, [])
        self.tick(3)
        self.assertEqual(sim.relay, 1, "no second run: the interval advanced")

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
        self.assertTrue(os.path.exists(self.last_eq))
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
        self.assertFalse(os.path.exists(self.last_eq), "not recorded as an equalisation")
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
        self.assertFalse(os.path.exists(self.last_eq))
        self.assertTrue(self.alarms)


if __name__ == '__main__':
    unittest.main()
