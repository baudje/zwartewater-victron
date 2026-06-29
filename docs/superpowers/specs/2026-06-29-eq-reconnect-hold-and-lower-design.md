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

Module constants `POLL_INTERVAL = 2` (s), `SETTLE_TIMEOUT = 120` (s), and `MAX_NONE_CYCLES = 10` (≈20s) live at the top of `voltage_matching.py`, alongside the existing `RELAY_CLOSE_DELTA_MAX = 1.0` (V) in `relay_control.py`.

**Firm invariant (decided in design review): the software never auto-closes the relay while `delta > RELAY_CLOSE_DELTA_MAX`.** There is no "last resort" high-delta close. If the bus can't be brought within threshold, the system holds and alarms and waits for the operator — full stop.

### 1. Hold the bus at fixed float, re-pinned every cycle
The original `wait_for_match` set the CVL to `float_voltage` (~27V, at/just below the LFP) **once** at entry and then only polled. That was never the bug — the completion path holds fine; the cascade came from the abort path tearing the temp battery down so nothing held the bus. Two refinements:

- **Re-pin float every poll cycle** (`temp_service.set_charge_voltage(float_voltage)` each loop), not just once, so the hold survives any drift and the resume path (§7) re-asserts it cleanly.
- Keep the target at **fixed float**, *not* `LFP + margin`. Float (~27V) is already ≈ the LFP, so the delta is small and stable, and holding at/just-below the LFP means the full LFP gently **supplies** the bus on close (the benign inrush direction — no nudging a full cell toward OVP). This also keeps the hold target independent of the live LFP reading.

The LFP voltage is still read each cycle, but only to compute the **delta** for the close decision. If `get_lfp_voltage()` returns `None`, the delta is unknown so we cannot decide to close; the hold continues at float, and sustained `None` (≥ `MAX_NONE_CYCLES`, ≈20s) trips the safe-hold (§5) with an alarm — we never close blind.

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
If the bus cannot settle within `RELAY_CLOSE_DELTA_MAX` — debounced over `SETTLE_TIMEOUT` (~120s) so a transient undershoot during the initial 31V→27V settle does not false-trigger — `wait_for_match` enters a **safe-hold**: it keeps looping with the CVL pinned to `float_voltage`, the relay open, the buzzer/alarm re-asserted, and the operation lock still held. It **does not return** while in safe-hold, so the caller never reaches teardown and the temp battery keeps holding the bus.

The realistic cause of non-convergence is **shore power lost mid-reconnect**; while it's lost the bus drains regardless, but safe-hold keeps the state *recoverable* (no DVCC teardown) and pages the operator. The **only** exits are:
1. **Convergence** — the bus comes back within `RELAY_CLOSE_DELTA_MAX` (e.g. shore restored) → close → normal teardown.
2. **Manual operator action** — the operator restores charge power (→ convergence), or closes relay 2 from the Cerbo GUI; the latter collapses the delta to ~0, which the loop sees as convergence and finishes cleanly. An explicit, out-of-band action, not the web Abort.

There is deliberately **no software "force stop" that tears down** — any teardown with the relay open *is* the free-fall cascade. A repeated web Abort while reconnecting is logged but does **not** abort the reconnect. The previous 4h convergence timeout is removed in favour of this indefinite safe-hold.

**Alerting** stays local (buzzer + the existing D-Bus alarm) — the operator lives aboard, so no VRM/remote paging is wired. The buzzer is simply re-asserted while held.

### 6. Crash / reboot survival of the hold
- **Parent-process crash** (daemontools restarts the fla service): the temp-battery **subprocess survives** — it independently re-reads `/tmp/fla_temp_cvl` and keeps publishing float, so the bus stays held with no gap. On restart the service **resumes** the reconnect (§7).
- **Full Cerbo reboot**: the subprocess dies with everything else, but relay 2's power-on default is **closed** (`/Settings/Relay/1/InitialState = 1`). During a safe-hold the bus is at float ≈ the LFP, so the boot-time relay close re-parallels at a tiny delta — benign, and the ship gets both banks. (Accepted residual: a reboot during the *active* 31V equalising phase boot-closes at a high delta → inrush, with the JK BMS protecting the LFP. Software cannot intercept a boot-time relay close, and defaulting the relay open would isolate the LFP on every normal reboot, which is worse. So default-closed stands.)

