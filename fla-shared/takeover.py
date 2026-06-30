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
# After restart_systemcalc() returns, systemcalc's slow post-restart D-Bus scan
# on Venus OS v3.80~33 keeps the bus congested, so the temp battery (instance
# 100) can take far longer than the wait_for_service_instance 10s default to
# answer a /DeviceInstance query. A 10s wait lost that race on 2026-06-28 and
# aborted the handoff into a safe-hold. The temp battery holds a SAFE CVL
# throughout this wait, so a generous timeout is free — same rationale as the
# 300s com.victronenergy.system wait in restart_systemcalc().
TEMP_DISCOVERY_TIMEOUT = 120

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

    # Sentinel returned by resume_attach when the bus is held and an alarm has
    # been raised (relay open + live temp battery but the DVCC snapshot is
    # missing). Distinct from None ("nothing to resume"): the caller must keep
    # the lock and NOT fall through to startup_safety_check.
    RESUME_HELD = object()

    def __init__(self, monitor, status, alerting_mod, service_name, states,
                 should_abort=None):
        self.monitor = monitor
        self.status = status
        self.alerting = alerting_mod
        self.service_name = service_name
        self.states = states
        # Operator-abort probe (e.g. the web "Abort" button). Injected because
        # check_abort lives in each service's web_server; the shared handoff
        # stays decoupled. Defaults to "never abort" so resume/tests need not set it.
        self._should_abort = should_abort if should_abort is not None else (lambda: False)
        self.temp_service = None
        self._aggregate_stopped = False
        self._originals = None
        self._torn_down = False
        self._dvcc_switched = False  # True once DVCC has been pointed at the temp battery
        self._alarm_message = None   # the most-specific alarm this operation raised, if any

    def _alarm(self, message):
        """Raise an alarm and remember its message, so the guarded teardown's
        generic safe-hold alarm does not bury this more-specific root cause."""
        self.alerting.raise_alarm(message, status_service=self.status)
        self._alarm_message = message

    def _fail(self, message):
        """Alarm, tear down, and signal failure."""
        self._alarm(message)
        self.teardown()
        return False

    def _abort(self, message):
        """Operator-requested abort during the handoff: tear down cleanly and
        signal not-done. Unlike _fail this raises NO alarm — an abort is an
        intentional action, not a fault. Safe only before the relay opens; the
        relay-guarded teardown holds the bus if it is somehow already open."""
        log.info("Takeover: %s — reconnecting (no alarm)", message)
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
            self._alarm("Failed to start temp battery service")
            return False

        # 2. Stop the aggregate driver.
        self.status.update(state=self.states.stopping_driver)
        if not aggregate_driver.stop():
            return self._fail("Failed to stop aggregate driver")
        self._aggregate_stopped = True

        # 3. Restart systemcalc so it discovers the temp battery. Both waits are
        #    abort-aware: the systemcalc wait (up to 300s) and the discovery wait
        #    (up to 120s) run before the relay opens, so an operator Abort during
        #    either must reconnect cleanly instead of pressing on to the disconnect.
        if not self.monitor.restart_systemcalc(should_abort=self._should_abort):
            if self._should_abort():
                return self._abort("Abort during systemcalc restart")
            return self._fail("Failed to restart systemcalc for temp battery discovery")
        if not self.monitor.wait_for_service_instance(
                TEMP_INSTANCE, timeout_seconds=TEMP_DISCOVERY_TIMEOUT,
                should_abort=self._should_abort):
            if self._should_abort():
                return self._abort("Abort during temp battery discovery")
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
        #    From here on DVCC is (at least partially) switched, so teardown must
        #    restore the originals on any later failure.
        self._dvcc_switched = True
        if not self.monitor.set_battery_service_setting(TEMP_SERVICE):
            return self._fail("Failed to switch BatteryService to temp battery")
        if not self.monitor.set_bms_instance(TEMP_INSTANCE):
            return self._fail("Failed to switch BmsInstance to temp battery")
        if not self.monitor.wait_for_bms_selection(TEMP_SERVICE, TEMP_INSTANCE):
            return self._fail("DVCC handoff to temp battery was not confirmed")

        # Last safe moment: honour an operator Abort requested at any point before
        # the irreversible LFP disconnect (e.g. during the DVCC switch above). The
        # relay is still closed, so teardown reconnects cleanly with no alarm.
        if self._should_abort():
            return self._abort("Abort before LFP disconnect")

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
        """The single relay-state-guarded restore. Idempotent (a completed
        teardown is a no-op on re-entry, so the service finally can always call it).

        Relay confirmed closed -> restore the DVCC originals (from the in-memory
        snapshot, or the persisted file on resume), deregister the temp battery,
        restart the aggregate, release the lock, delete the snapshot. Relay open
        -> hold the bus and alarm; restore NOTHING (handing DVCC back while the
        LFP is isolated is the free-fall).

        Teardown does NOT touch the alarm: a failure path raised an alarm before
        calling teardown and the operator must keep seeing it; clearing the alarm
        on success is the caller's job (run_*/resume on a matched hand_back)."""
        if self._torn_down:
            return  # a completed teardown already ran — never repeat it

        if self.monitor.get_relay_state() != 1:
            log.error("Takeover teardown: relay open — holding bus, NOT restoring "
                      "(temp battery, DVCC, lock all left in place)")
            # Raise the generic safe-hold alarm only if this operation has not
            # already raised a more-specific one (e.g. "Failed to close relay 2").
            # The D-Bus alarm path carries only a level, so the message lives only
            # in the log; a second raise would bury the root cause.
            if self._alarm_message is None:
                self.alerting.raise_alarm(
                    "Reconnect incomplete — bus held by temp battery, manual intervention required",
                    status_service=self.status,
                )
            else:
                log.error("Takeover teardown: bus held; manual intervention required "
                          "(root cause already raised: %s)", self._alarm_message)
            return  # NOT torn down — a later teardown (relay since closed) may still restore

        # Bring the aggregate driver back up FIRST, while the temp battery is
        # still registered and selected (holding CVL at float), so DVCC always
        # has a valid CVL source — never a window where the selection points at a
        # service that isn't running yet.
        if self._aggregate_stopped:
            try:
                aggregate_driver.start()
                self.monitor.invalidate_services()
            except Exception:
                log.error("CRITICAL: Failed to restart aggregate driver in teardown")
            self._aggregate_stopped = False

        # Hand DVCC selection back to the aggregate — but ONLY if we actually
        # switched it. An early hand_off_in failure (temp register / aggregate
        # stop / systemcalc) changed nothing, so there is nothing to restore and
        # no snapshot is expected. Restore each field only when it was readable
        # at snapshot time; a None means the read glitched and writing it back
        # would corrupt the setting (set_battery_service_setting(None) writes the
        # literal string "None"; set_bms_instance(None) raises).
        if self._dvcc_switched:
            originals = self._originals or load_originals()
            if originals is None:
                log.error("CRITICAL: DVCC was switched but no originals snapshot to restore from")
            else:
                if originals.get("bms_instance") is not None:
                    try:
                        self.monitor.set_bms_instance(originals["bms_instance"])
                        log.info("BmsInstance restored to %s", originals["bms_instance"])
                    except Exception:
                        log.error("CRITICAL: Failed to restore BmsInstance")
                if originals.get("battery_service") is not None:
                    try:
                        self.monitor.set_battery_service_setting(originals["battery_service"])
                        log.info("BatteryService restored to %s", originals["battery_service"])
                    except Exception:
                        log.error("CRITICAL: Failed to restore BatteryService")
                if originals.get("max_charge_voltage") is not None:
                    # Restored here too (not only in hand_back): covers the edge
                    # where the relay closed without a hand_back — e.g. an external
                    # relay close detected mid-loop at high CVL — so the ceiling is
                    # never stranded raised. On the normal path hand_back already
                    # lowered it, making this a harmless idempotent re-write.
                    try:
                        self.monitor.set_dvcc_max_charge_voltage(originals["max_charge_voltage"])
                        log.info("DVCC MaxChargeVoltage restored to %s", originals["max_charge_voltage"])
                    except Exception:
                        log.error("CRITICAL: Failed to restore DVCC MaxChargeVoltage")

        # The temp battery is no longer the selected BMS — safe to deregister.
        if self.temp_service is not None:
            try:
                self.temp_service.deregister()
            except Exception:
                pass
            self.temp_service = None

        # Delete the snapshot BEFORE releasing the lock, so the next operation to
        # acquire the lock can't have its fresh snapshot deleted out from under it.
        delete_originals()
        release_lock()
        self._torn_down = True

    def hand_back(self, float_voltage, voltage_delta_max, cache_callback=None):
        """Hold the bus at float until the Trojan<->LFP delta converges, close
        relay 2, then run the guarded teardown. Returns (matched, delta). The
        ceiling is restored from the snapshot first (relay still open — safe,
        it is only a ceiling, the temp battery CVL at float caps the bus). In
        production wait_for_match only returns on convergence (else safe-hold)."""
        originals = self._originals or load_originals()
        if originals is not None:
            self.monitor.set_dvcc_max_charge_voltage(originals["max_charge_voltage"])
            log.info("DVCC MaxChargeVoltage restored to %s before matching",
                     originals["max_charge_voltage"])

        self.status.update(state=self.states.voltage_matching)
        matched, delta = voltage_matching.wait_for_match(
            self.monitor, self.temp_service, self.status, self.alerting,
            voltage_delta_max=voltage_delta_max, float_voltage=float_voltage,
            cache_callback=cache_callback,
        )
        if not matched:
            return False, delta

        self.status.update(state=self.states.reconnecting)
        if not relay_control.close_relay_verified(self.monitor):
            self._alarm("Failed to close relay 2")
            return False, delta

        self.status.update(state=self.states.restarting_driver)
        self.teardown()
        return True, delta

    def abort_teardown(self):
        """Alias for service finally blocks — the guarded teardown belt-and-suspenders."""
        self.teardown()

    @classmethod
    def resume_attach(cls, monitor, status, alerting_mod, service_name, states):
        """Adopt an interrupted takeover on startup. Tristate return:

        - a Takeover ready for hand_back — adopt and finish it;
        - None — nothing to resume (relay closed, or the temp battery vanished
          between the caller's check and ours); the caller should release the
          lock and fall through to startup_safety_check;
        - Takeover.RESUME_HELD — relay open + live temp battery but the DVCC
          snapshot is missing; an alarm was raised and the bus is held. The
          caller must keep the lock and skip startup_safety_check. We refuse to
          restore to guessed values (ADR-0001)."""
        if monitor.get_relay_state() != 0:
            return None  # relay closed — nothing isolated
        if not is_temp_battery_running():
            return None  # relay open but no holder — caller runs startup_safety_check
        originals = load_originals()
        if originals is None:
            log.error("RESUME: relay open + temp battery but no DVCC snapshot — "
                      "refusing to guess; holding and alarming")
            alerting_mod.raise_alarm(
                "Reconnect incomplete — bus held, DVCC originals lost, manual intervention required",
                status_service=status,
            )
            return cls.RESUME_HELD
        t = cls(monitor, status, alerting_mod, service_name, states)
        t.temp_service = TempBatteryService(device_instance=TEMP_INSTANCE)
        t.temp_service.attach()
        t._originals = originals
        t._aggregate_stopped = True  # the interrupted operation stopped it
        t._dvcc_switched = True       # the interrupted operation had switched DVCC
        log.warning("RESUME: adopted interrupted takeover (snapshot loaded)")
        return t
