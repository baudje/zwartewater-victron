# Controlled hold-and-lower reconnect — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make FLA equalisation/charge reconnect a *stable held condition* — the temp battery actively pins the main bus at float while the LFP is isolated, the relay auto-closes only within the safe delta, and every other exit (operator Abort, non-convergence, crash, reboot) holds-and-alarms instead of free-falling the bus.

**Architecture:** All reconnect behaviour lives in shared `fla-shared/voltage_matching.py` (re-pin float each cycle, 2 s poll, indefinite safe-hold, never auto-close above threshold). `fla-shared/temp_battery.py` gains an "attach to existing" mode and a relay-aware orphan killer. Each service (`fla_equalisation.py`, `fla_charge.py`) changes only how it *enters* the reconnect (operator Abort in relay-open phases breaks into the reconnect), guards its teardown on a confirmed-closed relay, and adopts an interrupted hold on startup. The two services differ only in which phases are relay-open; the reconnect path itself is identical shared code.

**Tech Stack:** Python 3 (Venus OS), D-Bus (`dbus-python`), GLib main loop, daemontools. Tests use `unittest` + `unittest.mock`, mocking all D-Bus/GLib and the `time` module. No Venus OS required to test.

## Global Constraints

- **Firm invariant:** the software NEVER auto-closes relay 2 while `delta > RELAY_CLOSE_DELTA_MAX`. There is no "last-resort" high-delta close. If the bus can't be brought within threshold, hold-and-alarm and wait for the operator — full stop.
- `RELAY_CLOSE_DELTA_MAX = 1.0` (V) — unchanged, stays in `fla-shared/relay_control.py`.
- New module constants in `fla-shared/voltage_matching.py`: `POLL_INTERVAL = 2` (s), `SETTLE_TIMEOUT = 120` (s), `MAX_NONE_CYCLES = 10` (≈20 s).
- Relay-open → never tear down: deregistering the temp battery / restoring DVCC while the LFP is isolated is the free-fall cascade. Teardown runs ONLY after `monitor.get_relay_state() == 1`.
- Relay-open → never kill the temp battery: it is a live hold, not an orphan. Only kill a stray temp battery when relay 2 is **closed**.
- The reconnect path is **identical shared code** for both services. Per repo convention (`CLAUDE.md` "Duplication Between Services"), apply every shared-pattern change to BOTH `fla_equalisation.py` and `fla_charge.py`.
- Full existing suite (196 tests: 108 shared + 51 EQ + 37 charge) must stay green, save for tests whose *semantics* this design deliberately changes (the removed convergence timeout; Trojan-None now holds instead of returning) — those are rewritten, not deleted silently.
- Commit messages must read as written by a human developer — NO AI attribution, no `Co-Authored-By`.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `fla-shared/voltage_matching.py` | The reconnect hold loop | Near-total rewrite: constants, re-pin float each cycle, 2 s poll, dual safe-hold triggers, never-auto-close invariant, timeout removed |
| `fla-shared/temp_battery.py` | Temp battery subprocess lifecycle | Add `attach()` adopt-mode + attached `deregister()`; add `is_temp_battery_running()`; make `recover_orphan_temp_battery()` relay-aware |
| `fla-equalisation/fla_equalisation.py` | EQ state machine | Operator Abort (relay-open) breaks to reconnect; relay-state-guarded `finally`; resume-on-startup; relay-aware orphan ordering |
| `fla-charge/fla_charge.py` | Charge state machine | Same as EQ, plus add the missing operator-Abort path to the relay-open bulk/absorption loop |
| `fla-shared/tests/test_voltage_matching.py` | Reconnect-loop tests | Rewritten for new semantics |
| `fla-shared/tests/test_temp_battery_orphan.py` | Orphan/attach tests | Relay-aware kill + attach-mode + `is_temp_battery_running` |
| `fla-equalisation/tests/test_fla_equalisation.py` | EQ tests | Abort-routing, relay-state finally, resume |
| `fla-charge/tests/test_fla_charge.py` | Charge tests | Same |

---

## Task 1: Reconnect hold loop (`voltage_matching.py`)

**Files:**
- Modify: `fla-shared/voltage_matching.py` (full rewrite)
- Test: `fla-shared/tests/test_voltage_matching.py` (full rewrite)

**Interfaces:**
- Consumes: `monitor.get_trojan_voltage()`, `monitor.get_lfp_voltage()`, `monitor.get_trojan_current()` (all return `float|None`); `temp_service.set_charge_voltage(float)`, `temp_service.update_voltage_current(v, i)`; `status.update(**kwargs)`; `alerting_mod.raise_alarm(msg, status_service=...)`, `alerting_mod.activate_buzzer()`; `relay_control.RELAY_CLOSE_DELTA_MAX`.
- Produces: `wait_for_match(monitor, temp_service, status, alerting_mod, voltage_delta_max=RELAY_CLOSE_DELTA_MAX, float_voltage=None, cache_callback=None, max_cycles=None, timeout_hours=None) -> (bool, float|None)`. Returns `(True, delta)` on convergence; in production never returns otherwise (indefinite safe-hold). `max_cycles` is a test-only bound. `timeout_hours` is accepted-and-ignored (back-compat shim). Module constants `POLL_INTERVAL`, `SETTLE_TIMEOUT`, `MAX_NONE_CYCLES`.

- [ ] **Step 1: Write the new failing tests** — replace the entire contents of `fla-shared/tests/test_voltage_matching.py` with:

