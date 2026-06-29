#!/usr/bin/env python3
"""FLA Charge Service for Venus OS.

Detects undercharged Trojan FLA batteries and runs a full bulk+absorption
charge cycle by temporarily disconnecting LFPs.
Runs as a persistent daemontools service with GLib main loop.
"""

import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/data/apps/fla-shared")
sys.path.insert(1, os.path.join("/data/apps/fla-shared", "ext", "velib_python"))

from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

from dbus_monitor import DbusMonitor
from temp_battery import recover_orphan_temp_battery, is_temp_battery_running
from relay_control import verify_relay_still_open, startup_safety_check
from lock import acquire as acquire_lock
from temp_compensation import compensate as temp_compensate
import alerting

from dbus_status_service import (
    StatusService, STATE_IDLE, STATE_PHASE1_SHARED, STATE_STOPPING_DRIVER,
    STATE_DISCONNECTING, STATE_PHASE2_BULK, STATE_PHASE3_ABSORPTION,
    STATE_COOLING_DOWN, STATE_VOLTAGE_MATCHING, STATE_RECONNECTING,
    STATE_RESTARTING_DRIVER, STATE_ERROR,
)
from settings import Settings
from web_server import (
    start_web_server,
    update_cache,
    check_run_now,
    check_abort,
    clear_abort,
    drain_pending_settings,
    _cache,
)
from takeover import Takeover, TakeoverStates

CHARGE_TAKEOVER_STATES = TakeoverStates(
    stopping_driver=STATE_STOPPING_DRIVER,
    disconnecting=STATE_DISCONNECTING,
    voltage_matching=STATE_VOLTAGE_MATCHING,
    reconnecting=STATE_RECONNECTING,
    restarting_driver=STATE_RESTARTING_DRIVER,
)

LOG_FILE = "/data/log/fla-charge.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

LAST_CHARGE_FILE = "/data/apps/fla-charge/last_charge"
CHECK_INTERVAL_SEC = 60


def read_last_charge():
    try: return datetime.fromisoformat(Path(LAST_CHARGE_FILE).read_text().strip())
    except (FileNotFoundError, ValueError): return None


def write_last_charge():
    Path(LAST_CHARGE_FILE).write_text(datetime.now().isoformat())


def is_ac_available(monitor):
    """Check if AC input is active (shore power or generator).

    Returns False on any read failure so the caller treats "unknown" as
    "no AC" — but logs the exception at warning level so an operator can
    distinguish a genuine AC-loss from a transient D-Bus hiccup when a
    Phase 1 charge aborts (otherwise the abort is silent on this path).
    """
    try:
        import dbus
        bus = monitor.bus
        obj = bus.get_object("com.victronenergy.vebus.ttyS4", "/Ac/ActiveIn/ActiveInput")
        iface = dbus.Interface(obj, "com.victronenergy.BusItem")
        value = iface.GetValue()
        # 0 = AC-in-1, 1 = AC-in-2, 240 = disconnected
        return int(value) in (0, 1)
    except Exception as e:
        log.warning("is_ac_available: failed to read /Ac/ActiveIn/ActiveInput (%s) — treating as no-AC", e)
        return False


def get_max_lfp_cell_voltage(monitor):
    """Read max LFP cell voltage from serialbattery instances."""
    try:
        import dbus
        bus = monitor.bus
        max_v = 0
        for name in bus.list_names():
            name = str(name)
            if "com.victronenergy.battery" not in name or "aggregate" in name or "fla" in name:
                continue
            try:
                obj = bus.get_object(name, "/ProductName")
                iface = dbus.Interface(obj, "com.victronenergy.BusItem")
                product = str(iface.GetValue())
                if "SerialBattery" not in product:
                    continue
            except Exception:
                continue
            # Read cell voltages
            for i in range(1, 9):
                try:
                    obj = bus.get_object(name, "/Voltages/Cell%d" % i)
                    iface = dbus.Interface(obj, "com.victronenergy.BusItem")
                    v = float(iface.GetValue())
                    if v > max_v:
                        max_v = v
                except Exception:
                    continue
        return max_v if max_v > 0 else None
    except Exception:
        return None


