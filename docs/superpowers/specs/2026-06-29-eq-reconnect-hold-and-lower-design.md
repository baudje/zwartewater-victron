# Controlled hold-and-lower reconnect for FLA equalisation / charge

**Date:** 2026-06-29
**Status:** Approved design, pending implementation
**Applies to:** `fla-shared/voltage_matching.py`, `fla-equalisation/fla_equalisation.py`, `fla-charge/fla_charge.py`

## Problem

During an equalisation (or FLA charge) the LFP bank is isolated on the Orion (relay 2 open) and the Trojan/main bus is charged to a high voltage (~31V for EQ). When the operation **stops**, the bus must be brought back to within the LFP voltage and the relay closed to re-parallel the banks.

The current reconnect (`voltage_matching.wait_for_match`) sets the CVL to a fixed float (27V) and polls the Trojan↔LFP delta **every 30s**, closing the relay when delta ≤ 1V. Two failure modes were observed live on 2026-06-28/29 (Venus OS v3.80~33):

1. **Free-fall + missed window.** With the relay open the Trojan is the *sole source for the entire ship*, so when the CVL drops the Trojan voltage collapses quickly (31V → <25V). It sweeps through the ~26–28V band (delta ≤ 1V vs the LFP at ~27V) in seconds. A 30s poll sails past it. Once the Trojan is below `LFP − 1V` and still dropping, the delta only grows and the banks can never re-parallel without recharging the Trojan.

2. **Abort hard-stops and tears down the holder.** Operator Abort does not route through `wait_for_match` at all — it hard-stops, deregisters the temp battery, and restores DVCC. DVCC then reads the **isolated, full LFP** (27.3V ≥ 27.0V float) as "the battery", concludes it is full, and tells the Quattro to stop charging — so the main bus free-falls even though ample shore power is available. The relay is left open at a large delta with an alarm ("manual intervention required"), and the ship runs down the depleted Trojan. This is the cascade that occurred tonight.

## Root cause

During the relay-open phase the main bus must be **actively held** by the temp battery (which reads the Trojan/main-bus SmartShunt, instance 279). Whenever control is handed back to DVCC/aggregate while the LFP is still isolated, DVCC suppresses charging (full isolated LFP) and the bus free-falls. Reconnection is treated as "catch a fleeting window" when it should be "hold a stable condition".

## Constraints / facts

- EQ runs with **shore/Quattro power that is ample** — far more than the ship load (~40A observed). The main bus CAN be actively held near the LFP voltage during reconnect.
- The temp battery service (instance 100, `com.victronenergy.battery.fla_temp`) is the DVCC BMS during the handoff and exposes a settable CVL via `temp_service.set_charge_voltage()`.
- LFP (isolated on Orion) sits at ~27.0–27.3V and is stable during this phase.
- `RELAY_CLOSE_DELTA_MAX = 1.0V` is the safe-reconnect threshold and is unchanged.

## Goals

- Reconnection is a **stable** condition, not a race. The bus is held just above the LFP and closed cleanly.
- **Abort** ends in a safely reconnected state, never the high-delta stuck state.
- On any non-convergence (e.g. shore power lost mid-reconnect) the system **never free-falls** — it holds and alarms.

## Design

All behaviour lives in shared `voltage_matching.py`; the two service loops change only how they *enter* the reconnect.

Module constants `HOLD_MARGIN = 0.3` (V), `POLL_INTERVAL = 2` (s), `SETTLE_TIMEOUT = 120` (s), and `MAX_NONE_CYCLES = 10` live at the top of `voltage_matching.py`, alongside the existing `RELAY_CLOSE_DELTA_MAX` in `relay_control.py`.

### 1. Hold-and-lower in `wait_for_match`
Each poll cycle, set the temp-battery CVL to **`LFP_voltage + HOLD_MARGIN`**, re-evaluated live from the measured LFP voltage. With ample shore power the Quattro holds the bus at that target, so the Trojan settles ~0.3V above the LFP and *stays* there instead of sweeping past. Delta becomes small and stable. Replaces the fixed `float_voltage` CVL during matching.

**Unreadable LFP voltage.** If `get_lfp_voltage()` returns `None`, the `LFP + HOLD_MARGIN` target can't be computed. Hold the CVL at the **`float_voltage` fallback (~27V)** — never the EQ voltage — so the bus keeps heading toward a safe low value. Sustained `None` (≥ `MAX_NONE_CYCLES`, ~20s) is a fail-safe-hold trigger (§5): reconnection can't be managed safely without the LFP voltage, so hold at float and alarm rather than close.

### 2. Fast poll
Poll interval **30s → 2s** (`POLL_INTERVAL`). With the bus held this is belt-and-suspenders, and it makes overshoot detection responsive. The convergence condition is unchanged: close when `delta ≤ RELAY_CLOSE_DELTA_MAX`.

### 3. Abort routes through the controlled reconnect — only when the relay is open
An operator Abort (`check_abort()`) in a high-voltage loop **where the relay is open (LFP isolated)** — the `run_equalisation` equalising loop and the `run_charge` relay-open charging phases — **breaks out of the loop into the `wait_for_match` reconnect** instead of `return False` to the hard-stop path. The run is flagged as a non-completion: after a successful reconnect it returns failure and does **not** write the `last_equalisation` timestamp, but the system is left reconnected.

