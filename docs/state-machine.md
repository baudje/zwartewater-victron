# Integrated State Machine — Zwartewater Energy System

## Overview

Five software components interact to manage the 24V DC system on Zwartewater. Each runs as an independent service on Venus OS (Cerbo GX), communicating via D-Bus. This document describes how their states interlock.

```
                          DVCC
                           |
                    reads CVL/CCL/DCL
                           |
              +------------+------------+
              |                         |
    [Aggregate Battery]         [FLA Equaliser]
      (device inst. 99)        (temp svc inst. 100)
              |                         |
       reads from                 reads from
              |                         |
    +----+----+----+              +-----+-----+
    |    |         |              |           |
  [SB1] [SB2]  [SmartShunts]  [SmartShunt] [SmartShunt]
                                  Trojan      LFP
```

## Component States

### 1. Quattro II (com.victronenergy.vebus.ttyS4)

The Quattro is fully controlled by DVCC/ESS — it has no independent charge profile.

| State | Value | Meaning |
|-------|-------|---------|
| Off | 0 | Quattro off |
| Low Power | 1 | Standby/search mode |
| Fault | 2 | Error condition |
| Bulk | 3 | Charging at max current until CVL |
| Absorption | 4 | Holding at CVL, current tapering |
| Float | 5 | Holding at float voltage |
| Storage | 6 | Long-term storage voltage |
| Equalise | 7 | Equalisation charge (not used — DVCC controls) |
| Inverting | 9 | Discharging batteries to power loads |
| Passthru | 10 | AC pass-through, not charging |

**Who controls it:** DVCC reads CVL from the active battery service (`com.victronenergy.battery/99` = aggregate driver) and commands the Quattro. The Quattro never decides its own charge voltage.

**During FLA equalisation:** DVCC reads CVL from the temporary battery service (`com.victronenergy.battery.fla_equalisation`, device instance 100). The CVL starts at 28.4V (safe) and is only raised to 31.5V after the relay is confirmed open. CCL is set to 60A to protect the Trojan FLA bank.

**During FLA charge (absorption):** Same mechanism — temp service at 29.64V (Trojan L16H-AC bulk/absorption voltage per datasheet: 2.47V/cell × 12 = 29.64V), CCL 60A.

### 2. Cerbo GX / DVCC (com.victronenergy.system)

DVCC (Distributed Voltage and Current Control) is the central coordinator.

| Setting | Path | Current Value | Effect |
|---------|------|---------------|--------|
| MaxChargeVoltage | `/Settings/SystemSetup/MaxChargeVoltage` | From active battery service | Caps Quattro charge voltage |
| MaxChargeCurrent | `/Settings/SystemSetup/MaxChargeCurrent` | 120A | Caps total charge current |
| Active battery | `/ActiveBatteryService` | `com.victronenergy.battery/99` | Which service DVCC listens to |
| BatteryLife | `/Settings/CGwacs/BatteryLife/State` | 10 (active) | ESS battery management |

**Relay 2** (`/Relay/1/State`):
- `1` = closed (normal) — LFP direct path active, Orion off
- `0` = open (equalisation) — LFP direct path broken, Orion activates

**State transitions (crash-safe sequencing):**
```
Normal:       ActiveBattery = aggregate/99, Relay2 = closed, CVL = 28.4V
    |
    v (FLA equaliser registers temp service at 28.4V — coexists with aggregate)
Preparing:    ActiveBattery = aggregate/99 (still wins, lower instance), Relay2 = closed
    |
    v (FLA equaliser stops aggregate driver)
Transitioning: ActiveBattery = temp/100 at 28.4V (safe), Relay2 = closed
    |          *** If crash here: 28.4V is safe for both banks ***
    v (FLA equaliser opens relay 2, verifies open)
Disconnected: ActiveBattery = temp/100 at 28.4V, Relay2 = open, Orion active
    |          *** If crash here: LFPs on Orion (safe), Trojans at 28.4V (safe) ***
    v (FLA equaliser raises CVL to 31.5V — ONLY after relay confirmed open)
EQ mode:      ActiveBattery = temp/100 at 31.5V, Relay2 = open, Orion active
    |          *** If crash here: temp svc dies, Quattro stops charging, LFPs on Orion (safe) ***
    v (equalisation complete, CVL reduced to 27.0V float)
Cooling:      ActiveBattery = temp/100 at 27.0V, Relay2 = open
    |
    v (voltage delta < 1V, relay closes, verified via read-back)
Reconnected:  ActiveBattery = temp/100 at 27.0V, Relay2 = closed, inrush measured
    |
    v (temp service deregistered, aggregate driver restarted)
Normal:       ActiveBattery = aggregate/99, Relay2 = closed, CVL = 28.4V
```

