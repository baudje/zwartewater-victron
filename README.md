# Zwartewater Victron

Victron Energy system optimisation for vessel **Zwartewater** (ENI: 03330190).

## System Overview

| Component | Model | Details |
|-----------|-------|---------|
| GX device | Cerbo GX | `venus.local` |
| Inverter/charger | Quattro II 24V 5000/120 | ESS/DVCC controlled |
| LFP batteries | 2× EVE MB31 8s (314Ah each) | JK BMS, via dbus-serialbattery |
| FLA batteries | 4× Trojan L16H-AC 6V | Series (24V, 435Ah) |
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
**Equalisation mode:** Relay open, Orion charges LFPs at safe voltage independently.

## What's in This Repo

### 1. Aggregate Battery Config (`config/config.ini`)

Optimised [dbus-aggregate-batteries](https://github.com/Dr-Gigavolt/dbus-aggregate-batteries) configuration for 2× EVE MB31 8-cell LFP with JK BMS.

**Key settings:**
- `CURRENT_FROM_VICTRON = True` — uses SmartShunt LFP instead of inaccurate JK BMS current
- `OWN_CHARGE_PARAMETERS = True` — aggregate driver controls CVL/CCL/DCL
- Daily charge: 3.55V/cell (28.4V), balancing: 3.60V/cell every 14 days
- Cell protection: max 3.65V, min 2.80V (EVE MB31 datasheet limits)
- Current limits: 120A charge (Quattro limit), 150A discharge

### 2. Serial Battery Config (`config/sb-config.ini`)

Aligned [dbus-serialbattery](https://github.com/Louisvdw/dbus-serialbattery) configuration. Ensures serialbattery cell limits match the aggregate driver settings and don't conflict.

**Key settings:**
- `MAX_CELL_VOLTAGE = 3.650` — matches aggregate driver ceiling
- Temperature-based charge derating for EVE MB31 (0% at 0°C, 80% at 10°C)
- JK BMS addresses: 0x01, 0x02

### 3. FLA Equalisation Service (`fla-equalisation/`)

Standalone Python service for automated Trojan L16H-AC equalisation. Runs as a persistent daemontools service on Venus OS.

**Features:**
- Periodic equalisation every 90 days (configurable)
- Automated sequence: stop aggregate driver → register temp battery service → open relay → equalise at 31.2V → voltage match → reconnect → restart driver
- Orion DC-DC keeps LFPs charged at safe voltage during equalisation
- Voltage matching (delta < 1V) before reconnecting to limit inrush current
- Safety guards: SoC check, relay verification, SmartShunt responsiveness, timeouts
- Alerting: Cerbo buzzer + VRM notification + D-Bus alarm on failure
- Web dashboard at **http://venus.local:8088** with live status and editable settings
- All settings configurable via web UI or D-Bus

**Web Dashboard:**
- Live state, voltages, and delta
- Editable settings (equalisation voltage, duration, interval, etc.)
- "Run Now" button (still enforces SoC >= 95%)
- Auto-refreshes every 5 seconds

## Installation

### Prerequisites

- Venus OS on Cerbo GX
- [dbus-serialbattery](https://github.com/Louisvdw/dbus-serialbattery) installed
- [dbus-aggregate-batteries](https://github.com/Dr-Gigavolt/dbus-aggregate-batteries) installed

### Quick Start (from Cerbo shell)

```bash
# 1. Install FLA equalisation service
wget -qO- https://raw.githubusercontent.com/baudje/zwartewater-victron/main/fla-equalisation/install-remote.sh | bash

# 2. Install aggregate battery config
wget -qO /data/apps/dbus-aggregate-batteries/config.ini https://raw.githubusercontent.com/baudje/zwartewater-victron/main/config/config.ini
/data/apps/dbus-aggregate-batteries/restart.sh

# 3. Install serial battery config
wget -qO /data/apps/dbus-serialbattery/config.ini https://raw.githubusercontent.com/baudje/zwartewater-victron/main/config/sb-config.ini
svc -d /service/dbus-serialbattery.*; sleep 3; svc -u /service/dbus-serialbattery.*

# 4. Open web dashboard to configure settings
# http://venus.local:8088
```

### Alternative: Deploy via SCP (from local machine)

```bash
# Aggregate battery config
scp config/config.ini root@venus.local:/data/apps/dbus-aggregate-batteries/config.ini
ssh root@venus.local /data/apps/dbus-aggregate-batteries/restart.sh

# Serial battery config
scp config/sb-config.ini root@venus.local:/data/apps/dbus-serialbattery/config.ini
ssh root@venus.local 'svc -d /service/dbus-serialbattery.*; sleep 3; svc -u /service/dbus-serialbattery.*'
```

### Deploy FLA Equalisation Service

**One-liner install (from Cerbo shell):**

```bash
wget -qO- https://raw.githubusercontent.com/baudje/zwartewater-victron/main/fla-equalisation/install-remote.sh | bash
```

**Or manually via SCP:**

```bash
scp -r fla-equalisation/ root@venus.local:/tmp/fla-equalisation/
ssh root@venus.local 'bash /tmp/fla-equalisation/install.sh'
```

The installer:
- Copies files to `/data/apps/fla-equalisation/`
- Symlinks velib_python from dbus-aggregate-batteries
- Creates daemontools service at `/service/fla-equalisation`
- Adds auto-start entry to `/data/rc.local` (survives firmware upgrades)
- Registers settings in Venus OS localsettings

### Verify

```bash
# Check service is running
ssh root@venus.local 'svstat /service/fla-equalisation'

# Check D-Bus registration
ssh root@venus.local 'dbus -y | grep fla_equalisation'

# Open web dashboard
open http://venus.local:8088
```

## Service Management

```bash
# Stop
ssh root@venus.local 'svc -d /service/fla-equalisation'

# Start
ssh root@venus.local 'svc -u /service/fla-equalisation'

# View logs
ssh root@venus.local 'tail -50 /data/log/fla-equalisation.log'

# Trigger manual equalisation (SoC must be >= 95%)
ssh root@venus.local 'dbus -y com.victronenergy.settings /Settings/FlaEqualisation/RunNow SetValue 1'
```

## Equalisation Sequence

```
1. Pre-checks: enabled, interval elapsed, afternoon window, LFP SoC >= 95%
2. Stop dbus-aggregate-batteries
3. Register temporary battery service (CVL = 31.2V)
4. Open Cerbo relay 2 → LFP direct path disconnected, Orion activates
5. Quattro charges Trojans at 31.2V
6. Monitor SmartShunt Trojan: complete when current < 8A or 2.5hr timeout
7. Reduce CVL to float (27.6V)
8. Wait for |V_trojan - V_lfp| < 1V (max 4 hours)
9. Close relay 2 → LFP reconnected, Orion off
10. Deregister temporary service
11. Restart dbus-aggregate-batteries
```

## Configuration

All settings are editable via the web dashboard at `http://venus.local:8088` or via D-Bus:

| Setting | Default | Description |
|---------|---------|-------------|
| Equalisation voltage | 31.2 V | Trojan charge voltage |
| Completion current | 8 A | Current threshold for completion |
| Max duration | 2.5 hrs | Equalisation timeout |
| Float voltage | 27.6 V | Post-equalisation float |
| Max delta for reconnect | 1.0 V | Voltage difference before closing relay |
| Voltage match timeout | 4 hrs | Max wait for convergence |
| Interval | 90 days | Days between equalisations |
| Start hour | 14:00 | Earliest start time |
| End hour | 17:00 | Latest start time |
| Min LFP SoC | 95% | Minimum SoC to start |
| Enabled | Yes | Enable/disable scheduling |
| Run Now | — | Manual trigger (web UI button) |

## Safety Features

The service includes multiple safety guards to protect batteries on an unattended vessel:

| Guard | Trigger | Action |
|-------|---------|--------|
| **Crash-safe CVL** | Service crash before relay opens | Temp service registers at 28.4V (safe for LFPs), only raised to 31.2V after relay confirmed open |
| **Delta-aware relay** | `finally` block on any abort | Only closes relay if voltage delta < 2V; otherwise raises alarm, leaves LFPs on Orion |
| **Startup recovery** | Service restart with relay open | Checks delta: auto-closes if < 2V, alarms if > 2V |
| **Relay verification** | After every relay command | Reads back relay state; aborts if command didn't execute |
| **Orion failure detection** | LFP voltage drops > 0.5V during EQ | Aborts and reconnects |
| **Inrush monitoring** | After relay reconnect | Logs inrush current and delta for calibration |
| **SoC enforcement** | RunNow bypasses interval, not SoC | LFP SoC >= 95% always required |
| **SmartShunt watchdog** | SmartShunt Trojan goes offline | Immediate abort in both EQ and voltage matching |
| **Current monitoring** | Trojan current > 60A during EQ | Warning logged (dynamo/MPPT contribution) |
| **Bounds validation** | Web UI settings changes | Values checked against min/max before writing |

## Testing

Run the test suite from the repo root:

```bash
python3 -m unittest fla-equalisation/tests/test_fla_equalisation.py -v
```

26 tests covering: scheduling logic, safety guards, crash safety, inrush protection, startup recovery, and settings validation.

## EVE MB31 Key Specs

| Parameter | Value |
|-----------|-------|
| Nominal capacity | 314 Ah |
| Nominal voltage | 3.2 V/cell |
| Max charge voltage | 3.65 V/cell |
| Absolute max | 3.8 V/cell |
| Discharge cutoff | 2.5 V (>0°C) |
| Cycle life | 8000 cycles @ 70% SOH |
| Charge rate at 8°C | 0.3P (~94A/battery) |

## Files

```
zwartewater-victron/
├── README.md
├── CLAUDE.md
├── config/
│   ├── config.ini              # Aggregate battery config
│   └── sb-config.ini           # Serial battery config
├── docs/
│   ├── design.md               # Design specification
│   └── superpowers/plans/      # Implementation plan
└── fla-equalisation/
    ├── install.sh              # Venus OS installer
    ├── install-remote.sh       # Remote installer (wget one-liner)
    ├── fla_equalisation.py     # Main service (persistent, GLib loop)
    ├── dbus_battery_service.py # Temporary battery service for DVCC
    ├── dbus_monitor.py         # SmartShunt + relay D-Bus readings
    ├── dbus_status_service.py  # Status service for D-Bus
    ├── settings.py             # Venus OS settings integration
    ├── alerting.py             # Buzzer + alarm
    ├── web_server.py           # Web dashboard (port 8088)
    ├── service/
    │   └── run                 # Daemontools service runner
    └── tests/
        └── test_fla_equalisation.py  # 26 unit tests
```
