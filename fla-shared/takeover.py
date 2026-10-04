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
from relay_control import LFP_SAFE_CVL
import voltage_matching
from temp_battery import TempBatteryService, is_temp_battery_running
from lock import acquire as acquire_lock, release as release_lock, is_locked as lock_is_locked

log = logging.getLogger(__name__)

# Persistent (on /data), because the DVCC settings it protects are persistent
# too: a reboot mid-operation leaves BmsInstance/BatteryService pointing at the
# dead temp battery. The 2026-10-03 reboot wiped the old /tmp snapshot, the next
# run then snapshotted those takeover values as "originals", and its hand-back
# restored BmsInstance=100 -> "BMS lost". A snapshot left behind with no live
# operation is finished by recover_stale_takeover (the real guarded teardown).
SNAPSHOT_FILE = "/data/apps/fla-shared/dvcc_originals.json"

TEMP_INSTANCE = 100
TEMP_SERVICE = "com.victronenergy.battery/100"
# The aggregate battery (dbus-aggregate-batteries) DeviceInstance — the ONLY BMS
# that reports the full-bank 120A CCL. DVCC must read CVL/CCL from this in steady
# state; a drift onto a single serialbattery pack (instance 3/4, 60A) silently
# halves LFP charge (the 2026-05-28 event). Both the idle guard and the teardown
# restore key on this.
AGGREGATE_INSTANCE = 99
TEMP_CHARGE_CURRENT = 60.0  # FLA recommended max bulk current
# Isolation probe (hand_off_in step 6): lift the CVL to LFP_SAFE_CVL when the bus
# is at least HEADROOM below it, else drop it DROP volts below the bus.
ISOLATION_PROBE_HEADROOM = 0.5
ISOLATION_PROBE_DROP = 1.0
# /Settings/CGwacs/BatteryLife/State value for ESS "Keep batteries charged".
ESS_KEEP_CHARGED = 9
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


def save_originals(battery_service, bms_instance, max_charge_voltage, ess_state=None):
    """Persist the DVCC originals snapshot. Returns True on success."""
    # Atomic (tmp + fsync + replace): on /data a power loss mid-write must not
    # leave a truncated snapshot that reads back as "originals lost".
    tmp = SNAPSHOT_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({
                "battery_service": battery_service,
                "bms_instance": bms_instance,
                "max_charge_voltage": max_charge_voltage,
                "ess_state": ess_state,
            }, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SNAPSHOT_FILE)
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


# Per-episode dedup for the idle BMS guard: alarm once when the selection drifts,
# re-arm only after it returns to the aggregate. Module-level because the guard is
# stateless and both services call the same function (the lock guarantees they
# never run an operation concurrently).
_idle_bms_alarm_active = False