### 3. dbus-serialbattery (2 instances)

Each instance monitors one JK BMS via CAN+USB and publishes battery data on D-Bus.

| Instance | Service | Address | Name |
|----------|---------|---------|------|
| 3 | com.victronenergy.battery.ttyUSB0__0x01 | 0x01 | SerialBattery(Boven) |
| 4 | com.victronenergy.battery.ttyUSB0__0x02 | 0x02 | SerialBattery(Onder) |

**States (charge mode):**

```
Bulk ──> Absorption ──> Float
 ^                        |
 |                        v
 +────── Rebulk <────────+
```

- **Bulk**: Charging at max current. Transitions to Absorption when any cell reaches `MAX_CELL_VOLTAGE` (3.65V)
- **Absorption**: Holding voltage, current tapering per CVCM curve. Transitions to Float after `SWITCH_TO_FLOAT_WAIT_FOR_SEC` (300s)
- **Float**: Holding at `FLOAT_CELL_VOLTAGE` (3.375V/cell = 27.0V). Rebulks if voltage drops
- **CVCM active**: Current fraction tapers: 100% at 3.30V → 20% at 3.60V → 0% at 3.65V

**Temperature derating (DCCM_T):**

| Temp | Charge fraction | Effective max (per battery) |
|------|----------------|---------------------------|
| 0°C | 0% | 0A |
| 2°C | 10% | 6A |
| 5°C | 20% | 12A |
| 10°C | 80% | 48A |
| 15°C+ | 100% | 60A |
| 40°C | 40% | 24A |
| 55°C | 0% | 0A |

**Key interaction with aggregate driver:** serialbattery publishes its own CVL/CCL/DCL, but DVCC ignores these because it only reads from the aggregate battery service (device instance 99). serialbattery's CVCM still controls the JK BMS MOSFETs at the hardware level — if a cell exceeds 3.65V, the BMS will disconnect regardless of what DVCC commands.

**During FLA equalisation:** serialbattery continues running (BMS communication stays active via CAN+USB). The JK BMS MOSFETs are not affected by the equalisation — the Orion DC-DC charges the LFPs independently at a safe voltage.

### 4. dbus-aggregate-batteries (device instance 99)

Aggregates the two serialbattery instances into one virtual battery for DVCC.

**States:**

```
Startup ──> Searching ──> Running ──> (stopped by FLA equaliser)
                                         |
                                         v
                                    Stopped (svc -d)
                                         |
                                         v
                                    Restarted (svc -u) ──> Searching ──> Running
```

**Charge parameter state machine (OWN_CHARGE_PARAMETERS = True):**

```
Normal charge (3.55V/cell = 28.4V)
    |
    | (every 14 days, BALANCING_REPETITION)
    v
Balancing charge (3.60V/cell = 28.8V)
    |
    | (cell diff <= 0.005V AND next charge cycle begins)
    v
Normal charge (3.55V/cell = 28.4V)
```

**Dynamic CVL reduction:**
```
Normal CVL (28.4V or 28.8V)
    |
    | (any cell >= MAX_CELL_VOLTAGE = 3.65V)
    v
Reduced CVL (dynamically lowered to prevent overvoltage)
    |
    | (all cells < MAX_CELL_VOLTAGE)
    v
Normal CVL restored
```

**Current limiting state:**

```
                  Cell voltage
    2.80V    2.90V    3.30V    3.50V    3.60V    3.65V
      |--------|--------|--------|--------|--------|
CCL:  20%     100%     100%      80%      10%       0%
DCL:   0%       5%     100%
```

**Key published paths (read by DVCC):**

| Path | Normal | Balancing |
|------|--------|-----------|
| `/Info/MaxChargeVoltage` | 28.4V | 28.8V |
| `/Info/MaxChargeCurrent` | 120A | 120A |
| `/Info/MaxDischargeCurrent` | 150A | 150A |
| `/Dc/0/Voltage` | live | live |
| `/Dc/0/Current` | from SmartShunt LFP (277) | from SmartShunt LFP (277) |
| `/Soc` | own coulomb counter | own coulomb counter |

