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
      (device inst. 99)          (temp service)
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

**During FLA equalisation:** DVCC reads CVL from the temporary battery service (`com.victronenergy.battery.fla_equalisation`, device instance 100) which commands 31.2V. The Quattro enters Bulk/Absorption at this higher voltage.

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

**State transitions:**
```
Normal:    ActiveBattery = aggregate/99, Relay2 = closed
    |
    v (FLA equaliser stops aggregate driver)
Transition: ActiveBattery = gone (briefly), Relay2 = closed
    |
    v (FLA equaliser registers temp service)
EQ mode:   ActiveBattery = fla_equalisation/100, Relay2 = open
    |
    v (equalisation complete, voltage matched)
Transition: temp service deregistered, Relay2 = closed
    |
    v (aggregate driver restarted)
Normal:    ActiveBattery = aggregate/99, Relay2 = closed
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

Persistent service that monitors conditions and orchestrates equalisation.

**Main state machine:**

```
                    +──────────────────────────────+
                    |                              |
                    v                              |
             [0: Idle] ─── check every 60s ───────+
                    |                              |
                    | (conditions met OR RunNow)   |
                    v                              |
        [1: Stopping driver]                       |
                    |                              |
                    v                              |
        [2: Disconnecting LFP]                     |
                    |                              |
                    v                              |
        [3: Equalising FLA]                        |
                    |                              |
                    | (I < 8A OR 2.5hr timeout)    |
                    v                              |
        [4: Cooling down]                          |
                    |                              |
                    v                              |
        [5: Voltage matching]                      |
                    |                              |
                    | (delta < 1V)                 |
                    v                              |
        [6: Reconnecting LFP]                      |
                    |                              |
                    v                              |
        [7: Restarting driver]                      |
                    |                              |
                    +──────────────────────────────+

        Any state ──> [8: Error] (on failure)
                         |
                         | (safety cleanup: close relay, restart driver)
                         v
                      Manual intervention required
```

**Scheduling conditions (all must be true):**

| Condition | Check | Override |
|-----------|-------|---------|
| Enabled | settings flag | - |
| Interval elapsed | >= 90 days since last | RunNow bypasses |
| Afternoon window | 14:00-17:00 | RunNow bypasses |
| LFP SoC >= 95% | from aggregate/serialbattery | Never bypassed |

**Safety guards and recovery:**

| Guard | Trigger | Action |
|-------|---------|--------|
| LFP current after relay open | abs(I) > 5A | Abort → close relay → restart driver → alarm |
| SmartShunt Trojan offline | V_trojan = None | Abort → close relay → restart driver → alarm |
| Equalisation timeout | > 2.5 hours | Proceed to voltage matching (not abort) |
| Voltage match timeout | > 4 hours | Alarm, stay disconnected, manual intervention |
| Script crash | exception | Finally block: close relay, restart driver |
| Aggregate driver won't restart | svc -u fails | Alarm |

## Integrated State Transitions

### Normal Operation

```
Quattro:     Bulk/Absorption/Float/Inverting (ESS managed)
DVCC:        reads CVL from aggregate (28.4V or 28.8V)
Aggregate:   Running, publishing CVL/CCL/DCL
SerialBatt:  Running, monitoring BMS, CVCM active
FLA Eq:      Idle, checking every 60s, updating web UI
Relay 2:     Closed
Orion:       Off
```

### FLA Equalisation Sequence

```
Time  | Quattro          | DVCC              | Aggregate  | SerialBatt | FLA Eq              | Relay | Orion
------|------------------|-------------------|------------|------------|---------------------|-------|------
T+0   | Charging @ 28.4V | CVL from agg/99   | Running    | Running    | Conditions met      | Closed| Off
T+1   | Charging @ 28.4V | CVL from agg/99   | Stopping   | Running    | Stopping driver     | Closed| Off
T+6   | No battery svc   | No CVL (brief)    | Stopped    | Running    | Registering temp    | Closed| Off
T+7   | Charging @ 31.2V | CVL from temp/100 | Stopped    | Running    | Disconnecting       | Open  | On
T+17  | Charging @ 31.2V | CVL from temp/100 | Stopped    | Running    | Equalising          | Open  | On
T+167 | Tapering @ 31.2V | CVL from temp/100 | Stopped    | Running    | EQ complete (I<8A)  | Open  | On
T+168 | Charging @ 27.6V | CVL=27.6V (float) | Stopped    | Running    | Cooling down        | Open  | On
T+198 | Float @ 27.6V    | CVL=27.6V         | Stopped    | Running    | Voltage matching    | Open  | On
T+228 | Float @ 27.6V    | CVL=27.6V         | Stopped    | Running    | Delta < 1V, closing | Close | Off
T+233 | Float @ 27.6V    | No CVL (brief)    | Starting   | Running    | Restarting driver   | Closed| Off
T+243 | Charging @ 28.4V | CVL from agg/99   | Running    | Running    | Idle                | Closed| Off
```

*(Times in minutes, approximate)*

### Error Recovery

```
Time  | Event                        | Quattro     | Relay | FLA Eq action
------|------------------------------|-------------|-------|----------------------------
T+0   | SmartShunt Trojan offline    | @ 31.2V     | Open  | Detect error
T+1   | —                            | @ 31.2V     | Open  | Deregister temp service
T+2   | —                            | No CVL      | Open  | Close relay 2
T+4   | —                            | No CVL      | Closed| Restart aggregate driver
T+14  | —                            | @ 28.4V     | Closed| Alarm: buzzer + VRM + D-Bus
T+15  | —                            | Normal      | Closed| State = Error (manual check)
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
3. Temp battery service CVL (31.2V for Trojans)
4. DVCC reads from temp service
5. Quattro charges Trojans at 31.2V
```
