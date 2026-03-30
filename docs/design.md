# dbus-aggregate-batteries Optimisation — Design Spec

## Context

Optimise dbus-aggregate-batteries for the Victron Energy system on vessel Zwartewater (ENI: 03330190).
Electrical schema by ScheepsArts (Gijs Arts), dated 26-5-2025.

**Hardware:**
- Cerbo GX (venus.local)
- Quattro II 24V 5000/120 (ESS/DVCC controlled)
- 2x 8-cell LFP batteries: EVE MB31 (314Ah each, 626Ah total), JK BMS, via dbus-serialbattery (CAN+USB)
- 4x Trojan L16H-AC 6V FLA batteries in series (24V, 435Ah)
- Orion DC-DC charger (10ORION4) — charges LFPs at safe voltage when relay 2 is open
- SmartShunt LFP (10R3, 50mV/500A, VE.Direct2, D-Bus instance 277) — on LFP side
- SmartShunt Trojan (10R1, 50mV/500A, VE.Direct1, D-Bus instance 279) — on Trojan side
- SmartSolar MPPT 150/60 (10MPPT6, VE.Direct3) — 860Wp PV
- Cerbo relay 2 controls LFP direct connection and Orion activation
- Main engine dynamo 24V 80A

**DC Topology (two paths to LFP bank):**

```
                                      [PV 860Wp]
                                          |
                                    [MPPT 150/60]
                                          |
[Quattro II] -- [SmartShunt Trojan 10R1/279] -- [Trojan L16H-AC x4] -- [DC Bus 24V]
  24V 5000/120                                                              |
                                                                            +-- [RELAY 2 (closed)] -- [SmartShunt LFP 10R3/277] -- [EVE LFP 1]
                                                                            |         (direct path, normal operation)                [EVE LFP 2]
                                                                            |
                                                                            +-- [Orion DC-DC 10ORION4] -- [EVE LFP 1]
                                                                                  (activates when relay 2 opens)    [EVE LFP 2]
```

**Normal operation:** Relay 2 closed, direct path active, Orion off. LFPs in parallel with Trojans on DC bus.
**Equalisation mode:** Relay 2 open, direct path broken, Orion on (charges LFPs at safe voltage independently).

**Goals:**
1. Optimise charging strategy for EVE MB31 cells (proper CVL/CCL, balancing)
2. Safety — cell protection, alarms, overvoltage prevention
3. Periodic FLA equalisation with automated relay control and voltage matching

---

## Part A: config.ini Optimisation

No code changes to dbus-aggregate-batteries. Configuration only.

### Hardware Settings

```ini
NR_OF_BATTERIES = 2
NR_OF_CELLS_PER_BATTERY = 8
NR_OF_MPPTS = 1
```

### Current Measurement

```ini
CURRENT_FROM_VICTRON = True
USE_SMARTSHUNTS = [277]
INVERT_SMARTSHUNTS = False
IGNORE_SMARTSHUNT_ABSENCE = True
```

**Rationale:** JK BMS current measurement is inaccurate (often +/-5A or worse). SmartShunt LFP (277) directly measures the LFP bank current. Combined with Quattro and MPPT readings, this gives precise measurements. Fallback to BMS if SmartShunt drops momentarily.

### Own Charge Parameters

```ini
OWN_CHARGE_PARAMETERS = False

CHARGE_VOLTAGE_LIST =
    3.55,   ; January
    3.55,   ; February
    3.55,   ; March
    3.55,   ; April
    3.55,   ; May
    3.55,   ; June
    3.55,   ; July
    3.55,   ; August
    3.55,   ; September
    3.55,   ; October
    3.55,   ; November
    3.55    ; December

BALANCING_VOLTAGE = 3.60
BALANCING_REPETITION = 14
CELL_DIFF_MAX = 0.005
```

**Rationale:**
- 3.55V/cell daily (28.4V pack) — ~95% SoC, within EVE spec (max 3.65V), benefits parallel FLA charging
- Balancing at 3.60V every 14 days gives JK BMS active balancer headroom
- 5mV cell difference target is tight and achievable

### Cell Protection

```ini
MAX_CELL_VOLTAGE = 3.65
MIN_CELL_VOLTAGE = 2.80
MIN_CELL_HYSTERESIS = 0.2
```

**Rationale:**
- MAX_CELL_VOLTAGE at 3.65V matches EVE datasheet end-of-charge voltage. Dynamic CVL kicks in here as hard ceiling. Absolute max is 3.8V — this gives 150mV headroom before BMS disconnect
- MIN_CELL_VOLTAGE at 2.80V provides margin above the 2.5V datasheet hard cutoff
- Discharge resumes at 3.0V (2.80 + 0.20)

### Current Limiting

```ini
MAX_CHARGE_CURRENT = 120
MAX_DISCHARGE_CURRENT = 150

CELL_CHARGE_LIMITING_VOLTAGE = 2.80, 2.90, 3.30, 3.50, 3.60, 3.65
CELL_CHARGE_LIMITED_CURRENT =  0.2,  1.0,  1.0, 0.8,  0.1,  0

CELL_DISCHARGE_LIMITING_VOLTAGE = 2.80, 2.90, 3.00
CELL_DISCHARGE_LIMITED_CURRENT =  0,    0.05, 1

BATTERY_EFFICIENCY = 0.98
```

