# JK BMS Settings Reference — Zwartewater LFP bank

Authoritative, app-side settings for the two **JK Inverter/ESS BMS** units (PB model)
that manage the LFP bank. These live in each BMS's own memory (set via the JK app /
JK PC software over Bluetooth or USB) — they are **not** in `config.ini` or
`sb-config.ini`, so they are lost on a firmware flash and must be restored from here.

**Bank:** 2 × EVE MB31, 8 cells in series (8S), 314 Ah per pack, one JK BMS per pack,
both packs in parallel feeding `dbus-aggregate-batteries` via `dbus-serialbattery`
(`BMS_TYPE = Jkbms_pb`, `BATTERY_ADDRESSES = 0x01, 0x02`).

## Two rules for a parallel pair

1. **RS485 / device address must differ:** Pack A = `1` (0x01), Pack B = `2` (0x02).
   This is what `BATTERY_ADDRESSES = 0x01, 0x02` in `sb-config.ini` expects. If both
   default to the same address after a flash, serialbattery sees only one pack.
2. **Every other setting must be identical on both packs.** A past mismatch
   (SOC-100% at 3.545 on one, 3.540 on the other) caused asymmetric SoC behaviour.

Also: **both packs must run the same JK firmware version** (target **15.41**, the last
release for the V15 hardware line — see firmware note below). Verify in the app.

## Fastest safe way to configure the second pack

If one pack still holds a good config, **export its settings to a file and import to
the other pack** in the JK app (save-to-file / read-from-file). Far safer than
hand-entering every value twice. Only hand-enter from the table below if both were wiped.

## Settings

### Battery basics
| Setting | Value |
|---|---|
| Cell count (strings) | 8 |
| Capacity | 314 Ah |
| Chemistry | LiFePO4 (3.2 V nominal) |

### Cell voltage protection (safety-critical)
| Setting | Value | Note |
|---|---|---|
| Cell OVP (over-voltage protection) | 3.650 V | EVE MB31 absolute max |
| Cell OVPR (OVP recovery) | 3.400 V | must be **< SOC-100% Volt** (JK firmware rule) |
| Cell UVP (under-voltage protection) | 2.500 V | EVE discharge cutoff; a backstop below serialbattery's 2.8 V working floor |
| Cell UVPR (UVP recovery) | 2.900 V | |

### Charge control / SOC-100% latch
| Setting | Value | Note |
|---|---|---|
| Cell RCV (Request Charge Voltage / absorption) | 3.550 V | daily charge target |
| Cell RFV (Request Float Voltage) | 3.375 V | matches `FLOAT_CELL_VOLTAGE` |
| **SOC-100% Volt** | 3.500 V | ≥ 0.040 V below RCV (community rule); reliably reached at a 3.55 V charge. **Identical on both packs.** |

### Balancer
| Setting | Value |
|---|---|
| Active balance | ON |
| Balance start voltage | 3.400 V |
| Balance trigger Δ (start when cell diff >) | 0.003 V |
| Balance during charging | ON |

### Current protection (backstop only)
The working limits — **65 A charge / 70 A discharge per pack** — are enforced upstream
by serialbattery (`MAX_BATTERY_CHARGE_CURRENT` / `MAX_BATTERY_DISCHARGE_CURRENT`), not
the JK. Set the JK's own OCP to the **BMS's rated continuous current** (see the unit
label) as a hardware safety backstop, not to the working limit.

### Temperature protection
| Setting | Value | Note |
|---|---|---|
| Charge over-temp | 55 °C | |
| Charge under-temp | 0 °C | never charge LFP below freezing |
| Discharge over-temp | 55 °C | |
| Discharge under-temp | −20 °C | |
| MOSFET over-temp | ~90–100 °C | |

### System
- Device / RS485 address: **1** and **2** (distinct — see rule 1).
- Charge + Discharge MOSFETs: **ON**.

## The SOC-100% latch and firmware 15.38 (why these values)

The JK Inverter BMS latches its internal SoC to 100% only via a **timer + voltage check
at the end of its own absorption phase**: when the RCV/absorption timer expires it checks
whether pack voltage is still ≥ the SOC-100% Volt setting; if not, it loops back to
absorption and never latches.

**Firmware 15.38 had a documented bug** where this failed specifically when *an external
controller drives the charge voltage* — exactly this rig (the Quattro's CVL is set by
DVCC/aggregate/serialbattery, not by the JK's RCV). The JK's absorption timer and the
external charger's voltage schedule (`SWITCH_TO_FLOAT_WAIT_FOR_SEC`) are unsynchronised,
so the pack voltage often collapsed toward float before the JK's check fired → SoC stuck
in the 80s while SmartShunt 277 correctly read 100%. Only the 14-day balance cycle
(3.60 V/cell, high + sustained) reliably latched.

The JK changelog fix reads: *"Optimized the logic for SOC-100% reset to solve the bug
that some inverters use SOC to control charging."* → **update to 15.41.** With 15.41 and
SOC-100% at 3.500 V (a full 50 mV below the 3.55 V daily target), the daily charge should
latch 100%. **Verify on the next full charge and tune if needed** — this is the one value
worth confirming empirically, not trusting blindly.

Do **not** re-introduce the earlier "raise daily CVL to 28.5 V" workaround: that chased a
voltage theory that turned out wrong (the cause was firmware, not a few mV of headroom),
and 28.5 V pushed cells into the CVCM current-taper region for no benefit.

## Recovery from a bricked BMS (failed flash)

A failed flash usually leaves the BMS in **bootloader mode (black screen), not truly
bricked**. Recover with the JK PC software's **Force Update**, which needs a physical
button sequence — clicking "Force updating" alone does nothing:

1. Isolate the pack (no load, not in parallel), keep the BMS powered from the cells.
2. Connect via the USB/RS485 adapter; confirm a COM port appears in Device Manager.
3. In the PC software: menu → Upload/Upgrade Firmware → select the **correct** firmware
   file (matching model + the version on the other pack) → **Force updating**.
4. Enter the unlock/authorization code if prompted (request from JK support with the
   order number if you don't have it).
5. **As the flash runs:** press and hold the activation/power button, briefly press and
   release the RST (reset) button once, and keep holding the power button until the
   progress bar completes and you hear the confirmation beep.
6. If DIP switches are present: DIP #1 = ON, all others OFF for force/bootloader mode.

**PC software won't launch (v3.x):** those builds are 32-bit and fail on missing
`msvcp140.dll` / `vcruntime140.dll`. Install the **32-bit (x86) Microsoft Visual C++
2015–2022 Redistributable** and run as administrator. (v2.8.0 works without this.)

Only flash the exact correct firmware file, on a short direct USB cable, with other apps
closed — interference/interruption is what bricks it.
