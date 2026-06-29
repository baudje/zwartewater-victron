# Takeover Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the duplicated, ordering-sensitive DVCC handoff / reconnect / teardown / resume logic from both FLA services into one deep shared **Takeover** module (`fla-shared/takeover.py`), and in doing so fix the latent resume bug by restoring DVCC from a **persisted snapshot of the real originals** instead of hardcoded constants.

**Architecture:** A stateful `Takeover` object owns one operation's lock-release, aggregate-driver, DVCC-selection, and temp-battery lifecycle. It exposes `hand_off_in()` (the ordered setup ending with relay open + CVL raised), `hand_back()` (float-hold → close → guarded teardown), `teardown()`/`abort_teardown()` (the single relay-state-guarded restore), and a `resume_attach()` classmethod. It uses **composition** — it calls the existing `DbusMonitor`, `relay_control`, `voltage_matching`, `temp_battery`, `aggregate_driver`, and `lock` modules; it does NOT move methods out of `DbusMonitor` (that slimming is a deferred follow-up). The two services keep their scheduling and their charging loops and call the Takeover for everything between.

**Tech Stack:** Python 3 (Venus OS), D-Bus (`dbus-python`), GLib, daemontools. Tests use `unittest` + `unittest.mock`, mocking all D-Bus/GLib and the `time` module. Shared test helpers in `fla-shared/tests/helpers.py` (`MockMonitor`, `MockStatus`, `dbus_mock_setup`).

**Spec sources:** `CONTEXT.md` (domain language: Takeover, hand-off in, hand-back, DVCC originals, safe-hold, Operation profile), `docs/adr/0001-persist-dvcc-originals.md`, `docs/adr/0002-service-scaffolding-by-composition.md`. This plan covers Candidates 1+2 only (the Takeover). Candidate 3 (Operation-profile scaffolding) is a separate plan.

## Global Constraints

- **Firm safety invariant:** never auto-close relay 2 while delta > `RELAY_CLOSE_DELTA_MAX` (1.0V); never restore DVCC / release the lock / deregister the temp battery / restart the aggregate while relay 2 is open (`get_relay_state() != 1`). These already hold in the shipped code — the refactor must preserve them exactly.
- **DVCC originals are persisted, never guessed** (ADR-0001). The snapshot of `/Settings/SystemSetup/{BatteryService, BmsInstance, MaxChargeVoltage}` is captured at `hand_off_in` before any are changed, written to `/tmp/fla_dvcc_originals.json`, and is the ONLY source for every restore (happy path, finally, resume). Missing snapshot on resume → hold-and-alarm, never restore to a constant.
- **Composition, not relocation:** the Takeover calls `monitor.set_battery_service_setting`, `monitor.set_bms_instance`, `monitor.set_dvcc_max_charge_voltage`, `monitor.restart_systemcalc`, `monitor.wait_for_service_instance`, `monitor.wait_for_bms_selection` — these stay in `DbusMonitor`. Do NOT move them in this plan.
- **Behaviour preservation:** the only intended behaviour change is the resume/teardown restore values (persisted originals instead of `aggregate`/`-1`/`28.4`) and the snapshot lifecycle. The full existing suite (213 tests: 116 shared + 56 EQ + 41 charge) must stay green except tests whose semantics this plan deliberately changes (the resume restore values; service tests that move to Takeover tests) — those are rewritten, not deleted silently.
- **Lock split:** the caller acquires the operation lock (a go/no-go gate, returns bool); the Takeover owns the guarded RELEASE (released only when relay confirmed closed). The Takeover never calls `lock.acquire`.
- **Per-service display states differ:** the Takeover sets status phase states via a `TakeoverStates` namedtuple passed in at construction, so each service's existing state display is preserved exactly.
- Temp battery registration current: `60.0` A (unchanged). Temp device instance: `100`. Temp battery service path string: `"com.victronenergy.battery/100"`.
- Commit messages must read as written by a human — NO AI attribution, no `Co-Authored-By`.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `fla-shared/takeover.py` | The deep Takeover module: snapshot persistence, `Takeover` class (hand_off_in / hand_back / teardown / abort_teardown / resume_attach), `TakeoverStates` | Create |
| `fla-shared/tests/test_takeover.py` | Unit tests for the Takeover (ordering, guarded teardown, snapshot, resume) | Create |
| `fla-equalisation/fla_equalisation.py` | EQ state machine | `run_equalisation` and `FlaEqualisationService` call the Takeover instead of inline handoff/teardown/resume |
| `fla-charge/fla_charge.py` | Charge state machine | Same |
| `fla-equalisation/tests/test_fla_equalisation.py` | EQ tests | Safety-guard tests move to Takeover; service tests mock `Takeover` |
| `fla-charge/tests/test_fla_charge.py` | Charge tests | Same |

---

## Task 1: Snapshot persistence (DVCC originals)

**Files:**
- Create: `fla-shared/takeover.py` (snapshot helpers + module constants only, for now)
- Test: `fla-shared/tests/test_takeover.py`

**Interfaces:**
- Produces: module constants `SNAPSHOT_FILE = "/tmp/fla_dvcc_originals.json"`, `TEMP_INSTANCE = 100`, `TEMP_SERVICE = "com.victronenergy.battery/100"`, `TEMP_CHARGE_CURRENT = 60.0`. Functions `save_originals(battery_service, bms_instance, max_charge_voltage) -> bool`, `load_originals() -> dict|None` (keys `battery_service`, `bms_instance`, `max_charge_voltage`), `delete_originals() -> None`.

- [ ] **Step 1: Write the failing test** — create `fla-shared/tests/test_takeover.py`:

```python
"""Tests for the Takeover module (DVCC handoff / reconnect orchestration)."""

import os
import sys
import json
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))
from helpers import dbus_mock_setup, MockMonitor, MockStatus

dbus_mock_setup()

import takeover


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        # Redirect the snapshot file to a temp path per test.
        self._tmp = os.path.join(os.path.dirname(__file__), "_snap_test.json")
        self._patch = patch.object(takeover, "SNAPSHOT_FILE", self._tmp)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(lambda: os.path.exists(self._tmp) and os.unlink(self._tmp))

    def test_save_then_load_roundtrip(self):
        ok = takeover.save_originals("com.victronenergy.battery/277", -1, 32.0)
        self.assertTrue(ok)
        got = takeover.load_originals()
        self.assertEqual(got["battery_service"], "com.victronenergy.battery/277")
        self.assertEqual(got["bms_instance"], -1)
        self.assertEqual(got["max_charge_voltage"], 32.0)

    def test_load_missing_returns_none(self):
        self.assertIsNone(takeover.load_originals())

    def test_load_corrupt_returns_none(self):
        with open(self._tmp, "w") as f:
            f.write("{not json")
        self.assertIsNone(takeover.load_originals())

    def test_delete_removes_file(self):
        takeover.save_originals("com.victronenergy.battery/277", -1, 32.0)
        takeover.delete_originals()
        self.assertFalse(os.path.exists(self._tmp))
        takeover.delete_originals()  # idempotent — no raise


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest fla-shared.tests.test_takeover -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'takeover'`.

- [ ] **Step 3: Create `fla-shared/takeover.py` with constants + snapshot helpers**

