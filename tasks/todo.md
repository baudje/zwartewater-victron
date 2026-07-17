# Task: harden DVCC BMS selection (aggregate/99) against silent fallback

## Background
Root cause of the 2026-05-28 half-charge-current event: DVCC's controlling BMS
(`/Settings/SystemSetup/BmsInstance`) drifted off the aggregate (instance 99,
CCL 120A) onto a single JK serialbattery pack (CCL 60A), silently halving LFP
charge for six weeks. Owner fixed by re-selecting the aggregate. Two guards so it
can't recur silently. See memory `project_dvcc_bms_fallback`.

## Acceptance criteria
- [x] `AGGREGATE_INSTANCE = 99` constant (single source of truth).
- [x] Fix #1 — idle guard: while NO FLA op holds the lock, if BmsInstance != 99,
      raise an alarm (buzzer + status). Idempotent per episode; re-arms after recovery.
      Wired into BOTH services' `_check` idle branch.
- [x] Fix #2 — teardown symmetric with hand-off: after the aggregate restart,
      wait for instance 99 to be visible BEFORE re-selecting it; then CONFIRM the
      BMS selection took; re-assert once and alarm if it did not.
- [x] All existing tests still pass; new tests added first (TDD), watched red. (339 total)
- [x] Mirror the service wiring into both fla-charge and fla-equalisation (repo rule).
- [ ] Deploy to Cerbo (scp + svc bounce) — only when no FLA op is mid-run.

## Results
Changed: fla-shared/takeover.py (AGGREGATE_INSTANCE, verify_idle_bms_selection,
teardown wait+confirm+re-assert+alarm); fla-charge/fla_charge.py & fla-equalisation/
fla_equalisation.py (import + idle-branch call). Tests: fla-shared/tests/test_bms_guard.py
(6), TestTeardownBmsConfirm in test_takeover.py (3), one _check integration test per
service (2). Verified: 218 shared + 69 EQ + 52 charge = 339 pass.

## Working notes
- Steady state on this boat is now BmsInstance PINNED to 99 (was -1 auto, the
  ambiguous state that let DVCC pick a pack). Guard enforces == 99.
- Detection is setting-based (get_bms_instance); the observed failure was the
  SETTING itself on a pack, so setting-based catch is correct.
- lock.is_locked() = "an FLA op is active" gate (either service).

---

# Pending real-world verification

These items can only be confirmed by observing the live system over a normal
operating cycle — they're listed here so they don't get lost after PR #7 merges.

## From PR #7 (SoC-reset + SmartShunt patches)

- [ ] **Next engine run**: confirm alternator charge is visible in aggregate
      `/Dc/0/Current` and in VRM (it should show as a positive current into
      the bank, matching SmartShunt LFP). Before the patch this was always
      ~0 because Orion DC-DC isn't summed by the upstream driver.
- [ ] **Next full charge cycle (with shore power)**: confirm the 30-min
      `SWITCH_TO_FLOAT_WAIT_FOR_SEC` hold actually runs at 28.4V CVL, and that
      both Boven and Onder reach SoC = 100% (the JK BMS internal SoC reset
      should trigger now that `SOC-100% Volt` is at 3.545V).
- [ ] **SmartShunt LFP (instance 277)**: confirm its own SoC syncs to 100%
      during the same cycle (charged-detection requires sustained 28.4V +
      tail current — should now have enough hold time).
- [ ] **Verify-patches boot hook**: after the next Cerbo reboot, confirm
      `/data/var/zwartewater-patches.status` is updated with a fresh
      timestamp and shows `OK:`.