**Rationale:**
- MAX_CHARGE_CURRENT 120A — Quattro II 5000/120 is the bottleneck
- MAX_DISCHARGE_CURRENT 150A — sufficient for all normal use, safer than Quattro's full 200A draw
- Charge current tapers smoothly from 3.50V, cuts to zero at 3.65V
- Discharge current ramps to zero approaching 2.80V
- Temperature-dependent derating not needed in config: at worst case 8degC indoors, EVE allows 0.3P (188A total), Quattro charge max is 120A — always within limits

### SoC

```ini
OWN_SOC = False
ZERO_SOC = False
CHARGE_SAVE_PRECISION = 0.0025
MAX_CELL_VOLTAGE_SOC_FULL = 3.55
MIN_CELL_VOLTAGE_SOC_EMPTY = 2.80
```

**Rationale:**
- Uses serialbattery SoC (resets to 100% each charge cycle at SOC_RESET_CELL_VOLTAGE=3.55V)
- OWN_SOC=True added a redundant coulomb counter that drifted from SmartShunt SoC
- SOC full resets at 3.55V (daily charge voltage)
- SOC empty aligned with MIN_CELL_VOLTAGE

### Other Settings (unchanged)

```ini
BATTERY_SERVICE_NAME = com.victronenergy.battery
DCLOAD_SERVICE_NAME = com.victronenergy.dcload
BATTERY_PRODUCT_NAME_PATH = /ProductName
BATTERY_PRODUCT_NAME = SerialBattery
BATTERY_INSTANCE_NAME_PATH = /Serial
MULTI_KEYWORD = com.victronenergy.vebus
MPPT_KEYWORD = com.victronenergy.solarcharger
SMARTSHUNT_NAME_KEYWORD = SmartShunt
SMARTSHUNT_INSTANCE_NAME_PATH = /CustomName
SEARCH_TRIALS = 10
READ_TRIALS = 10
KEEP_MAX_CVL = False
SEND_CELL_VOLTAGES = 1
LOGGING = INFO
LOG_PERIOD = 300
```

---

## Part C: FLA Equalisation Script

Standalone Python script running on the Cerbo GX. Does not modify the dbus-aggregate-batteries driver.

### Overview