def should_run(settings, monitor):
    """Check if FLA charge conditions are met.

    RunNow bypasses the SoC trigger (intentional — allows manual charge at any SoC).
    AC input is always enforced, even on RunNow.
    RunNow is only consumed once all pre-conditions pass.
    """
    if not settings.enabled:
        return False

    trojan_soc = monitor.get_trojan_soc()
    run_now_flag = settings.run_now
    if not run_now_flag:
        if trojan_soc is None:
            log.warning("Cannot read Trojan SoC")
            return False
        if trojan_soc >= settings.trojan_soc_trigger:
            return False

    if not is_ac_available(monitor):
        log.debug("No AC input available")
        return False

    # All pre-conditions passed — now consume RunNow
    if run_now_flag:
        log.info("RunNow flag set — bypassing SoC trigger")
        settings.clear_run_now()

    log.info("FLA charge conditions met: Trojan SoC=%.1f%%, AC available", trojan_soc or 0)
    return True


def run_charge(settings, monitor, status):
    """Execute the full FLA charge sequence. Returns True on success."""
    aborted_by_operator = False

    if not acquire_lock("fla-charge"):
        log.info("Operation lock held — skipping")
        return False

    t = Takeover(monitor, status, alerting, "fla-charge", CHARGE_TAKEOVER_STATES)
    try:
        # === PHASE 1: Shared charging ===
        status.update(state=STATE_PHASE1_SHARED)
        log.info("Phase 1: Shared charging — both banks on bus")
        phase1_start = time.time()
        phase1_timeout = settings.phase1_timeout_hours * 3600

        while True:
            elapsed = time.time() - phase1_start
            remaining = max(0, phase1_timeout - elapsed)

            lfp_soc = monitor.get_lfp_soc()
            trojan_soc = monitor.get_trojan_soc()
            v_trojan = monitor.get_trojan_voltage()
            v_lfp = monitor.get_lfp_voltage()
            i_trojan = monitor.get_trojan_current()
            charge_current = monitor.get_lfp_current()  # Total through LFP shunt
            max_cell_v = get_max_lfp_cell_voltage(monitor)

            status.update(
                time_remaining=remaining,
                trojan_v=v_trojan, trojan_i=i_trojan, trojan_soc=trojan_soc,
                lfp_v=v_lfp, lfp_soc=lfp_soc,
            )
            update_cache(
                state=STATE_PHASE1_SHARED, time_remaining=remaining,
                trojan_v=v_trojan, trojan_soc=trojan_soc,
                lfp_v=v_lfp, lfp_soc=lfp_soc,
            )

            # Check AC still available
            if not is_ac_available(monitor):
                log.warning("AC input lost during Phase 1 — aborting")
                status.update(state=STATE_ERROR)
                return False

            # Transition triggers (any)
            transition_reason = None
            if lfp_soc is not None and lfp_soc >= settings.lfp_soc_transition:
                transition_reason = "LFP SoC %.1f%% >= %d%%" % (lfp_soc, settings.lfp_soc_transition)
            elif charge_current is not None and charge_current > 0 and charge_current < settings.current_taper_threshold:
                transition_reason = "Charge current %.1fA < %.1fA" % (abs(charge_current), settings.current_taper_threshold)
            elif max_cell_v is not None and max_cell_v >= settings.lfp_cell_voltage_disconnect:
                transition_reason = "LFP cell voltage %.3fV >= %.3fV" % (max_cell_v, settings.lfp_cell_voltage_disconnect)

            if transition_reason:
                log.info("Phase 1 → 2 transition: %s", transition_reason)
                break

            if elapsed > phase1_timeout:
                log.warning("Phase 1 timeout after %.0f hours — aborting", elapsed / 3600)
                status.update(state=STATE_ERROR)
                return False

            if check_abort():
                log.warning("Abort requested via web UI during Phase 1")
                clear_abort()
                status.update(state=STATE_ERROR)
                alerting.raise_alarm("FLA charge aborted by operator", status_service=status)
                return False

            if int(elapsed) % 300 < 30:
                log.info("Phase 1: %.0f min, LFP SoC=%.0f%%, Trojan SoC=%.0f%%, I=%.0fA, max cell=%.3fV",
                         elapsed / 60, lfp_soc or 0, trojan_soc or 0, abs(charge_current or 0), max_cell_v or 0)

            time.sleep(30)

        # === PHASE 2-3: FLA-only bulk/absorption via the Takeover ===
        # Hand off DVCC to the temp battery and isolate the LFP bank. The safe
        # voltage is the live bus voltage (crash-safe before the relay opens);
        # the target is the temp-compensated FLA bulk/absorption voltage.
        battery_temp = monitor.get_battery_temperature()
        abs_voltage = temp_compensate(settings.fla_bulk_voltage, battery_temp)
        if not t.hand_off_in(safe_voltage=(v_lfp or v_trojan or 28.0),
                             target_voltage=abs_voltage):
            status.update(state=STATE_ERROR)
            return False

        lfp_voltage_at_disconnect = monitor.get_lfp_voltage()
        if lfp_voltage_at_disconnect:
            log.info("LFP voltage at disconnect: %.2fV", lfp_voltage_at_disconnect)

        # === PHASE 3: FLA absorption ===
        status.update(state=STATE_PHASE2_BULK)
        log.info("Phase 2-3: FLA bulk/absorption at %.2fV", abs_voltage)
        abs_start = time.time()
        abs_timeout = settings.fla_absorption_max_hours * 3600
        i_trojan_none_count = 0

        while True:
            elapsed = time.time() - abs_start
            remaining = max(0, abs_timeout - elapsed)

            v_trojan = monitor.get_trojan_voltage()
            i_trojan = monitor.get_trojan_current()
            v_lfp = monitor.get_lfp_voltage()

            current_state = STATE_PHASE3_ABSORPTION if elapsed > 300 else STATE_PHASE2_BULK
            delta = round(abs(v_trojan - v_lfp), 2) if (v_trojan is not None and v_lfp is not None) else None
            status.update(
                state=current_state,
                time_remaining=remaining,
                trojan_v=v_trojan, trojan_i=i_trojan,
                lfp_v=v_lfp,
            )
            update_cache(
                state=current_state, time_remaining=remaining,
                trojan_v=v_trojan, lfp_v=v_lfp, voltage_delta=delta,
                trojan_current=i_trojan,
            )

            if v_trojan is None:
                status.update(state=STATE_ERROR)
                alerting.raise_alarm("SmartShunt Trojan unresponsive during absorption", status_service=status)
                return False

            # Verify relay still open — external close at compensated CVL would damage LFPs
            if not verify_relay_still_open(monitor, abs_voltage):
                status.update(state=STATE_ERROR)
                alerting.raise_alarm("Relay closed externally during absorption — aborting", status_service=status)
                return False

            # Orion failure detection
            if (lfp_voltage_at_disconnect is not None and v_lfp is not None
                    and v_lfp < lfp_voltage_at_disconnect - 0.5):
                status.update(state=STATE_ERROR)
                alerting.raise_alarm("LFP voltage dropping — Orion may have failed", status_service=status)
                return False

            # AC loss detection
            if not is_ac_available(monitor) and i_trojan is not None and abs(i_trojan) < 2:
                log.warning("AC input lost during absorption — proceeding to voltage matching")
                break

            # Current lost
            if i_trojan is None:
                i_trojan_none_count += 1
                if i_trojan_none_count >= 10:
                    log.warning("Trojan current unreadable for 5 min — proceeding to voltage matching")
                    break
            else:
                i_trojan_none_count = 0

            # High current warning
            if i_trojan is not None and abs(i_trojan) > 60:
                log.warning("High Trojan charge current: %.1fA (dynamo/MPPT?)", abs(i_trojan))

            # Absorption complete — only after voltage reaches the target
            voltage_reached = v_trojan is not None and v_trojan >= (abs_voltage - 0.1)
            if voltage_reached and i_trojan is not None and abs(i_trojan) < settings.fla_absorption_complete_current:
                log.info("Absorption complete: V=%.1fV (target %.1fV), current %.1fA < %.1fA (%.0f min)",
                         v_trojan, abs_voltage, abs(i_trojan),
                         settings.fla_absorption_complete_current, elapsed / 60)
                break

            if elapsed > abs_timeout:
                log.warning("Absorption timeout after %.0f min", elapsed / 60)
                break

            if check_abort():
                # Relay is open here (LFP isolated) — break into the controlled
                # reconnect instead of hard-stopping and free-falling the bus.
                log.warning("Operator abort during absorption — proceeding to controlled reconnect")
                clear_abort()
                aborted_by_operator = True
                break

            if int(elapsed) % 300 < 30:
                log.info("Absorption: %.0f min, V=%.1fV, I=%.1fA",
                         elapsed / 60, v_trojan or 0, i_trojan or 0)

            time.sleep(30)

        # === PHASE 4: Hand back — float-hold, voltage-match, close, teardown ===
        update_cache(state=STATE_VOLTAGE_MATCHING)
        def _vm_cache_cb(**kwargs):
            update_cache(state=STATE_VOLTAGE_MATCHING, **kwargs)

        # Re-read battery temperature: the charge run may have lasted hours and
        # engine-room temperature can swing 10°C+ over that window. Using the
        # original battery_temp here would compensate the float target against
        # a stale environment, hurting voltage-match convergence.
        float_battery_temp = monitor.get_battery_temperature()
        matched, delta = t.hand_back(
            float_voltage=temp_compensate(settings.fla_float_voltage, float_battery_temp),
            voltage_delta_max=settings.voltage_delta_max,
            cache_callback=_vm_cache_cb,
        )
        if not matched:
            status.update(state=STATE_ERROR)
            return False

        status.update(state=STATE_IDLE, time_remaining=0)
        alerting.clear_alarm(status_service=status)
        if aborted_by_operator:
            log.info("Operator-aborted charge reconnected safely — not recording completion")
            return False
        write_last_charge()
        log.info("FLA charge completed successfully")
        return True

    except Exception as e:
        log.exception("Unexpected error: %s", e)
        status.update(state=STATE_ERROR)
        alerting.raise_alarm("FLA charge error: %s" % e, status_service=status)
        return False

    finally:
        t.abort_teardown()