```python
"""Takeover — the temporary transfer of main-bus control from the aggregate
driver to the temp battery while the LFP bank is isolated, and back.

Owns one operation's lock-release, aggregate-driver, DVCC-selection, and
temp-battery lifecycle. Shared by the FLA equalisation and charge services; they
differ only in the charging loop between hand_off_in() and hand_back().

See CONTEXT.md and docs/adr/0001-persist-dvcc-originals.md.
"""

import json
import logging
import os
import time
from collections import namedtuple

import aggregate_driver
import relay_control
import voltage_matching
from temp_battery import TempBatteryService, is_temp_battery_running
from lock import release as release_lock

log = logging.getLogger(__name__)

# Volatile by design: survives a parent crash (where resume applies — the temp
# battery subprocess is still alive), and is correctly gone after a full reboot
# (where resume does NOT apply — relay 2 boot-closes and the subprocess dies).
SNAPSHOT_FILE = "/tmp/fla_dvcc_originals.json"

TEMP_INSTANCE = 100
TEMP_SERVICE = "com.victronenergy.battery/100"
TEMP_CHARGE_CURRENT = 60.0  # FLA recommended max bulk current

# Per-service display states for the handoff phases (values differ per service).
TakeoverStates = namedtuple(
    "TakeoverStates",
    ["stopping_driver", "disconnecting", "voltage_matching",
     "reconnecting", "restarting_driver"],
)


def save_originals(battery_service, bms_instance, max_charge_voltage):
    """Persist the DVCC originals snapshot. Returns True on success."""
    try:
        with open(SNAPSHOT_FILE, "w") as f:
            json.dump({
                "battery_service": battery_service,
                "bms_instance": bms_instance,
                "max_charge_voltage": max_charge_voltage,
            }, f)
        log.info("DVCC originals snapshot saved: %s / %s / %s",
                 battery_service, bms_instance, max_charge_voltage)
        return True
    except OSError as e:
        log.error("Failed to persist DVCC originals snapshot: %s", e)
        return False


def load_originals():
    """Load the persisted DVCC originals, or None if missing/corrupt."""
    try:
        with open(SNAPSHOT_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def delete_originals():
    """Remove the snapshot file (idempotent)."""
    try:
        os.unlink(SNAPSHOT_FILE)
    except OSError:
        pass
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest fla-shared.tests.test_takeover -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add fla-shared/takeover.py fla-shared/tests/test_takeover.py
git commit -m "Add Takeover module skeleton with persisted DVCC originals snapshot"
```

---

## Task 2: Takeover.hand_off_in (the ordered handoff)

**Files:**
- Modify: `fla-shared/takeover.py`
- Test: `fla-shared/tests/test_takeover.py`

**Interfaces:**
- Consumes: `save_originals` (Task 1); `monitor` methods `restart_systemcalc()`, `wait_for_service_instance(100)`, `get_battery_service_setting()`, `get_bms_instance()`, `get_dvcc_max_charge_voltage()`, `set_battery_service_setting(str)`, `set_bms_instance(int)`, `set_dvcc_max_charge_voltage(float)`, `wait_for_bms_selection(str, int)`; `relay_control.open_relay(monitor)`, `relay_control.verify_relay_open(monitor)`; `aggregate_driver.stop()`; `TempBatteryService(device_instance=100).register(charge_voltage, charge_current)` + `.set_charge_voltage(v)`.
- Produces: `Takeover.__init__(monitor, status, alerting_mod, service_name, states)`; `Takeover.hand_off_in(safe_voltage, target_voltage, charge_current=TEMP_CHARGE_CURRENT) -> bool`. On success the temp battery is registered, the aggregate stopped, DVCC switched to temp/100, relay 2 open, CVL raised to `target_voltage`, and the snapshot persisted. On any failure it calls `self.teardown()` (Task 3) and returns False. Sets `self.temp_service`, `self._aggregate_stopped`, `self._originals`.

- [ ] **Step 1: Write the failing tests** — add to `fla-shared/tests/test_takeover.py`:

```python
def _states():
    return takeover.TakeoverStates("STOP", "DISC", "VM", "RECON", "RESTART")


class TestHandOffIn(unittest.TestCase):
    def setUp(self):
        self._tmp = os.path.join(os.path.dirname(__file__), "_snap_hoi.json")
        p = patch.object(takeover, "SNAPSHOT_FILE", self._tmp); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: os.path.exists(self._tmp) and os.unlink(self._tmp))
        self.alerting = MagicMock()
        self.status = MockStatus()

    def _make(self, monitor):
        return takeover.Takeover(monitor, self.status, self.alerting, "fla-equalisation", _states())

    @patch('takeover.aggregate_driver')
    @patch('takeover.relay_control')
    @patch('takeover.TempBatteryService')
    @patch('takeover.time')
    def test_success_opens_relay_only_after_bms_confirmed(self, mtime, MockTBS, mrelay, magg):
        mtime.sleep = MagicMock()
        magg.stop.return_value = True
        mrelay.open_relay.return_value = True
        mrelay.verify_relay_open.return_value = True
        MockTBS.return_value = MagicMock(**{"register.return_value": True})
        monitor = MockMonitor(battery_service="com.victronenergy.battery/277", bms_instance=-1)
        # Record call order across the monitor + relay to prove ordering.
        order = []
        monitor.wait_for_bms_selection = MagicMock(side_effect=lambda *a, **k: order.append("bms") or True)
        mrelay.open_relay.side_effect = lambda *a, **k: order.append("open") or True

        t = self._make(monitor)
        ok = t.hand_off_in(safe_voltage=28.4, target_voltage=31.5)

        self.assertTrue(ok)
        # The relay must open only AFTER the BMS selection is confirmed.
        self.assertEqual(order, ["bms", "open"])

    @patch('takeover.aggregate_driver')
    @patch('takeover.relay_control')
    @patch('takeover.TempBatteryService')
    @patch('takeover.time')
    def test_success_persists_snapshot_of_real_originals(self, mtime, MockTBS, mrelay, magg):
        mtime.sleep = MagicMock()
        magg.stop.return_value = True
        mrelay.open_relay.return_value = True
        mrelay.verify_relay_open.return_value = True
        MockTBS.return_value = MagicMock(**{"register.return_value": True})
        monitor = MockMonitor(battery_service="com.victronenergy.battery/277", bms_instance=-1)
        monitor.get_dvcc_max_charge_voltage = MagicMock(return_value=32.0)

        t = self._make(monitor)
        t.hand_off_in(safe_voltage=28.4, target_voltage=31.5)

        snap = takeover.load_originals()
        self.assertEqual(snap["battery_service"], "com.victronenergy.battery/277")
        self.assertEqual(snap["bms_instance"], -1)
        self.assertEqual(snap["max_charge_voltage"], 32.0)

    @patch('takeover.aggregate_driver')
    @patch('takeover.relay_control')
    @patch('takeover.TempBatteryService')
    @patch('takeover.time')
    def test_aggregate_stop_failure_tears_down_and_returns_false(self, mtime, MockTBS, mrelay, magg):
        mtime.sleep = MagicMock()
        magg.stop.return_value = False  # aggregate stop fails
        MockTBS.return_value = MagicMock(**{"register.return_value": True})
        monitor = MockMonitor()
        t = self._make(monitor)
        ok = t.hand_off_in(safe_voltage=28.4, target_voltage=31.5)
        self.assertFalse(ok)
        self.assertTrue(self.alerting.raise_alarm.called)

    @patch('takeover.aggregate_driver')
    @patch('takeover.relay_control')
    @patch('takeover.TempBatteryService')
    @patch('takeover.time')
    def test_temp_register_failure_returns_false_before_stopping_aggregate(self, mtime, MockTBS, mrelay, magg):
        mtime.sleep = MagicMock()
        MockTBS.return_value = MagicMock(**{"register.return_value": False})
        monitor = MockMonitor()
        t = self._make(monitor)
        ok = t.hand_off_in(safe_voltage=28.4, target_voltage=31.5)
        self.assertFalse(ok)
        magg.stop.assert_not_called()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest fla-shared.tests.test_takeover.TestHandOffIn -v`
Expected: FAIL — `AttributeError: module 'takeover' has no attribute 'Takeover'`.

- [ ] **Step 3: Add the `Takeover` class with `__init__` and `hand_off_in`** — append to `fla-shared/takeover.py`:

