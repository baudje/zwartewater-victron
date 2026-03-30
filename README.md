# Zwartewater Victron

Victron Energy system optimisation for vessel **Zwartewater** (ENI: 03330190).

## Why Hybrid LFP + FLA?

This vessel runs two battery chemistries in parallel on the same 24V DC bus: lithium iron phosphate (EVE MB31 LFP, 628Ah) and flooded lead-acid (Trojan L16H-AC FLA, 435Ah). This is uncommon because LFP and FLA have fundamentally different charge profiles — but on a liveaboard vessel, the combination makes practical sense.

**What each chemistry brings:**

- **LFP** is the workhorse. High round-trip efficiency (~98%), flat discharge curve, 8000+ cycle life, and high charge/discharge rates. LFP thrives on cycling — it can operate between 10% and 100% SoC daily without degradation. It handles solar harvest, inverter loads, and engine starting.
- **FLA** is the backup and buffer. Lead-acid prefers to sit near 100% SoC and should not be discharged below ~40%, but it doesn't need a BMS to stay balanced and can't be hard-disconnected by protection electronics. On a vessel with unpredictable shore power, the Trojans absorb excess solar, cushion heavy transient loads, and — critically — keep the DC bus alive if the LFP BMS disconnects.

**How the two chemistries support each other:**

During normal cycling, the LFPs do virtually all the work. Their lower impedance means they absorb most charge current and deliver most discharge current, while the Trojans float near the top of their charge range — exactly where lead-acid wants to be. The FLA bank barely cycles during normal use, which maximises its lifespan.

When the LFPs are depleted (below ~10% SoC, approaching 2.8V/cell), their internal resistance rises and the FLA bank naturally starts taking over discharge duty. If the BMS disconnects the LFPs entirely (cell undervoltage protection), the Trojans keep the DC bus alive — lights stay on, the fridge keeps running, and the Quattro can still charge from shore power or generator to bring everything back up.

**Why parallel works (with constraints):**

LFP and FLA can share a DC bus safely because their normal operating voltage ranges overlap. At the daily LFP charge target of 3.55V/cell (28.4V pack), the Trojans see approximately 2.37V/cell — above their float voltage (2.25V/cell) but well below their gassing threshold (2.47V/cell). The LFP bank cycles freely between 10% and 100% without pushing the FLA bank into harmful territory — the voltage window that corresponds to LFP daily use (roughly 26–28.4V) keeps the Trojans comfortably between 50% and 100% SoC.

**The problem this repo solves:**

The parallel setup works well for daily cycling, but FLA batteries periodically need voltages that would destroy LFP cells:

- **Absorption charge** at 29.64V (2.47V/cell FLA) — this is 3.71V/cell for the LFPs, above the 3.65V absolute max
- **Equalisation** at 31.5V (2.625V/cell FLA) — this is 3.94V/cell for the LFPs, catastrophically above max

The solution is temporary physical isolation: a relay disconnects the LFPs from the DC bus during high-voltage FLA charging, while an Orion DC-DC charger independently maintains the LFPs at a safe voltage. After the FLA charge completes, the system waits for the voltages to converge (delta < 1V) before reconnecting. This repo automates the entire sequence — scheduling, relay control, voltage management, convergence monitoring, and reconnection — with layered safety guards to protect against every failure mode identified during development.

## System Overview

| Component | Model | Details |
|-----------|-------|---------|
| GX device | Cerbo GX | `venus.local` |
| Inverter/charger | Quattro II 24V 5000/120 | ESS/DVCC controlled |
| LFP batteries | 2x EVE MB31 8s (314Ah each) | JK BMS, via dbus-serialbattery |
| FLA batteries | 4x Trojan L16H-AC 6V | Series (24V, 435Ah) |
| Solar | SmartSolar MPPT 150/60 | 860Wp PV |
| Current sensing | SmartShunt LFP (inst. 277) | On LFP side of relay |
| Current sensing | SmartShunt Trojan (inst. 279) | Between Quattro and DC bus |
| DC-DC charger | Orion (10ORION4) | Charges LFPs when relay opens |
| Relay | Cerbo relay 2 | Controls LFP direct connection |

### DC Topology

```
                                      [PV 860Wp]
                                          |
                                    [MPPT 150/60]
                                          |
[Quattro II] -- [SmartShunt Trojan 279] -- [Trojan L16H-AC x4] -- [DC Bus 24V]
  24V 5000/120                                                         |
                                                                       +-- [RELAY 2] -- [SmartShunt LFP 277] -- [EVE LFP 1+2]
                                                                       |    (direct path, normal operation)
                                                                       +-- [Orion DC-DC]
                                                                            (activates when relay opens)
```