### 5. FLA Equalisation Service

Persistent daemontools service that monitors conditions and orchestrates equalisation. Web UI at port 8088.

**Main state machine:**

```
              +───────────────────────────────────────────+
              |                                           |
              v                                           |
     [0: Idle] ─── check every 60s, update web UI ───────+
              |                                           |
              | (conditions met OR RunNow + SoC >= 95%)   |
              v                                           |
  [1: Stopping driver]                                    |
              |  register temp svc at 28.4V (safe)        |
              |  stop aggregate driver                    |
              v                                           |
  [2: Disconnecting LFP]                                  |
              |  open relay 2                             |
              |  verify: LFP current < 5A                 |
              |  record LFP voltage at disconnect         |
              |  raise CVL to 31.5V (EQ voltage)          |
              v                                           |
  [3: Equalising FLA]                                     |
              |  monitor SmartShunt Trojan current         |
              |  detect Orion failure (LFP V drop > 0.5V) |
              |  warn if Trojan I > 60A (dynamo/MPPT)     |
              |  handle i_trojan=None (5 min timeout)     |
              |                                           |
              | (V >= 31.4V AND I < 10A, OR timeout/lost) |
              v                                           |
  [4: Cooling down]                                       |
              |  reduce CVL to 27.0V (float)              |
              v                                           |
  [5: Voltage matching]                                   |
              |  wait for |V_trojan - V_lfp| < 1V         |
              |  check SmartShunt Trojan responsive        |
              |                                           |
              | (delta < 1V)                              |
              v                                           |
  [6: Reconnecting LFP]                                   |
              |  close relay 2                            |
              |  verify: relay state read-back = closed    |
              |  measure inrush current + reconnect delta  |
              v                                           |
  [7: Restarting driver]                                  |
              |  deregister temp service                  |
              |  restart aggregate driver                 |
              |  invalidate service cache                 |
              |  record timestamp                         |
              |                                           |
              +───────────────────────────────────────────+

     Any state ──> [8: Error]
                      |
                      v
                   finally block:
                     - deregister temp service
                     - check relay state:
                         if open AND delta > 1V: alarm, do NOT close (manual)
                         if open AND delta <= 1V: auto-close
                     - restart aggregate driver if stopped
```

**Startup safety check (CRIT-4):**
```
Service starts (boot / crash recovery / manual restart)
    |
    v
Read relay 2 state
    |
    +── closed (1) ──> Normal idle (no action needed)
    |
    +── open (0) ──> Read voltage delta
                       |
                       +── delta < 1V ──> Auto-close relay, log recovery
                       |
                       +── delta >= 1V ──> Raise alarm, stay open (manual intervention)
                       |
                       +── voltages unreadable ──> Raise alarm, stay open (manual intervention)
```

**Scheduling conditions (all must be true):**

| Condition | Check | Override |
|-----------|-------|---------|
| Enabled | settings flag | — |
| Interval elapsed | >= 90 days since last | RunNow bypasses |
| Afternoon window | 14:00-17:00 | RunNow bypasses |
| LFP SoC >= 95% | from aggregate/serialbattery | **Never bypassed (even by RunNow)** |

**Safety guards and recovery:**

| Guard | Trigger | Action |
|-------|---------|--------|
| **Crash-safe CVL** | Service crash before relay opens | Temp service at 28.4V (safe for LFPs), not 31.5V. CCL = 60A |
| **Delta-aware finally** | Any abort with relay open | Only closes if delta < 1V; otherwise alarm + manual (limits inrush) |
| **Startup recovery** | Service start with open relay | Checks delta, auto-closes or alarms |
| **Relay verification** | After relay open AND close | Reads back hardware state, aborts if mismatch |
| **Orion failure** | LFP V drops > 0.5V during EQ | Abort + reconnect |
| **SmartShunt Trojan** | V_trojan = None | Abort in both EQ and voltage matching |
| **Current loss** | i_trojan = None for 5 min | Proceed to voltage matching (not full timeout) |
| **High current** | Trojan I > 60A during EQ | Warning (dynamo/MPPT detected) |
| **Voltage match timeout** | > 4 hours | Alarm, stay disconnected, manual intervention |
| **SoC enforcement** | RunNow with SoC < 95% | Refused — SoC never bypassed |
| **Inrush monitoring** | After relay reconnect | Measures and logs current + delta for calibration |
| **Bounds validation** | Web UI setting change | Checked against min/max before D-Bus write |
| **Double trigger** | RunNow race condition | Flag cleared when equalisation starts |

