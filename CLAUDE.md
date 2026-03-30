# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Victron Energy system optimisation for vessel Zwartewater (ENI: 03330190). Three deliverables:

1. **config/** — Optimised `config.ini` for dbus-aggregate-batteries (2× EVE MB31 8s LFP + JK BMS)
2. **fla-equalisation/** — Automated Trojan L16H-AC equalisation service on Venus OS (port 8088)
3. **fla-charge/** — Automated Trojan FLA bulk+absorption charge service on Venus OS (port 8089)

## System Overview

- **Cerbo GX** at `venus.local` (root/Zwartewater)
- **Quattro II** 24V 5000/120, ESS/DVCC controlled
- **LFP bank**: 2× 8-cell EVE MB31 (314Ah each), JK BMS, via dbus-serialbattery
- **FLA bank**: 4× Trojan L16H-AC 6V in series (24V, 435Ah)
- **SmartShunt LFP**: instance 277 (10R3, VE.Direct2)
- **SmartShunt Trojan**: instance 279 (10R1, VE.Direct1)
- **MPPT**: SmartSolar 150/60 (10MPPT6, VE.Direct3), 860Wp PV
- **Cerbo relay 2**: controls LFP direct connection + Orion DC-DC activation
- **Orion DC-DC** (10ORION4): charges LFPs at safe voltage when relay 2 opens

## Architecture

### State Machine (Shared by Both Services)

Both FLA services follow the same orchestration pattern:

```
Normal:       aggregate/99 active, relay closed, CVL 28.4V
    ↓ register temp battery at 28.4V (SAFE — aggregate still wins by lower instance)
    ↓ stop aggregate driver
Transitioning: temp/100 at 28.4V, relay closed (still safe for both banks)
    ↓ open relay 2 (LFPs disconnect, Orion activates)
Disconnected: temp/100 at 28.4V, relay open, LFPs on Orion
    ↓ raise CVL to target (31.5V EQ or 29.64V absorption)
Charging:     temp/100 at high voltage, relay open, only FLA charging
    ↓ lower CVL to 27.0V float, wait for delta < 1V
Reconnecting: close relay, restart aggregate
```

**Crash safety**: at every stage, if the service dies the system is in a safe state — temp battery defaults to 28.4V (safe for LFPs), and if relay was open the LFPs stay on Orion.

### Subprocess Isolation Pattern

`temp_battery_process.py` runs as a separate subprocess because the temp D-Bus battery service needs root path `/` which conflicts with the main status service. Parent communicates voltage changes via file `/tmp/fla_eq_cvl` (polled every 2s by subprocess).

### Shared Modules (`fla-shared/`)

| Module | Purpose |
|--------|---------|
| `relay_control.py` | Open/close relay 2 with read-back verification, delta-aware cleanup, startup recovery |
| `voltage_matching.py` | Convergence loop: polls delta every 30s, waits for < 1V |
| `temp_battery.py` | Launches temp battery subprocess |
| `temp_battery_process.py` | Standalone D-Bus battery service, reads SmartShunt Trojan, watches CVL file |
| `temp_compensation.py` | Trojan temp compensation: ±0.005V/cell/°C (±0.06V/°C for 12 cells) |
| `dbus_monitor.py` | Reads SmartShunt voltages/currents, SoC, relay state, DVCC settings |
| `alerting.py` | Cerbo buzzer activation, D-Bus alarm path |
| `lock.py` | Atomic file lock (`O_EXCL`) preventing concurrent charge + EQ |
| `aggregate_driver.py` | Start/stop dbus-aggregate-batteries via `svc -u/-d` |

### Service-Specific Components

Each service (`fla-equalisation/`, `fla-charge/`) contains:
- `fla_*.py` — main state machine (entry point)
- `settings.py` — D-Bus settings registration (accessible from Cerbo GUI/VRM)
- `dbus_status_service.py` — publishes state, voltages, time remaining to D-Bus
- `web_server.py` — dashboard UI (8088 for EQ, 8089 for charge)
- `install.sh` — Venus OS installer (daemontools service + rc.local)
- `service/run` — daemontools runner script

## Key Design Decisions

- JK BMS current is inaccurate → use `CURRENT_FROM_VICTRON = True` with SmartShunt LFP
- Daily charge at 3.55V/cell, balancing at 3.60V/cell every 14 days
- FLA equalisation at 31.5V every 90 days (Trojan datasheet max 32.4V, capped for safety), CCL 60A
- FLA absorption at 29.64V when Trojan SoC < 85% (Trojan datasheet: 2.47V/cell × 12), CCL 60A
- Voltage matching (delta < 1V) required before reconnecting LFP bank (limits inrush current)
- Relay re-verified every 30s during high-voltage phases to catch external closes
- Delta-aware cleanup: `finally` block only closes relay if delta < 1V; otherwise alarms and leaves LFPs on Orion
- Temperature compensation adjusts all target voltages per Trojan datasheet (reads from JK BMS sensor)
- All settings exposed via Venus OS D-Bus settings and web UIs

## Testing

```bash
# Run all tests (138 total)
python3 -m unittest discover -s fla-shared/tests -v      # 83 tests — shared modules
python3 -m unittest discover -s fla-equalisation/tests -v  # 33 tests — EQ service
python3 -m unittest discover -s fla-charge/tests -v        # 22 tests — charge service

# Run a single test file
python3 -m unittest fla-shared/tests/test_relay_control.py -v

# Run a single test
python3 -m unittest fla-equalisation.tests.test_fla_equalisation.TestScheduling.test_disabled_returns_false -v
```

Tests mock D-Bus calls — no Venus OS required. Shared test helpers in `fla-shared/tests/helpers.py` (MockMonitor, MockStatus, dbus_mock_setup).

## Deployment

### Config (Part A)
```bash
scp config/config.ini root@venus.local:/data/apps/dbus-aggregate-batteries/config.ini
ssh root@venus.local /data/apps/dbus-aggregate-batteries/restart.sh
```

### FLA Services (deploy via sshpass)
```bash
# Shared modules
sshpass -p 'Zwartewater' scp fla-shared/*.py root@venus.local:/data/apps/fla-shared/

# EQ service
sshpass -p 'Zwartewater' scp fla-equalisation/fla_equalisation.py fla-equalisation/settings.py \
  root@venus.local:/data/apps/fla-equalisation/

# Charge service
sshpass -p 'Zwartewater' scp fla-charge/fla_charge.py \
  root@venus.local:/data/apps/fla-charge/

# Restart both services
sshpass -p 'Zwartewater' ssh root@venus.local 'svc -d /service/fla-equalisation /service/fla-charge && sleep 2 && svc -u /service/fla-equalisation /service/fla-charge'
```

### Fresh Install
```bash
sshpass -p 'Zwartewater' ssh root@venus.local 'cd /data/apps/fla-equalisation && bash install.sh'
sshpass -p 'Zwartewater' ssh root@venus.local 'cd /data/apps/fla-charge && bash install.sh'
```

### Checking Logs on Cerbo
```bash
sshpass -p 'Zwartewater' ssh root@venus.local 'tail -100 /var/log/fla-equalisation/current'
sshpass -p 'Zwartewater' ssh root@venus.local 'tail -100 /var/log/fla-charge/current'
```

## References

- Design spec: `docs/design.md`
- State machine doc: `docs/state-machine.md`
- Electrical schema: ScheepsArts, Zwartewater 20250526.pdf
- EVE MB31 datasheet: PBRI-MB31-D06-01 (Nov 2023)
- Upstream driver: https://github.com/Dr-Gigavolt/dbus-aggregate-batteries
