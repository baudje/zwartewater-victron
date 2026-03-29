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

**During FLA equalisation:** DVCC reads CVL from the temporary battery service (`com.victronenergy.battery.fla_equalisation`, device instance 100). The CVL starts at 28.4V (safe) and is only raised to 31.2V after the relay is confirmed open.

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
    v (FLA equaliser raises CVL to 31.2V — ONLY after relay confirmed open)
EQ mode:      ActiveBattery = temp/100 at 31.2V, Relay2 = open, Orion active
    |          *** If crash here: temp svc dies, Quattro stops charging, LFPs on Orion (safe) ***
    v (equalisation complete, CVL reduced to 27.6V float)
Cooling:      ActiveBattery = temp/100 at 27.6V, Relay2 = open
    |
    v (voltage delta < 1V, relay closes, verified via read-back)
Reconnected:  ActiveBattery = temp/100 at 27.6V, Relay2 = closed, inrush measured
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
              |  raise CVL to 31.2V (EQ voltage)          |
              v                                           |
  [3: Equalising FLA]                                     |
              |  monitor SmartShunt Trojan current         |
              |  detect Orion failure (LFP V drop > 0.5V) |
              |  warn if Trojan I > 60A (dynamo/MPPT)     |
              |  handle i_trojan=None (5 min timeout)     |
              |                                           |
              | (I < 8A OR 2.5hr timeout OR current lost) |
              v                                           |
  [4: Cooling down]                                       |
              |  reduce CVL to 27.6V (float)              |
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
                         if open AND delta > 2V: alarm, do NOT close (manual)
                         if open AND delta <= 2V: auto-close
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
                       +── delta < 2V ──> Auto-close relay, log recovery
                       |
                       +── delta >= 2V ──> Raise alarm, stay open (manual intervention)
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
| **Crash-safe CVL** | Service crash before relay opens | Temp service at 28.4V (safe for LFPs), not 31.2V |
| **Delta-aware finally** | Any abort with relay open | Only closes if delta < 2V; otherwise alarm + manual |
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
T+17  | Charging @ 28.4V | CVL from temp/100     | Stopped    | Running    | Verify + raise CVL  | Open  | On    | 31.2V
T+18  | Charging @ 31.2V | CVL from temp/100     | Stopped    | Running    | Equalising          | Open  | On    | 31.2V
T+168 | Tapering @ 31.2V | CVL from temp/100     | Stopped    | Running    | EQ done (I<8A)      | Open  | On    | 31.2V
T+169 | Charging @ 27.6V | CVL=27.6V (float)     | Stopped    | Running    | Cooling down        | Open  | On    | 27.6V
T+199 | Float @ 27.6V    | CVL=27.6V             | Stopped    | Running    | Voltage matching    | Open  | On    | 27.6V
T+229 | Float @ 27.6V    | CVL=27.6V             | Stopped    | Running    | Delta < 1V          | Open  | On    | 27.6V
T+230 | Float @ 27.6V    | CVL=27.6V             | Stopped    | Running    | Close + verify relay| Closed| Off   | 27.6V
T+231 | Float @ 27.6V    | CVL=27.6V             | Stopped    | Running    | Measure inrush      | Closed| Off   | 27.6V
T+234 | Float @ 27.6V    | No CVL (brief)        | Starting   | Running    | Restart driver      | Closed| Off   | —
T+244 | Charging @ 28.4V | CVL from agg/99       | Running    | Running    | Idle                | Closed| Off   | 28.4V
```

*(Times in minutes, approximate. *DVCC uses lowest device instance — agg/99 wins over temp/100 while both exist)*

### Crash at Each Step

| Crash point | CVL on bus | LFP exposure | Recovery |
|---|---|---|---|
| T+1 (temp registered, aggregate still running) | 28.4V from agg/99 | Safe — on bus at 28.4V | Temp dies, aggregate unaffected |
| T+2-T+7 (aggregate stopping) | 28.4V from temp/100 | Safe — 28.4V / 8 = 3.55V/cell | Startup check: relay closed, idle |
| T+7-T+17 (relay open, CVL still 28.4V) | 28.4V from temp/100 | Safe — on Orion | Startup check: relay open, delta < 2V → auto-close |
| T+17-T+168 (equalising at 31.2V) | **Temp dies → no CVL** | **Safe — on Orion** | Quattro stops charging. Startup: relay open, check delta |
| T+169-T+229 (cooling/matching at 27.6V) | **Temp dies → no CVL** | Safe — on Orion | Startup: relay open, check delta |
| T+230 (relay just closed) | 27.6V from temp/100 | Safe — on bus at 27.6V | Normal restart |
| T+234 (aggregate restarting) | No CVL briefly | Safe — relay closed, both banks on bus | Aggregate starts normally |

**Key safety invariant**: CVL is never raised above 28.4V while the relay is closed. The 31.2V equalisation voltage is only applied after relay open is verified.

### Error Recovery

```
Time  | Event                        | Quattro     | Relay | FLA Eq action
------|------------------------------|-------------|-------|----------------------------
T+0   | SmartShunt Trojan offline    | @ 31.2V     | Open  | Detect error
T+1   | —                            | @ 31.2V     | Open  | Deregister temp service
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
      - If delta < 2V: auto-close relay, log recovery, proceed to idle
      - If delta >= 2V: ALARM — do not close, manual intervention
      - If voltages unreadable: ALARM — do not close, manual intervention
5. Aggregate driver also restarts via rc.local (independent)
6. System returns to normal operation (or error state for manual check)
```

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
com.victronenergy.battery.fla_equalisation    # Temp battery service [inst 100] (only during EQ)
```

## CVL Authority Chain

```
Priority (highest to lowest):

1. JK BMS hardware MOSFET disconnect (3.65V cell → BMS cuts off)
2. serialbattery CVCM (tapers to 0% at 3.65V cell)
3. Aggregate battery CVL (3.55V or 3.60V cell, dynamic reduction at 3.65V)
4. DVCC MaxChargeVoltage (reads from active battery service)
5. Quattro charges up to DVCC limit

During FLA equalisation:
1. JK BMS — not relevant (LFPs disconnected, Orion charges safely)
2. serialbattery — still running but DVCC ignores its CVL
3. Temp battery service CVL:
   - Phase 1 (relay closed): 28.4V — safe for LFPs
   - Phase 2 (relay open, verified): 31.2V — for Trojans only
   - Phase 3 (cooling): 27.6V — float
4. DVCC reads from temp service
5. Quattro charges per DVCC command
```

## Inrush Current Monitoring

After each successful reconnection, the service measures and logs:
- **Inrush current** (from SmartShunt LFP, 1 second after relay close)
- **Reconnect delta** (voltage difference at moment of closure)

These values are displayed on the web UI and stored on D-Bus (`/InrushCurrent`, `/ReconnectDelta`).
Use these measurements to calibrate the `voltage_delta_max` setting:
- If inrush current is consistently low (< 50A), the delta threshold can be increased for faster reconnection
- If inrush current is high (> 100A), reduce the delta threshold to protect relay contacts and SmartShunt wiring

## Web UI

Dashboard at **http://venus.local:8088** provides:
- **Live status**: state, time remaining, last/next equalisation
- **Live voltages**: Trojan, LFP, delta
- **Inrush data**: last recorded inrush current and reconnect delta
- **Editable settings**: all 10 configuration parameters
- **Run Now button**: triggers immediate equalisation (SoC still enforced)
- Auto-refreshes every 5 seconds