```python
"""Tests for voltage_matching.wait_for_match (controlled hold-and-lower reconnect)."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))
from helpers import dbus_mock_setup, MockMonitor, MockStatus

dbus_mock_setup()

from voltage_matching import (
    wait_for_match, POLL_INTERVAL, SETTLE_TIMEOUT, MAX_NONE_CYCLES,
)


class TestWaitForMatch(unittest.TestCase):
    def setUp(self):
        self.temp_service = MagicMock()
        self.alerting_mod = MagicMock()
        self.status = MockStatus()

    # --- Convergence (the only production return) ---

    @patch('voltage_matching.time')
    def test_immediate_convergence_closes(self, mock_time):
        mock_time.time.side_effect = [0, 1]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.3, lfp_voltage=27.0)

        ok, delta = wait_for_match(monitor, self.temp_service, self.status,
                                   self.alerting_mod, float_voltage=27.0)

        self.assertTrue(ok)
        self.assertAlmostEqual(delta, 0.3)
        mock_time.sleep.assert_not_called()

    @patch('voltage_matching.time')
    def test_converges_after_descent(self, mock_time):
        mock_time.time.side_effect = [0, 2, 4, 6]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(lfp_voltage=27.0)
        monitor.get_trojan_voltage = MagicMock(side_effect=[29.0, 28.2, 27.2])

        ok, delta = wait_for_match(monitor, self.temp_service, self.status,
                                   self.alerting_mod, float_voltage=27.0)

        self.assertTrue(ok)
        self.assertAlmostEqual(delta, 0.2)

    # --- Re-pin float every cycle ---

    @patch('voltage_matching.time')
    def test_float_repinned_every_cycle(self, mock_time):
        mock_time.time.side_effect = [0, 2, 4, 6]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(lfp_voltage=27.0)
        monitor.get_trojan_voltage = MagicMock(side_effect=[29.0, 28.0, 27.0])

        wait_for_match(monitor, self.temp_service, self.status,
                       self.alerting_mod, float_voltage=27.0)

        # 3 cycles ran before convergence → float pinned 3 times, all to 27.0
        self.assertEqual(self.temp_service.set_charge_voltage.call_count, 3)
        for call in self.temp_service.set_charge_voltage.call_args_list:
            self.assertAlmostEqual(call.args[0], 27.0)

    @patch('voltage_matching.time')
    def test_no_float_voltage_skips_pin(self, mock_time):
        mock_time.time.side_effect = [0, 1]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.3, lfp_voltage=27.0)

        wait_for_match(monitor, self.temp_service, self.status,
                       self.alerting_mod, float_voltage=None)

        self.temp_service.set_charge_voltage.assert_not_called()

    # --- Never auto-close above threshold ---

    @patch('voltage_matching.time')
    def test_never_closes_above_threshold(self, mock_time):
        # Bus stuck 2V high for the whole bounded run — must never converge.
        mock_time.time.side_effect = [0] + [i * POLL_INTERVAL for i in range(1, 12)]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=29.0, lfp_voltage=27.0)  # delta 2.0

        ok, delta = wait_for_match(monitor, self.temp_service, self.status,
                                   self.alerting_mod, float_voltage=27.0,
                                   max_cycles=8)

        self.assertFalse(ok)              # bounded by max_cycles, never converged
        self.assertAlmostEqual(delta, 2.0)
        # Relay close is the caller's job and only after (True, _) — assert the
        # loop never told the temp service to do anything but hold float.
        self.assertNotIn(1, monitor._relay_set_calls)

    # --- SETTLE_TIMEOUT debounce ---

    @patch('voltage_matching.time')
    def test_transient_undershoot_does_not_alarm(self, mock_time):
        # Non-converged but still inside the settle window → no alarm yet.
        mock_time.time.side_effect = [0, SETTLE_TIMEOUT - 10, SETTLE_TIMEOUT - 8]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=29.0, lfp_voltage=27.0)

        wait_for_match(monitor, self.temp_service, self.status,
                       self.alerting_mod, float_voltage=27.0, max_cycles=2)

        self.alerting_mod.raise_alarm.assert_not_called()

    @patch('voltage_matching.time')
    def test_sustained_nonconvergence_enters_safe_hold(self, mock_time):
        # Past SETTLE_TIMEOUT and still 2V high → safe-hold + alarm, float still pinned.
        mock_time.time.side_effect = [0, SETTLE_TIMEOUT + 1, SETTLE_TIMEOUT + 3]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=29.0, lfp_voltage=27.0)

        ok, _ = wait_for_match(monitor, self.temp_service, self.status,
                               self.alerting_mod, float_voltage=27.0, max_cycles=2)

        self.assertFalse(ok)
        self.alerting_mod.raise_alarm.assert_called()      # safe-hold alarm
        self.temp_service.set_charge_voltage.assert_called_with(27.0)  # still holding
        self.assertNotIn(1, monitor._relay_set_calls)

    # --- None-shunt safe-hold ---

    @patch('voltage_matching.time')
    def test_lfp_none_never_closes_blind(self, mock_time):
        mock_time.time.side_effect = [0] + [i * POLL_INTERVAL for i in range(1, 6)]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.0, lfp_voltage=None)

        ok, _ = wait_for_match(monitor, self.temp_service, self.status,
                               self.alerting_mod, float_voltage=27.0, max_cycles=4)

        self.assertFalse(ok)
        self.assertNotIn(1, monitor._relay_set_calls)

    @patch('voltage_matching.time')
    def test_sustained_none_enters_safe_hold(self, mock_time):
        mock_time.time.side_effect = [0] + [i * POLL_INTERVAL for i in range(1, MAX_NONE_CYCLES + 3)]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.0, lfp_voltage=None)

        wait_for_match(monitor, self.temp_service, self.status,
                       self.alerting_mod, float_voltage=27.0,
                       max_cycles=MAX_NONE_CYCLES + 1)

        self.alerting_mod.raise_alarm.assert_called()

    @patch('voltage_matching.time')
    def test_transient_none_resets_and_converges(self, mock_time):
        mock_time.time.side_effect = [0, 2, 4, 6, 8]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.0)
        monitor.get_lfp_voltage = MagicMock(side_effect=[None, None, None, 27.0])

        ok, _ = wait_for_match(monitor, self.temp_service, self.status,
                               self.alerting_mod, float_voltage=27.0)

        self.assertTrue(ok)
        self.alerting_mod.raise_alarm.assert_not_called()

    # --- Status / cache plumbing preserved ---

    @patch('voltage_matching.time')
    def test_status_and_cache_updated(self, mock_time):
        mock_time.time.side_effect = [0, 1]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.3, lfp_voltage=27.0)
        cb = MagicMock()

        wait_for_match(monitor, self.temp_service, self.status,
                       self.alerting_mod, float_voltage=27.0, cache_callback=cb)

        self.assertTrue(self.status.updates)
        last = self.status.updates[-1]
        self.assertAlmostEqual(last['trojan_v'], 27.3)
        self.assertAlmostEqual(last['lfp_v'], 27.0)
        cb.assert_called_once()
        self.assertAlmostEqual(cb.call_args[1]['voltage_delta'], 0.3)

    @patch('voltage_matching.time')
    def test_timeout_hours_is_ignored(self, mock_time):
        # Back-compat shim: callers still pass timeout_hours; it must be accepted.
        mock_time.time.side_effect = [0, 1]
        mock_time.sleep = MagicMock()
        monitor = MockMonitor(trojan_voltage=27.2, lfp_voltage=27.0)

        ok, _ = wait_for_match(monitor, self.temp_service, self.status,
                               self.alerting_mod, float_voltage=27.0, timeout_hours=4.0)

        self.assertTrue(ok)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `python3 -m unittest fla-shared.tests.test_voltage_matching -v`
Expected: FAIL — `ImportError: cannot import name 'POLL_INTERVAL'` (constants don't exist yet).

- [ ] **Step 3: Rewrite `fla-shared/voltage_matching.py`** — replace the entire file with:

```python
"""Reconnect hold loop — holds the main bus at float, reconnects the LFP bank
when the Trojan<->LFP delta converges.

Shared between FLA equalisation and FLA charge services.

Reconnect is a *stable hold*, not a race. While the relay is open the temp
battery pins the bus to float (≈ the isolated LFP voltage) EVERY cycle, so the
bus never free-falls. The relay is auto-closed ONLY when the delta is within
RELAY_CLOSE_DELTA_MAX. If the bus cannot be brought within threshold, the loop
enters an indefinite safe-hold (float pinned, relay open, alarm re-asserted) and
never returns into the caller's teardown — the only exits are convergence or an
out-of-band manual operator action (restore shore power, or close relay 2 from
the GUI, which collapses the delta the loop then sees as convergence).
"""

import logging
import time

from relay_control import RELAY_CLOSE_DELTA_MAX

log = logging.getLogger(__name__)

POLL_INTERVAL = 2       # seconds between reconnect polls (was 30 — bus is held now)
SETTLE_TIMEOUT = 120    # seconds of non-convergence before declaring safe-hold
MAX_NONE_CYCLES = 10    # consecutive None shunt reads (~20s) before safe-hold