```python
class Takeover:
    """Owns one operation's takeover of DVCC from the aggregate driver.

    Lifecycle: the caller acquires the operation lock (a go/no-go gate), then
    hand_off_in() -> [caller runs its charging loop] -> hand_back(). The guarded
    teardown (restore DVCC, release lock, etc.) runs ONLY when relay 2 is
    confirmed closed; otherwise the bus is held and an alarm raised.
    """

    def __init__(self, monitor, status, alerting_mod, service_name, states):
        self.monitor = monitor
        self.status = status
        self.alerting = alerting_mod
        self.service_name = service_name
        self.states = states
        self.temp_service = None
        self._aggregate_stopped = False
        self._originals = None
        self._torn_down = False

    def _fail(self, message):
        """Alarm, tear down, and signal failure."""
        self.alerting.raise_alarm(message, status_service=self.status)
        self.teardown()
        return False

    def hand_off_in(self, safe_voltage, target_voltage, charge_current=TEMP_CHARGE_CURRENT):
        """Run the ordered handoff: temp battery at safe voltage, stop aggregate,
        restart systemcalc, snapshot+persist DVCC originals, switch DVCC to the
        temp battery, confirm the BMS selection, open relay 2, raise CVL to the
        target. Returns True on success; on any failure tears down and returns
        False. The relay opens ONLY after the BMS selection is confirmed."""
        # 1. Temp battery at a SAFE voltage first (crash-safe before the relay opens).
        self.temp_service = TempBatteryService(device_instance=TEMP_INSTANCE)
        if not self.temp_service.register(charge_voltage=safe_voltage,
                                          charge_current=charge_current):
            self.alerting.raise_alarm("Failed to start temp battery service",
                                      status_service=self.status)
            return False

        # 2. Stop the aggregate driver.
        self.status.update(state=self.states.stopping_driver)
        if not aggregate_driver.stop():
            return self._fail("Failed to stop aggregate driver")
        self._aggregate_stopped = True

        # 3. Restart systemcalc so it discovers the temp battery.
        if not self.monitor.restart_systemcalc():
            return self._fail("Failed to restart systemcalc for temp battery discovery")
        if not self.monitor.wait_for_service_instance(TEMP_INSTANCE):
            return self._fail("Temp battery service instance 100 not discovered on D-Bus")

        # 4. Snapshot the DVCC originals (all three) BEFORE changing any of them,
        #    and persist so a crash-then-resume restores the truth (ADR-0001).
        originals = {
            "battery_service": self.monitor.get_battery_service_setting(),
            "bms_instance": self.monitor.get_bms_instance(),
            "max_charge_voltage": self.monitor.get_dvcc_max_charge_voltage(),
        }
        self._originals = originals
        save_originals(originals["battery_service"], originals["bms_instance"],
                       originals["max_charge_voltage"])
        log.info("Saving BatteryService=%s, BmsInstance=%s, DVCC MaxChargeVoltage=%s",
                 originals["battery_service"], originals["bms_instance"],
                 originals["max_charge_voltage"])

        # 5. Switch DVCC to the temp battery and CONFIRM before touching the relay.
        if not self.monitor.set_battery_service_setting(TEMP_SERVICE):
            return self._fail("Failed to switch BatteryService to temp battery")
        if not self.monitor.set_bms_instance(TEMP_INSTANCE):
            return self._fail("Failed to switch BmsInstance to temp battery")
        if not self.monitor.wait_for_bms_selection(TEMP_SERVICE, TEMP_INSTANCE):
            return self._fail("DVCC handoff to temp battery was not confirmed")

        # 6. Open relay 2 (isolate the LFP bank) — only now that DVCC is the temp battery.
        self.status.update(state=self.states.disconnecting)
        if not relay_control.open_relay(self.monitor):
            return self._fail("Failed to open relay 2")
        if not relay_control.verify_relay_open(self.monitor):
            return self._fail("LFP not disconnected after relay open")

        # 7. Raise the DVCC ceiling and the temp battery CVL to the target.
        self.monitor.set_dvcc_max_charge_voltage(target_voltage + 0.5)  # headroom above target
        self.temp_service.set_charge_voltage(target_voltage)
        log.info("CVL raised to target %.2fV (ceiling %.2fV)", target_voltage, target_voltage + 0.5)
        return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest fla-shared.tests.test_takeover.TestHandOffIn -v`
Expected: PASS (4 tests). (`teardown` is referenced by `_fail`; it is added in Task 3. The aggregate-stop-failure test calls `_fail` → `teardown`, so define a minimal `teardown` now or implement Task 3 first. To keep tasks green independently, add a temporary `teardown` stub at the end of the class: `def teardown(self): pass` and replace it fully in Task 3. The two failure tests above only assert the alarm and the return value, which the stub satisfies.)

- [ ] **Step 5: Add the temporary teardown stub** — append inside the class (it is replaced in Task 3):

```python
    def teardown(self):
        """Placeholder — replaced in Task 3 with the guarded restore."""
        pass
```

Re-run Step 4; expected PASS.

- [ ] **Step 6: Commit**

```bash
git add fla-shared/takeover.py fla-shared/tests/test_takeover.py
git commit -m "Takeover.hand_off_in: ordered DVCC handoff with persisted originals"
```

---

## Task 3: Takeover.teardown + abort_teardown (the single guarded restore)

**Files:**
- Modify: `fla-shared/takeover.py` (replace the teardown stub)
- Test: `fla-shared/tests/test_takeover.py`

**Interfaces:**
- Consumes: `load_originals`/`delete_originals` (Task 1); `monitor.get_relay_state()` (1 closed / 0 open), `monitor.set_bms_instance(int)`, `monitor.set_battery_service_setting(str)`, `monitor.set_dvcc_max_charge_voltage(float)`; `aggregate_driver.start()`; `release_lock()`; `self.temp_service.deregister()`; `alerting_mod.clear_alarm(status_service=...)`.
- Produces: `Takeover.teardown()` — when relay confirmed closed: restore the three DVCC originals from `self._originals` (or the persisted snapshot if `self._originals` is None, e.g. resume), deregister the temp battery, restart the aggregate (if it was stopped), release the lock, clear the alarm, delete the snapshot. When relay open: raise the hold alarm and do NONE of it (keep lock, temp battery, DVCC, aggregate stopped, snapshot). `Takeover.abort_teardown()` is an alias used by service `finally` blocks.

- [ ] **Step 1: Write the failing tests** — add to `fla-shared/tests/test_takeover.py`:

```python
class TestTeardown(unittest.TestCase):
    def setUp(self):
        self._tmp = os.path.join(os.path.dirname(__file__), "_snap_td.json")
        p = patch.object(takeover, "SNAPSHOT_FILE", self._tmp); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: os.path.exists(self._tmp) and os.unlink(self._tmp))
        self.alerting = MagicMock()
        self.status = MockStatus()

    def _make(self, monitor):
        t = takeover.Takeover(monitor, self.status, self.alerting, "fla-equalisation", _states())
        t.temp_service = MagicMock()
        t._aggregate_stopped = True
        t._originals = {"battery_service": "com.victronenergy.battery/277",
                        "bms_instance": -1, "max_charge_voltage": 32.0}
        return t

    @patch('takeover.aggregate_driver')
    @patch('takeover.release_lock')
    def test_relay_closed_restores_from_snapshot_and_releases(self, mrelease, magg):
        monitor = MockMonitor(relay_state=1)
        recorded = {}
        monitor.set_battery_service_setting = MagicMock(side_effect=lambda v: recorded.update(bs=v) or True)
        monitor.set_bms_instance = MagicMock(side_effect=lambda v: recorded.update(bms=v) or True)
        monitor.set_dvcc_max_charge_voltage = MagicMock(side_effect=lambda v: recorded.update(cvl=v) or True)
        t = self._make(monitor)
        takeover.save_originals("com.victronenergy.battery/277", -1, 32.0)

        t.teardown()

        self.assertEqual(recorded["bs"], "com.victronenergy.battery/277")  # NOT aggregate
        self.assertEqual(recorded["bms"], -1)
        self.assertEqual(recorded["cvl"], 32.0)  # NOT 28.4
        t.temp_service.deregister.assert_called_once()
        magg.start.assert_called_once()
        mrelease.assert_called_once()
        self.assertIsNone(takeover.load_originals())  # snapshot deleted

    @patch('takeover.aggregate_driver')
    @patch('takeover.release_lock')
    def test_relay_open_holds_and_does_not_restore_or_release(self, mrelease, magg):
        monitor = MockMonitor(relay_state=0)
        t = self._make(monitor)
        takeover.save_originals("com.victronenergy.battery/277", -1, 32.0)

        t.teardown()

        t.temp_service.deregister.assert_not_called()
        mrelease.assert_not_called()
        magg.start.assert_not_called()
        self.assertTrue(self.alerting.raise_alarm.called)
        self.assertIsNotNone(takeover.load_originals())  # snapshot KEPT for resume

    @patch('takeover.aggregate_driver')
    @patch('takeover.release_lock')
    def test_relay_closed_uses_persisted_snapshot_when_originals_none(self, mrelease, magg):
        # Resume case: self._originals is None; teardown must read the snapshot.
        monitor = MockMonitor(relay_state=1)
        recorded = {}
        monitor.set_battery_service_setting = MagicMock(side_effect=lambda v: recorded.update(bs=v) or True)
        t = self._make(monitor)
        t._originals = None
        takeover.save_originals("com.victronenergy.battery/277", -1, 32.0)

        t.teardown()
        self.assertEqual(recorded["bs"], "com.victronenergy.battery/277")

    @patch('takeover.aggregate_driver')
    @patch('takeover.release_lock')
    def test_does_not_clear_alarm(self, mrelease, magg):
        # teardown must NOT clear the alarm — a failure path raised one before
        # calling teardown, and clearing it here would hide the failure.
        monitor = MockMonitor(relay_state=1)
        t = self._make(monitor)
        takeover.save_originals("com.victronenergy.battery/277", -1, 32.0)
        t.teardown()
        self.alerting.clear_alarm.assert_not_called()

    @patch('takeover.aggregate_driver')
    @patch('takeover.release_lock')
    def test_second_teardown_is_noop(self, mrelease, magg):
        # The service finally re-calls teardown after a completed hand_back;
        # the second call must do nothing (no double release / restore).
        monitor = MockMonitor(relay_state=1)
        t = self._make(monitor)
        takeover.save_originals("com.victronenergy.battery/277", -1, 32.0)
        t.teardown()
        t.teardown()
        mrelease.assert_called_once()
        magg.start.assert_called_once()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest fla-shared.tests.test_takeover.TestTeardown -v`
Expected: FAIL — the stub `teardown` does nothing, so the assertions fail.

- [ ] **Step 3: Replace the teardown stub** — in `fla-shared/takeover.py`, replace the `def teardown(self): ...` placeholder with:

```python
    def teardown(self):
        """The single relay-state-guarded restore. Idempotent (a completed
        teardown is a no-op on re-entry, so the service finally can always call it).

        Relay confirmed closed -> restore the DVCC originals (from the in-memory
        snapshot, or the persisted file on resume), deregister the temp battery,
        restart the aggregate, release the lock, delete the snapshot. Relay open
        -> hold the bus and alarm; restore NOTHING (handing DVCC back while the
        LFP is isolated is the free-fall).

        Teardown does NOT touch the alarm: a failure path raised an alarm before
        calling teardown and the operator must keep seeing it; clearing the alarm
        on success is the caller's job (run_*/resume on a matched hand_back)."""
        if self._torn_down:
            return  # a completed teardown already ran — never repeat it

        if self.monitor.get_relay_state() != 1:
            log.error("Takeover teardown: relay open — holding bus, NOT restoring "
                      "(temp battery, DVCC, lock all left in place)")
            self.alerting.raise_alarm(
                "Reconnect incomplete — bus held by temp battery, manual intervention required",
                status_service=self.status,
            )
            return  # NOT torn down — a later teardown (relay since closed) may still restore

        originals = self._originals or load_originals()
        if originals is not None:
            try:
                self.monitor.set_bms_instance(originals["bms_instance"])
                self.monitor.set_battery_service_setting(originals["battery_service"])
                # Restore the ceiling here too (not only in hand_back): covers the
                # edge where the relay closed without a hand_back — e.g. an external
                # relay close detected mid-loop at high CVL — so the ceiling never
                # gets stranded raised. On the normal path hand_back already lowered
                # it to the same value, so this is a harmless idempotent re-write.
                self.monitor.set_dvcc_max_charge_voltage(originals["max_charge_voltage"])
                log.info("Restored DVCC originals: %s / %s / %s",
                         originals["battery_service"], originals["bms_instance"],
                         originals["max_charge_voltage"])
            except Exception:
                log.error("CRITICAL: Failed to restore one or more DVCC originals")
        else:
            log.error("CRITICAL: no DVCC originals snapshot to restore from")

        if self.temp_service is not None:
            try:
                self.temp_service.deregister()
            except Exception:
                pass
            self.temp_service = None

        if self._aggregate_stopped:
            try:
                aggregate_driver.start()
                self.monitor.invalidate_services()
            except Exception:
                log.error("CRITICAL: Failed to restart aggregate driver in teardown")
            self._aggregate_stopped = False

        release_lock()
        delete_originals()
        self._torn_down = True

    def abort_teardown(self):
        """Alias for service finally blocks — the guarded teardown belt-and-suspenders."""
        self.teardown()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest fla-shared.tests.test_takeover.TestTeardown fla-shared.tests.test_takeover.TestHandOffIn -v`
Expected: PASS (7 tests). Note: `TestHandOffIn.test_aggregate_stop_failure...` now exercises the real teardown (relay state from `MockMonitor` default is 1/closed, so it restores+releases harmlessly — the snapshot may be absent at that point, hitting the "no snapshot" log path, which is fine).

- [ ] **Step 5: Commit**

```bash
git add fla-shared/takeover.py fla-shared/tests/test_takeover.py
git commit -m "Takeover.teardown: one relay-guarded restore from persisted originals"
```

---

## Task 4: Takeover.hand_back (float-hold → close → teardown)

**Files:**
- Modify: `fla-shared/takeover.py`
- Test: `fla-shared/tests/test_takeover.py`