An abort **before the relay opens** (handoff setup, or any relay-closed phase — nothing is isolated yet) is a plain safe stop on the existing path; there is nothing to reconnect. Likewise the hard *error* aborts — Orion failure, relay-closed-externally, temp-battery-unresponsive — keep their existing immediate behaviour; those are genuine emergencies, not "operator wants to stop". This phase-scoping is the **only** way the two services differ here (fla-charge has more relay-open phases than fla-equalisation); the reconnect path itself is identical shared code.

### 4. Teardown is guarded on a confirmed-closed relay
The teardown sequence — deregister the temp battery, restore `BatteryService`/`BmsInstance`, restore DVCC `MaxChargeVoltage`, restart the aggregate driver — runs **only after the relay is confirmed closed** (`get_relay_state() == 1`). This must include the `finally`/cleanup block, which today deregisters the temp battery and restores DVCC *unconditionally* — exactly what lets the bus free-fall while the relay is still open. Restructure the `finally` to branch on relay state:

- **Relay closed** → normal teardown (deregister temp, restore DVCC, restart aggregate, release lock).
- **Relay open** → do **not** deregister the temp battery or restore DVCC (handing back to DVCC is what suppresses charging of the isolated full LFP and free-falls the bus). Leave the temp battery registered and holding, raise a critical "reconnect incomplete — bus held, manual intervention required" alarm, and keep the operation lock held so the other service and `startup_safety_check` stay clear.

In normal operation the §5 safe-hold means the worker never leaves `wait_for_match`, so the `finally` is not reached at all; this relay-state guard is the belt-and-suspenders for any unexpected exit (exception, etc.).

### 5. Fail-safe: hold forever, never free-fall, no unsafe stop
If the bus cannot settle within `RELAY_CLOSE_DELTA_MAX` — debounced over `SETTLE_TIMEOUT` (~120s) so a transient undershoot during the initial 31V→27V settle does not false-trigger — `wait_for_match` enters a **safe-hold**: it keeps looping with the CVL pinned to `LFP_voltage + HOLD_MARGIN` (or the float fallback if LFP is `None`), the relay open, a periodic alarm raised, and the operation lock still held. It **does not return** while in safe-hold, so the caller never reaches teardown and the temp battery keeps holding the bus.

The **only** exits from safe-hold are:
1. **Convergence** — the bus comes back within `RELAY_CLOSE_DELTA_MAX` (e.g. shore power restored) → close the relay → normal teardown.
2. **Manual operator intervention at the hardware/GUI** — the operator restores charge power, or deliberately closes relay 2 from the Cerbo GUI accepting the inrush. An explicit, out-of-band action, not the web Abort.

There is deliberately **no software "force stop" that tears down**, because any teardown with the relay open *is* the free-fall cascade. A repeated web Abort while reconnecting is logged and acknowledged but does **not** abort the reconnect — the system is already driving toward the only safe end-state (reconnected, or safely held). The previous 4h convergence timeout is removed in favour of this indefinite safe-hold, so the cascade is structurally impossible.

### 6. Apply identically to fla-charge
fla-charge shares the temp-battery handoff and the entire reconnect path; the loop-break-to-reconnect (for its relay-open phases per §3), the relay-state-guarded teardown (§4), and the safe-hold (§5) are mirrored there, per the repo convention to apply shared-pattern changes to both services.

## Affected files

- `fla-shared/voltage_matching.py` — hold-at-LFP+margin, 2s poll, fail-safe hold.
- `fla-equalisation/fla_equalisation.py` — abort breaks to reconnect; teardown after close.
- `fla-charge/fla_charge.py` — same.
- `fla-shared/tests/test_voltage_matching.py` — new/updated tests.
- `fla-equalisation/tests/`, `fla-charge/tests/` — abort-routing tests.

All tests mock D-Bus and `time` (the helpers already do this); the safe-hold loop is made testable by injecting/patching the convergence check so the loop terminates deterministically (e.g. delta returns sub-threshold after N mocked cycles, or the loop exits on a patched sentinel). The indefinite loop is bounded in tests by a max-iteration guard on the mocked monitor.

- `wait_for_match`: sets temp CVL to `LFP + HOLD_MARGIN` each cycle; closes the relay when `delta ≤ RELAY_CLOSE_DELTA_MAX`; does NOT close while delta > threshold; a brief sub-`SETTLE_TIMEOUT` undershoot does NOT trigger the fail-safe (debounce); sustained non-convergence enters safe-hold (CVL pinned, relay open, alarm raised) and does NOT return into teardown.
- `None` LFP voltage: CVL falls back to `float_voltage` (not EQ voltage); sustained `None` triggers safe-hold + alarm.
- Teardown guard: with the relay open, the cleanup/`finally` does NOT deregister the temp battery or restore DVCC (alarms instead); with the relay closed it tears down normally. (Test in both services.)
- Service loops: operator Abort during a **relay-open** high-voltage phase enters the reconnect path (not the hard-stop) and does not write `last_equalisation`; abort during a **relay-closed** phase / hard error aborts still take the immediate path.
- Full existing suite (196 tests) stays green.

## Out of scope

- No change to `RELAY_CLOSE_DELTA_MAX` (stays 1.0V) or to the open/EQ/charge phases.
- No change to the systemcalc-restart / relay-open / divergence-verify logic (already fixed in PRs #10–12).
- Solar-only / no-shore EQ is not specially handled beyond the fail-safe hold; EQ is run on shore per the confirmed constraint.

## Operational note

Never restart one FLA service while the other is mid-operation (the lock-aware `startup_safety_check` from PR #12 guards the relay, but a restart still disrupts the run).