def wait_for_match(monitor, temp_service, status, alerting_mod,
                   voltage_delta_max=RELAY_CLOSE_DELTA_MAX, float_voltage=None,
                   cache_callback=None, max_cycles=None, timeout_hours=None):
    """Hold the bus at float and wait for |V_trojan - V_lfp| <= voltage_delta_max.

    Re-pins float_voltage on the temp battery every cycle so the bus is actively
    held while the relay is open. Returns (True, delta) on convergence.

    On sustained non-convergence (elapsed > SETTLE_TIMEOUT) or sustained
    unreadable shunts (>= MAX_NONE_CYCLES), enters a safe-hold: keeps looping
    with float pinned, relay open, buzzer re-asserted — it does NOT return, so
    the caller never reaches teardown while the LFP is isolated. The firm
    invariant is that the relay is NEVER auto-closed while delta > threshold.

    max_cycles is a TEST-ONLY bound: production passes None (truly indefinite
    safe-hold). When set, the loop returns (False, delta) after that many cycles
    so unit tests of the safe-hold terminate deterministically.

    timeout_hours is accepted but IGNORED — the old 4h convergence timeout was
    replaced by the indefinite safe-hold. Retained so existing call sites that
    still pass it keep working.
    """
    log.info("Reconnect hold: pinning bus to float %s, closing when delta <= %.2fV",
             "%.2fV" % float_voltage if float_voltage is not None else "N/A",
             voltage_delta_max)
    match_start = time.time()
    delta = None
    none_count = 0
    safe_hold = False
    cycle = 0

    while True:
        cycle += 1
        if max_cycles is not None and cycle > max_cycles:
            return False, delta  # test-only bound; production never sets max_cycles

        # Re-pin the hold every cycle so drift / a resume re-asserts float cleanly.
        if float_voltage is not None:
            temp_service.set_charge_voltage(float_voltage)

        elapsed = time.time() - match_start
        v_trojan = monitor.get_trojan_voltage()
        v_lfp = monitor.get_lfp_voltage()

        # Delta unknown — cannot decide to close; keep holding, never close blind.
        if v_trojan is None or v_lfp is None:
            none_count += 1
            status.update(time_remaining=0, trojan_v=v_trojan, lfp_v=v_lfp)
            if none_count >= MAX_NONE_CYCLES:
                if not safe_hold:
                    log.error("Shunt unreadable for %d cycles during reconnect — "
                              "entering safe-hold (bus held at float, relay open)", none_count)
                    if alerting_mod and status:
                        alerting_mod.raise_alarm(
                            "Reconnect: shunt unreadable — bus held, manual intervention required",
                            status_service=status,
                        )
                    safe_hold = True
                elif alerting_mod:
                    alerting_mod.activate_buzzer()
            time.sleep(POLL_INTERVAL)
            continue

        none_count = 0
        delta = abs(v_trojan - v_lfp)
        status.update(time_remaining=0, trojan_v=v_trojan, lfp_v=v_lfp)
        temp_service.update_voltage_current(v_trojan, monitor.get_trojan_current())
        if cache_callback:
            cache_callback(trojan_v=v_trojan, lfp_v=v_lfp,
                           voltage_delta=delta, time_remaining=0)

        if delta <= voltage_delta_max:
            log.info("Voltage converged: Trojan=%.2fV, LFP=%.2fV, delta=%.2fV",
                     v_trojan, v_lfp, delta)
            return True, delta

        # Not converged. A transient undershoot during the initial 31V->27V
        # settle must not false-trigger, so only declare safe-hold once the bus
        # has had SETTLE_TIMEOUT to settle.
        if elapsed > SETTLE_TIMEOUT:
            if not safe_hold:
                log.error("Bus not within %.2fV after %.0fs — entering safe-hold "
                          "(float pinned, relay open). Trojan=%.2fV, LFP=%.2fV, delta=%.2fV",
                          voltage_delta_max, elapsed, v_trojan, v_lfp, delta)
                if alerting_mod and status:
                    alerting_mod.raise_alarm(
                        "Reconnect not converging — bus held at float, manual intervention required",
                        status_service=status,
                    )
                safe_hold = True
            elif alerting_mod:
                alerting_mod.activate_buzzer()
        elif int(elapsed) % 30 < POLL_INTERVAL:
            log.info("Reconnect hold: Trojan=%.2fV, LFP=%.2fV, delta=%.2fV (%.0fs)",
                     v_trojan, v_lfp, delta, elapsed)

        time.sleep(POLL_INTERVAL)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest fla-shared.tests.test_voltage_matching -v`
Expected: PASS (all tests).

- [ ] **Step 5: Run the full shared suite to catch fallout**

Run: `python3 -m unittest discover -s fla-shared/tests`
Expected: `OK`. (No other shared module imports `wait_for_match`; only the rewritten test file references its internals.)

- [ ] **Step 6: Commit**

```bash
git add fla-shared/voltage_matching.py fla-shared/tests/test_voltage_matching.py
git commit -m "Hold the bus at float during reconnect with indefinite safe-hold

Re-pin float every poll cycle, drop poll interval to 2s, and replace the
4h convergence timeout with an indefinite safe-hold: on sustained
non-convergence or unreadable shunts the loop holds at float with the
relay open and alarms, never auto-closing above the safe delta."
```

---

## Task 2: Attach-mode + relay-aware orphan (`temp_battery.py`)

**Files:**
- Modify: `fla-shared/temp_battery.py`
- Test: `fla-shared/tests/test_temp_battery_orphan.py`

**Interfaces:**
- Consumes: `subprocess.run([...])`, `lock.is_locked()`, module constant `PROCESS_MATCH`.
- Produces:
  - `is_temp_battery_running() -> bool` — true if a `temp_battery_process.py` subprocess is alive.
  - `recover_orphan_temp_battery(relay_state=None) -> bool` — when `relay_state == 0` (relay open) it NEVER kills (live hold) and returns `False`; otherwise unchanged lock-based orphan kill.
  - `TempBatteryService.attach() -> bool` — adopt an already-running subprocess (no `Popen`); sets `_registered=True`, `_attached=True`, `_process=None`.
  - `TempBatteryService.deregister()` — when `_attached`, stops via `pkill -TERM -f PROCESS_MATCH` instead of a `Popen` signal.

- [ ] **Step 1: Write the failing tests** — replace the contents of `fla-shared/tests/test_temp_battery_orphan.py` with:

```python
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

    def test_default_relay_state_none_behaves_as_closed(self, mock_lock, mock_subproc):
        # Back-compat: a caller that passes no relay_state still gets orphan kill.
        mock_lock.is_locked.return_value = False
        mock_subproc.run.side_effect = [
            MagicMock(returncode=0, stdout=b"7487\n"),
            MagicMock(returncode=0, stdout=b""),
        ]
        self.assertTrue(temp_battery.recover_orphan_temp_battery())


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest fla-shared.tests.test_temp_battery_orphan -v`
Expected: FAIL — `recover_orphan_temp_battery()` takes no `relay_state`; `is_temp_battery_running` / `attach` don't exist.

- [ ] **Step 3: Add `is_temp_battery_running()` and make orphan recovery relay-aware** — in `fla-shared/temp_battery.py`, replace the `recover_orphan_temp_battery` function (currently lines 27–54) with:

```python
def is_temp_battery_running():
    """True if a temp_battery_process.py subprocess is currently alive."""
    try:
        found = subprocess.run(["pgrep", "-f", PROCESS_MATCH], capture_output=True)
    except OSError as e:
        log.warning("temp-battery liveness check failed (pgrep): %s", e)
        return False
    return found.returncode == 0 and bool(found.stdout.strip())


def recover_orphan_temp_battery(relay_state=None):
    """Kill a stray temp battery subprocess left running with no operation lock.

    Relay-aware. When relay 2 is OPEN (relay_state == 0) the temp battery is
    holding the isolated main bus — killing it is the free-fall cascade, so we
    NEVER kill in that case; the resume path adopts it instead. We only treat a
    stray temp battery as a true orphan when the relay is closed (or unknown).

    The temp battery subprocess registers com.victronenergy.battery.fla_temp.
    If it outlives its operation — e.g. dbus-daemon is restarted mid-handoff by
    a firmware update — it keeps that name registered in a half-dead state,
    which hangs every Victron dbusmonitor scan (systemcalc, the aggregate
    driver) and takes the whole DVCC chain down. The operation lock guarantees
    only one operation runs at a time, so a temp_battery_process.py running with
    no lock held (and the relay closed) is definitively an orphan. Call at
    service startup. Returns True if an orphan was found and killed."""
    if relay_state == 0:
        log.info("Relay open at startup — temp battery is a live hold, not an orphan; "
                 "deferring to the resume path")
        return False
    if lock.is_locked():
        return False  # A real operation owns the temp battery — leave it alone
    try:
        found = subprocess.run(["pgrep", "-f", PROCESS_MATCH], capture_output=True)
    except OSError as e:
        log.warning("Orphan temp-battery check failed (pgrep): %s", e)
        return False
    if found.returncode != 0 or not found.stdout.strip():
        return False  # Nothing running — nothing to recover
    log.warning("Orphaned temp battery subprocess running with no operation lock "
                "— killing to unblock D-Bus discovery")
    try:
        subprocess.run(["pkill", "-9", "-f", PROCESS_MATCH], capture_output=True)
    except OSError as e:
        log.error("Failed to kill orphan temp battery: %s", e)
        return False
    return True
```

- [ ] **Step 4: Add attach mode to `TempBatteryService`** — in `fla-shared/temp_battery.py`:

In `__init__` (after `self._registered = False`), add:

```python
        self._attached = False  # True when adopting an already-running subprocess
```

Add a new `attach()` method immediately after `register()`:

```python
    def attach(self):
        """Adopt an already-running temp battery subprocess (resume path).

        Used when a service starts up and finds the relay open with a temp
        battery still holding the bus. There is no Popen handle to manage — CVL
        is steered through the shared file and the subprocess is stopped via
        pkill at teardown (no respawn, so the hold never gaps)."""
        self._registered = True
        self._attached = True
        self._process = None
        log.info("Attached to existing temp battery subprocess (resume)")
        return True