**Normal operation:** Relay closed, LFPs directly on DC bus in parallel with Trojans.
**FLA charge/EQ mode:** Relay open, Orion charges LFPs at safe voltage independently.

## What's in This Repo

### 1. Aggregate Battery Config (`config/config.ini`)

Optimised [dbus-aggregate-batteries](https://github.com/Dr-Gigavolt/dbus-aggregate-batteries) configuration for 2x EVE MB31 8-cell LFP with JK BMS.

**Key settings:**
- `CURRENT_FROM_VICTRON = True` — uses SmartShunt LFP instead of inaccurate JK BMS current
- `OWN_CHARGE_PARAMETERS = False` — serialbattery controls charge cycle (Bulk/Absorption/Float)
- Daily charge: 3.55V/cell (28.4V), balancing: 3.60V/cell every 14 days
- Cell protection: max 3.65V, min 2.80V (EVE MB31 datasheet limits)
- Current limits: 120A charge (Quattro limit), 150A discharge

### 2. Serial Battery Config (`config/sb-config.ini`)

Aligned [dbus-serialbattery](https://github.com/Louisvdw/dbus-serialbattery) configuration. Ensures serialbattery cell limits match the aggregate driver settings.

### 3. FLA Equalisation Service (`fla-equalisation/`)

Automated Trojan L16H-AC equalisation. Runs as a persistent daemontools service on Venus OS. Web dashboard at **http://venus.local:8088**.

- Equalisation every 90 days at 31.5V (temperature-compensated per Trojan datasheet)
- Exit criteria: bus voltage >= 31.4V AND current < 10A
- CCL = 60A to protect FLA bank
- LFP isolation via relay 2, Orion DC-DC maintains LFP charge
- Voltage matching (delta < 1V) before reconnecting to limit inrush current

### 4. FLA Charge Service (`fla-charge/`)

Automated Trojan FLA bulk+absorption charge. Runs as a persistent daemontools service on Venus OS. Web dashboard at **http://venus.local:8089**.

- Triggers when Trojan SoC < 85% and AC input available
- Phase 1: shared charging (both banks on bus) until LFP is full
- Phase 2-3: FLA-only absorption at 29.64V (temperature-compensated), CCL 60A
- Exit criteria: bus voltage >= 29.54V AND current < 10A
- Same isolation, voltage matching, and safety guards as EQ service

### 5. Shared Modules (`fla-shared/`)

Common code shared between both services:

| Module | Purpose |
|--------|---------|
| `relay_control.py` | Relay open/close with verification, delta-aware cleanup, startup recovery |
| `voltage_matching.py` | Convergence loop — waits for delta < 1V before reconnect |
| `temp_battery.py` | Subprocess manager for temporary D-Bus battery service |
| `temp_battery_process.py` | Standalone D-Bus battery service (runs as subprocess) |
| `temp_compensation.py` | Trojan L16H-AC temperature compensation (±0.005V/cell/°C) |
| `dbus_monitor.py` | SmartShunt readings, relay state, DVCC settings |
| `alerting.py` | Cerbo buzzer + D-Bus alarm |
| `lock.py` | Atomic file-based lock (prevents concurrent charge + EQ) |
| `aggregate_driver.py` | Start/stop dbus-aggregate-batteries |

## Trojan L16H-AC Charge Parameters

From datasheet, for 24V system (12 cells), at 25°C reference:

| Parameter | Per cell | 24V system | Temperature compensation |
|-----------|----------|------------|--------------------------|
| Bulk/Absorption | 2.47V | 29.64V | ±0.06V/°C |
| Float | 2.25V | 27.00V | ±0.06V/°C |
| Equalise | 2.70V | 32.40V (capped to 31.5V) | ±0.06V/°C |
| C20 capacity | — | 435 Ah | — |

Temperature is read from JK BMS (serialbattery) as proxy — same engine room as FLA bank. If unavailable, base voltage is used.

## Installation

### Prerequisites

- Venus OS on Cerbo GX
- [dbus-serialbattery](https://github.com/Louisvdw/dbus-serialbattery) installed
- [dbus-aggregate-batteries](https://github.com/Dr-Gigavolt/dbus-aggregate-batteries) installed

### Quick Start (from Cerbo shell)

```bash
# Install FLA equalisation service
wget -qO- https://raw.githubusercontent.com/baudje/zwartewater-victron/main/fla-equalisation/install-remote.sh | bash

# Install configs
wget -qO /data/apps/dbus-aggregate-batteries/config.ini https://raw.githubusercontent.com/baudje/zwartewater-victron/main/config/config.ini
/data/apps/dbus-aggregate-batteries/restart.sh
```

### Deploy Updates (from local machine)

```bash
# Shared modules
sshpass -p "$CERBO_ROOT_PASSWORD" scp fla-shared/*.py root@venus.local:/data/apps/fla-shared/

# EQ service
sshpass -p "$CERBO_ROOT_PASSWORD" scp fla-equalisation/fla_equalisation.py fla-equalisation/settings.py \
  root@venus.local:/data/apps/fla-equalisation/

# Charge service
sshpass -p "$CERBO_ROOT_PASSWORD" scp fla-charge/fla_charge.py \
  root@venus.local:/data/apps/fla-charge/

# Restart
sshpass -p "$CERBO_ROOT_PASSWORD" ssh root@venus.local 'svc -d /service/fla-equalisation /service/fla-charge && sleep 2 && svc -u /service/fla-equalisation /service/fla-charge'
```

### Fresh Install

```bash
sshpass -p "$CERBO_ROOT_PASSWORD" scp -r fla-equalisation/ root@venus.local:/tmp/fla-equalisation/
sshpass -p "$CERBO_ROOT_PASSWORD" ssh root@venus.local 'bash /tmp/fla-equalisation/install.sh'

sshpass -p "$CERBO_ROOT_PASSWORD" scp -r fla-charge/ root@venus.local:/tmp/fla-charge/
sshpass -p "$CERBO_ROOT_PASSWORD" ssh root@venus.local 'bash /tmp/fla-charge/install.sh'
```

### Verify

```bash
ssh root@venus.local 'svstat /service/fla-equalisation /service/fla-charge'
# Web dashboards:
# http://venus.local:8088 (equalisation)
# http://venus.local:8089 (charge)
```

## Service Management

```bash
# Stop/start
ssh root@venus.local 'svc -d /service/fla-equalisation /service/fla-charge'
ssh root@venus.local 'svc -u /service/fla-equalisation /service/fla-charge'

# View logs
ssh root@venus.local 'tail -50 /data/log/fla-equalisation.log'
ssh root@venus.local 'tail -50 /data/log/fla-charge.log'

# Trigger manual equalisation (SoC must be >= 95%)
ssh root@venus.local 'dbus -y com.victronenergy.settings /Settings/FlaEqualisation/RunNow SetValue 1'

# Trigger manual charge (AC must be available)
ssh root@venus.local 'dbus -y com.victronenergy.settings /Settings/FlaCharge/RunNow SetValue 1'
```

## Configuration

### EQ Settings (http://venus.local:8088)

| Setting | Default | Description |
|---------|---------|-------------|
| Equalisation voltage | 31.5 V | Base voltage (temperature-compensated) |
| Completion current | 10 A | Current threshold for EQ complete |
| Max duration | 2.5 hrs | Equalisation timeout |
| Float voltage | 27.0 V | Post-EQ float (temperature-compensated) |
| Max delta for reconnect | 1.0 V | Voltage difference before closing relay |
| Voltage match timeout | 4 hrs | Max wait for convergence |
| Interval | 90 days | Days between equalisations |
| Start hour | 14:00 | Earliest start time |
| End hour | 17:00 | Latest start time |
| Min LFP SoC | 95% | Minimum SoC to start (never bypassed) |

### Charge Settings (http://venus.local:8089)

| Setting | Default | Description |
|---------|---------|-------------|
| Trojan SoC trigger | 85% | Start charge when below this |
| LFP SoC transition | 95% | Disconnect LFP when SoC reaches this |
| LFP cell V disconnect | 3.50 V | Alternative disconnect trigger |
| FLA bulk voltage | 29.64 V | Absorption voltage (temperature-compensated) |
| Absorption complete I | 10 A | Current threshold for absorption complete |
| Absorption max hours | 4 hrs | Absorption timeout |
| FLA float voltage | 27.0 V | Post-absorption float (temperature-compensated) |
| Max delta for reconnect | 1.0 V | Voltage difference before closing relay |
| Phase 1 timeout | 8 hrs | Max shared charging phase |

## Safety Features

| Guard | Trigger | Action |
|-------|---------|--------|
| **Crash-safe CVL** | Service crash before relay opens | Temp service at 28.4V (safe for LFPs), not 31.5V. CCL = 60A |
| **Relay re-verification** | Every 30s during high-voltage phases | If relay found closed while CVL > 28.4V: abort immediately |
| **Delta-aware relay** | `finally` block on any abort | Only closes relay if delta < 1V; otherwise alarm, leaves LFPs on Orion |
| **Startup recovery** | Service restart with relay open | Checks delta: auto-closes if < 1V, alarms if >= 1V |
| **Relay verification** | After every relay command | Reads back relay state; aborts if mismatch |
| **Orion failure detection** | LFP voltage drops > 0.5V during EQ/charge | Aborts and reconnects |
| **Temperature compensation** | Every charge cycle | Adjusts voltages per Trojan datasheet (±0.06V/°C from 25°C) |
| **Inrush monitoring** | After relay reconnect | Logs inrush current and delta for calibration |
| **SoC enforcement** | RunNow on EQ service | LFP SoC >= 95% always required (never bypassed) |
| **SmartShunt watchdog** | SmartShunt Trojan goes offline | Immediate abort |
| **Current limiting** | CCL = 60A via temp battery service | DVCC enforces across Quattro + MPPT |
| **Atomic locking** | Both services share operation lock | Prevents concurrent charge + EQ (O_EXCL) |

## Testing

```bash
# Run all tests (138 total)
python3 -m unittest discover -s fla-shared/tests -v      # 83 tests — shared modules
python3 -m unittest discover -s fla-equalisation/tests -v  # 33 tests — EQ service
python3 -m unittest discover -s fla-charge/tests -v        # 22 tests — charge service

# Run a single test file
python3 -m unittest fla-shared/tests/test_relay_control.py -v
```

138 tests covering: all shared modules (relay control, voltage matching, temp compensation, lock, alerting, aggregate driver), EQ scheduling/safety/happy path/Orion failure detection, and charge scheduling/phase transitions/safety guards.

## Files

```
zwartewater-victron/
+-- README.md
+-- CLAUDE.md
+-- config/
|   +-- config.ini                  # Aggregate battery config
|   +-- sb-config.ini               # Serial battery config
+-- docs/
|   +-- design.md                   # Design specification
|   +-- state-machine.md            # Integrated state machine documentation
+-- fla-shared/
|   +-- relay_control.py            # Relay control with safety verification
|   +-- voltage_matching.py         # Convergence loop for reconnection
|   +-- temp_battery.py             # Temp battery subprocess manager
|   +-- temp_battery_process.py     # Standalone D-Bus battery service
|   +-- temp_compensation.py        # Trojan temperature compensation
|   +-- dbus_monitor.py             # SmartShunt + system D-Bus readings
|   +-- alerting.py                 # Buzzer + alarm
|   +-- lock.py                     # Atomic file-based operation lock
|   +-- aggregate_driver.py         # Start/stop aggregate batteries
|   +-- tests/                      # 83 unit tests for shared modules
+-- fla-equalisation/
|   +-- install.sh                  # Venus OS installer
|   +-- install-remote.sh           # Remote installer (wget one-liner)
|   +-- fla_equalisation.py         # Main EQ service
|   +-- dbus_status_service.py      # D-Bus status (com.victronenergy.fla_equalisation)
|   +-- settings.py                 # Venus OS settings integration
|   +-- web_server.py               # Web dashboard (port 8088)
|   +-- service/run                 # Daemontools service runner
|   +-- tests/                      # 33 unit tests
+-- fla-charge/
    +-- install.sh                  # Venus OS installer
    +-- fla_charge.py               # Main charge service
    +-- dbus_status_service.py      # D-Bus status (com.victronenergy.fla_charge)
    +-- settings.py                 # Venus OS settings integration
    +-- web_server.py               # Web dashboard (port 8089)
    +-- service/run                 # Daemontools service runner
    +-- tests/                      # 22 unit tests
```

## References

- Design spec: `docs/design.md`
- State machine: `docs/state-machine.md`
- Trojan L16H-AC datasheet: `L16HAC_Trojan_Data_Sheets.pdf`
- EVE MB31 datasheet: PBRI-MB31-D06-01 (Nov 2023)
- Electrical schema: ScheepsArts, Zwartewater 20250526.pdf
- Upstream aggregate driver: https://github.com/Dr-Gigavolt/dbus-aggregate-batteries
