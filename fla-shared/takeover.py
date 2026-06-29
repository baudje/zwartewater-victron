"""Takeover — the temporary transfer of main-bus control from the aggregate
driver to the temp battery while the LFP bank is isolated, and back.

Owns one operation's lock-release, aggregate-driver, DVCC-selection, and
temp-battery lifecycle. Shared by the FLA equalisation and charge services; they
differ only in the charging loop between hand_off_in() and hand_back().

See CONTEXT.md and docs/adr/0001-persist-dvcc-originals.md.
"""

import json
import logging
import os
import time
from collections import namedtuple

import aggregate_driver
import relay_control
import voltage_matching
from temp_battery import TempBatteryService, is_temp_battery_running
from lock import release as release_lock

log = logging.getLogger(__name__)

# Volatile by design: survives a parent crash (where resume applies — the temp
# battery subprocess is still alive), and is correctly gone after a full reboot
# (where resume does NOT apply — relay 2 boot-closes and the subprocess dies).
SNAPSHOT_FILE = "/tmp/fla_dvcc_originals.json"

TEMP_INSTANCE = 100
TEMP_SERVICE = "com.victronenergy.battery/100"
TEMP_CHARGE_CURRENT = 60.0  # FLA recommended max bulk current

# Per-service display states for the handoff phases (values differ per service).
TakeoverStates = namedtuple(
    "TakeoverStates",
    ["stopping_driver", "disconnecting", "voltage_matching",
     "reconnecting", "restarting_driver"],
)


def save_originals(battery_service, bms_instance, max_charge_voltage):
    """Persist the DVCC originals snapshot. Returns True on success."""
    try:
        with open(SNAPSHOT_FILE, "w") as f:
            json.dump({
                "battery_service": battery_service,
                "bms_instance": bms_instance,
                "max_charge_voltage": max_charge_voltage,
            }, f)
        log.info("DVCC originals snapshot saved: %s / %s / %s",
                 battery_service, bms_instance, max_charge_voltage)
        return True
    except OSError as e:
        log.error("Failed to persist DVCC originals snapshot: %s", e)
        return False


def load_originals():
    """Load the persisted DVCC originals, or None if missing/corrupt."""
    try:
        with open(SNAPSHOT_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def delete_originals():
    """Remove the snapshot file (idempotent)."""
    try:
        os.unlink(SNAPSHOT_FILE)
    except OSError:
        pass
