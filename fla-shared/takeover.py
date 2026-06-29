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


class Takeover:
    """Owns one operation's takeover of DVCC from the aggregate driver.

    Lifecycle: the caller acquires the operation lock (a go/no-go gate), then
    hand_off_in() -> [caller runs its charging loop] -> hand_back(). The guarded
    teardown (restore DVCC, release lock, etc.) runs ONLY when relay 2 is
    confirmed closed; otherwise the bus is held and an alarm raised.
    """

    def __init__(self, monitor, status, alerting_mod, service_name, states):
        self.monitor = monitor
        self.status = status
        self.alerting = alerting_mod
        self.service_name = service_name
        self.states = states
        self.temp_service = None
        self._aggregate_stopped = False
        self._originals = None
        self._torn_down = False

    def _fail(self, message):
        """Alarm, tear down, and signal failure."""
        self.alerting.raise_alarm(message, status_service=self.status)
        self.teardown()
        return False

    def hand_off_in(self, safe_voltage, target_voltage, charge_current=TEMP_CHARGE_CURRENT):
        """Run the ordered handoff: temp battery at safe voltage, stop aggregate,
        restart systemcalc, snapshot+persist DVCC originals, switch DVCC to the
        temp battery, confirm the BMS selection, open relay 2, raise CVL to the
        target. Returns True on success; on any failure tears down and returns
        False. The relay opens ONLY after the BMS selection is confirmed."""
        # 1. Temp battery at a SAFE voltage first (crash-safe before the relay opens).
        self.temp_service = TempBatteryService(device_instance=TEMP_INSTANCE)
        if not self.temp_service.register(charge_voltage=safe_voltage,
                                          charge_current=charge_current):
            self.alerting.raise_alarm("Failed to start temp battery service",
                                      status_service=self.status)
            return False

        # 2. Stop the aggregate driver.
        self.status.update(state=self.states.stopping_driver)
        if not aggregate_driver.stop():
            return self._fail("Failed to stop aggregate driver")
        self._aggregate_stopped = True

        # 3. Restart systemcalc so it discovers the temp battery.
        if not self.monitor.restart_systemcalc():
            return self._fail("Failed to restart systemcalc for temp battery discovery")
        if not self.monitor.wait_for_service_instance(TEMP_INSTANCE):
            return self._fail("Temp battery service instance 100 not discovered on D-Bus")

        # 4. Snapshot the DVCC originals (all three) BEFORE changing any of them,
        #    and persist so a crash-then-resume restores the truth (ADR-0001).
        originals = {
            "battery_service": self.monitor.get_battery_service_setting(),
            "bms_instance": self.monitor.get_bms_instance(),
            "max_charge_voltage": self.monitor.get_dvcc_max_charge_voltage(),
        }
        self._originals = originals
        save_originals(originals["battery_service"], originals["bms_instance"],
                       originals["max_charge_voltage"])
        log.info("Saving BatteryService=%s, BmsInstance=%s, DVCC MaxChargeVoltage=%s",
                 originals["battery_service"], originals["bms_instance"],
                 originals["max_charge_voltage"])

        # 5. Switch DVCC to the temp battery and CONFIRM before touching the relay.
        if not self.monitor.set_battery_service_setting(TEMP_SERVICE):
            return self._fail("Failed to switch BatteryService to temp battery")
        if not self.monitor.set_bms_instance(TEMP_INSTANCE):
            return self._fail("Failed to switch BmsInstance to temp battery")
        if not self.monitor.wait_for_bms_selection(TEMP_SERVICE, TEMP_INSTANCE):
            return self._fail("DVCC handoff to temp battery was not confirmed")

        # 6. Open relay 2 (isolate the LFP bank) — only now that DVCC is the temp battery.
        self.status.update(state=self.states.disconnecting)
        if not relay_control.open_relay(self.monitor):
            return self._fail("Failed to open relay 2")
        if not relay_control.verify_relay_open(self.monitor):
            return self._fail("LFP not disconnected after relay open")

        # 7. Raise the DVCC ceiling and the temp battery CVL to the target.
        self.monitor.set_dvcc_max_charge_voltage(target_voltage + 0.5)  # headroom above target
        self.temp_service.set_charge_voltage(target_voltage)
        log.info("CVL raised to target %.2fV (ceiling %.2fV)", target_voltage, target_voltage + 0.5)
        return True

    def teardown(self):
        """Placeholder — replaced in Task 3 with the guarded restore."""
        pass
