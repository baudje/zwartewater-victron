# FLA Charge Service — Design Spec

## Context

When Zwartewater runs on battery without shore power, both the LFP and FLA banks discharge. When shore power reconnects, the LFPs absorb ~96% of the charge current due to lower internal resistance. The Trojans barely charge — they never reach the 29.64V bulk voltage needed for a full charge cycle because the aggregate battery CVL caps at 28.4-28.8V (LFP limits).

This service detects undercharged Trojans and ensures they get a complete bulk + absorption charge cycle by temporarily disconnecting the LFPs once they're sufficiently charged.

**Hardware context:** Same Zwartewater system — see `docs/design.md` for full topology.

## Approach: Shared Library + Separate Service

Extract common safety-critical code from the FLA equalisation service into shared modules. The FLA charge service is a second consumer of the same infrastructure.

### Shared Modules (`fla-shared/`)

| Module | Extracted from | Responsibility |
|---|---|---|
| `relay_control.py` | `fla_equalisation.py` finally block + startup check | Open/close relay with verification, delta-aware safety, startup recovery |
| `temp_battery.py` | `dbus_battery_service.py` | Crash-safe CVL sequencing (register at safe voltage, raise only after relay verified) |
| `voltage_matching.py` | `fla_equalisation.py` voltage matching loop | Wait for delta < threshold, with timeout and SmartShunt watchdog |
| `lock.py` | New | File-based lock preventing simultaneous operations |
| `dbus_monitor.py` | Existing, moved | SmartShunt readings, relay state, SoC, cell voltages |
| `alerting.py` | Existing, moved | Buzzer, D-Bus alarm, logging |

### Lock Mechanism

- File lock at `/data/apps/fla-shared/operation.lock`
- Contains: service name, start timestamp, current phase
- Both services check lock before starting any operation
- Released in `finally` block
- Both services check lock before starting; whoever acquires first runs

---

## Trigger Conditions

All must be true:
1. **Enabled** (setting)
2. **No operation lock held** (equalisation not running)
3. **AC input available** (AC-in-1 shore power OR AC-in-2 generator active)
4. **Trojan SoC < 85%** (configurable, from SmartShunt Trojan 279)

Checked every 60 seconds. No calendar-based skip — if the Trojans discharge shortly after an equalisation, they still need charging. The SoC trigger is the ground truth.

---

## Charging Sequence

### Phase 1: Shared Charging (relay closed, both banks on bus)

Aggregate driver running normally. Both banks charge at aggregate CVL (28.4V or 28.8V). LFPs absorb most current due to lower internal resistance, but FLAs still get some charge.

**Monitor every 30 seconds. Transition to Phase 2 when ANY of:**
- LFP SoC >= 95% (configurable)
- Aggregate charge current < 20A (configurable)
- Any LFP cell voltage >= 3.50V (configurable)

**Phase 1 timeout:** 8 hours. If no transition trigger fires, abort — something is wrong (AC lost, loads too high, etc.)

**AC loss during Phase 1:** Abort, no cleanup needed (nothing changed).

### Phase 2: FLA-Only Bulk (relay open, LFPs on Orion)

Crash-safe CVL sequencing:
1. Register temp battery service at current bus voltage (safe)
2. Stop aggregate driver → DVCC switches to temp service (safe voltage)
3. Open relay 2, verify open (LFP current < 5A)
4. Record LFP voltage at disconnect (Orion failure detection)
5. **Only now** raise CVL to 29.64V (Trojan bulk voltage)

Quattro pushes full charge current into Trojans at 29.64V. Orion keeps LFPs topped up independently.

**AC loss during Phase 2:** Detect via Quattro state or Trojan current drop. Proceed directly to Phase 4 (voltage matching + reconnect).

### Phase 3: FLA Absorption

Hold 29.64V until:
- Trojan current < 10A (configurable, ~2% of C20 for 435Ah)
- **OR** max absorption time exceeded (configurable, default 4 hours)

**Monitoring during absorption:**
- SmartShunt Trojan responsive (abort if V_trojan = None)
- LFP voltage stable (Orion failure: drop > 0.5V → abort)
- Trojan current > 60A warning (dynamo/MPPT contribution)
- i_trojan = None for 5 min → proceed to Phase 4

### Phase 4: Reconnect

1. Reduce CVL to 27.0V (Trojan float)
2. Wait for |V_trojan - V_lfp| < 1V (configurable, 4hr timeout)
3. Close relay 2, verify closed via read-back
4. Measure inrush current + reconnect delta
5. Deregister temp battery service
6. Restart aggregate driver
7. Invalidate service cache
8. Release lock, record timestamp

**Voltage match timeout:** Alarm, stay disconnected, manual intervention (same as equalisation).

---

## Trojan L16H-AC Charge Specs

| Phase | Per cell | 24V bank (12 cells) |
|---|---|---|
| Bulk | 2.47V | 29.64V |
| Float | 2.25V | 27.00V |
| Equalise | 2.70V | 32.40V |
| Absorption complete | Current < 2-3% C20 | < 9-13A |

Temperature compensation: +0.005V/cell per 1°C below 25°C, -0.005V/cell per 1°C above 25°C.

---

## Safety Guards