def verify_idle_bms_selection(monitor, alerting, status=None):
    """Guard the DVCC controlling-BMS selection while NO FLA operation is active.

    Steady state: DVCC reads CVL/CCL from the aggregate (AGGREGATE_INSTANCE, full
    120A). If it drifts onto a single serialbattery pack (60A) — the 2026-05-28
    failure — LFP charge current is silently halved. Raise an alarm so it's caught
    in minutes, not weeks.

    Skipped while an FLA op holds the lock (BmsInstance is then legitimately the
    temp battery, 100). Idempotent per episode: alarms once on drift, re-arms after
    the selection returns to the aggregate. Returns True (healthy), False (drifted
    and alarmed), or None (op active — not checked)."""
    global _idle_bms_alarm_active
    if lock_is_locked():
        return None  # an FLA op owns the DVCC selection right now
    if monitor.get_bms_instance() == AGGREGATE_INSTANCE:
        if _idle_bms_alarm_active:
            log.info("DVCC BMS selection back on the aggregate (instance %d)",
                     AGGREGATE_INSTANCE)
            _idle_bms_alarm_active = False
        return True
    if not _idle_bms_alarm_active:
        alerting.raise_alarm(
            "DVCC controlling BMS is not the aggregate (instance %d) — LFP charge "
            "is limited to a single 60A pack; re-select the aggregate in DVCC"
            % AGGREGATE_INSTANCE,
            status_service=status,
        )
        _idle_bms_alarm_active = True
    return False


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
        # check_abort lives on each service's web engine instance; the shared
        # handoff stays decoupled. Defaults to "never abort" so resume/tests
        # need not set it.
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

    def _read_ess_state(self):
        """ESS BatteryLife state, retried: right after the systemcalc restart the
        bus is congested and a single read can time out to None."""
        for _ in range(3):
            state = self.monitor.get_ess_state()
            if state is not None:
                return state
            time.sleep(1)
        return None

    def hand_off_in(self, safe_voltage, target_voltage, charge_current=TEMP_CHARGE_CURRENT):
        """Run the ordered handoff: temp battery at safe voltage, stop aggregate,
        restart systemcalc, snapshot+persist DVCC originals, switch DVCC to the
        temp battery, confirm the BMS selection, open relay 2, raise CVL to the
        target. Returns True on success; on any failure tears down and returns
        False. The relay opens ONLY after the BMS selection is confirmed."""
        # 0. DVCC still on the temp battery means an earlier takeover never tore
        #    down. Its values are not originals; snapshotting them would make our
        #    hand-back select a dead service (2026-10-03). Refuse, change nothing.
        if (self.monitor.get_bms_instance() == TEMP_INSTANCE
                or self.monitor.get_battery_service_setting() == TEMP_SERVICE):
            self._alarm("DVCC still selects the temp battery from an earlier run — "
                        "not starting; re-select the aggregate in DVCC")
            # We created nothing, so the caller's finally-teardown has nothing to
            # undo: it must not delete an earlier run's snapshot or keep the lock.
            self._torn_down = True
            release_lock()
            return False

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
            "ess_state": self._read_ess_state(),
        }
        self._originals = originals
        save_originals(originals["battery_service"], originals["bms_instance"],
                       originals["max_charge_voltage"], originals["ess_state"])
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

        # ESS "Optimized" discharges the selected battery down to its minimum SoC
        # even on shore power, and the selected battery is about to be the
        # isolated Trojan bank (2026-10-03: 99% -> 29% during an "equalisation").
        # Force "Keep batteries charged" until teardown restores the saved mode.
        # Fail closed: an unreadable mode must not be taken for "no ESS".
        if originals["ess_state"] is None:
            return self._fail("Cannot read the ESS mode — not isolating the LFP bank")
        if originals["ess_state"] != ESS_KEEP_CHARGED:
            if not self.monitor.set_ess_state(ESS_KEEP_CHARGED):
                return self._fail("Failed to set ESS to keep-batteries-charged")

        # 6. Open relay 2 (isolate the LFP bank) — only now that DVCC is the temp battery.
        self.status.update(state=self.states.disconnecting)
        v_bus = self.monitor.get_lfp_voltage() or safe_voltage
        if not relay_control.open_relay(self.monitor):
            return self._fail("Failed to open relay 2")
        # The isolation check below needs the two banks to drift apart. With ESS
        # on keep-charged the charger holds the Trojans AT the temp battery CVL,
        # so a CVL equal to the bus voltage leaves both banks level and the check
        # fails with the relay open (2026-10-04). Probe: move the CVL away from
        # the bus voltage, up to the LFP-safe maximum when there is headroom,
        # otherwise down (the charger backs off and the Orion load pulls the
        # isolated Trojans down). Never above LFP_SAFE_CVL: if the relay did not
        # open, the LFP bank must not see more than its absorption voltage.
        if v_bus <= LFP_SAFE_CVL - ISOLATION_PROBE_HEADROOM:
            probe = LFP_SAFE_CVL
        else:
            probe = v_bus - ISOLATION_PROBE_DROP
        self.temp_service.set_charge_voltage(probe)
        if not relay_control.verify_relay_open(self.monitor):
            # Don't leave a hold (possibly with the LFP still connected) pinned
            # at the probe voltage: back to the gentlest known-safe level.
            self.temp_service.set_charge_voltage(min(safe_voltage, v_bus))
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
                # Wait for the aggregate (instance 99) to be rediscovered BEFORE
                # re-selecting it below — symmetric with hand_off's temp-battery
                # discovery wait. Writing BmsInstance to an instance DVCC can't yet
                # see lets it reject the choice and auto-fall-back to a single 60A
                # pack: the silent 2026-05-28 half-charge failure.
                self.monitor.wait_for_service_instance(AGGREGATE_INSTANCE)
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
        confirmed = True
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
                # Confirm the selection actually took (symmetric with hand_off's
                # wait_for_bms_selection). If DVCC didn't accept it — e.g. the
                # aggregate wasn't ready — re-assert once. If it STILL won't stick,
                # only LOG it: a hand_back success makes run_*/resume clear the
                # alarm right after teardown (#33), so an alarm here would be wiped
                # moments later. The idle guard (verify_idle_bms_selection) is the
                # durable backstop — it re-alarms within one ~60s tick once the
                # lock releases and DVCC is still off the aggregate.
                if (originals.get("bms_instance") is not None
                        and originals.get("battery_service") is not None
                        and not self.monitor.wait_for_bms_selection(
                            originals["battery_service"], originals["bms_instance"])):
                    log.error("CRITICAL: DVCC BMS selection did not return to %s/%s — "
                              "re-asserting", originals["battery_service"],
                              originals["bms_instance"])
                    try:
                        self.monitor.set_bms_instance(originals["bms_instance"])
                        self.monitor.set_battery_service_setting(originals["battery_service"])
                    except Exception:
                        log.error("CRITICAL: Failed to re-assert DVCC BMS selection")
                    if not self.monitor.wait_for_bms_selection(
                            originals["battery_service"], originals["bms_instance"]):
                        log.error("CRITICAL: DVCC BMS selection did not return to the "
                                  "aggregate after reconnect — the idle guard will "
                                  "alarm shortly; verify the DVCC controlling BMS")
                        confirmed = False
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
                # Restore the ESS mode only if this takeover changed it AND it is
                # still what we set: an operator who switched it mid-run keeps
                # their choice. A failed read/write keeps the snapshot (confirmed
                # = False) so recover_stale_takeover retries on the next tick.
                ess = originals.get("ess_state")
                if ess not in (None, ESS_KEEP_CHARGED):
                    current = self.monitor.get_ess_state()
                    if current == ESS_KEEP_CHARGED and self.monitor.set_ess_state(ess):
                        log.info("ESS BatteryLife state restored to %s", ess)
                    elif current is None or current == ESS_KEEP_CHARGED:
                        log.error("CRITICAL: Failed to restore the ESS mode (%s) — "
                                  "will retry", ess)
                        confirmed = False
                    else:
                        log.info("ESS mode changed during the run (now %s) — leaving it", current)

        # The temp battery is no longer the selected BMS — safe to deregister.
        if self.temp_service is not None:
            try:
                self.temp_service.deregister()
            except Exception:
                pass
            self.temp_service = None

        # Delete the snapshot BEFORE releasing the lock, so the next operation to
        # acquire the lock can't have its fresh snapshot deleted out from under it.
        # Only a snapshot this takeover owns, and only once the selection is
        # confirmed: an unconfirmed restore keeps it so recover_stale_takeover
        # retries on the next idle tick instead of losing the originals.
        if self._dvcc_switched and confirmed:
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


