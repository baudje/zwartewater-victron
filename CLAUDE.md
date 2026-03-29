# CLAUDE.md

## What This Is

Victron Energy system optimisation for vessel Zwartewater (ENI: 03330190). Three deliverables:

1. **config/** — Optimised `config.ini` for dbus-aggregate-batteries (2x EVE MB31 8s LFP + JK BMS)
2. **fla-equalisation/** — Automated Trojan L16H-AC equalisation service on Venus OS (port 8088)
3. **fla-charge/** — Automated Trojan FLA bulk+absorption charge service on Venus OS (port 8089)

## System Overview

- **Cerbo GX** at `venus.local` (root/Zwartewater)
- **Quattro II** 24V 5000/120, ESS/DVCC controlled
- **LFP bank**: 2x 8-cell EVE MB31 (314Ah each), JK BMS, via dbus-serialbattery
- **FLA bank**: 4x Trojan L16H-AC 6V in series (24V, 435Ah)
- **SmartShunt LFP**: instance 277 (10R3, VE.Direct2)
- **SmartShunt Trojan**: instance 279 (10R1, VE.Direct1)
- **MPPT**: SmartSolar 150/60 (10MPPT6, VE.Direct3), 860Wp PV
- **Cerbo relay 2**: controls LFP direct connection + Orion DC-DC activation
- **Orion DC-DC** (10ORION4): charges LFPs at safe voltage when relay 2 opens

## Key Design Decisions

- JK BMS current is inaccurate → use `CURRENT_FROM_VICTRON = True` with SmartShunt LFP
- Daily charge at 3.55V/cell, balancing at 3.60V/cell every 14 days
- FLA equalisation at 31.5V every 90 days (Trojan datasheet: 32.4V, capped for safety), CCL 60A
- FLA absorption at 29.64V when Trojan SoC < 85% (Trojan datasheet: 2.47V/cell × 12), CCL 60A
- Both require stopping aggregate driver and registering temporary D-Bus battery service
- Voltage matching (delta < 1V) required before reconnecting LFP bank (limits inrush current)
- All settings exposed via Venus OS D-Bus settings and web UIs (ports 8088/8089)

## Deployment

### config.ini (Part A)
```bash
scp config/config.ini root@venus.local:/data/apps/dbus-aggregate-batteries/config.ini
ssh root@venus.local /data/apps/dbus-aggregate-batteries/restart.sh
```

### FLA services (deploy via sshpass)
```bash
# Shared modules
sshpass -p 'Zwartewater' scp fla-shared/*.py root@venus.local:/data/apps/fla-shared/

# EQ service
sshpass -p 'Zwartewater' scp fla-equalisation/fla_equalisation.py fla-equalisation/settings.py \
  root@venus.local:/data/apps/fla-equalisation/

# Charge service
sshpass -p 'Zwartewater' scp fla-charge/fla_charge.py \
  root@venus.local:/data/apps/fla-charge/

# Restart
sshpass -p 'Zwartewater' ssh root@venus.local 'svc -d /service/fla-equalisation /service/fla-charge && sleep 2 && svc -u /service/fla-equalisation /service/fla-charge'
```

### Fresh install
```bash
# Run install.sh on the Cerbo (copies files, creates daemontools service, adds to rc.local)
sshpass -p 'Zwartewater' ssh root@venus.local 'cd /data/apps/fla-equalisation && bash install.sh'
sshpass -p 'Zwartewater' ssh root@venus.local 'cd /data/apps/fla-charge && bash install.sh'
```

## References

- Design spec: `docs/design.md`
- Electrical schema: ScheepsArts, Zwartewater 20250526.pdf
- EVE MB31 datasheet: PBRI-MB31-D06-01 (Nov 2023)
- Upstream driver: https://github.com/Dr-Gigavolt/dbus-aggregate-batteries