### 7. Resume an in-progress hold on startup; relay-aware orphan handling
On startup, if **relay 2 is open AND a temp-battery subprocess is running**, an operation was interrupted mid-reconnect. The service **adopts** it rather than killing it:

- It attempts to acquire the operation lock (atomic `O_EXCL`). **Whichever service wins** adopts the running subprocess via a new "attach to existing" mode on `TempBatteryService` (manage CVL through `/tmp/fla_temp_cvl`, stop via `pkill` at teardown — no `Popen`, no respawn, no hold gap) and runs the **shared reconnect** (§1/§5) to completion. The reconnect is identical for both services, so it does not matter which one originally started the operation. The other service backs off.
- **`recover_orphan_temp_battery()` becomes relay-aware** (extends the PR #10 function): it only SIGKILLs a stray temp battery when **relay 2 is closed** (a true leftover). When the relay is **open**, the temp battery is a hold-in-progress — it must be adopted/resumed, never killed (killing it is the free-fall). `startup_safety_check` likewise defers to the resume path when the relay is open.

### 8. Apply identically to fla-charge
fla-charge shares the temp-battery handoff and the entire reconnect path. The loop-break-to-reconnect is added to its **relay-open** bulk/absorption phases (mirroring its existing `"AC input lost ... proceeding to voltage matching"` break — note these phases currently have **no operator-abort path**, so this adds one); its Phase-1 (relay-closed) abort stays a plain stop. The relay-state-guarded teardown (§4), safe-hold (§5), crash/reboot survival (§6), and resume (§7) are mirrored, per the repo convention to apply shared-pattern changes to both services.

## Affected files

- `fla-shared/voltage_matching.py` — re-pin float each cycle, 2s poll, safe-hold (never returns into teardown), `SETTLE_TIMEOUT` debounce, `MAX_NONE_CYCLES` blind-close guard.
- `fla-shared/temp_battery.py` — "attach to existing" mode on `TempBatteryService`; make `recover_orphan_temp_battery()` relay-aware (only kill when relay closed).
- `fla-equalisation/fla_equalisation.py` — abort (relay-open) breaks to reconnect; relay-state-guarded `finally`; resume-on-startup.
- `fla-charge/fla_charge.py` — same, plus add the missing abort path to the relay-open bulk/absorption phases.
- `fla-shared/tests/test_voltage_matching.py`, `test_temp_battery_orphan.py`, `test_relay_control.py` — new/updated tests.
- `fla-equalisation/tests/`, `fla-charge/tests/` — abort-routing + resume tests.

## Testing

All tests mock D-Bus and `time` (the helpers already do this). The safe-hold loop is made testable with a max-iteration guard on the mocked monitor / a convergence sentinel so it terminates deterministically.

- `wait_for_match`: re-pins `float_voltage` each cycle; closes when `delta ≤ RELAY_CLOSE_DELTA_MAX`; never closes while delta > threshold; a brief sub-`SETTLE_TIMEOUT` undershoot does NOT trip the fail-safe; sustained non-convergence enters safe-hold (float pinned, relay open, alarm) and does NOT return into teardown.
- `None` LFP voltage: never closes blind; sustained `None` (≥ `MAX_NONE_CYCLES`) trips safe-hold + alarm.
- Teardown guard: relay open → cleanup/`finally` does NOT deregister temp battery or restore DVCC (alarms instead); relay closed → normal teardown. (Both services.)
- Relay-aware orphan: `recover_orphan_temp_battery()` kills only when relay closed; with relay open it does not kill.
- Resume: startup with relay open + temp battery running → service acquires lock, attaches to the existing subprocess (no respawn), runs the reconnect; the other service backs off.
- Service loops: operator Abort in a relay-open phase enters the reconnect path and does not write `last_equalisation`; abort in a relay-closed phase / hard error aborts take the immediate path.
- Full existing suite (196 tests) stays green.

## Out of scope

- No change to `RELAY_CLOSE_DELTA_MAX` (stays 1.0V) or to the open/EQ/charge phases.
- No change to the systemcalc-restart / relay-open / divergence-verify logic (already fixed in PRs #10–12).
- Solar-only / no-shore EQ is not specially handled beyond the fail-safe hold; EQ is run on shore per the confirmed constraint.

## Operational note

Never restart one FLA service while the other is mid-operation (the lock-aware `startup_safety_check` from PR #12 guards the relay, but a restart still disrupts the run).