Automates periodic Trojan L16H-AC equalisation by:
1. Stopping the aggregate battery driver (so its CVL doesn't constrain DVCC)
2. Registering a temporary D-Bus battery service to control Quattro charge voltage
3. Opening relay 2 to disconnect LFP direct path (Orion activates to maintain LFPs at safe voltage)
4. Running equalisation at 31.5V
5. Voltage matching before reconnecting
6. Restarting the aggregate battery driver

### Trojan L16H-AC Equalisation Specs

- 4x 6V in series = 24V bank, 435Ah
- Equalisation voltage: 31.5V (2.625V/cell × 12 cells, mid-range of Trojan spec 2.58–2.70V/cell)
- Equalisation complete: current drops below 10A
- Max duration: 2.5 hours

### Scheduling

- Cron runs **every hour**
- Script checks:
  1. Has it been >= 90 days since last equalisation?
  2. Is current time in afternoon window (14:00-17:00)?
  3. Is LFP SoC >= 95%?
- If all conditions met: start equalisation
- Otherwise: exit immediately
- Last equalisation timestamp stored in `/data/apps/fla-equalisation/last_equalisation`

### Step-by-Step Sequence

```
 1. Pre-checks pass (90 days, afternoon, LFP SoC >= 95%)
 2. Stop dbus-aggregate-batteries service:
    - svc -d /service/dbus-aggregate-batteries
    - This removes the LFP battery service from D-Bus so DVCC is no longer constrained by LFP CVL
 3. Register temporary battery service on D-Bus:
    - Service: com.victronenergy.battery (device instance 100)
    - /Info/MaxChargeVoltage = 31.5
    - /Info/MaxChargeCurrent = appropriate for Trojans
    - /Info/MaxDischargeCurrent = 0
    - /Dc/0/Voltage = live from SmartShunt Trojan (279)
    - /Dc/0/Current = live from SmartShunt Trojan (279)
 4. Open Cerbo relay 2 (com.victronenergy.system /Relay/1/State = 0):
    - Direct LFP path breaks
    - Orion DC-DC activates automatically, charges LFPs at safe voltage
    - LFP BMS communication (CAN+USB) remains active
 5. Quattro charges Trojans at 31.5V via temporary D-Bus service
 6. Monitor SmartShunt Trojan (279):
    - Complete when current < 10A
    - Timeout: 2.5 hours max
 7. Temporary service reduces CVL to normal float (~27.0V)
 8. Wait for voltage convergence:
    - V_trojan from SmartShunt Trojan (279) /Dc/0/Voltage
    - V_lfp from SmartShunt LFP (277) /Dc/0/Voltage (fallback: dbus-serialbattery)
    - Condition: |V_trojan - V_lfp| < 1V
    - Timeout: 4 hours
 9. Close Cerbo relay 2 (/Relay/1/State = 1):
    - Direct LFP path restored
    - Orion switches off
    - Inrush current limited by < 1V delta
10. Deregister temporary battery service
11. Start dbus-aggregate-batteries:
    - svc -u /service/dbus-aggregate-batteries
    - Driver rediscovers batteries, DVCC resumes normal operation
12. Record timestamp to last_equalisation file
```

### Venus OS Settings Integration (GUI v2 compatible)

Equalisation parameters registered via `com.victronenergy.settings` on D-Bus, accessible from Cerbo touchscreen, VRM remote console, and GUI v2:

| Setting path | Default | Description |
|-------------|---------|-------------|
| `/Settings/FlaEqualisation/EqualisationVoltage` | 31.5 | Trojan equalisation voltage (V), 2.625V/cell |
| `/Settings/FlaEqualisation/EqualisationCurrentComplete` | 10 | Current threshold for completion (A) |
| `/Settings/FlaEqualisation/EqualisationTimeoutHours` | 2.5 | Max equalisation duration (hours) |
| `/Settings/FlaEqualisation/FloatVoltage` | 27.0 | Float voltage after equalisation (V), 2.25V/cell |
| `/Settings/FlaEqualisation/VoltageDeltaMax` | 1.0 | Max voltage difference for reconnect (V) |
| `/Settings/FlaEqualisation/VoltageMatchTimeoutHours` | 4 | Max wait for voltage convergence (hours) |
| `/Settings/FlaEqualisation/DaysBetweenEqualisation` | 90 | Interval between equalisations (days) |
| `/Settings/FlaEqualisation/AfternoonStartHour` | 14 | Earliest start hour |
| `/Settings/FlaEqualisation/AfternoonEndHour` | 17 | Latest start hour |
| `/Settings/FlaEqualisation/LfpSocMin` | 95 | Minimum LFP SoC to start (%) |
| `/Settings/FlaEqualisation/Enabled` | 1 | Enable/disable automatic equalisation |
| `/Settings/FlaEqualisation/RunNow` | 0 | Set to 1 to trigger immediate equalisation |

### D-Bus Service for Cerbo GUI Visibility

The script registers `com.victronenergy.fla_equalisation` exposing:

| Path | Description |
|------|-------------|
| `/State` | Idle / Waiting for LFP full / Disconnecting / Equalising / Cooling down / Voltage matching / Reconnecting |
| `/TimeRemaining` | Estimated seconds remaining |
| `/TrojanVoltage` | Live Trojan bank voltage |
| `/LfpVoltage` | Live LFP bank voltage |
| `/VoltageDelta` | Difference between the two |

Visible on Cerbo Device List page.

### Safety Guards

| Guard | Action |
|-------|--------|
| LFP SoC < 95% | Don't start |
| Relay open but LFP current != ~0A | Abort, close relay, restart aggregate driver, alarm |
| Equalisation timeout (2.5 hrs) | Stop equalisation, proceed to voltage matching |
| Voltage delta doesn't converge (4 hrs) | Alarm, stay disconnected, manual intervention required |
| SmartShunt Trojan (279) unresponsive | Abort, close relay, restart aggregate driver, alarm |
| SmartShunt LFP (277) unresponsive | Use dbus-serialbattery for LFP voltage |
| Script crash / unexpected exit | Systemd watchdog restarts, closes relay as first action on startup |
| Aggregate driver fails to restart | Alarm, log error |

### Alerting (non-converging delta or critical failure)

- D-Bus alarm visible on Cerbo GX GUI
- VRM portal notification (via D-Bus alarm path, picked up automatically by VRM)
- Cerbo GX buzzer activated
- LFPs stay disconnected until manual intervention
- Full event log at `/data/log/fla-equalisation.log`

### File Layout on Cerbo

```
/data/apps/fla-equalisation/
    fla-equalisation.py        # Main script
    last_equalisation          # Timestamp of last successful run
    ext/velib_python/          # Victron D-Bus library (same as used by aggregate driver)
/data/log/
    fla-equalisation.log       # Event log
/etc/cron.d/
    fla-equalisation           # Hourly cron entry
```

Settings stored in Venus OS settings (D-Bus), not in a config file — accessible via GUI.

---

## EVE MB31 Key Specifications (Reference)

| Parameter | Value |
|-----------|-------|
| Nominal capacity | 314 Ah |
| Nominal voltage | 3.2V/cell |
| End-of-charge voltage (Umax) | 3.65V/cell |
| Absolute max charge voltage | 3.8V/cell |
| Discharge cutoff | 2.5V (>0degC) |
| Absolute min discharge voltage | 1.8V/cell |
| Standard charge/discharge | 0.5P (~157A) |
| Charge temp range | 0-60degC |
| Cycle life | 8000 cycles @ 70% SOH |
| Charge rate at 8degC | ~0.3P (~94A per battery) |
