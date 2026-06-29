#!/usr/bin/env python3
"""FLA Equalisation Service for Venus OS.

Automates periodic Trojan L16H-AC equalisation on vessel Zwartewater.
Runs as a persistent daemontools service with GLib main loop.
Checks conditions every 60 seconds, visible on Cerbo GUI Device List.
"""

import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Add shared modules and velib_python to path
sys.path.insert(0, "/data/apps/fla-shared")
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext", "velib_python"))

from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

from temp_battery import TempBatteryService, recover_orphan_temp_battery, is_temp_battery_running
from dbus_monitor import DbusMonitor
from dbus_status_service import (
    StatusService, STATE_IDLE, STATE_STOPPING_DRIVER, STATE_DISCONNECTING,
    STATE_EQUALISING, STATE_COOLING_DOWN, STATE_VOLTAGE_MATCHING,
    STATE_RECONNECTING, STATE_RESTARTING_DRIVER, STATE_ERROR,
)
from settings import Settings
import alerting
from alerting import raise_alarm, clear_alarm
from relay_control import open_relay, verify_relay_open, verify_relay_still_open, close_relay_verified, close_relay_delta_aware, startup_safety_check, LFP_SAFE_CVL
from voltage_matching import wait_for_match
from aggregate_driver import stop as stop_aggregate_driver, start as start_aggregate_driver
from temp_compensation import compensate as temp_compensate
from lock import acquire as acquire_lock, release as release_lock
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

EQ_TAKEOVER_STATES = TakeoverStates(
    stopping_driver=STATE_STOPPING_DRIVER,
    disconnecting=STATE_DISCONNECTING,
    voltage_matching=STATE_VOLTAGE_MATCHING,
    reconnecting=STATE_RECONNECTING,
    restarting_driver=STATE_RESTARTING_DRIVER,
)

# Logging setup
LOG_FILE = "/data/log/fla-equalisation.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

LAST_EQ_FILE = "/data/apps/fla-equalisation/last_equalisation"
CHECK_INTERVAL_SEC = 60  # Check conditions every 60 seconds