def has_stale_snapshot():
    """True if a DVCC originals snapshot exists while no operation holds the lock:
    a takeover whose owner died (reboot, kill) before its teardown."""
    return not lock_is_locked() and load_originals() is not None


def recover_stale_takeover(monitor, status, alerting_mod, service_name, states):
    """Finish a dead takeover by running the real guarded teardown under the
    operation lock: restart + rediscover the aggregate, restore and confirm the
    DVCC originals, stop a leftover temp battery. Returns True once torn down.

    Relay 2 must be confirmed closed; an open or unreadable relay is left to the
    resume path / safe-hold and the snapshot is kept for the next tick."""
    if monitor.get_relay_state() != 1:
        return False
    if not acquire_lock(service_name):
        return False
    originals = load_originals()  # re-read under the lock
    if originals is None:
        release_lock()
        return False
    log.warning("Stale DVCC originals snapshot with no active operation — "
                "finishing the interrupted teardown (%s)", originals)
    t = Takeover(monitor, status, alerting_mod, service_name, states)
    t._originals = originals
    t._aggregate_stopped = True  # unknown after a crash; starting it is idempotent
    t._dvcc_switched = True
    if is_temp_battery_running():
        t.temp_service = TempBatteryService(device_instance=TEMP_INSTANCE)
        t.temp_service.attach()
    t.teardown()
    if not t._torn_down:
        release_lock()  # relay opened under us — we hold nothing; retry next tick
    return t._torn_down