```

Replace the `deregister()` method (currently lines 116–134) with:

```python
    def deregister(self):
        """Stop the subprocess (Popen-managed) or kill it by match (attached)."""
        if self._attached:
            try:
                subprocess.run(["pkill", "-TERM", "-f", PROCESS_MATCH],
                               capture_output=True)
                log.info("Attached temp battery subprocess signalled to stop")
            except OSError as e:
                log.warning("Failed to stop attached temp battery: %s", e)
            try:
                os.unlink(CVL_FILE)
            except OSError:
                pass
            self._registered = False
            self._attached = False
            return

        if not self._registered or self._process is None:
            return
        try:
            self._process.send_signal(signal.SIGTERM)
            self._process.wait(timeout=5)
            log.info("Temp battery subprocess stopped (PID %d)", self._process.pid)
        except subprocess.TimeoutExpired:
            self._process.kill()
            log.warning("Temp battery subprocess killed (PID %d)", self._process.pid)
        except Exception as e:
            log.warning("Error stopping subprocess: %s", e)
        try:
            os.unlink(CVL_FILE)
        except OSError:
            pass
        self._process = None
        self._registered = False
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m unittest fla-shared.tests.test_temp_battery_orphan -v`
Expected: PASS.

- [ ] **Step 6: Run the full shared suite**

Run: `python3 -m unittest discover -s fla-shared/tests`
Expected: `OK`.

- [ ] **Step 7: Commit**

```bash
git add fla-shared/temp_battery.py fla-shared/tests/test_temp_battery_orphan.py
git commit -m "Add temp-battery attach mode and relay-aware orphan recovery

Never kill a temp battery while relay 2 is open — it is holding the
isolated bus, and killing it free-falls the ship. Add attach() to adopt a
running subprocess on resume and is_temp_battery_running() for the startup
resume check."
```

---

## Task 3: EQ — operator Abort routes through the controlled reconnect

**Files:**
- Modify: `fla-equalisation/fla_equalisation.py` (`run_equalisation`, lines ~125–363)
- Test: `fla-equalisation/tests/test_fla_equalisation.py`

**Interfaces:**
- Consumes: `check_abort()`, `clear_abort()`, `wait_for_match(...)` (Task 1), `write_last_equalisation()`.
- Produces: `run_equalisation` now treats an operator Abort *in the relay-open equalising loop* as "stop charging early and reconnect cleanly". It breaks out of the equalising loop into the existing voltage-matching/reconnect, and on a clean reconnect returns `False` **without** writing `last_equalisation`. Hard errors and pre-relay aborts keep their existing immediate-return behaviour.

- [ ] **Step 1: Write the failing test** — add to `fla-equalisation/tests/test_fla_equalisation.py`, inside a new test class (place after `TestSafetyGuards`):

```python
class TestOperatorAbortRouting(unittest.TestCase):
    """Operator Abort in the relay-open equalising loop reconnects cleanly."""

    def _make_mocks(self, **kw):
        settings = MockSettings(**kw)
        monitor = MockMonitor(trojan_voltage=27.2, lfp_voltage=27.0, relay_state=0)
        status = MockStatus()
        return settings, monitor, status

    @patch('fla_equalisation.write_last_equalisation')
    @patch('fla_equalisation.wait_for_match', return_value=(True, 0.2))
    @patch('fla_equalisation.close_relay_verified', return_value=True)
    @patch('fla_equalisation.start_aggregate_driver', return_value=True)
    @patch('fla_equalisation.stop_aggregate_driver', return_value=True)
    @patch('fla_equalisation.verify_relay_open', return_value=True)
    @patch('fla_equalisation.open_relay', return_value=True)
    @patch('fla_equalisation.release_lock')
    @patch('fla_equalisation.acquire_lock', return_value=True)
    @patch('fla_equalisation.check_abort', return_value=True)
    @patch('fla_equalisation.time')
    def test_operator_abort_reconnects_without_timestamp(
        self, mock_time, mock_abort, mock_lock, mock_unlock, mock_open, mock_verify,
        mock_stop, mock_start, mock_close, mock_match, mock_write,
    ):
        mock_time.time.side_effect = [i for i in range(0, 200, 5)]
        mock_time.sleep = MagicMock()
        settings, monitor, status = self._make_mocks()
        with patch('fla_equalisation.TempBatteryService') as MockTBS:
            MockTBS.return_value = MagicMock()
            with patch('fla_equalisation.update_cache'):
                result = run_equalisation(settings, monitor, status)

        # Reconnect happened…
        mock_match.assert_called_once()
        mock_close.assert_called_once()
        # …but the run is flagged non-completion: no timestamp, returns False.
        mock_write.assert_not_called()
        self.assertFalse(result)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest fla-equalisation.tests.test_fla_equalisation.TestOperatorAbortRouting -v`
Expected: FAIL — abort currently returns `False` immediately (no `wait_for_match`/`close_relay_verified` call).

- [ ] **Step 3: Add the abort flag and break-to-reconnect** — in `run_equalisation`:

Add the flag with the other locals (after `original_bms_instance = None`, ~line 135):

```python
    aborted_by_operator = False
```

Replace the equalising-loop abort block (currently lines 285–290):

```python
            if check_abort():
                log.warning("Abort requested via web UI")
                clear_abort()
                status.update(state=STATE_ERROR)
                raise_alarm("Equalisation aborted by operator", status_service=status)
                return False
```

with:

```python
            if check_abort():
                # Relay is open here (LFP isolated). A hard-stop would tear down
                # the temp battery and free-fall the bus, so instead break into
                # the controlled reconnect and flag the run as a non-completion.
                log.warning("Operator abort during equalisation — proceeding to controlled reconnect")
                clear_abort()
                aborted_by_operator = True
                break
```

Replace the success-record block (currently lines 358–363):

```python
        # Step 11: Record success
        write_last_equalisation()
        status.update(state=STATE_IDLE, time_remaining=0)
        log.info("Equalisation completed successfully")
        clear_alarm(status_service=status)
        return True
```

with:

```python
        # Step 11: Record outcome. An operator-aborted run reconnects safely but
        # is NOT a completion — do not advance the equalisation interval.
        status.update(state=STATE_IDLE, time_remaining=0)
        clear_alarm(status_service=status)
        if aborted_by_operator:
            log.info("Operator-aborted equalisation reconnected safely — interval not advanced")
            return False
        write_last_equalisation()
        log.info("Equalisation completed successfully")
        return True
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest fla-equalisation.tests.test_fla_equalisation.TestOperatorAbortRouting -v`
Expected: PASS.

- [ ] **Step 5: Run the full EQ suite**

Run: `python3 -m unittest discover -s fla-equalisation/tests`
Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add fla-equalisation/fla_equalisation.py fla-equalisation/tests/test_fla_equalisation.py
git commit -m "Route EQ operator abort through the controlled reconnect

An abort while the LFP is isolated now breaks into voltage matching and
reconnects the bank cleanly instead of hard-stopping and free-falling the
bus. The run is flagged non-completion: it reconnects but does not advance
the equalisation interval."
```

---

## Task 4: EQ — relay-state-guarded teardown (`finally`)

**Files:**
- Modify: `fla-equalisation/fla_equalisation.py` (`run_equalisation` `finally`, lines ~371–409)
- Test: `fla-equalisation/tests/test_fla_equalisation.py`

**Interfaces:**
- Consumes: `monitor.get_relay_state()` (returns `1` closed / `0` open), `raise_alarm`, existing teardown calls.
- Produces: the `finally` tears down (restore BmsInstance/BatteryService/DVCC, deregister temp, restart aggregate, release lock) ONLY when the relay reads closed (`== 1`). When the relay is open it does NONE of that — it leaves the temp battery registered and holding, keeps the lock held, leaves the aggregate stopped, and raises a critical hold alarm.

- [ ] **Step 1: Write the failing tests** — add a new class to `fla-equalisation/tests/test_fla_equalisation.py`:

```python
class TestRelayStateGuardedFinally(unittest.TestCase):
    """The finally must not tear down while the relay is open."""

    @patch('fla_equalisation.raise_alarm')
    @patch('fla_equalisation.start_aggregate_driver', return_value=True)
    @patch('fla_equalisation.stop_aggregate_driver', return_value=True)
    @patch('fla_equalisation.verify_relay_open', return_value=True)
    @patch('fla_equalisation.open_relay', return_value=True)
    @patch('fla_equalisation.release_lock')
    @patch('fla_equalisation.acquire_lock', return_value=True)
    @patch('fla_equalisation.time')
    def test_relay_open_exit_holds_and_does_not_teardown(
        self, mock_time, mock_lock, mock_unlock, mock_open, mock_verify,
        mock_stop, mock_start, mock_alarm,
    ):
        # Trojan goes unresponsive mid-EQ (hard error) → return False with relay open.
        mock_time.time.side_effect = [i for i in range(0, 200, 5)]
        mock_time.sleep = MagicMock()
        settings = MockSettings()
        monitor = MockMonitor(relay_state=0, lfp_voltage=27.0)
        monitor.get_trojan_voltage = MagicMock(return_value=None)  # unresponsive
        status = MockStatus()
        temp = MagicMock()
        with patch('fla_equalisation.TempBatteryService', return_value=temp):
            with patch('fla_equalisation.update_cache'):
                result = run_equalisation(settings, monitor, status)

        self.assertFalse(result)
        temp.deregister.assert_not_called()      # temp battery left holding
        mock_unlock.assert_not_called()          # lock stays held
        self.assertTrue(mock_alarm.called)       # hold alarm raised

    @patch('fla_equalisation.close_relay_delta_aware')
    @patch('fla_equalisation.start_aggregate_driver', return_value=True)
    @patch('fla_equalisation.stop_aggregate_driver', return_value=False)
    @patch('fla_equalisation.release_lock')
    @patch('fla_equalisation.acquire_lock', return_value=True)
    def test_relay_closed_exit_tears_down_normally(
        self, mock_lock, mock_unlock, mock_stop, mock_start, mock_close,
    ):
        # stop_aggregate fails before relay ever opens → relay stays closed.
        settings = MockSettings()
        monitor = MockMonitor(relay_state=1)
        status = MockStatus()
        with patch('fla_equalisation.TempBatteryService') as MockTBS:
            MockTBS.return_value = MagicMock()
            result = run_equalisation(settings, monitor, status)

        self.assertFalse(result)
        mock_unlock.assert_called_once()         # normal teardown released the lock
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest fla-equalisation.tests.test_fla_equalisation.TestRelayStateGuardedFinally -v`
Expected: FAIL — current `finally` deregisters/releases unconditionally, so `deregister`/`release_lock` are called even with the relay open.

- [ ] **Step 3: Guard the `finally` on relay state** — replace the entire `finally` block (currently lines 371–409) with:

```python
    finally:
        # Teardown (handing control back to DVCC/aggregate) is SAFE ONLY once
        # the relay is confirmed closed. With the relay open the LFP is still
        # isolated and the temp battery is holding the bus — deregistering it or
        # restoring DVCC now is exactly the free-fall cascade. So branch on relay
        # state. In normal operation the safe-hold means we never reach here with
        # the relay open; this is the belt-and-suspenders for any unexpected exit.
        if monitor.get_relay_state() != 1:
            log.error("CLEANUP: relay open at exit — holding bus, NOT tearing down "
                      "(temp battery left registered, lock held, aggregate stopped)")
            raise_alarm(
                "Reconnect incomplete — bus held by temp battery, manual intervention required",
                status_service=status,
            )
        else:
            # Relay confirmed closed — restore DVCC and hand back to the aggregate.
            if original_bms_instance is not None:
                try:
                    monitor.set_bms_instance(original_bms_instance)
                    log.info("BmsInstance restored to %s", original_bms_instance)
                except Exception:
                    log.error("CRITICAL: Failed to restore BmsInstance setting")

            if original_battery_service is not None:
                try:
                    monitor.set_battery_service_setting(original_battery_service)
                    log.info("BatteryService restored to %s", original_battery_service)
                except Exception:
                    log.error("CRITICAL: Failed to restore BatteryService setting")

            if original_dvcc_voltage is not None:
                try:
                    monitor.set_dvcc_max_charge_voltage(original_dvcc_voltage)
                    log.info("DVCC MaxChargeVoltage restored to %.1fV", original_dvcc_voltage)
                except Exception:
                    log.error("CRITICAL: Failed to restore DVCC MaxChargeVoltage")

            if temp_service is not None:
                try:
                    temp_service.deregister()
                except Exception:
                    pass

            # No-op when the relay is already closed; harmless belt-and-suspenders.
            close_relay_delta_aware(monitor, alerting, status)

            if aggregate_stopped:
                try:
                    start_aggregate_driver()
                except Exception:
                    log.error("CRITICAL: Failed to restart aggregate driver in cleanup")

            release_lock()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest fla-equalisation.tests.test_fla_equalisation.TestRelayStateGuardedFinally -v`
Expected: PASS.

- [ ] **Step 5: Run the full EQ suite — watch for the semantic change**

Run: `python3 -m unittest discover -s fla-equalisation/tests`
Expected: `OK`. If any pre-existing test that drives `run_equalisation` to a *relay-open* exit asserted `release_lock`/`deregister`/`start_aggregate_driver` was called, it now legitimately fails — update that test to reflect the new safe-hold semantics (relay-open exit holds, does not tear down) and note the change in the commit body.

- [ ] **Step 6: Commit**

```bash
git add fla-equalisation/fla_equalisation.py fla-equalisation/tests/test_fla_equalisation.py
git commit -m "Guard EQ teardown on a confirmed-closed relay

The finally block now tears down (deregister temp battery, restore DVCC,
restart aggregate, release lock) only when relay 2 reads closed. With the
relay open it holds the bus and alarms instead — handing back to DVCC while
the LFP is isolated is the free-fall cascade."
```

---

## Task 5: EQ — resume an interrupted hold on startup

**Files:**
- Modify: `fla-equalisation/fla_equalisation.py` (`FlaEqualisationService.__init__` ~415–428; add `_resume_interrupted_reconnect`)
- Test: `fla-equalisation/tests/test_fla_equalisation.py`

**Interfaces:**
- Consumes: `is_temp_battery_running()`, `recover_orphan_temp_battery(relay_state)`, `TempBatteryService.attach()`/`.deregister()` (Task 2); `wait_for_match(...)` (Task 1); `acquire_lock`, `release_lock`, `close_relay_verified`, `start_aggregate_driver`, `temp_compensate`, `monitor` accessors; `relay_control.LFP_SAFE_CVL`.
- Produces: on startup, if relay 2 is open AND a temp-battery subprocess is running, the service adopts it: acquires the lock, attaches, and runs the shared reconnect to a clean close + normal-target teardown in a worker thread. `recover_orphan_temp_battery` is called with the live relay state and runs AFTER the monitor exists. `startup_safety_check` runs only when resume does not take over.

- [ ] **Step 1: Write the failing tests** — add a new class to `fla-equalisation/tests/test_fla_equalisation.py`:

```python
class TestResumeOnStartup(unittest.TestCase):
    """Startup adopts an interrupted hold instead of killing it."""

    def _service(self, monitor):
        """Build a FlaEqualisationService with construction side-effects stubbed."""
        from fla_equalisation import FlaEqualisationService
        with patch('fla_equalisation.recover_orphan_temp_battery') as mock_recover, \
             patch('fla_equalisation.Settings', return_value=MockSettings()), \
             patch('fla_equalisation.DbusMonitor', return_value=monitor), \
             patch('fla_equalisation.StatusService', return_value=MockStatus()), \
             patch.object(FlaEqualisationService, '_update_idle_status'):
            return FlaEqualisationService, mock_recover

    @patch('fla_equalisation.threading')
    @patch('fla_equalisation.startup_safety_check')
    @patch('fla_equalisation.is_temp_battery_running', return_value=True)
    @patch('fla_equalisation.acquire_lock', return_value=True)
    def test_relay_open_with_subprocess_resumes(
        self, mock_acquire, mock_running, mock_safety, mock_threading,
    ):
        monitor = MockMonitor(relay_state=0)
        Service, mock_recover = self._service(monitor)
        Service()
        # Orphan recovery saw relay open and was a no-op; resume started a worker;
        # the normal startup safety check was skipped.
        mock_recover.assert_called_once_with(0)
        mock_threading.Thread.assert_called_once()
        mock_safety.assert_not_called()

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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest fla-equalisation.tests.test_fla_equalisation.TestResumeOnStartup -v`
Expected: FAIL — `recover_orphan_temp_battery` is currently called with no args and before the monitor exists; there is no resume path; `is_temp_battery_running` is not imported.