## Integrated State Transitions

### Normal Operation

```
Quattro:     Bulk/Absorption/Float/Inverting (ESS managed)
DVCC:        reads CVL from aggregate (28.4V or 28.8V)
Aggregate:   Running, publishing CVL/CCL/DCL
SerialBatt:  Running, monitoring BMS, CVCM active
FLA Eq:      Idle, checking every 60s, updating web UI with live voltages
Relay 2:     Closed
Orion:       Off
```

### FLA Equalisation Sequence (crash-safe)

```
Time  | Quattro          | DVCC                 | Aggregate  | SerialBatt | FLA Eq              | Relay | Orion | CVL
------|------------------|----------------------|------------|------------|---------------------|-------|-------|------
T+0   | Charging @ 28.4V | CVL from agg/99      | Running    | Running    | Conditions met      | Closed| Off   | 28.4V
T+1   | Charging @ 28.4V | CVL from agg/99*     | Running    | Running    | Register temp @28.4V| Closed| Off   | 28.4V
T+2   | Charging @ 28.4V | CVL from temp/100     | Stopping   | Running    | Stop aggregate      | Closed| Off   | 28.4V
T+7   | Charging @ 28.4V | CVL from temp/100     | Stopped    | Running    | Open relay          | Open  | On    | 28.4V
T+17  | Charging @ 28.4V | CVL from temp/100     | Stopped    | Running    | Verify + raise CVL  | Open  | On    | 31.5V
T+18  | Charging @ 31.5V | CVL from temp/100     | Stopped    | Running    | Equalising          | Open  | On    | 31.5V
T+168 | Tapering @ 31.5V | CVL from temp/100     | Stopped    | Running    | EQ done (V≥31.4,I<10A)| Open  | On    | 31.5V
T+169 | Charging @ 27.0V | CVL=27.0V (float)     | Stopped    | Running    | Cooling down        | Open  | On    | 27.0V
T+199 | Float @ 27.0V    | CVL=27.0V             | Stopped    | Running    | Voltage matching    | Open  | On    | 27.0V
T+229 | Float @ 27.0V    | CVL=27.0V             | Stopped    | Running    | Delta < 1V          | Open  | On    | 27.0V
T+230 | Float @ 27.0V    | CVL=27.0V             | Stopped    | Running    | Close + verify relay| Closed| Off   | 27.0V
T+231 | Float @ 27.0V    | CVL=27.0V             | Stopped    | Running    | Measure inrush      | Closed| Off   | 27.0V
T+234 | Float @ 27.0V    | No CVL (brief)        | Starting   | Running    | Restart driver      | Closed| Off   | —
T+244 | Charging @ 28.4V | CVL from agg/99       | Running    | Running    | Idle                | Closed| Off   | 28.4V
```

*(Times in minutes, approximate. *DVCC uses lowest device instance — agg/99 wins over temp/100 while both exist)*

### Crash at Each Step

| Crash point | CVL on bus | LFP exposure | Recovery |
|---|---|---|---|
| T+1 (temp registered, aggregate still running) | 28.4V from agg/99 | Safe — on bus at 28.4V | Temp dies, aggregate unaffected |
| T+2-T+7 (aggregate stopping) | 28.4V from temp/100 | Safe — 28.4V / 8 = 3.55V/cell | Startup check: relay closed, idle |
| T+7-T+17 (relay open, CVL still 28.4V) | 28.4V from temp/100 | Safe — on Orion | Startup check: relay open, delta < 1V → auto-close |
| T+17-T+168 (equalising at 31.5V) | **Temp dies → no CVL** | **Safe — on Orion** | Quattro stops charging. Startup: relay open, check delta |
| T+169-T+229 (cooling/matching at 27.0V) | **Temp dies → no CVL** | Safe — on Orion | Startup: relay open, check delta |
| T+230 (relay just closed) | 27.0V from temp/100 | Safe — on bus at 27.0V | Normal restart |
| T+234 (aggregate restarting) | No CVL briefly | Safe — relay closed, both banks on bus | Aggregate starts normally |