class FlaChargeService:
    """Persistent service that checks conditions and runs FLA charge."""

    def __init__(self):
        # Build the monitor first (cheap), read the live relay state, then do
        # relay-aware orphan recovery — never kill a temp battery that is holding
        # an isolated bus (relay open). get_relay_state() reads system only.
        self.settings = Settings()
        self.monitor = DbusMonitor(lfp_instance=277, trojan_instance=279)
        relay_state = self.monitor.get_relay_state()
        recover_orphan_temp_battery(relay_state)
        self.status = StatusService()
        self.status.register()
        self._running = False
        self._failed = False
        if not self._resume_interrupted_reconnect():
            startup_safety_check(self.monitor, self.status, alerting)
        self._update_idle_status()
        log.info("FLA charge service started — checking every %ds", CHECK_INTERVAL_SEC)

    def _resume_interrupted_reconnect(self):
        """Adopt and finish a takeover interrupted mid-hand-back. Returns True if
        this service took over (or another owns it), so the caller skips the
        normal startup_safety_check."""
        if self.monitor.get_relay_state() != 0:
            return False
        if not is_temp_battery_running():
            return False
        if not acquire_lock("fla-charge"):
            log.info("RESUME: another service owns the interrupted reconnect — backing off")
            return True
        t = Takeover.resume_attach(self.monitor, self.status, alerting,
                                   "fla-charge", CHARGE_TAKEOVER_STATES)
        if t is None:
            # Snapshot missing — resume_attach already alarmed; hold (keep lock).
            return True
        log.warning("RESUME: adopting interrupted takeover and finishing hand-back")
        self._running = True

        def _worker():
            try:
                float_temp = self.monitor.get_battery_temperature()
                matched, _ = t.hand_back(
                    float_voltage=temp_compensate(self.settings.fla_float_voltage, float_temp),
                    voltage_delta_max=self.settings.voltage_delta_max,
                )
                # teardown no longer clears the alarm; the success path owns it.
                if matched:
                    alerting.clear_alarm(status_service=self.status)
            except Exception as e:
                log.exception("RESUME worker error: %s", e)
                t.abort_teardown()
            finally:
                self._running = False

        threading.Thread(target=_worker, daemon=True).start()
        return True

    def _update_idle_status(self):
        v_trojan = self.monitor.get_trojan_voltage()
        v_lfp = self.monitor.get_lfp_voltage()
        trojan_soc = self.monitor.get_trojan_soc()
        lfp_soc = self.monitor.get_lfp_soc()
        delta = round(abs(v_trojan - v_lfp), 2) if (v_trojan is not None and v_lfp is not None) else None
        self.status.update(
            state=STATE_IDLE, trojan_v=v_trojan, lfp_v=v_lfp,
            trojan_soc=trojan_soc, lfp_soc=lfp_soc,
        )
        last = read_last_charge()
        update_cache(
            state=STATE_IDLE, time_remaining=0,
            trojan_v=v_trojan, trojan_soc=trojan_soc,
            lfp_v=v_lfp, lfp_soc=lfp_soc,
            voltage_delta=delta,
            last_charge=last.strftime("%Y-%m-%d %H:%M") if last else None,
            settings={
                "enabled": self.settings.enabled,
                "trojan_soc_trigger": self.settings.trojan_soc_trigger,
                "lfp_soc_transition": self.settings.lfp_soc_transition,
                "lfp_cell_voltage_disconnect": self.settings.lfp_cell_voltage_disconnect,
                "current_taper_threshold": self.settings.current_taper_threshold,
                "fla_bulk_voltage": self.settings.fla_bulk_voltage,
                "fla_absorption_complete_current": self.settings.fla_absorption_complete_current,
                "fla_absorption_max_hours": self.settings.fla_absorption_max_hours,
                "fla_float_voltage": self.settings.fla_float_voltage,
                "voltage_delta_max": self.settings.voltage_delta_max,
                "voltage_match_timeout_hours": self.settings.voltage_match_timeout_hours,
                "phase1_timeout_hours": self.settings.phase1_timeout_hours,
            },
        )

    def _apply_pending_settings(self):
        from settings import SETTINGS_DEFS
        pending = drain_pending_settings()
        if not pending:
            return
        for key, value in pending:
            if key not in SETTINGS_DEFS:
                log.warning("Unknown setting key '%s' — skipped", key)
                continue
            _, _, minimum, maximum = SETTINGS_DEFS[key]
            if value < minimum or value > maximum:
                log.warning("Setting %s value %s out of bounds — rejected", key, value)
                continue
            self.settings._write(key, value)
            log.info("Setting %s updated to %s via web UI", key, value)

    def _check(self):
        if self._running:
            if check_run_now():
                log.info("RunNow requested while already running — ignored to prevent queueing")
            return True
        try:
            self._apply_pending_settings()

            if check_abort():
                log.info("Abort requested while idle — cleared to prevent queueing")
                clear_abort()

            if check_run_now():
                self.settings._write("run_now", 1)
            # Don't overwrite error state on subsequent ticks
            if not self._failed:
                self._update_idle_status()
            if should_run(self.settings, self.monitor):
                self._running = True
                self._failed = False
                _cache["run_now_requested"] = False

                def _worker():
                    success = False
                    try:
                        success = run_charge(self.settings, self.monitor, self.status)
                        if success:
                            log.info("FLA charge completed successfully")
                        else:
                            log.error("FLA charge failed — check alarms")
                            self._failed = True
                    except Exception as e:
                        log.exception("Worker thread error: %s", e)
                        self._failed = True
                    finally:
                        self._running = False
                        if success:
                            self._failed = False
                            GLib.idle_add(self._update_idle_status)

                threading.Thread(target=_worker, daemon=True).start()
        except Exception as e:
            log.exception("Error in periodic check: %s", e)
        return True


def main():
    import dbus.mainloop.glib
    dbus.mainloop.glib.threads_init()
    DBusGMainLoop(set_as_default=True)
    log.info("FLA charge service starting")
    try:
        service = FlaChargeService()
        start_web_server()
        GLib.timeout_add_seconds(CHECK_INTERVAL_SEC, service._check)
        log.info("Entering GLib main loop")
        GLib.MainLoop().run()
    except Exception as e:
        log.exception("Fatal error: %s", e)
        alerting.raise_alarm("FLA charge fatal error: %s" % e)


if __name__ == "__main__":
    main()