- [ ] **Step 3: Import the new helpers** — in `fla-equalisation/fla_equalisation.py`, update the temp_battery import (line 24):

```python
from temp_battery import TempBatteryService, recover_orphan_temp_battery, is_temp_battery_running
```

and add to the relay_control import (line 34) `LFP_SAFE_CVL`:

```python
from relay_control import open_relay, verify_relay_open, verify_relay_still_open, close_relay_verified, close_relay_delta_aware, startup_safety_check, LFP_SAFE_CVL
```

- [ ] **Step 4: Reorder `__init__` and add the resume dispatch** — replace `FlaEqualisationService.__init__` (currently lines 415–428) with:

```python
    def __init__(self):
        # Build the monitor first (cheap — no D-Bus scan; service discovery is
        # lazy), then read the live relay state so orphan recovery can be
        # relay-aware: a temp battery with the relay OPEN is a live hold and must
        # never be killed. get_relay_state() reads com.victronenergy.system only,
        # so it does not touch (and cannot hang on) a half-dead battery name.
        self.settings = Settings()
        self.monitor = DbusMonitor(lfp_instance=277, trojan_instance=279)
        relay_state = self.monitor.get_relay_state()
        recover_orphan_temp_battery(relay_state)
        self.status = StatusService()
        self.status.register()
        self._running = False
        self._failed = False
        # If an operation was interrupted mid-reconnect (relay open + temp
        # battery still holding), adopt and finish it. Otherwise run the normal
        # startup relay-safety check.
        if not self._resume_interrupted_reconnect():
            startup_safety_check(self.monitor, self.status, alerting)
        self._update_idle_status()
        log.info("FLA equalisation service started — checking every %ds", CHECK_INTERVAL_SEC)
```

Delete the now-unused `_startup_safety_check` method (currently lines 430–432) — its single caller is gone.

Also move the early `recover_orphan_temp_battery()` call: it is currently the FIRST line of `__init__` (line 419) with no args. The replacement above already calls it with `relay_state`, so ensure there is no second arg-less call left.

- [ ] **Step 5: Add the resume worker** — add this method to `FlaEqualisationService` (after `__init__`):

```python
    def _resume_interrupted_reconnect(self):
        """Adopt and finish a reconnect interrupted mid-hold.

        Returns True if this service took over (or another service owns it), so
        the caller skips the normal startup_safety_check. Returns False when
        there is nothing to resume (relay closed, or no holder subprocess)."""
        if self.monitor.get_relay_state() != 0:
            return False  # relay closed — nothing is isolated
        if not is_temp_battery_running():
            return False  # relay open but no holder — let startup_safety_check handle it
        if not acquire_lock("fla-equalisation"):
            log.info("RESUME: another service owns the interrupted reconnect — backing off")
            return True   # the other service handles it; skip our safety check
        log.warning("RESUME: relay open + temp battery alive — adopting and finishing reconnect")
        self._running = True

        def _worker():
            temp_service = TempBatteryService(device_instance=100)
            temp_service.attach()
            try:
                self.status.update(state=STATE_VOLTAGE_MATCHING)
                float_temp = self.monitor.get_battery_temperature()
                matched, _ = wait_for_match(
                    self.monitor, temp_service, self.status, alerting,
                    voltage_delta_max=self.settings.voltage_delta_max,
                    float_voltage=temp_compensate(self.settings.float_voltage, float_temp),
                )
                if not matched:
                    return  # safe-hold never returns in production; guard anyway
                self.status.update(state=STATE_RECONNECTING)
                if not close_relay_verified(self.monitor):
                    raise_alarm("RESUME: failed to close relay 2", status_service=self.status)
                    return
                time.sleep(2)
            except Exception as e:
                log.exception("RESUME worker error: %s", e)
            finally:
                # Mirror the relay-state-guarded teardown. The interrupted run's
                # saved DVCC originals are lost, so restore to known-safe normals.
                if self.monitor.get_relay_state() == 1:
                    try:
                        temp_service.deregister()
                    except Exception:
                        pass
                    try:
                        self.monitor.set_battery_service_setting("com.victronenergy.battery.aggregate")
                        self.monitor.set_bms_instance(-1)
                        self.monitor.set_dvcc_max_charge_voltage(LFP_SAFE_CVL)
                    except Exception:
                        log.error("RESUME: failed to restore DVCC to normal")
                    try:
                        start_aggregate_driver()
                        self.monitor.invalidate_services()
                    except Exception:
                        log.error("RESUME: failed to restart aggregate driver")
                    release_lock()
                    alerting.clear_alarm(status_service=self.status)
                    log.info("RESUME: reconnect completed, control handed back to aggregate")
                else:
                    raise_alarm(
                        "RESUME incomplete — bus held by temp battery, manual intervention required",
                        status_service=self.status,
                    )
                self._running = False

        threading.Thread(target=_worker, daemon=True).start()
        return True
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m unittest fla-equalisation.tests.test_fla_equalisation.TestResumeOnStartup -v`
Expected: PASS.

- [ ] **Step 7: Run the full EQ suite**

Run: `python3 -m unittest discover -s fla-equalisation/tests`
Expected: `OK`. (Any pre-existing test constructing `FlaEqualisationService` must still pass; the resume dispatch is a no-op when the relay is closed.)

- [ ] **Step 8: Commit**

```bash
git add fla-equalisation/fla_equalisation.py fla-equalisation/tests/test_fla_equalisation.py
git commit -m "Resume an interrupted EQ reconnect on startup

If the service restarts while relay 2 is open with the temp battery still
holding the bus, adopt the running subprocess (no respawn, no hold gap) and
finish the controlled reconnect. Orphan recovery is now relay-aware and runs
after the monitor exists; startup safety check only runs when there is
nothing to resume."
```

---

## Task 6: Charge — abort path, abort-routing, and relay-state-guarded teardown

**Files:**
- Modify: `fla-charge/fla_charge.py` (`run_charge`, lines ~159–501)
- Test: `fla-charge/tests/test_fla_charge.py`

**Interfaces:** identical to Tasks 3 + 4, mirrored for `run_charge`. NOTE: the relay-open bulk/absorption loop currently has **no operator-abort path** (only AC-loss and current-loss breaks). This task adds one. Phase 1 (relay closed) keeps its existing immediate-stop abort.

- [ ] **Step 1: Write the failing tests** — add to `fla-charge/tests/test_fla_charge.py` (mirror the EQ tests; use `MockChargeSettings`):

```python
class TestChargeOperatorAbortRouting(unittest.TestCase):
    """Operator Abort in the relay-open absorption loop reconnects cleanly."""

    @patch('fla_charge.write_last_charge')
    @patch('fla_charge.wait_for_match', return_value=(True, 0.2))
    @patch('fla_charge.close_relay_verified', return_value=True)
    @patch('fla_charge.start_aggregate', return_value=True)
    @patch('fla_charge.stop_aggregate', return_value=True)
    @patch('fla_charge.verify_relay_still_open', return_value=True)
    @patch('fla_charge.verify_relay_open', return_value=True)
    @patch('fla_charge.open_relay', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.release_lock')
    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.time')
    def test_operator_abort_in_absorption_reconnects_without_timestamp(
        self, mock_time, mock_lock, mock_unlock, mock_ac, mock_cell, mock_open,
        mock_verify, mock_still, mock_stop, mock_start, mock_close, mock_match, mock_write,
    ):
        mock_time.time.side_effect = [i for i in range(0, 400, 5)]
        mock_time.sleep = MagicMock()
        settings = MockChargeSettings()
        # Phase 1 transitions immediately (LFP SoC high), relay then opens.
        monitor = MockMonitor(relay_state=0, lfp_soc=96.0,
                              trojan_voltage=27.2, lfp_voltage=27.0)
        status = MockStatus()
        # Abort only AFTER Phase 1 (which has its own check_abort): first call
        # in Phase 1 returns False, subsequent (absorption) returns True.
        with patch('fla_charge.check_abort', side_effect=[False] + [True] * 50):
            with patch('fla_charge.TempBatteryService', return_value=MagicMock()):
                with patch('fla_charge.update_cache'):
                    result = run_charge(settings, monitor, status)

        mock_match.assert_called_once()
        mock_close.assert_called_once()
        mock_write.assert_not_called()
        self.assertFalse(result)


class TestChargeRelayStateGuardedFinally(unittest.TestCase):
    """The charge finally must not tear down while the relay is open."""

    @patch('fla_charge.alerting')
    @patch('fla_charge.start_aggregate', return_value=True)
    @patch('fla_charge.stop_aggregate', return_value=True)
    @patch('fla_charge.verify_relay_open', return_value=True)
    @patch('fla_charge.open_relay', return_value=True)
    @patch('fla_charge.get_max_lfp_cell_voltage', return_value=3.40)
    @patch('fla_charge.is_ac_available', return_value=True)
    @patch('fla_charge.release_lock')
    @patch('fla_charge.acquire_lock', return_value=True)
    @patch('fla_charge.time')
    def test_relay_open_exit_holds_and_does_not_teardown(
        self, mock_time, mock_lock, mock_unlock, mock_ac, mock_cell, mock_open,
        mock_verify, mock_stop, mock_start, mock_alerting,
    ):
        mock_time.time.side_effect = [i for i in range(0, 400, 5)]
        mock_time.sleep = MagicMock()
        settings = MockChargeSettings()
        monitor = MockMonitor(relay_state=0, lfp_soc=96.0, lfp_voltage=27.0)
        monitor.get_trojan_voltage = MagicMock(return_value=None)  # unresponsive in absorption
        status = MockStatus()
        temp = MagicMock()
        with patch('fla_charge.TempBatteryService', return_value=temp):
            with patch('fla_charge.update_cache'):
                result = run_charge(settings, monitor, status)

        self.assertFalse(result)
        temp.deregister.assert_not_called()
        mock_unlock.assert_not_called()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest fla-charge.tests.test_fla_charge.TestChargeOperatorAbortRouting fla-charge.tests.test_fla_charge.TestChargeRelayStateGuardedFinally -v`