**Key safety invariant**: CVL is never raised above 28.4V while the relay is closed. The 31.5V equalisation voltage is only applied after relay open is verified.

### Error Recovery

```
Time  | Event                        | Quattro     | Relay | FLA Eq action
------|------------------------------|-------------|-------|----------------------------
T+0   | SmartShunt Trojan offline    | @ 31.5V     | Open  | Detect error
T+1   | —                            | @ 31.5V     | Open  | Deregister temp service
T+2   | —                            | No CVL      | Open  | Finally: check delta
T+3   | Delta < 2V                   | No CVL      | Open  | Auto-close relay
T+5   | —                            | No CVL      | Closed| Restart aggregate driver
T+15  | —                            | @ 28.4V     | Closed| Alarm: buzzer + VRM + D-Bus
      |                              |             |       |
T+3   | Delta > 2V (alternative)     | No CVL      | Open  | DO NOT close relay
T+4   | —                            | No CVL      | Open  | Alarm: manual intervention
      | LFPs remain safely on Orion  |             |       | Restart aggregate driver
```

### Startup After Cerbo Reboot During Equalisation

```
1. Cerbo reboots (shore power loss, firmware update, watchdog)
2. /data/rc.local recreates /service/fla-equalisation symlink
3. Daemontools starts fla_equalisation.py
4. _startup_safety_check() runs:
   a. Read relay 2 state
   b. If closed (1): normal — relay was restored by hardware default or closed before reboot
   c. If open (0): interrupted equalisation
      - Read V_trojan and V_lfp
      - If delta < 1V: auto-close relay, log recovery, proceed to idle
      - If delta >= 1V: ALARM — do not close, manual intervention
      - If voltages unreadable: ALARM — do not close, manual intervention
5. Aggregate driver also restarts via rc.local (independent)
6. System returns to normal operation (or error state for manual check)
```

### 6. FLA Charge Service

Persistent daemontools service that detects undercharged Trojan FLA batteries and runs a full bulk+absorption charge cycle. Web UI at port 8089.

**Main state machine:**

```
              +───────────────────────────────────────────+
              |                                           |
              v                                           |
     [0: Idle] ─── check every 60s, update web UI ───────+
              |                                           |
              | (Trojan SoC < 85% AND AC available,       |
              |  OR RunNow + AC available)                |
              v                                           |
  [1: Phase 1 — Shared charging]                          |
              |  both banks on bus, serialbattery controls |
              |  wait for LFP SoC >= 95% OR taper OR cell |
              |  voltage >= 3.50V                         |
              v                                           |
  [2: Stopping driver]                                    |
              |  register temp svc at current bus V (safe) |
              |  stop aggregate driver                    |
              |  switch BatteryService to temp/100        |
              v                                           |
  [3: Disconnecting LFP]                                  |
              |  open relay 2, verify LFP current < 5A    |
              |  raise CVL to 29.64V (Trojan absorption)  |
              |  CCL = 60A                                |
              v                                           |
  [4: Phase 2 — FLA bulk] → [5: Phase 3 — Absorption]    |
              |  monitor SmartShunt Trojan voltage/current |
              |  detect Orion failure, AC loss, high I     |
              |                                           |
              | (V >= 29.64V AND I < 10A, OR timeout)     |
              v                                           |
  [7: Voltage matching]                                   |
              |  reduce CVL to 27.0V (float)              |
              |  wait for |V_trojan - V_lfp| < 1V         |
              v                                           |
  [8: Reconnecting LFP]                                   |
              |  close relay 2, verify, measure inrush    |
              v                                           |
  [9: Restarting driver]                                  |
              |  deregister temp, restart aggregate       |
              +───────────────────────────────────────────+

     Any state ──> [10: Error] (same finally block as EQ service)
```

**Scheduling conditions:**

| Condition | Check | Override |
|-----------|-------|---------|
| Enabled | settings flag | — |
| Trojan SoC < 85% | from SmartShunt Trojan | RunNow bypasses (intentional — allows manual charge at any SoC) |
| AC input available | shore power or generator | **Never bypassed** |

**Trojan L16H-AC charge parameters (from datasheet):**

