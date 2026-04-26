# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Victron Energy system optimisation for vessel Zwartewater (ENI: 03330190). Manages a hybrid parallel LFP + FLA battery system where LFP handles daily cycling and FLA acts as backup/buffer. Three deliverables:

1. **config/** — Optimised `config.ini` for dbus-aggregate-batteries + `sb-config.ini` for dbus-serialbattery
2. **fla-equalisation/** — Automated Trojan L16H-AC equalisation service on Venus OS (port 8088)
3. **fla-charge/** — Automated Trojan FLA bulk+absorption charge service on Venus OS (port 8089)

## System Overview

- **Cerbo GX** at `venus.local` (root/<device password>)
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
    ↓ restart systemcalc (forces discovery of temp service)
    ↓ switch BatteryService + BmsInstance to temp/100
Transitioning: temp/100 at 28.4V, relay closed (still safe for both banks)
    ↓ open relay 2 (LFPs disconnect, Orion activates)
Disconnected: temp/100 at 28.4V, relay open, LFPs on Orion
    ↓ raise CVL to target (31.5V EQ or 29.64V absorption)
Charging:     temp/100 at high voltage, relay open, only FLA charging
    ↓ lower CVL to 27.0V float, wait for delta <= 1V
Reconnecting: close relay, restart aggregate, restore BmsInstance
```

**Crash safety**: at every stage, if the service dies the system is in a safe state — temp battery defaults to 28.4V (safe for LFPs), and if relay was open the LFPs stay on Orion.

### Subprocess Isolation Pattern

`temp_battery_process.py` runs as a separate subprocess because the temp D-Bus battery service needs root path `/` which conflicts with the main status service. Parent communicates voltage changes via file `/tmp/fla_eq_cvl` (polled every 2s by subprocess).

### D-Bus Service Names

- Status services register as `com.victronenergy.fla_equalisation` / `com.victronenergy.fla_charge` (custom prefix — not visible on Device List but avoids introspection issues with `battery` or `genset` prefixes)
- Web dashboards at ports 8088/8089 are the primary monitoring interface (Run Now + Abort buttons)

### Shared Modules (`fla-shared/`)

| Module | Purpose |
|--------|---------|
| `relay_control.py` | Open/close relay 2 with read-back verification, delta-aware cleanup, startup recovery |
| `voltage_matching.py` | Sets CVL to float then polls delta every 30s; relay closes when <= 1V |
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

### Duplication Between Services

The DVCC handoff sequence (temp battery registration → aggregate stop → systemcalc restart → BMS switch → relay open), the finally/cleanup block, `_check()` + worker thread pattern, `web_server.py` infrastructure, and `settings.py` base methods are duplicated between fla-charge and fla-equalisation. This is intentional — the two services have different state enums, scheduling logic, and charging phases, so the cost of extracting shared abstractions outweighs the benefit for exactly two consumers. **When modifying any of these shared patterns, always apply the same change to both services.**

## Key Design Decisions

- JK BMS current is inaccurate → use `CURRENT_FROM_VICTRON = True` with SmartShunt LFP
- `OWN_SOC = False` — serialbattery SoC resets at each daily charge cycle; own counter drifts
- `OWN_CHARGE_PARAMETERS = False` — serialbattery controls Bulk/Absorption/Float transitions
- `KEEP_MAX_CVL = True` — when one pack wants Float and the other Absorption, aggregate picks the higher CVL so the slower pack can finish. With False, whichever pack tail-currented first dragged the bus to 27V and cut the other short
- Daily charge at 3.55V/cell, balancing at 3.60V/cell every 14 days
- `SWITCH_TO_FLOAT_WAIT_FOR_SEC = 1800` — hold 28.4V absorption for 30 min after tail current before dropping to float. Default 5 min was too brief for both packs' JK BMS full-charge detection to fire and for SmartShunt 277 charged-detection to sync
- `SWITCH_TO_BULK_SOC_THRESHOLD = 95` — rebulk at 95% SoC (default 80% left the pack drifting 80-95% without any daily absorption opportunity, missing SoC-reset events entirely)
- JK BMS `SOC-100% Volt` set to 3.545V and `Cell OVPR` lowered correspondingly (JK requires OVPR < SOC-100%). App-side settings on each JK, not in config.ini. JK's internal SoC only resets to 100% when every cell reaches this threshold; default 3.595V was unreachable at our 3.55V daily charge target, so SoC never synced between 14-day balance cycles
- `SMARTSHUNT_AS_BATTERY_CURRENT = True` (Zwartewater patch to dbus-aggregate-batteries) — aggregate uses SmartShunt LFP (instance 277) directly as the bank current. Required because (a) upstream `settings.py` parses `USE_SMARTSHUNTS` as bool only, silently coercing `[277]` to `False`, and (b) upstream's formula `Current = Quattro + MPPT + shunts` would double-count what flows into the LFP bank (the shunt already includes Quattro+MPPT contributions). With the patch, alternator charge via Orion DC-DC is finally visible to aggregate (previously invisible because Orion registers as `com.victronenergy.dcdc`, which the driver doesn't sum). See `patches/dbus-aggregate-batteries/README.md`
- FLA equalisation at 31.5V every 90 days (Trojan datasheet max 32.4V, capped for safety), CCL 60A
- FLA absorption at 29.64V when Trojan SoC < 85% (Trojan datasheet: 2.47V/cell × 12), CCL 60A
- Voltage matching (delta <= 1V) required before reconnecting LFP bank (limits inrush current)
- Float reduction and voltage matching are one phase: `wait_for_match()` sets CVL to float then checks delta
- Relay re-verified every 30s during high-voltage phases using temp-compensated CVL
- DVCC uses `BmsInstance` (not `BatteryService`) to select which BMS provides CVL/CCL/DCL
- systemcalc must be restarted after registering temp battery service (doesn't discover services registered after boot)
- Web dashboards have Abort button (visible during active operations, triggers safe cleanup via finally block)
- Delta-aware cleanup: `finally` block only closes relay if delta < 1V; otherwise alarms and leaves LFPs on Orion
- Temperature compensation adjusts all target voltages per Trojan datasheet (reads from JK BMS sensor)
- All settings exposed via Venus OS D-Bus settings and web UIs

## Testing

```bash
# Run all tests (137 total)
python3 -m unittest discover -s fla-shared/tests -v      # 82 tests — shared modules
python3 -m unittest discover -s fla-equalisation/tests -v  # 33 tests — EQ service
python3 -m unittest discover -s fla-charge/tests -v        # 22 tests — charge service

# Run a single test file
python3 -m unittest fla-shared/tests/test_relay_control.py -v

# Run a single test
python3 -m unittest fla-equalisation.tests.test_fla_equalisation.TestScheduling.test_disabled_returns_false -v
```

Tests mock D-Bus calls — no Venus OS required. Shared test helpers in `fla-shared/tests/helpers.py` (MockMonitor, MockStatus, dbus_mock_setup). All service test files import from helpers — do not duplicate mock classes.

## Deployment

### Config
```bash
sshpass -p "$CERBO_ROOT_PASSWORD" scp config/config.ini root@venus.local:/data/apps/dbus-aggregate-batteries/config.ini
sshpass -p "$CERBO_ROOT_PASSWORD" ssh root@venus.local '/data/apps/dbus-aggregate-batteries/restart.sh'
```

### Aggregate driver patches (deploy after upstream updates)
```bash
sshpass -p "$CERBO_ROOT_PASSWORD" scp \
  patches/dbus-aggregate-batteries/settings.py \
  patches/dbus-aggregate-batteries/dbus-aggregate-batteries.py \
  patches/dbus-aggregate-batteries/config.default.ini \
  root@venus.local:/data/apps/dbus-aggregate-batteries/
sshpass -p "$CERBO_ROOT_PASSWORD" ssh root@venus.local '/data/apps/dbus-aggregate-batteries/restart.sh'
```

### FLA Services (deploy via sshpass)
```bash
# Shared modules
sshpass -p "$CERBO_ROOT_PASSWORD" scp fla-shared/*.py root@venus.local:/data/apps/fla-shared/

# EQ service
sshpass -p "$CERBO_ROOT_PASSWORD" scp fla-equalisation/fla_equalisation.py fla-equalisation/settings.py \
  fla-equalisation/dbus_status_service.py fla-equalisation/web_server.py \
  root@venus.local:/data/apps/fla-equalisation/

# Charge service
sshpass -p "$CERBO_ROOT_PASSWORD" scp fla-charge/fla_charge.py fla-charge/settings.py \
  fla-charge/dbus_status_service.py fla-charge/web_server.py \
  root@venus.local:/data/apps/fla-charge/

# Restart both services
sshpass -p "$CERBO_ROOT_PASSWORD" ssh root@venus.local 'svc -d /service/fla-equalisation /service/fla-charge && sleep 2 && svc -u /service/fla-equalisation /service/fla-charge'
```

### Fresh Install
```bash
sshpass -p "$CERBO_ROOT_PASSWORD" ssh root@venus.local 'cd /data/apps/fla-equalisation && bash install.sh'
sshpass -p "$CERBO_ROOT_PASSWORD" ssh root@venus.local 'cd /data/apps/fla-charge && bash install.sh'
```

### Checking Logs on Cerbo
```bash
sshpass -p "$CERBO_ROOT_PASSWORD" ssh root@venus.local 'tail -100 /data/log/fla-equalisation.log'
sshpass -p "$CERBO_ROOT_PASSWORD" ssh root@venus.local 'tail -100 /data/log/fla-charge.log'
```

## References

- Design spec: `docs/design.md`
- State machine doc: `docs/state-machine.md`
- Voltage diagrams: `docs/voltage-profiles.png`, `docs/charge-sequences.png` (regenerate with `python3 docs/generate_diagrams.py`)
- Electrical schema: ScheepsArts, Zwartewater 20250526.pdf
- EVE MB31 datasheet: PBRI-MB31-D06-01 (Nov 2023)
- Upstream driver: https://github.com/Dr-Gigavolt/dbus-aggregate-batteries