All safety guards from FLA equalisation apply (crash-safe CVL, delta-aware relay, startup recovery, relay verification, Orion failure detection, SmartShunt watchdog, inrush monitoring, bounds validation).

Additional guards for the charge service:

| Guard | Trigger | Action |
|---|---|---|
| AC loss during Phase 2-3 | Quattro not charging or Trojan current drops to ~0A | Skip to Phase 4 (voltage match + reconnect) |
| Phase 1 timeout | 8 hours without transition trigger | Abort, no cleanup needed |
| Lock held | Other service operating | Wait for next check cycle |

---

## Settings (Venus OS D-Bus, web UI at port 8089)

| Setting | Default | Description |
|---|---|---|
| `/Settings/FlaCharge/Enabled` | 1 | Enable/disable |
| `/Settings/FlaCharge/TrojanSocTrigger` | 85 | Start charge when Trojan SoC below this (%) |
| `/Settings/FlaCharge/LfpSocTransition` | 95 | Disconnect LFPs when SoC above this (%) |
| `/Settings/FlaCharge/LfpCellVoltageDisconnect` | 3.50 | Disconnect when any LFP cell above this (V) |
| `/Settings/FlaCharge/CurrentTaperThreshold` | 20 | Disconnect when charge current below this (A) |
| `/Settings/FlaCharge/FlaBulkVoltage` | 29.64 | Trojan bulk/absorption voltage (V) |
| `/Settings/FlaCharge/FlaAbsorptionCompleteCurrent` | 10 | Absorption done when Trojan I below this (A) |
| `/Settings/FlaCharge/FlaAbsorptionMaxHours` | 4 | Maximum absorption duration (hours) |
| `/Settings/FlaCharge/FlaFloatVoltage` | 27.0 | Post-absorption float voltage (V) |
| `/Settings/FlaCharge/VoltageDeltaMax` | 1.0 | Max delta for reconnect (V) |
| `/Settings/FlaCharge/VoltageMatchTimeoutHours` | 4 | Max wait for convergence (hours) |
| `/Settings/FlaCharge/Phase1TimeoutHours` | 8 | Max shared charging duration (hours) |

---

## D-Bus Status Service

`com.victronenergy.fla_charge` (device instance 201):

| Path | Description |
|---|---|
| `/State` | Idle / Phase 1 Shared Charging / Phase 2 FLA Bulk / Phase 3 Absorption / Phase 4 Voltage Matching / Reconnecting / Restarting / Error |
| `/TimeRemaining` | Estimated seconds remaining in current phase |
| `/TrojanVoltage` | Live Trojan bank voltage |
| `/TrojanCurrent` | Live Trojan charge current |
| `/TrojanSoc` | Trojan SoC from SmartShunt |
| `/LfpVoltage` | Live LFP bank voltage |
| `/LfpSoc` | LFP SoC from aggregate/serialbattery |
| `/VoltageDelta` | Difference between banks |
| `/InrushCurrent` | Last recorded inrush on reconnect |
| `/ReconnectDelta` | Voltage delta at last reconnect |
| `/Alarms/Charge` | 0=OK, 1=Warning, 2=Alarm |

---

## Web UI (port 8089)

Same pattern as equalisation dashboard:
- **Status card**: Phase, time remaining, last charge, next due
- **Voltages card**: Trojan V/I/SoC, LFP V/SoC, delta, cell voltages
- **Settings card**: All settings editable inline
- **Control card**: "Run Charge Now" button (still enforces AC + lock)
- **Links**: Link to equalisation UI (port 8088) and vice versa
- Auto-refresh every 5 seconds

---

## File Layout on Cerbo

```
/data/apps/fla-shared/
    relay_control.py
    temp_battery.py
    voltage_matching.py
    lock.py
    dbus_monitor.py
    alerting.py
    operation.lock          (runtime, not deployed)
    ext/velib_python/       (symlink)

/data/apps/fla-equalisation/
    fla_equalisation.py     (refactored to use fla-shared)
    dbus_status_service.py
    settings.py
    web_server.py
    service/run

/data/apps/fla-charge/
    fla_charge.py           (new service)
    dbus_status_service.py  (own status paths)
    settings.py             (own settings)
    web_server.py           (port 8089)
    service/run
```

---

## Interaction with FLA Equalisation

| Scenario | Behaviour |
|---|---|
| Both want to run | Lock prevents simultaneous execution. First wins, other waits. |
| Equalisation just ran | If Trojans are above 85% SoC, charge service won't trigger (SoC is the ground truth) |
| Charge ran recently but equalisation is due | Equalisation runs normally — higher voltage serves a different purpose |
| Crash during either service | Startup safety check (shared relay_control) recovers relay state |

---

## Observed Charging Profile (from live data)

At 81.7% LFP SoC with shore power connected:
- Total charge current: 100.6A
- LFP absorbs: 96.6A (96%)
- Trojan absorbs: 0.5A (0.5%)
- LFP cells at: 3.37-3.38V

This confirms: without intervention, the Trojans receive negligible current. The LFPs must reach near-full before any meaningful FLA charging occurs. The shared charging Phase 1 allows both to benefit from available current, then Phase 2 dedicates the full charge capacity to the Trojans.