Expected: FAIL — no abort path in absorption; `finally` tears down unconditionally.

- [ ] **Step 3: Add the abort flag and break-to-reconnect** — in `run_charge`:

Add the flag with the other locals (after `original_bms_instance = None`, ~line 165):

```python
    aborted_by_operator = False
```

In the **absorption loop** (Phase 2-3), add an operator-abort check. Insert it immediately before `if int(elapsed) % 300 < 30:` (currently ~line 399), mirroring the AC-loss break:

```python
            if check_abort():
                # Relay is open here (LFP isolated) — break into the controlled
                # reconnect instead of hard-stopping and free-falling the bus.
                log.warning("Operator abort during absorption — proceeding to controlled reconnect")
                clear_abort()
                aborted_by_operator = True
                break
```

(Leave the existing Phase 1 `check_abort()` block at lines 225–231 unchanged — Phase 1 is relay-closed, nothing is isolated, so a plain stop is correct.)

Replace the success-record block (currently lines 459–463):

```python
        write_last_charge()
        status.update(state=STATE_IDLE, time_remaining=0)
        alerting.clear_alarm(status_service=status)
        log.info("FLA charge completed successfully")
        return True
```

with:

```python
        status.update(state=STATE_IDLE, time_remaining=0)
        alerting.clear_alarm(status_service=status)
        if aborted_by_operator:
            log.info("Operator-aborted charge reconnected safely — not recording completion")
            return False
        write_last_charge()
        log.info("FLA charge completed successfully")
        return True
```

- [ ] **Step 4: Guard the `finally` on relay state** — replace the entire `finally` block (currently lines 471–501) with:

```python
    finally:
        # Teardown is SAFE ONLY once the relay is confirmed closed — handing back
        # to DVCC while the LFP is isolated free-falls the bus. Branch on relay
        # state (belt-and-suspenders; the safe-hold means we normally never reach
        # here with the relay open).
        if monitor.get_relay_state() != 1:
            log.error("CLEANUP: relay open at exit — holding bus, NOT tearing down")
            alerting.raise_alarm(
                "Reconnect incomplete — bus held by temp battery, manual intervention required",
                status_service=status,
            )
        else:
            if original_bms_instance is not None:
                try:
                    monitor.set_bms_instance(original_bms_instance)
                    log.info("BmsInstance restored to %s", original_bms_instance)
                except Exception:
                    log.error("CRITICAL: Failed to restore BmsInstance setting")

            if original_battery_service is not None:
                try:
                    monitor.set_battery_service_setting(original_battery_service)
                    log.info("BatteryService restored to %s", original_battery_service)
                except Exception:
                    log.error("CRITICAL: Failed to restore BatteryService setting")

            if original_dvcc_voltage is not None:
                try:
                    monitor.set_dvcc_max_charge_voltage(original_dvcc_voltage)
                    log.info("DVCC MaxChargeVoltage restored to %.1fV", original_dvcc_voltage)
                except Exception:
                    log.error("CRITICAL: Failed to restore DVCC MaxChargeVoltage")

            if temp_service is not None:
                try:
                    temp_service.deregister()
                except Exception:
                    pass
            close_relay_delta_aware(monitor, alerting, status)
            if aggregate_stopped:
                try:
                    start_aggregate()
                except Exception:
                    log.error("CRITICAL: Failed to restart aggregate driver")
            release_lock()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m unittest fla-charge.tests.test_fla_charge.TestChargeOperatorAbortRouting fla-charge.tests.test_fla_charge.TestChargeRelayStateGuardedFinally -v`
Expected: PASS.

- [ ] **Step 6: Run the full charge suite — watch for the semantic change**

Run: `python3 -m unittest discover -s fla-charge/tests`
Expected: `OK`. As in Task 4, update any pre-existing test that drove `run_charge` to a relay-open exit and asserted unconditional teardown.

- [ ] **Step 7: Commit**

```bash
git add fla-charge/fla_charge.py fla-charge/tests/test_fla_charge.py
git commit -m "Mirror reconnect safety into the charge service

Add the missing operator-abort path to the relay-open bulk/absorption loop
(breaks into the controlled reconnect, no completion timestamp), and guard
the charge teardown on a confirmed-closed relay so a relay-open exit holds
the bus and alarms instead of tearing down."
```

---

## Task 7: Charge — resume an interrupted hold on startup

**Files:**
- Modify: `fla-charge/fla_charge.py` (`FlaChargeService.__init__` ~507–520; add `_resume_interrupted_reconnect`)
- Test: `fla-charge/tests/test_fla_charge.py`

**Interfaces:** identical to Task 5, mirrored for `FlaChargeService` (lock name `"fla-charge"`, float setting `settings.fla_float_voltage`, aggregate fns `stop_aggregate`/`start_aggregate`).

- [ ] **Step 1: Write the failing tests** — add to `fla-charge/tests/test_fla_charge.py`:

```python
class TestChargeResumeOnStartup(unittest.TestCase):
    """Startup adopts an interrupted hold instead of killing it."""

    def _service(self, monitor):
        from fla_charge import FlaChargeService
        with patch('fla_charge.recover_orphan_temp_battery') as mock_recover, \
             patch('fla_charge.Settings', return_value=MockChargeSettings()), \
             patch('fla_charge.DbusMonitor', return_value=monitor), \
             patch('fla_charge.StatusService', return_value=MockStatus()), \
             patch.object(FlaChargeService, '_update_idle_status'):
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
        Service()
        mock_recover.assert_called_once_with(0)
        mock_threading.Thread.assert_called_once()
        mock_safety.assert_not_called()

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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest fla-charge.tests.test_fla_charge.TestChargeResumeOnStartup -v`
Expected: FAIL — no resume path; `recover_orphan_temp_battery` called with no args before the monitor exists; `is_temp_battery_running` not imported.

- [ ] **Step 3: Import the new helpers** — in `fla-charge/fla_charge.py`, update the temp_battery import (line 24):

```python
from temp_battery import TempBatteryService, recover_orphan_temp_battery, is_temp_battery_running
```

and add `LFP_SAFE_CVL` to the relay_control import (lines 25–28):

```python
from relay_control import (
    open_relay, verify_relay_open, verify_relay_still_open,
    close_relay_verified, close_relay_delta_aware, startup_safety_check,
    LFP_SAFE_CVL,
)
```

- [ ] **Step 4: Reorder `__init__` and add the resume dispatch** — replace `FlaChargeService.__init__` (currently lines 507–520) with:

```python
    def __init__(self):
        # Build the monitor first (cheap), read the live relay state, then do
        # relay-aware orphan recovery — never kill a temp battery that is holding
        # an isolated bus (relay open). get_relay_state() reads system only.
        self.settings = Settings()
        self.monitor = DbusMonitor(lfp_instance=277, trojan_instance=279)
        relay_state = self.monitor.get_relay_state()
        recover_orphan_temp_battery(relay_state)
        self.status = StatusService()
        self.status.register()
        self._running = False
        self._failed = False
        if not self._resume_interrupted_reconnect():
            startup_safety_check(self.monitor, self.status, alerting)
        self._update_idle_status()
        log.info("FLA charge service started — checking every %ds", CHECK_INTERVAL_SEC)
```

- [ ] **Step 5: Add the resume worker** — add this method to `FlaChargeService` (after `__init__`):

```python
    def _resume_interrupted_reconnect(self):
        """Adopt and finish a reconnect interrupted mid-hold (see EQ equivalent).

        Returns True if this service took over (or another owns it), so the
        caller skips startup_safety_check. False when there is nothing to resume."""
        if self.monitor.get_relay_state() != 0:
            return False
        if not is_temp_battery_running():
            return False
        if not acquire_lock("fla-charge"):
            log.info("RESUME: another service owns the interrupted reconnect — backing off")
            return True
        log.warning("RESUME: relay open + temp battery alive — adopting and finishing reconnect")
        self._running = True

        def _worker():
            temp_service = TempBatteryService(device_instance=100)
            temp_service.attach()
            try:
                self.status.update(state=STATE_VOLTAGE_MATCHING)
                float_temp = self.monitor.get_battery_temperature()
                matched, _ = wait_for_match(
                    self.monitor, temp_service, self.status, alerting,
                    voltage_delta_max=self.settings.voltage_delta_max,
                    float_voltage=temp_compensate(self.settings.fla_float_voltage, float_temp),
                )
                if not matched:
                    return
                self.status.update(state=STATE_RECONNECTING)
                if not close_relay_verified(self.monitor):
                    alerting.raise_alarm("RESUME: failed to close relay 2", status_service=self.status)
                    return
                time.sleep(2)
            except Exception as e:
                log.exception("RESUME worker error: %s", e)
            finally:
                if self.monitor.get_relay_state() == 1:
                    try:
                        temp_service.deregister()
                    except Exception:
                        pass
                    try:
                        self.monitor.set_battery_service_setting("com.victronenergy.battery.aggregate")
                        self.monitor.set_bms_instance(-1)
                        self.monitor.set_dvcc_max_charge_voltage(LFP_SAFE_CVL)
                    except Exception:
                        log.error("RESUME: failed to restore DVCC to normal")
                    try:
                        start_aggregate()
                        self.monitor.invalidate_services()
                    except Exception:
                        log.error("RESUME: failed to restart aggregate driver")
                    release_lock()
                    alerting.clear_alarm(status_service=self.status)
                    log.info("RESUME: reconnect completed, control handed back to aggregate")
                else:
                    alerting.raise_alarm(
                        "RESUME incomplete — bus held by temp battery, manual intervention required",
                        status_service=self.status,
                    )
                self._running = False

        threading.Thread(target=_worker, daemon=True).start()
        return True
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m unittest fla-charge.tests.test_fla_charge.TestChargeResumeOnStartup -v`
Expected: PASS.

- [ ] **Step 7: Run the full charge suite**

Run: `python3 -m unittest discover -s fla-charge/tests`
Expected: `OK`.

- [ ] **Step 8: Commit**

```bash
git add fla-charge/fla_charge.py fla-charge/tests/test_fla_charge.py
git commit -m "Resume an interrupted charge reconnect on startup

Mirror the EQ resume path: on restart with relay 2 open and the temp battery
still holding, adopt the running subprocess and finish the controlled
reconnect. Relay-aware orphan recovery now runs after the monitor exists."
```

---

## Task 8: Full-suite regression + cross-service parity check

**Files:** none (verification only).

- [ ] **Step 1: Run the entire test suite**

Run:
```bash
python3 -m unittest discover -s fla-shared/tests
python3 -m unittest discover -s fla-equalisation/tests
python3 -m unittest discover -s fla-charge/tests
```
Expected: three `OK` results. Total ≥ 196 (the new tests add to the baseline of 108 + 51 + 37).

- [ ] **Step 2: Verify the firm invariant holds in code** — confirm no path auto-closes the relay above threshold:

Run: `grep -rn "set_relay(1)\|close_relay" fla-shared/voltage_matching.py`
Expected: no matches — `wait_for_match` never closes the relay itself; closing is always the caller's job after `(True, _)`.

- [ ] **Step 3: Verify both services were changed in parity** — confirm the four shared patterns exist in both service files:

Run:
```bash
grep -c "aborted_by_operator\|get_relay_state() != 1\|_resume_interrupted_reconnect\|recover_orphan_temp_battery(relay_state)" fla-equalisation/fla_equalisation.py fla-charge/fla_charge.py
```
Expected: a non-zero count for each file (the abort flag, the relay-state finally guard, the resume method, and relay-aware orphan ordering all present in both).

- [ ] **Step 4: Final commit (if any test fixups were needed)**

```bash
git add -A
git commit -m "Finalise reconnect hold-and-lower: full suite green"
```
(Skip if Steps 1–3 required no changes.)

---

## Self-Review

**Spec coverage:**

| Spec section | Task(s) |
|---|---|
| §1 Hold at fixed float, re-pinned every cycle | Task 1 (re-pin each cycle; LFP read only for delta; None → hold) |
| §2 Fast 2 s poll | Task 1 (`POLL_INTERVAL = 2`) |
| §3 Abort routes through reconnect, relay-open only | Task 3 (EQ), Task 6 (charge); pre-relay/hard-error aborts unchanged |
| §4 Teardown guarded on confirmed-closed relay | Task 4 (EQ `finally`), Task 6 (charge `finally`) |
| §5 Fail-safe hold forever, never free-fall, no unsafe stop | Task 1 (safe-hold, never returns, never auto-close; timeout removed) |
| §6 Crash/reboot survival | Subprocess already survives parent crash (existing `temp_battery_process.py`); resume (Tasks 5/7) re-adopts; reboot default-closed relay is hardware, unchanged |
| §7 Resume in-progress hold; relay-aware orphan | Task 2 (attach + relay-aware orphan + `is_temp_battery_running`), Tasks 5/7 (resume dispatch) |
| §8 Apply identically to fla-charge | Tasks 6 + 7 mirror Tasks 3–5; Task 8 Step 3 checks parity |

**Constants:** `POLL_INTERVAL`, `SETTLE_TIMEOUT`, `MAX_NONE_CYCLES` added in `voltage_matching.py` (Task 1); `RELAY_CLOSE_DELTA_MAX` unchanged in `relay_control.py` and imported as the default for `voltage_delta_max`.

**Type consistency:** `wait_for_match(...) -> (bool, float|None)` signature is consistent across Task 1 (definition), Tasks 5/7 (resume callers, keyword args), and the unchanged in-body callers in `run_equalisation`/`run_charge` (which pass `voltage_delta_max=`, `float_voltage=`, `cache_callback=`, `timeout_hours=` as keywords — order change is safe). `recover_orphan_temp_battery(relay_state=None)` and `is_temp_battery_running()` names match between Task 2 (definition) and Tasks 5/7 (callers). `TempBatteryService.attach()`/`.deregister()` names match between Task 2 and the resume workers.

**No-placeholder scan:** every code step shows complete code; no "TBD"/"handle errors"/"similar to". The one deliberate test-only seam (`max_cycles`) and the accepted-and-ignored back-compat param (`timeout_hours`) are documented in the function docstring.

**Known semantic changes to existing tests (expected, not silent):**
- `test_voltage_matching.py` is fully rewritten (Task 1): the old timeout tests and the old "Trojan None → return False" test encode behaviour this design removes.
- Any pre-existing EQ/charge test that drove the run to a *relay-open* exit and asserted unconditional teardown (`release_lock`/`deregister`/`start_aggregate*`) must be updated to the new hold semantics (Tasks 4/6, Step 5/6 call this out explicitly).

**Out of scope (per spec):** no change to `RELAY_CLOSE_DELTA_MAX`, the open/EQ/charge phases, or the systemcalc-restart/divergence-verify logic; no VRM/remote paging (alerting stays local).

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-29-eq-reconnect-hold-and-lower.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
