"""ScenarioCase — runs a real FLA service against SimBoat.

Subclasses name the service module; setUp swaps the clock, the lock and snapshot
files (a temp dir stands in for /data), the temp battery, the aggregate driver
and the alarms for the simulation. `tick()` advances 60 simulated seconds and
calls the service's real `_check()`; a run started by a tick finishes inside it.
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import alerting
import lock
import takeover
from helpers import MockStatus
from sim_boat import SimBoat


class InlineThread:
    """threading.Thread stand-in: the worker runs to completion inside start()."""
    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class ScenarioCase(unittest.TestCase):
    module = None          # the service module under test
    service_class = None   # its service class name
    settings_class = None
    last_run_attr = None   # module attribute holding the last-run timestamp path
    bus_voltage = 26.9     # bus at handoff; 28.4 = LFP bank in absorption

    def extra_patches(self):
        return []

    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="simboat-")   # stands in for /data
        self.addCleanup(shutil.rmtree, self.data, True)
        self.sim = sim = SimBoat(self.data, bus_voltage=self.bus_voltage)
        self.alarms = []
        self.last_run = os.path.join(self.data, "last_run")

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
            (self.module, self.last_run_attr, self.last_run),
            (self.module, "RUN_HISTORY_FILE", os.path.join(self.data, "run-history.jsonl")),
            (self.module.threading, "Thread", InlineThread),
        ] + [(self.module, name, fn) for name, fn in
             (("raise_alarm", raise_alarm), ("clear_alarm", clear_alarm))
             if hasattr(self.module, name)] + self.extra_patches():
            p = patch(target, value) if attr is None else patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        takeover._idle_bms_alarm_active = False
        self.service = self.start_service()

    def start_service(self):
        """A fresh service process (in-memory state such as the backoff is new)."""
        cls = getattr(self.module, self.service_class)
        svc = cls.__new__(cls)
        svc.settings = self.settings_class()
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
        self.assertEqual(sim.shared_voltage_sense, 1, "shared voltage sense restored")
        self.assertEqual(sim.max_charge_voltage, 32.0)
        self.assertIsNone(sim.temp_cvl, "temp battery stopped")
        self.assertTrue(sim.aggregate_running)
        self.assertFalse(os.path.exists(lock.LOCK_FILE), "operation lock released")
        self.assertIsNone(takeover.load_originals(), "DVCC snapshot consumed")
        self.assertEqual(sim.lfp_overvoltage_s, 0, "LFP never saw more than 28.4V")