**Interfaces:**
- Consumes: `self._originals`/`load_originals`; `monitor.set_dvcc_max_charge_voltage`; `voltage_matching.wait_for_match(monitor, temp_service, status, alerting_mod, voltage_delta_max=, float_voltage=, cache_callback=) -> (bool, float|None)`; `relay_control.close_relay_verified(monitor) -> bool`; `teardown` (Task 3).
- Produces: `Takeover.hand_back(float_voltage, voltage_delta_max, cache_callback=None) -> (bool, float|None)`. Restores the DVCC MaxChargeVoltage ceiling from the snapshot first (relay still open — safe, it's a ceiling), then runs `wait_for_match` (which holds at float and in production only returns on convergence), closes the relay, and calls `teardown()`. Returns `(matched, delta)`. If not matched, returns without closing or tearing down (the safe-hold case never returns in production).

- [ ] **Step 1: Write the failing tests** — add to `fla-shared/tests/test_takeover.py`:

```python
class TestHandBack(unittest.TestCase):
    def setUp(self):
        self._tmp = os.path.join(os.path.dirname(__file__), "_snap_hb.json")
        p = patch.object(takeover, "SNAPSHOT_FILE", self._tmp); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: os.path.exists(self._tmp) and os.unlink(self._tmp))
        self.alerting = MagicMock()
        self.status = MockStatus()

    def _make(self, monitor):
        t = takeover.Takeover(monitor, self.status, self.alerting, "fla-equalisation", _states())
        t.temp_service = MagicMock()
        t._aggregate_stopped = True
        t._originals = {"battery_service": "com.victronenergy.battery/277",
                        "bms_instance": -1, "max_charge_voltage": 32.0}
        return t

    @patch('takeover.aggregate_driver')
    @patch('takeover.release_lock')
    @patch('takeover.relay_control')
    @patch('takeover.voltage_matching')
    def test_converged_closes_and_tears_down(self, mvm, mrelay, mrelease, magg):
        mvm.wait_for_match.return_value = (True, 0.2)
        mrelay.close_relay_verified.return_value = True
        monitor = MockMonitor(relay_state=1)  # close_relay flips MockMonitor to 1 anyway
        t = self._make(monitor)
        matched, delta = t.hand_back(float_voltage=27.0, voltage_delta_max=1.0)
        self.assertTrue(matched)
        mrelay.close_relay_verified.assert_called_once()
        mrelease.assert_called_once()  # teardown ran (relay closed)

    @patch('takeover.aggregate_driver')
    @patch('takeover.release_lock')
    @patch('takeover.relay_control')
    @patch('takeover.voltage_matching')
    def test_restores_ceiling_from_snapshot_before_matching(self, mvm, mrelay, mrelease, magg):
        mvm.wait_for_match.return_value = (True, 0.2)
        mrelay.close_relay_verified.return_value = True
        monitor = MockMonitor(relay_state=1)
        recorded = []
        monitor.set_dvcc_max_charge_voltage = MagicMock(side_effect=lambda v: recorded.append(v) or True)
        t = self._make(monitor)
        t.hand_back(float_voltage=27.0, voltage_delta_max=1.0)
        self.assertIn(32.0, recorded)  # ceiling restored to the snapshot value

    @patch('takeover.aggregate_driver')
    @patch('takeover.release_lock')
    @patch('takeover.relay_control')
    @patch('takeover.voltage_matching')
    def test_not_matched_does_not_close_or_teardown(self, mvm, mrelay, mrelease, magg):
        mvm.wait_for_match.return_value = (False, 2.0)  # bounded non-convergence (test only)
        monitor = MockMonitor(relay_state=0)
        t = self._make(monitor)
        matched, delta = t.hand_back(float_voltage=27.0, voltage_delta_max=1.0)
        self.assertFalse(matched)
        mrelay.close_relay_verified.assert_not_called()
        mrelease.assert_not_called()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest fla-shared.tests.test_takeover.TestHandBack -v`
Expected: FAIL — `AttributeError: 'Takeover' object has no attribute 'hand_back'`.

- [ ] **Step 3: Add `hand_back`** — append inside the `Takeover` class:

```python
    def hand_back(self, float_voltage, voltage_delta_max, cache_callback=None):
        """Hold the bus at float until the Trojan<->LFP delta converges, close
        relay 2, then run the guarded teardown. Returns (matched, delta). The
        ceiling is restored from the snapshot first (relay still open — safe,
        it is only a ceiling, the temp battery CVL at float caps the bus). In
        production wait_for_match only returns on convergence (else safe-hold)."""
        originals = self._originals or load_originals()
        if originals is not None:
            self.monitor.set_dvcc_max_charge_voltage(originals["max_charge_voltage"])
            log.info("DVCC MaxChargeVoltage restored to %s before matching",
                     originals["max_charge_voltage"])

        self.status.update(state=self.states.voltage_matching)
        matched, delta = voltage_matching.wait_for_match(
            self.monitor, self.temp_service, self.status, self.alerting,
            voltage_delta_max=voltage_delta_max, float_voltage=float_voltage,
            cache_callback=cache_callback,
        )
        if not matched:
            return False, delta

        self.status.update(state=self.states.reconnecting)
        if not relay_control.close_relay_verified(self.monitor):
            self.alerting.raise_alarm("Failed to close relay 2", status_service=self.status)
            return False, delta

        self.status.update(state=self.states.restarting_driver)
        self.teardown()
        return True, delta
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest fla-shared.tests.test_takeover.TestHandBack -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add fla-shared/takeover.py fla-shared/tests/test_takeover.py
git commit -m "Takeover.hand_back: float-hold, close, guarded teardown"
```

---

## Task 5: Takeover.resume_attach (adopt an interrupted takeover)

**Files:**
- Modify: `fla-shared/takeover.py`
- Test: `fla-shared/tests/test_takeover.py`

**Interfaces:**
- Consumes: `is_temp_battery_running()` (from `temp_battery`); `load_originals` (Task 1); `monitor.get_relay_state()`; `TempBatteryService(device_instance=100).attach()`.
- Produces: classmethod `Takeover.resume_attach(monitor, status, alerting_mod, service_name, states) -> Takeover|None`. Returns a Takeover positioned for `hand_back` when relay 2 is open AND a temp battery is running AND a snapshot exists (attaches the temp battery, loads the snapshot into `_originals`, sets `_aggregate_stopped = True`). Returns None when the relay is closed or no temp battery runs (nothing to resume). When relay open + temp running but the snapshot is MISSING, raises the hold alarm and returns None (refuse to guess — ADR-0001).

- [ ] **Step 1: Write the failing tests** — add to `fla-shared/tests/test_takeover.py`:

```python
class TestResumeAttach(unittest.TestCase):
    def setUp(self):
        self._tmp = os.path.join(os.path.dirname(__file__), "_snap_res.json")
        p = patch.object(takeover, "SNAPSHOT_FILE", self._tmp); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: os.path.exists(self._tmp) and os.unlink(self._tmp))
        self.alerting = MagicMock()
        self.status = MockStatus()

    def _resume(self, monitor):
        return takeover.Takeover.resume_attach(monitor, self.status, self.alerting,
                                               "fla-equalisation", _states())

    @patch('takeover.is_temp_battery_running', return_value=True)
    @patch('takeover.TempBatteryService')
    def test_relay_open_with_snapshot_attaches(self, MockTBS, _running):
        MockTBS.return_value = MagicMock()
        takeover.save_originals("com.victronenergy.battery/277", -1, 32.0)
        monitor = MockMonitor(relay_state=0)
        t = self._resume(monitor)
        self.assertIsNotNone(t)
        t.temp_service.attach.assert_called_once()
        self.assertEqual(t._originals["battery_service"], "com.victronenergy.battery/277")
        self.assertTrue(t._aggregate_stopped)

    @patch('takeover.is_temp_battery_running', return_value=False)
    def test_relay_open_no_temp_returns_none(self, _running):
        monitor = MockMonitor(relay_state=0)
        self.assertIsNone(self._resume(monitor))

    @patch('takeover.is_temp_battery_running', return_value=True)
    def test_relay_closed_returns_none(self, _running):
        monitor = MockMonitor(relay_state=1)
        self.assertIsNone(self._resume(monitor))

    @patch('takeover.is_temp_battery_running', return_value=True)
    @patch('takeover.TempBatteryService')
    def test_relay_open_missing_snapshot_alarms_and_returns_none(self, MockTBS, _running):
        monitor = MockMonitor(relay_state=0)  # no snapshot saved
        t = self._resume(monitor)
        self.assertIsNone(t)
        self.assertTrue(self.alerting.raise_alarm.called)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest fla-shared.tests.test_takeover.TestResumeAttach -v`
Expected: FAIL — `AttributeError: type object 'Takeover' has no attribute 'resume_attach'`.

- [ ] **Step 3: Add the classmethod** — append inside the `Takeover` class:

```python
    @classmethod
    def resume_attach(cls, monitor, status, alerting_mod, service_name, states):
        """Adopt an interrupted takeover on startup. Returns a Takeover ready for
        hand_back, or None when there is nothing to resume (relay closed, or no
        temp battery). When the relay is open with a live temp battery but the
        DVCC originals snapshot is missing, alarms and returns None — we refuse
        to restore to guessed values (ADR-0001)."""
        if monitor.get_relay_state() != 0:
            return None  # relay closed — nothing isolated
        if not is_temp_battery_running():
            return None  # relay open but no holder — caller runs startup_safety_check
        originals = load_originals()
        if originals is None:
            log.error("RESUME: relay open + temp battery but no DVCC snapshot — "
                      "refusing to guess; holding and alarming")
            alerting_mod.raise_alarm(
                "Reconnect incomplete — bus held, DVCC originals lost, manual intervention required",
                status_service=status,
            )
            return None
        t = cls(monitor, status, alerting_mod, service_name, states)
        t.temp_service = TempBatteryService(device_instance=TEMP_INSTANCE)
        t.temp_service.attach()
        t._originals = originals
        t._aggregate_stopped = True  # the interrupted operation stopped it
        log.warning("RESUME: adopted interrupted takeover (snapshot loaded)")
        return t
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest fla-shared.tests.test_takeover -v`
Expected: PASS (all Takeover tests).

- [ ] **Step 5: Commit**

```bash
git add fla-shared/takeover.py fla-shared/tests/test_takeover.py
git commit -m "Takeover.resume_attach: adopt an interrupted takeover from snapshot"
```

---

## Task 6: Migrate run_equalisation to the Takeover

**Files:**
- Modify: `fla-equalisation/fla_equalisation.py` (`run_equalisation`, lines ~125–430)
- Test: `fla-equalisation/tests/test_fla_equalisation.py`

**Interfaces:**
- Consumes: `Takeover(monitor, status, alerting, "fla-equalisation", states)`, `t.hand_off_in(safe_voltage, target_voltage)`, `t.hand_back(float_voltage, voltage_delta_max, cache_callback)`, `t.abort_teardown()`, `TakeoverStates` (Task 1–5).
- Produces: `run_equalisation` keeps the equalising loop and the abort-routing/timestamp logic; the inline handoff (139–195), the VM→close→deregister→restart tail (302–363), and the relay-state-guarded `finally` (378–430) are replaced by Takeover calls. Behaviour unchanged except DVCC restore now uses persisted originals.

- [ ] **Step 1: Add the Takeover import and a module-level states constant** — in `fla-equalisation/fla_equalisation.py`, after the existing imports add:

```python
from takeover import Takeover, TakeoverStates

EQ_TAKEOVER_STATES = TakeoverStates(
    stopping_driver=STATE_STOPPING_DRIVER,
    disconnecting=STATE_DISCONNECTING,
    voltage_matching=STATE_VOLTAGE_MATCHING,
    reconnecting=STATE_RECONNECTING,
    restarting_driver=STATE_RESTARTING_DRIVER,
)
```

- [ ] **Step 2: Replace the body of `run_equalisation`** — replace lines 125–430 (the whole function from `def run_equalisation` through the end of its `finally`) with:

```python
def run_equalisation(settings, monitor, status):
    """Execute the full equalisation sequence. Returns True on success."""
    if not acquire_lock("fla-equalisation"):
        log.warning("Operation lock held — skipping equalisation")
        return False

    aborted_by_operator = False
    t = Takeover(monitor, status, alerting, "fla-equalisation", EQ_TAKEOVER_STATES)
    try:
        battery_temp = monitor.get_battery_temperature()
        eq_voltage = temp_compensate(settings.eq_voltage, battery_temp)
        if not t.hand_off_in(safe_voltage=28.4, target_voltage=eq_voltage):
            status.update(state=STATE_ERROR)
            return False

        lfp_voltage_at_disconnect = monitor.get_lfp_voltage()
        if lfp_voltage_at_disconnect is not None:
            log.info("LFP voltage at disconnect: %.2fV", lfp_voltage_at_disconnect)

        # Step: Equalisation loop (service-specific — stays here)
        status.update(state=STATE_EQUALISING)
        log.info("Starting equalisation at %.1fV", settings.eq_voltage)
        eq_start = time.time()
        eq_timeout = settings.eq_timeout_hours * 3600
        i_trojan_none_count = 0

        while True:
            elapsed = time.time() - eq_start
            v_trojan = monitor.get_trojan_voltage()
            i_trojan = monitor.get_trojan_current()
            if v_trojan is not None and i_trojan is not None:
                t.temp_service.update_voltage_current(v_trojan, i_trojan)
            v_lfp = monitor.get_lfp_voltage()
            remaining = max(0, eq_timeout - elapsed)
            delta = round(abs(v_trojan - v_lfp), 2) if (v_trojan is not None and v_lfp is not None) else None
            status.update(time_remaining=remaining, trojan_v=v_trojan, lfp_v=v_lfp)
            update_cache(state=STATE_EQUALISING, time_remaining=remaining,
                         trojan_v=v_trojan, lfp_v=v_lfp, voltage_delta=delta)

            if v_trojan is None:
                status.update(state=STATE_ERROR)
                raise_alarm("SmartShunt Trojan (279) unresponsive during equalisation", status_service=status)
                return False

            if not verify_relay_still_open(monitor, eq_voltage):
                status.update(state=STATE_ERROR)
                raise_alarm("Relay closed externally during EQ — aborting", status_service=status)
                return False

            if (lfp_voltage_at_disconnect is not None and v_lfp is not None
                    and v_lfp < lfp_voltage_at_disconnect - 0.5):
                log.warning("LFP voltage dropping (%.2fV -> %.2fV) — possible Orion failure",
                            lfp_voltage_at_disconnect, v_lfp)
                status.update(state=STATE_ERROR)
                raise_alarm("LFP voltage dropping — Orion may have failed", status_service=status)
                return False

            if i_trojan is None:
                i_trojan_none_count += 1
                if i_trojan_none_count >= 10:
                    log.warning("Trojan current unreadable for 5 min — proceeding to voltage matching")
                    break
            else:
                i_trojan_none_count = 0

            if i_trojan is not None and abs(i_trojan) > 60:
                log.warning("High Trojan charge current: %.1fA (dynamo/MPPT active?)", abs(i_trojan))

            voltage_reached = v_trojan is not None and v_trojan >= (eq_voltage - 0.1)
            if voltage_reached and i_trojan is not None and abs(i_trojan) < settings.eq_current_complete:
                log.info("Equalisation complete: V=%.1fV (target %.1fV), current %.1fA < %.1fA (%.0f min)",
                         v_trojan, eq_voltage, abs(i_trojan), settings.eq_current_complete, elapsed / 60)
                break

            if elapsed > eq_timeout:
                log.warning("Equalisation timeout after %.0f min, current %.1fA",
                            elapsed / 60, abs(i_trojan) if i_trojan else 0)
                break

            if check_abort():
                log.warning("Operator abort during equalisation — proceeding to controlled reconnect")
                clear_abort()
                aborted_by_operator = True
                break

            if int(elapsed) % 300 < 30:
                log.info("Equalising: %.0f min, V=%.1fV, I=%.1fA",
                         elapsed / 60, v_trojan or 0, i_trojan or 0)

            time.sleep(30)

        # Hand back: float-hold, close, guarded teardown.
        update_cache(state=STATE_VOLTAGE_MATCHING)
        def _vm_cache_cb(**kwargs):
            update_cache(state=STATE_VOLTAGE_MATCHING, **kwargs)
        float_battery_temp = monitor.get_battery_temperature()
        matched, delta = t.hand_back(
            float_voltage=temp_compensate(settings.float_voltage, float_battery_temp),
            voltage_delta_max=settings.voltage_delta_max,
            cache_callback=_vm_cache_cb,
        )
        if not matched:
            status.update(state=STATE_ERROR)
            return False

        status.update(state=STATE_IDLE, time_remaining=0)
        clear_alarm(status_service=status)
        if aborted_by_operator:
            log.info("Operator-aborted equalisation reconnected safely — interval not advanced")
            return False
        write_last_equalisation()
        log.info("Equalisation completed successfully")
        return True

    except Exception as e:
        log.exception("Unexpected error during equalisation: %s", e)
        status.update(state=STATE_ERROR)
        raise_alarm("Equalisation script error: %s" % e, status_service=status)
        return False

    finally:
        t.abort_teardown()
```

(Note: `hand_back` already closes the relay, deregisters the temp battery, restarts the aggregate, and the inrush logging from the old tail is dropped — it was display-only; if you want to keep it, add it inside `hand_back` in a follow-up. The `finally`'s `abort_teardown()` is the belt-and-suspenders: on the happy path the relay is closed and teardown already ran inside `hand_back`, so `abort_teardown` restores from a now-deleted snapshot with `_originals` also cleared — guard teardown against a double-run by making it a no-op when `temp_service is None` and the lock is already released; see Step 3.)

- [ ] **Step 3: (No code change — teardown idempotency already handled.)** `teardown()` is made idempotent by the `self._torn_down` flag added in Task 3 (a completed relay-closed teardown sets it; re-entry from the service `finally` is then a no-op). Nothing to add here. Confirm by re-running the Takeover tests: `python3 -m unittest fla-shared.tests.test_takeover -v` — expected PASS.

- [ ] **Step 4: Update the EQ tests** — in `fla-equalisation/tests/test_fla_equalisation.py`:
  - The `TestSafetyGuards` tests that asserted handoff-step failures (`test_aggregate_stop_failure_aborts`, `test_temp_service_register_failure_aborts_before_stopping_driver`, `test_systemcalc_restart_failure_aborts_before_relay_open`, `test_bms_switch_confirmation_failure_aborts_before_relay_open`, `test_relay_open_failure_aborts`, `test_high_lfp_current_after_relay_open_aborts`) now belong to the Takeover and are covered by `test_takeover.py`. Replace each with a single test that patches `fla_equalisation.Takeover` and asserts the service surfaces a hand_off_in failure:

```python
    @patch('fla_equalisation.release_lock')
    @patch('fla_equalisation.acquire_lock', return_value=True)
    def test_hand_off_in_failure_aborts(self, mock_lock, mock_unlock):
        settings, monitor, status = self._make_mocks()
        with patch('fla_equalisation.Takeover') as MockT:
            inst = MagicMock()
            inst.hand_off_in.return_value = False
            MockT.return_value = inst
            result = run_equalisation(settings, monitor, status)
        self.assertFalse(result)
        self.assertIn(STATE_ERROR, status.states)
        inst.abort_teardown.assert_called_once()
```

  - `TestOperatorAbortRouting` and `TestRelayStateGuardedFinally` (from the reconnect feature): update them to patch `fla_equalisation.Takeover` — `hand_off_in` returns True, `hand_back` returns `(True, 0.2)`, and assert `write_last_equalisation` is/ isn't called and `abort_teardown` is called in the finally. The relay-state-guarded behaviour itself is now tested in `test_takeover.py::TestTeardown`.

- [ ] **Step 5: Run the EQ suite**

Run: `python3 -m unittest discover -s fla-equalisation/tests`
Expected: `OK`. Fix any test still referencing the removed inline structure by re-pointing it at `fla_equalisation.Takeover`.

- [ ] **Step 6: Commit**

```bash
git add fla-shared/takeover.py fla-equalisation/fla_equalisation.py fla-equalisation/tests/test_fla_equalisation.py
git commit -m "Migrate run_equalisation to the Takeover module"
```

---

## Task 7: Migrate FlaEqualisationService startup/resume to the Takeover

**Files:**
- Modify: `fla-equalisation/fla_equalisation.py` (`FlaEqualisationService.__init__`, `_resume_interrupted_reconnect`)
- Test: `fla-equalisation/tests/test_fla_equalisation.py`

**Interfaces:**
- Consumes: `Takeover.resume_attach(monitor, status, alerting, "fla-equalisation", EQ_TAKEOVER_STATES) -> Takeover|None`; `t.hand_back(float_voltage, voltage_delta_max)`.
- Produces: `_resume_interrupted_reconnect` uses `Takeover.resume_attach` + a worker that calls `t.hand_back`, replacing the hand-rolled attach/teardown worker.

- [ ] **Step 1: Replace `_resume_interrupted_reconnect`** — replace the method body (the version added in the reconnect feature) with:

```python
    def _resume_interrupted_reconnect(self):
        """Adopt and finish a takeover interrupted mid-hand-back. Returns True if
        this service took over (or another owns it), so the caller skips the
        normal startup_safety_check."""
        if self.monitor.get_relay_state() != 0:
            return False
        if not is_temp_battery_running():
            return False
        if not acquire_lock("fla-equalisation"):
            log.info("RESUME: another service owns the interrupted reconnect — backing off")
            return True
        t = Takeover.resume_attach(self.monitor, self.status, alerting,
                                   "fla-equalisation", EQ_TAKEOVER_STATES)
        if t is None:
            # Snapshot missing — resume_attach already alarmed; hold (keep lock).
            return True
        log.warning("RESUME: adopting interrupted takeover and finishing hand-back")
        self._running = True

        def _worker():
            try:
                float_temp = self.monitor.get_battery_temperature()
                matched, _ = t.hand_back(
                    float_voltage=temp_compensate(self.settings.float_voltage, float_temp),
                    voltage_delta_max=self.settings.voltage_delta_max,
                )
                # teardown no longer clears the alarm; the success path owns it.
                if matched:
                    clear_alarm(status_service=self.status)
            except Exception as e:
                log.exception("RESUME worker error: %s", e)
                t.abort_teardown()
            finally:
                self._running = False

        threading.Thread(target=_worker, daemon=True).start()
        return True
```

(`is_temp_battery_running` is already imported from the reconnect feature. The `__init__` ordering — monitor → `recover_orphan_temp_battery(relay_state)` → resume dispatch → `startup_safety_check` — is unchanged from the reconnect feature and needs no edit.)

- [ ] **Step 2: Update the resume tests** — in `fla-equalisation/tests/test_fla_equalisation.py`, `TestResumeOnStartup`: patch `fla_equalisation.Takeover` so `resume_attach` returns a mock Takeover, and assert a worker thread is started and `startup_safety_check` is skipped; the relay-closed case still returns None → `startup_safety_check` runs. Example:

```python
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
```

- [ ] **Step 3: Run the EQ suite**

Run: `python3 -m unittest discover -s fla-equalisation/tests`
Expected: `OK`.

- [ ] **Step 4: Commit**

```bash
git add fla-equalisation/fla_equalisation.py fla-equalisation/tests/test_fla_equalisation.py
git commit -m "Migrate EQ resume path to Takeover.resume_attach"
```

---

## Task 8: Migrate the charge service to the Takeover

**Files:**
- Modify: `fla-charge/fla_charge.py` (`run_charge`, `FlaChargeService._resume_interrupted_reconnect`)
- Test: `fla-charge/tests/test_fla_charge.py`

**Interfaces:** identical to Tasks 6+7, mirrored for charge (lock `"fla-charge"`, float `settings.fla_float_voltage`, target `settings.fla_bulk_voltage`, the absorption loop, `write_last_charge`, the Phase-1 shared-charging loop stays before `hand_off_in`).

- [ ] **Step 1: Add the import + states constant** — in `fla-charge/fla_charge.py`:

```python
from takeover import Takeover, TakeoverStates

CHARGE_TAKEOVER_STATES = TakeoverStates(
    stopping_driver=STATE_STOPPING_DRIVER,
    disconnecting=STATE_DISCONNECTING,
    voltage_matching=STATE_VOLTAGE_MATCHING,
    reconnecting=STATE_RECONNECTING,
    restarting_driver=STATE_RESTARTING_DRIVER,
)
```

- [ ] **Step 2: Replace `run_charge`'s handoff/teardown** — keep Phase 1 (the shared-charging `while` loop, lines ~172–236) exactly as is; replace the Phase-2 handoff block (241–286), the Phase-4 VM→close→deregister→restart tail (405–463), and the `finally` (471–501) with Takeover calls, mirroring Task 6:
  - After Phase 1 breaks, compute `abs_voltage = temp_compensate(settings.fla_bulk_voltage, monitor.get_battery_temperature())`, then `if not t.hand_off_in(safe_voltage=(v_lfp or v_trojan or 28.0), target_voltage=abs_voltage): status.update(state=STATE_ERROR); return False`.
  - Keep the absorption `while` loop (319–403) including its operator-abort break (added in the reconnect feature) and AC-loss break.
  - Replace the tail with `matched, delta = t.hand_back(float_voltage=temp_compensate(settings.fla_float_voltage, monitor.get_battery_temperature()), voltage_delta_max=settings.voltage_delta_max, cache_callback=_vm_cache_cb)`; on `not matched` → STATE_ERROR + return False; else the success-record block (skip `write_last_charge` when `aborted_by_operator`).
  - Replace the `finally` with `t.abort_teardown()`.
  - Construct `t = Takeover(monitor, status, alerting, "fla-charge", CHARGE_TAKEOVER_STATES)` after `acquire_lock`, and reference `t.temp_service.update_voltage_current(...)` inside the absorption loop where the old code used `temp_service`.

- [ ] **Step 3: Replace `_resume_interrupted_reconnect`** — mirror Task 7 with charge names:

```python
    def _resume_interrupted_reconnect(self):
        if self.monitor.get_relay_state() != 0:
            return False
        if not is_temp_battery_running():
            return False
        if not acquire_lock("fla-charge"):
            log.info("RESUME: another service owns the interrupted reconnect — backing off")
            return True
        t = Takeover.resume_attach(self.monitor, self.status, alerting,
                                   "fla-charge", CHARGE_TAKEOVER_STATES)
        if t is None:
            return True
        log.warning("RESUME: adopting interrupted takeover and finishing hand-back")
        self._running = True

        def _worker():
            try:
                float_temp = self.monitor.get_battery_temperature()
                matched, _ = t.hand_back(
                    float_voltage=temp_compensate(self.settings.fla_float_voltage, float_temp),
                    voltage_delta_max=self.settings.voltage_delta_max,
                )
                # teardown no longer clears the alarm; the success path owns it.
                if matched:
                    alerting.clear_alarm(status_service=self.status)
            except Exception as e:
                log.exception("RESUME worker error: %s", e)
                t.abort_teardown()
            finally:
                self._running = False

        threading.Thread(target=_worker, daemon=True).start()
        return True
```

- [ ] **Step 4: Update the charge tests** — mirror Tasks 6+7 Step 4: collapse the handoff-step safety-guard tests into one `test_hand_off_in_failure_aborts` (patch `fla_charge.Takeover`), and re-point `TestChargeOperatorAbortRouting`, `TestChargeRelayStateGuardedFinally`, `TestChargeResumeOnStartup` at `fla_charge.Takeover`. Keep `TestPhase1Transitions` (Phase 1 is unchanged).

- [ ] **Step 5: Run the charge suite**

Run: `python3 -m unittest discover -s fla-charge/tests`
Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add fla-charge/fla_charge.py fla-charge/tests/test_fla_charge.py
git commit -m "Migrate charge service to the Takeover module"
```

---

## Task 9: Full-suite regression + verify the resume-bug fix

**Files:** none (verification only).

- [ ] **Step 1: Run the entire suite**

Run:
```bash
python3 -m unittest discover -s fla-shared/tests
python3 -m unittest discover -s fla-equalisation/tests
python3 -m unittest discover -s fla-charge/tests
```
Expected: three `OK`. Total ≥ 213 (the Takeover suite adds tests; the collapsed service safety-guard tests subtract a few).

- [ ] **Step 2: Confirm the resume-bug fix is encoded** — verify a test asserts resume restores the persisted `battery/277` (not `aggregate`) and `32.0` (not `28.4`):

Run: `python3 -m unittest fla-shared.tests.test_takeover.TestTeardown.test_relay_closed_uses_persisted_snapshot_when_originals_none -v`
Expected: PASS.

- [ ] **Step 3: Confirm no hardcoded DVCC normals remain in the services** — the old known-normals (`com.victronenergy.battery.aggregate`, `set_bms_instance(-1)`, `LFP_SAFE_CVL`) must no longer appear in the resume/teardown paths of either service:

Run: `grep -n "battery.aggregate\|set_bms_instance(-1)\|LFP_SAFE_CVL" fla-equalisation/fla_equalisation.py fla-charge/fla_charge.py`
Expected: no matches in resume/teardown code (the only restore source is now the Takeover's persisted snapshot).

- [ ] **Step 4: Confirm the invariant — wait_for_match still never closes the relay, and teardown is the only DVCC restore site**

Run: `grep -rn "set_battery_service_setting\|set_bms_instance\|set_dvcc_max_charge_voltage" fla-equalisation/fla_equalisation.py fla-charge/fla_charge.py`
Expected: no matches (all DVCC writes now live in `takeover.py`).

- [ ] **Step 5: Commit (if any fixups were needed)**

```bash
git add -A -- fla-shared fla-equalisation fla-charge
git commit -m "Finalise Takeover migration: full suite green, resume restores persisted originals"
```
(Skip if Steps 1–4 needed no changes.)

---

## Self-Review

**Spec coverage** (against `CONTEXT.md` + ADR-0001/0002):
- *Takeover owns lock-release + aggregate + DVCC + temp battery* → Tasks 2–4 (hand_off_in / teardown / hand_back).
- *Hand-off in ordering: relay opens only after BMS confirmed* → Task 2 `test_success_opens_relay_only_after_bms_confirmed`.
- *One guarded teardown, relay-state-gated* → Task 3.
- *Persisted DVCC originals; restore from snapshot everywhere; missing → alarm not guess* → Tasks 1, 3 (`..._uses_persisted_snapshot...`), 5 (`..._missing_snapshot_alarms...`).
- *Safe-hold preserved (wait_for_match unchanged)* → Task 4 calls `voltage_matching.wait_for_match` as-is.
- *Resume adopts via attach + snapshot* → Task 5, wired in Tasks 7/8.
- *Both services migrated identically* → Tasks 6–8; Task 9 Step 3–4 greps for parity.
- *Composition, DbusMonitor unchanged* → no task modifies `dbus_monitor.py`; noted as deferred follow-up.
- *Resume-bug fix (battery/277, 32V)* → Task 9 Step 2.

**Placeholder scan:** every code step contains full code; the migration Tasks 6/8 give exact replace targets with complete function bodies (Task 6) or precise per-block instructions referencing exact line ranges and the Task-6 body as the template (Task 8). No "TBD"/"handle errors"/"similar to" without the code.

**Type consistency:** `Takeover.__init__(monitor, status, alerting_mod, service_name, states)`, `hand_off_in(safe_voltage, target_voltage, charge_current=)`, `hand_back(float_voltage, voltage_delta_max, cache_callback=)`, `teardown()`, `abort_teardown()`, `resume_attach(...)` are used consistently across Tasks 2–8. `TakeoverStates` fields (`stopping_driver`/`disconnecting`/`voltage_matching`/`reconnecting`/`restarting_driver`) match between Task 1 (definition) and Tasks 6/8 (construction).

**Known deliberate test changes:** the per-service handoff safety-guard tests collapse into one `test_hand_off_in_failure_aborts` per service plus the Takeover suite; the reconnect-feature tests (`TestOperatorAbortRouting`, `TestRelayStateGuardedFinally`, `TestResumeOnStartup`, and charge equivalents) re-point at the `Takeover` mock. These are rewrites, not silent deletions.

**Alarm lifecycle (do not regress):** `Takeover.teardown()` never touches the alarm. A failure path raises an alarm *before* calling teardown and the operator must keep seeing it; clearing the alarm is owned by each success path — `run_equalisation`/`run_charge` clear it after a matched `hand_back`, and the resume workers (Tasks 7/8) clear it explicitly on a matched `hand_back`. Task 3 has tests (`test_does_not_clear_alarm`, `test_second_teardown_is_noop`) pinning this.

**Risk note (for the executor):** this refactors safety-critical code merged in PR #13. The existing 213-test suite is the safety net — keep it green at every task. The one intended behaviour change (resume/teardown restores persisted originals, fixing the latent bug from PR #13) is explicitly asserted in Task 3 and Task 9. The display-only inrush logging from the old reconnect tails is dropped; re-add inside `hand_back` later if the dashboards need it.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-29-takeover-module.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