| Parameter | Per cell | 24V system (12 cells) |
|-----------|----------|----------------------|
| Bulk/Absorption | 2.47V | 29.64V |
| Float | 2.25V | 27.00V |
| Equalise (datasheet) | 2.70V | 32.40V (capped to 31.5V) |
| C20 capacity | — | 435 Ah |
| Temp compensation | ±0.005V/cell/°C | ±0.06V/°C |

## D-Bus Service Map

```
com.victronenergy.vebus.ttyS4                 # Quattro II
com.victronenergy.system                      # Cerbo GX / DVCC
com.victronenergy.settings                    # Localsettings (persistent)
com.victronenergy.battery.ttyUSB0__0x01       # SerialBattery (Boven) [inst 3]
com.victronenergy.battery.ttyUSB0__0x02       # SerialBattery (Onder) [inst 4]
com.victronenergy.battery.ttyS5              # SmartShunt LFP [inst 277]
com.victronenergy.battery.ttyS7              # SmartShunt Trojan [inst 279]
com.victronenergy.battery.aggregate           # Aggregate battery [inst 99]
com.victronenergy.solarcharger.ttyS6          # MPPT 150/60
com.victronenergy.fla_equalisation            # FLA Equalisation status [inst 200]
com.victronenergy.fla_charge                  # FLA Charge status [inst 201]
com.victronenergy.battery.fla_equalisation    # Temp battery service [inst 100] (during EQ or charge)
```

## CVL Authority Chain

```
Priority (highest to lowest):

1. JK BMS hardware MOSFET disconnect (3.65V cell → BMS cuts off)
2. serialbattery CVCM (tapers to 0% at 3.65V cell)
3. Aggregate battery CVL (3.55V or 3.60V cell, dynamic reduction at 3.65V)
4. DVCC MaxChargeVoltage (reads from active battery service)
5. Quattro charges up to DVCC limit

During FLA equalisation (31.5V, CCL 60A):
1. JK BMS — not relevant (LFPs disconnected, Orion charges safely)
2. serialbattery — still running but DVCC ignores its CVL
3. Temp battery service CVL + CCL:
   - Phase 1 (relay closed): 28.4V / 60A — safe for LFPs
   - Phase 2 (relay open, verified): 31.5V / 60A — for Trojans only
   - Phase 3 (cooling): 27.0V / 60A — float, waiting for delta < 1V
4. DVCC reads from temp service
5. Quattro + MPPT charge per DVCC command, capped at 60A total

During FLA charge (29.64V absorption, CCL 60A):
1-2. Same as above
3. Temp battery service CVL + CCL:
   - Phase 1 (shared): aggregate controls, both banks on bus
   - Phase 2 (relay open, verified): 29.64V / 60A — Trojan absorption
   - Phase 3 (cooling): 27.0V / 60A — float, waiting for delta < 1V
4-5. Same as above
```

## Inrush Current Monitoring

After each successful reconnection, the service measures and logs:
- **Inrush current** (from SmartShunt LFP, 1 second after relay close)
- **Reconnect delta** (voltage difference at moment of closure)

These values are displayed on the web UI and stored on D-Bus (`/InrushCurrent`, `/ReconnectDelta`).
Use these measurements to calibrate the `voltage_delta_max` setting:
- If inrush current is consistently low (< 50A), the delta threshold can be increased for faster reconnection
- If inrush current is high (> 100A), reduce the delta threshold to protect relay contacts and SmartShunt wiring

## Web UIs

**FLA Equalisation** at **http://venus.local:8088**:
- Live status: state, time remaining, last/next equalisation
- Live voltages: Trojan, LFP, delta (updates during all phases including EQ and voltage matching)
- Inrush data: last recorded inrush current and reconnect delta
- Editable settings: all 10 configuration parameters
- Run Now button: triggers immediate equalisation (LFP SoC still enforced)
- Auto-refreshes every 5 seconds

**FLA Charge** at **http://venus.local:8089**:
- Live status: state, time remaining, last charge
- Live readings: Trojan voltage/current/SoC, LFP voltage/SoC, delta
- Editable settings: all 12 configuration parameters
- Run Now button: triggers immediate charge (bypasses SoC trigger, AC still enforced)
- Auto-refreshes every 5 seconds

Both dashboards update live during all active phases (bulk, absorption, equalisation, voltage matching) via the web cache mechanism.