def read_last_equalisation():
    """Read timestamp of last successful equalisation."""
    try:
        return datetime.fromisoformat(Path(LAST_EQ_FILE).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def write_last_equalisation():
    """Record current time as last successful equalisation."""
    Path(LAST_EQ_FILE).write_text(datetime.now().isoformat())


def days_until_next(settings):
    """Calculate days until next equalisation is due."""
    last = read_last_equalisation()
    if last is None:
        return 0
    days_since = (datetime.now() - last).days
    remaining = settings.days_between - days_since
    return max(0, remaining)


def should_run(settings, monitor):
    """Check if all scheduling conditions are met.

    RunNow bypasses interval and time window, NOT SoC.
    RunNow is only consumed once all hard safety gates pass.
    """
    if not settings.enabled:
        return False

    run_now_flag = settings.run_now
    if not run_now_flag:
        # Check interval
        if days_until_next(settings) > 0:
            return False

        # Check afternoon window
        now = datetime.now()
        if not (settings.start_hour <= now.hour < settings.end_hour):
            return False

    # Check LFP SoC (always enforced, even on RunNow)
    soc = monitor.get_lfp_soc()
    if soc is None:
        log.warning("Cannot read LFP SoC")
        return False
    if soc < settings.lfp_soc_min:
        return False

    # All gates passed — now consume RunNow
    if run_now_flag:
        log.info("RunNow flag set — bypassing interval and time window checks")
        settings.clear_run_now()

    log.info("All conditions met: SoC=%.1f%%, time=%s", soc, datetime.now().strftime("%H:%M"))
    return True


def run_equalisation(settings, monitor, status):
    """Execute the full equalisation sequence. Returns True on success."""
    if not acquire_lock("fla-equalisation"):
        log.warning("Operation lock held — skipping equalisation")
        return False

    aborted_by_operator = False
    t = Takeover(monitor, status, alerting, "fla-equalisation", EQ_TAKEOVER_STATES)
    try:
        battery_temp = monitor.get_battery_temperature()
        eq_voltage = temp_compensate(settings.eq_voltage, battery_temp)
        if not t.hand_off_in(safe_voltage=28.4, target_voltage=eq_voltage):
            status.update(state=STATE_ERROR)
            return False

        lfp_voltage_at_disconnect = monitor.get_lfp_voltage()
        if lfp_voltage_at_disconnect is not None:
            log.info("LFP voltage at disconnect: %.2fV", lfp_voltage_at_disconnect)

        # Step: Equalisation loop (service-specific — stays here)
        status.update(state=STATE_EQUALISING)
        log.info("Starting equalisation at %.1fV", settings.eq_voltage)
        eq_start = time.time()
        eq_timeout = settings.eq_timeout_hours * 3600
        i_trojan_none_count = 0

        while True:
            elapsed = time.time() - eq_start
            v_trojan = monitor.get_trojan_voltage()
            i_trojan = monitor.get_trojan_current()
            if v_trojan is not None and i_trojan is not None:
                t.temp_service.update_voltage_current(v_trojan, i_trojan)
            v_lfp = monitor.get_lfp_voltage()
            remaining = max(0, eq_timeout - elapsed)
            delta = round(abs(v_trojan - v_lfp), 2) if (v_trojan is not None and v_lfp is not None) else None
            status.update(time_remaining=remaining, trojan_v=v_trojan, lfp_v=v_lfp)
            update_cache(state=STATE_EQUALISING, time_remaining=remaining,
                         trojan_v=v_trojan, lfp_v=v_lfp, voltage_delta=delta)

            if v_trojan is None:
                status.update(state=STATE_ERROR)
                raise_alarm("SmartShunt Trojan (279) unresponsive during equalisation", status_service=status)
                return False

            if not verify_relay_still_open(monitor, eq_voltage):
                status.update(state=STATE_ERROR)
                raise_alarm("Relay closed externally during EQ — aborting", status_service=status)
                return False

            if (lfp_voltage_at_disconnect is not None and v_lfp is not None
                    and v_lfp < lfp_voltage_at_disconnect - 0.5):
                log.warning("LFP voltage dropping (%.2fV -> %.2fV) — possible Orion failure",
                            lfp_voltage_at_disconnect, v_lfp)
                status.update(state=STATE_ERROR)
                raise_alarm("LFP voltage dropping — Orion may have failed", status_service=status)
                return False

            if i_trojan is None:
                i_trojan_none_count += 1
                if i_trojan_none_count >= 10:
                    log.warning("Trojan current unreadable for 5 min — proceeding to voltage matching")
                    break
            else:
                i_trojan_none_count = 0

            if i_trojan is not None and abs(i_trojan) > 60:
                log.warning("High Trojan charge current: %.1fA (dynamo/MPPT active?)", abs(i_trojan))

            voltage_reached = v_trojan is not None and v_trojan >= (eq_voltage - 0.1)
            if voltage_reached and i_trojan is not None and abs(i_trojan) < settings.eq_current_complete:
                log.info("Equalisation complete: V=%.1fV (target %.1fV), current %.1fA < %.1fA (%.0f min)",
                         v_trojan, eq_voltage, abs(i_trojan), settings.eq_current_complete, elapsed / 60)
                break

            if elapsed > eq_timeout:
                log.warning("Equalisation timeout after %.0f min, current %.1fA",
                            elapsed / 60, abs(i_trojan) if i_trojan else 0)
                break

            if check_abort():
                log.warning("Operator abort during equalisation — proceeding to controlled reconnect")
                clear_abort()
                aborted_by_operator = True
                break

            if int(elapsed) % 300 < 30:
                log.info("Equalising: %.0f min, V=%.1fV, I=%.1fA",
                         elapsed / 60, v_trojan or 0, i_trojan or 0)

            time.sleep(30)

        # Hand back: float-hold, close, guarded teardown.
        update_cache(state=STATE_VOLTAGE_MATCHING)
        def _vm_cache_cb(**kwargs):
            update_cache(state=STATE_VOLTAGE_MATCHING, **kwargs)
        float_battery_temp = monitor.get_battery_temperature()
        matched, delta = t.hand_back(
            float_voltage=temp_compensate(settings.float_voltage, float_battery_temp),
            voltage_delta_max=settings.voltage_delta_max,
            cache_callback=_vm_cache_cb,
        )
        if not matched:
            status.update(state=STATE_ERROR)
            return False

        status.update(state=STATE_IDLE, time_remaining=0)
        clear_alarm(status_service=status)
        if aborted_by_operator:
            log.info("Operator-aborted equalisation reconnected safely — interval not advanced")
            return False
        write_last_equalisation()
        log.info("Equalisation completed successfully")
        return True

    except Exception as e:
        log.exception("Unexpected error during equalisation: %s", e)
        status.update(state=STATE_ERROR)
        raise_alarm("Equalisation script error: %s" % e, status_service=status)
        return False

    finally:
        t.abort_teardown()


class FlaEqualisationService:
    """Persistent service that checks conditions and runs equalisation."""

    def __init__(self):
        # Build the monitor first (cheap — no D-Bus scan; service discovery is
        # lazy), then read the live relay state so orphan recovery can be
        # relay-aware: a temp battery with the relay OPEN is a live hold and must
        # never be killed. get_relay_state() reads com.victronenergy.system only,
        # so it does not touch (and cannot hang on) a half-dead battery name.
        self.settings = Settings()
        self.monitor = DbusMonitor(lfp_instance=277, trojan_instance=279)
        relay_state = self.monitor.get_relay_state()
        recover_orphan_temp_battery(relay_state)
        self.status = StatusService()
        self.status.register()
        self._running = False
        self._failed = False
        # If an operation was interrupted mid-reconnect (relay open + temp
        # battery still holding), adopt and finish it. Otherwise run the normal
        # startup relay-safety check.
        if not self._resume_interrupted_reconnect():
            startup_safety_check(self.monitor, self.status, alerting)
        self._update_idle_status()
        log.info("FLA equalisation service started — checking every %ds", CHECK_INTERVAL_SEC)

    def _resume_interrupted_reconnect(self):
        """Adopt and finish a reconnect interrupted mid-hold.

        Returns True if this service took over (or another service owns it), so
        the caller skips the normal startup_safety_check. Returns False when
        there is nothing to resume (relay closed, or no holder subprocess)."""
        if self.monitor.get_relay_state() != 0:
            return False  # relay closed — nothing is isolated
        if not is_temp_battery_running():
            return False  # relay open but no holder — let startup_safety_check handle it
        if not acquire_lock("fla-equalisation"):
            log.info("RESUME: another service owns the interrupted reconnect — backing off")
            return True   # the other service handles it; skip our safety check
        log.warning("RESUME: relay open + temp battery alive — adopting and finishing reconnect")
        self._running = True

        def _worker():
            temp_service = TempBatteryService(device_instance=100)
            temp_service.attach()
            try:
                self.status.update(state=STATE_VOLTAGE_MATCHING)
                float_temp = self.monitor.get_battery_temperature()
                matched, _ = wait_for_match(
                    self.monitor, temp_service, self.status, alerting,
                    voltage_delta_max=self.settings.voltage_delta_max,
                    float_voltage=temp_compensate(self.settings.float_voltage, float_temp),
                )
                if not matched:
                    return  # safe-hold never returns in production; guard anyway
                self.status.update(state=STATE_RECONNECTING)
                if not close_relay_verified(self.monitor):
                    raise_alarm("RESUME: failed to close relay 2", status_service=self.status)
                    return
                time.sleep(2)
            except Exception as e:
                log.exception("RESUME worker error: %s", e)
            finally:
                # Mirror the relay-state-guarded teardown. The interrupted run's
                # saved DVCC originals are lost, so restore to known-safe normals.
                if self.monitor.get_relay_state() == 1:
                    try:
                        temp_service.deregister()
                    except Exception:
                        pass
                    try:
                        self.monitor.set_battery_service_setting("com.victronenergy.battery.aggregate")
                        self.monitor.set_bms_instance(-1)
                        self.monitor.set_dvcc_max_charge_voltage(LFP_SAFE_CVL)
                    except Exception:
                        log.error("RESUME: failed to restore DVCC to normal")
                    try:
                        start_aggregate_driver()
                        self.monitor.invalidate_services()
                    except Exception:
                        log.error("RESUME: failed to restart aggregate driver")
                    release_lock()
                    alerting.clear_alarm(status_service=self.status)
                    log.info("RESUME: reconnect completed, control handed back to aggregate")
                else:
                    raise_alarm(
                        "RESUME incomplete — bus held by temp battery, manual intervention required",
                        status_service=self.status,
                    )
                self._running = False

        threading.Thread(target=_worker, daemon=True).start()
        return True

    def _update_idle_status(self):
        """Update status display and web cache with idle info."""
        v_trojan = self.monitor.get_trojan_voltage()
        v_lfp = self.monitor.get_lfp_voltage()
        delta = round(abs(v_trojan - v_lfp), 2) if (v_trojan is not None and v_lfp is not None) else None
        self.status.update(state=STATE_IDLE, trojan_v=v_trojan, lfp_v=v_lfp)

        # Update web cache
        last = read_last_equalisation()
        last_str = last.strftime("%Y-%m-%d %H:%M") if last else None
        update_cache(
            state=STATE_IDLE,
            time_remaining=0,
            trojan_v=v_trojan,
            lfp_v=v_lfp,
            voltage_delta=delta,
            last_eq=last_str,
            days_until=days_until_next(self.settings),
            settings={
                "eq_voltage": self.settings.eq_voltage,
                "eq_current_complete": self.settings.eq_current_complete,
                "eq_timeout_hours": self.settings.eq_timeout_hours,
                "float_voltage": self.settings.float_voltage,
                "voltage_delta_max": self.settings.voltage_delta_max,
                "days_between": self.settings.days_between,
                "start_hour": self.settings.start_hour,
                "end_hour": self.settings.end_hour,
                "lfp_soc_min": self.settings.lfp_soc_min,
                "enabled": self.settings.enabled,
            },
        )

    def _apply_pending_settings(self):
        """Write any pending settings from web UI to D-Bus."""
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
                log.warning("Setting %s value %s out of bounds [%s, %s] — rejected",
                            key, value, minimum, maximum)
                continue
            self.settings._write(key, value)
            log.info("Setting %s updated to %s via web UI", key, value)

    def _check(self):
        """Periodic check — called by GLib timer. Returns True to keep running."""
        if self._running:
            if check_run_now():
                log.info("RunNow requested while already running — ignored to prevent queueing")
            return True

        try:
            # Apply any pending settings from web UI
            self._apply_pending_settings()

            if check_abort():
                log.info("Abort requested while idle — cleared to prevent queueing")
                clear_abort()

            # Check web UI RunNow button
            if check_run_now():
                self.settings._write("run_now", 1)

            # Don't overwrite error state on subsequent ticks
            if not self._failed:
                self._update_idle_status()

            if should_run(self.settings, self.monitor):
                self._running = True
                self._failed = False
                _cache["run_now_requested"] = False  # CRIT-5: Clear flag after eq starts

                def _worker():
                    success = False
                    try:
                        success = run_equalisation(self.settings, self.monitor, self.status)
                        if success:
                            log.info("Equalisation run completed successfully")
                        else:
                            log.error("Equalisation run failed — check alarms")
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
    """Entry point: start persistent service with GLib main loop."""
    import dbus.mainloop.glib
    dbus.mainloop.glib.threads_init()
    DBusGMainLoop(set_as_default=True)

    log.info("FLA equalisation service starting")

    try:
        service = FlaEqualisationService()

        # Start web UI
        start_web_server()

        # Schedule periodic checks
        GLib.timeout_add_seconds(CHECK_INTERVAL_SEC, service._check)

        log.info("Entering GLib main loop")
        mainloop = GLib.MainLoop()
        mainloop.run()

    except Exception as e:
        log.exception("Fatal error: %s", e)
        raise_alarm("FLA equalisation fatal error: %s" % e)


if __name__ == "__main__":
    main()
