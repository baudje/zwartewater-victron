# CLAUDE.md

## What This Is

Victron Energy system optimisation for vessel Zwartewater (ENI: 03330190). Two deliverables:

1. **config/** — Optimised `config.ini` for dbus-aggregate-batteries (2x EVE MB31 8s LFP + JK BMS)
2. **fla-equalisation/** — Standalone Python script for automated Trojan L16H-AC equalisation on Venus OS

## System Overview

- **Cerbo GX** at `venus.local` (root/<device password>)
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
- FLA equalisation at 31.2V every 90 days, requires stopping aggregate driver and registering temporary D-Bus battery service
- Voltage matching (delta < 1V) required before reconnecting LFP bank
- All equalisation settings exposed via Venus OS D-Bus settings (GUI v2 compatible)

## Deployment

### config.ini (Part A)
```bash
scp config/config.ini root@venus.local:/data/apps/dbus-aggregate-batteries/config.ini
ssh root@venus.local /data/apps/dbus-aggregate-batteries/restart.sh
```

### fla-equalisation (Part C)
```bash
scp -r fla-equalisation/ root@venus.local:/data/apps/fla-equalisation/
ssh root@venus.local "chmod +x /data/apps/fla-equalisation/fla-equalisation.py"
# Install cron
ssh root@venus.local "echo '0 * * * * root /usr/bin/python3 /data/apps/fla-equalisation/fla-equalisation.py' > /etc/cron.d/fla-equalisation"
```

## References

- Design spec: `docs/design.md`
- Electrical schema: ScheepsArts, Zwartewater 20250526.pdf
- EVE MB31 datasheet: PBRI-MB31-D06-01 (Nov 2023)
- Upstream driver: https://github.com/Dr-Gigavolt/dbus-aggregate-batteries
