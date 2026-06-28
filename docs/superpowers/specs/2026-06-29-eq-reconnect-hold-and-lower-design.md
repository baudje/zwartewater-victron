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

### 1. Hold-and-lower in `wait_for_match`
Each poll cycle, set the temp-battery CVL to **`LFP_voltage + HOLD_MARGIN`** (HOLD_MARGIN = 0.3V), re-evaluated live from the measured LFP voltage. With ample shore power the Quattro holds the bus at that target, so the Trojan settles ~0.3V above the LFP and *stays* there instead of sweeping past. Delta becomes small and stable. Replaces the fixed `float_voltage` CVL during matching. (Float is still applied at the very end / on teardown via the existing normal path.)

### 2. Fast poll
Poll interval **30s → 2s** (`POLL_INTERVAL`). With the bus held this is belt-and-suspenders, and it makes overshoot detection responsive. The convergence condition is unchanged: close when `delta ≤ RELAY_CLOSE_DELTA_MAX`.

### 3. Abort routes through the controlled reconnect
In both service high-voltage loops (`run_equalisation` equalising loop, `run_charge` charging loop), an operator Abort (`check_abort()`) **breaks out of the loop into the existing `wait_for_match` reconnect** instead of `return False` to the hard-stop path. The run is flagged as a non-completion: after a successful reconnect it returns failure and does **not** write the `last_equalisation` timestamp, but the system is left reconnected. (Hard error aborts — Orion failure, relay-closed-externally, temp-battery-unresponsive — keep their existing immediate-abort behaviour; those are genuine emergencies, not "operator wants to stop".)

### 4. Tear down only after the relay is closed
The temp battery is deregistered and DVCC/aggregate restored **only after** `close_relay_verified` succeeds. The teardown sequence must never run while the relay is open and the LFP isolated.

### 5. Fail-safe: hold, never free-fall
If the bus cannot settle within `RELAY_CLOSE_DELTA_MAX` (shore lost, load spike, Quattro fault) or overshoots below `LFP − RELAY_CLOSE_DELTA_MAX`, `wait_for_match` must **keep the temp battery holding a safe bus voltage and the relay open, and raise an alarm for manual intervention** — it must not return into a teardown that deregisters the temp battery and lets the bus free-fall. Concretely: keep holding/looping with the CVL pinned to `LFP_voltage + HOLD_MARGIN`, emit a periodic alarm, and only exit to teardown once either convergence is reached (then close) or the operator forces a stop. The existing 4h convergence timeout is replaced by this hold-and-alarm behaviour so the cascade is structurally impossible.

### 6. Apply identically to fla-charge
fla-charge shares the temp-battery handoff and reconnect; the loop-break-to-reconnect and teardown-after-close changes are mirrored there (per the repo convention to apply shared-pattern changes to both services).

## Affected files

- `fla-shared/voltage_matching.py` — hold-at-LFP+margin, 2s poll, fail-safe hold.
- `fla-equalisation/fla_equalisation.py` — abort breaks to reconnect; teardown after close.
- `fla-charge/fla_charge.py` — same.
- `fla-shared/tests/test_voltage_matching.py` — new/updated tests.
- `fla-equalisation/tests/`, `fla-charge/tests/` — abort-routing tests.

## Testing

- `wait_for_match`: sets temp CVL to `LFP + HOLD_MARGIN` each cycle; closes the relay when delta ≤ 1V; does NOT close while delta > 1V; on sustained non-convergence holds (keeps temp CVL pinned, relay open, raises alarm) rather than tearing down; mock time/sleep for determinism.
- Service loops: operator Abort during the high-voltage phase enters the reconnect path (not the hard-stop) and does not write `last_equalisation`; hard error aborts still take the immediate path.
- Full existing suite (196 tests) stays green.

## Out of scope

- No change to `RELAY_CLOSE_DELTA_MAX` (stays 1.0V) or to the open/EQ/charge phases.
- No change to the systemcalc-restart / relay-open / divergence-verify logic (already fixed in PRs #10–12).
- Solar-only / no-shore EQ is not specially handled beyond the fail-safe hold; EQ is run on shore per the confirmed constraint.

## Operational note

Never restart one FLA service while the other is mid-operation (the lock-aware `startup_safety_check` from PR #12 guards the relay, but a restart still disrupts the run).
