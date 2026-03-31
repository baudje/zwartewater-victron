#!/usr/bin/env python3
"""FLA Equalisation Service for Venus OS.

Automates periodic Trojan L16H-AC equalisation on vessel Zwartewater.
Runs as a persistent daemontools service with GLib main loop.
Checks conditions every 60 seconds, visible on Cerbo GUI Device List.
"""

import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Add shared modules and velib_python to path
sys.path.insert(0, "/data/apps/fla-shared")
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext", "velib_python"))

from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

from temp_battery import TempBatteryService
from dbus_monitor import DbusMonitor
from dbus_status_service import (
    StatusService, STATE_IDLE, STATE_STOPPING_DRIVER, STATE_DISCONNECTING,
    STATE_EQUALISING, STATE_COOLING_DOWN, STATE_VOLTAGE_MATCHING,
    STATE_RECONNECTING, STATE_RESTARTING_DRIVER, STATE_ERROR,
)
from settings import Settings
import alerting
from alerting import raise_alarm, clear_alarm
from relay_control import open_relay, verify_relay_open, verify_relay_still_open, close_relay_verified, close_relay_delta_aware, startup_safety_check
from voltage_matching import wait_for_match
from aggregate_driver import stop as stop_aggregate_driver, start as start_aggregate_driver
from temp_compensation import compensate as temp_compensate
from lock import acquire as acquire_lock, release as release_lock
from web_server import start_web_server, update_cache, check_run_now, _cache

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
    """Check if all scheduling conditions are met."""
    if not settings.enabled:
        return False

    # Check RunNow override (bypasses interval and time window, NOT SoC)
    run_now_flag = settings.run_now
    if run_now_flag:
        log.info("RunNow flag set — bypassing interval and time window checks")
        settings.clear_run_now()

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

    log.info("All conditions met: SoC=%.1f%%, time=%s", soc, datetime.now().strftime("%H:%M"))
    return True


def run_equalisation(settings, monitor, status):
    """Execute the full equalisation sequence. Returns True on success."""
    if not acquire_lock("fla-equalisation"):
        log.warning("Operation lock held — skipping equalisation")
        return False

    temp_service = None
    aggregate_stopped = False
    original_dvcc_voltage = None
    original_battery_service = None

    try:
        # Step 1: Register temporary battery service at SAFE voltage first
        # (not equalisation voltage — protect LFPs if crash before relay opens)
        temp_service = TempBatteryService(device_instance=100)
        temp_service.register(
            charge_voltage=28.4,  # Safe LFP voltage — raised to EQ after relay opens
            charge_current=60.0,  # FLA recommended max bulk current
            discharge_current=0,
        )

        # Step 2: Stop aggregate driver
        status.update(state=STATE_STOPPING_DRIVER)
        if not stop_aggregate_driver():
            status.update(state=STATE_ERROR)
            raise_alarm("Failed to stop aggregate driver", status_service=status)
            return False
        aggregate_stopped = True

        # Switch DVCC to our temp battery service
        original_battery_service = monitor.get_battery_service_setting()
        log.info("Saving BatteryService setting: %s", original_battery_service)
        monitor.set_battery_service_setting("com.victronenergy.battery/100")
        time.sleep(5)  # Let DVCC switch

        # Step 3: Open relay 2 (disconnect LFP direct path)
        status.update(state=STATE_DISCONNECTING)
        if not open_relay(monitor):
            status.update(state=STATE_ERROR)
            raise_alarm("Failed to open relay 2", status_service=status)
            return False
        if not verify_relay_open(monitor):
            status.update(state=STATE_ERROR)
            raise_alarm("LFP not disconnected after relay open", status_service=status)
            return False

        # Record LFP voltage at disconnect for Orion failure detection (CRIT-3)
        lfp_voltage_at_disconnect = monitor.get_lfp_voltage()
        if lfp_voltage_at_disconnect is not None:
            log.info("LFP voltage at disconnect: %.2fV", lfp_voltage_at_disconnect)

        # Step 5: Raise DVCC system limit and CVL — LFPs are disconnected, safe
        battery_temp = monitor.get_battery_temperature()
        eq_voltage = temp_compensate(settings.eq_voltage, battery_temp)
        original_dvcc_voltage = monitor.get_dvcc_max_charge_voltage()
        log.info("Saving DVCC MaxChargeVoltage: %.1fV", original_dvcc_voltage or 0)
        monitor.set_dvcc_max_charge_voltage(eq_voltage + 0.5)  # Headroom above target
        temp_service.set_charge_voltage(eq_voltage)
        log.info("CVL raised to equalisation voltage: %.1fV (base %.1fV, temp %s)",
                 eq_voltage, settings.eq_voltage,
                 "%.1f°C" % battery_temp if battery_temp is not None else "N/A")

        # Step 6: Equalisation — monitor Trojan current
        status.update(state=STATE_EQUALISING)
        log.info("Starting equalisation at %.1fV", settings.eq_voltage)
        eq_start = time.time()
        eq_timeout = settings.eq_timeout_hours * 3600
        i_trojan_none_count = 0  # IMP-4: track consecutive None readings

        while True:
            elapsed = time.time() - eq_start

            v_trojan = monitor.get_trojan_voltage()
            i_trojan = monitor.get_trojan_current()
            if v_trojan is not None and i_trojan is not None:
                temp_service.update_voltage_current(v_trojan, i_trojan)

            v_lfp = monitor.get_lfp_voltage()
            remaining = max(0, eq_timeout - elapsed)
            delta = round(abs(v_trojan - v_lfp), 2) if (v_trojan and v_lfp) else None
            status.update(time_remaining=remaining, trojan_v=v_trojan, lfp_v=v_lfp)
            update_cache(
                state=STATE_EQUALISING, time_remaining=remaining,
                trojan_v=v_trojan, lfp_v=v_lfp, voltage_delta=delta,
            )

            if v_trojan is None:
                status.update(state=STATE_ERROR)
                raise_alarm("SmartShunt Trojan (279) unresponsive during equalisation", status_service=status)
                return False

            # Verify relay still open — external close at 31.5V would damage LFPs
            if not verify_relay_still_open(monitor, settings.eq_voltage):
                status.update(state=STATE_ERROR)
                raise_alarm("Relay closed externally during EQ — aborting", status_service=status)
                return False

            # CRIT-3: Detect Orion failure — LFP voltage dropping
            if (lfp_voltage_at_disconnect is not None and v_lfp is not None
                    and v_lfp < lfp_voltage_at_disconnect - 0.5):
                log.warning("LFP voltage dropping (%.2fV -> %.2fV) — possible Orion failure",
                            lfp_voltage_at_disconnect, v_lfp)
                raise_alarm("LFP voltage dropping — Orion may have failed", status_service=status)
                return False

            # IMP-4: Handle i_trojan=None with a counter
            if i_trojan is None:
                i_trojan_none_count += 1
                if i_trojan_none_count >= 10:
                    log.warning("Trojan current unreadable for 5 min — proceeding to voltage matching")
                    break
            else:
                i_trojan_none_count = 0

            # IMP-6: Warn on high Trojan charge current
            if i_trojan is not None and abs(i_trojan) > 60:
                log.warning("High Trojan charge current: %.1fA (dynamo/MPPT active?)", abs(i_trojan))

            # Only check current completion AFTER voltage reaches the target
            # Use compensated voltage minus 0.1V margin for wiring losses
            voltage_reached = v_trojan is not None and v_trojan >= (eq_voltage - 0.1)
            if voltage_reached and i_trojan is not None and abs(i_trojan) < settings.eq_current_complete:
                log.info(
                    "Equalisation complete: V=%.1fV (target %.1fV), current %.1fA < %.1fA threshold (%.0f min)",
                    v_trojan, eq_voltage, abs(i_trojan), settings.eq_current_complete, elapsed / 60,
                )
                break

            if elapsed > eq_timeout:
                log.warning("Equalisation timeout after %.0f min, current %.1fA",
                    elapsed / 60, abs(i_trojan) if i_trojan else 0)
                break

            if int(elapsed) % 300 < 30:
                log.info("Equalising: %.0f min, V=%.1fV, I=%.1fA",
                    elapsed / 60, v_trojan or 0, i_trojan or 0)

            time.sleep(30)

        # Step 6: Reduce CVL to float + voltage matching
        status.update(state=STATE_VOLTAGE_MATCHING)
        update_cache(state=STATE_VOLTAGE_MATCHING)
        def _vm_cache_cb(**kwargs):
            update_cache(state=STATE_VOLTAGE_MATCHING, **kwargs)

        matched, delta = wait_for_match(
            monitor, temp_service, status, alerting,
            voltage_delta_max=settings.voltage_delta_max,
            timeout_hours=settings.voltage_match_timeout_hours,
            float_voltage=temp_compensate(settings.float_voltage, battery_temp),
            cache_callback=_vm_cache_cb,
        )
        if not matched:
            return False

        # Step 8: Close relay 2
        status.update(state=STATE_RECONNECTING)
        if not close_relay_verified(monitor):
            raise_alarm("Failed to close relay 2", status_service=status)
            return False

        # New feature: Register inrush current on reconnect
        time.sleep(1)
        inrush = monitor.get_lfp_current()
        v_t_reconnect = monitor.get_trojan_voltage()
        v_l_reconnect = monitor.get_lfp_voltage()
        reconnect_delta = abs(v_t_reconnect - v_l_reconnect) if (v_t_reconnect and v_l_reconnect) else None
        log.info("Reconnect: inrush=%.1fA, delta=%.2fV",
                 abs(inrush) if inrush else 0, reconnect_delta or 0)
        status.update(
            inrush_current=abs(inrush) if inrush else None,
            reconnect_delta=reconnect_delta,
        )
        time.sleep(2)

        # Step 9: Deregister temp service
        temp_service.deregister()
        temp_service = None

        # Step 10: Restart aggregate driver
        status.update(state=STATE_RESTARTING_DRIVER)
        if not start_aggregate_driver():
            raise_alarm("Failed to restart aggregate driver", status_service=status)
            return False
        monitor.invalidate_services()

        # Step 11: Record success
        write_last_equalisation()
        status.update(state=STATE_IDLE, time_remaining=0)
        log.info("Equalisation completed successfully")
        clear_alarm(status_service=status)
        return True

    except Exception as e:
        log.exception("Unexpected error during equalisation: %s", e)
        raise_alarm("Equalisation script error: %s" % e, status_service=status)
        return False

    finally:
        # Restore DVCC settings before anything else
        if original_battery_service is not None:
            try:
                monitor.set_battery_service_setting(original_battery_service)
                log.info("BatteryService restored to %s", original_battery_service)
            except Exception:
                log.error("CRITICAL: Failed to restore BatteryService setting")

        if original_dvcc_voltage is not None:
            try:
                monitor.set_dvcc_max_charge_voltage(original_dvcc_voltage)
                log.info("DVCC MaxChargeVoltage restored to %.1fV", original_dvcc_voltage)
            except Exception:
                log.error("CRITICAL: Failed to restore DVCC MaxChargeVoltage")

        if temp_service is not None:
            try:
                temp_service.deregister()
            except Exception:
                pass

        # CRIT-2: Check voltage delta before closing relay
        close_relay_delta_aware(monitor, alerting, status)

        if aggregate_stopped:
            try:
                start_aggregate_driver()
            except Exception:
                log.error("CRITICAL: Failed to restart aggregate driver in cleanup")

        release_lock()


class FlaEqualisationService:
    """Persistent service that checks conditions and runs equalisation."""

    def __init__(self):
        self.settings = Settings()
        self.monitor = DbusMonitor(lfp_instance=277, trojan_instance=279)
        self.status = StatusService()
        self.status.register()
        self._running = False
        self._startup_safety_check()
        self._update_idle_status()
        log.info("FLA equalisation service started — checking every %ds", CHECK_INTERVAL_SEC)

    def _startup_safety_check(self):
        """Check relay state on startup — recover from interrupted equalisation."""
        startup_safety_check(self.monitor, self.status, alerting)

    def _update_idle_status(self):
        """Update status display and web cache with idle info."""
        v_trojan = self.monitor.get_trojan_voltage()
        v_lfp = self.monitor.get_lfp_voltage()
        delta = round(abs(v_trojan - v_lfp), 2) if (v_trojan and v_lfp) else None
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

        pending = _cache.pop("pending_settings", None)
        if not pending:
            return
        for key, value in pending.items():
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
            return True

        try:
            # Apply any pending settings from web UI
            self._apply_pending_settings()

            # Check web UI RunNow button
            if check_run_now():
                self.settings._write("run_now", 1)

            # Update idle status with live voltages
            self._update_idle_status()

            if should_run(self.settings, self.monitor):
                self._running = True
                _cache["run_now_requested"] = False  # CRIT-5: Clear flag after eq starts
                success = False
                try:
                    success = run_equalisation(self.settings, self.monitor, self.status)
                    if success:
                        log.info("Equalisation run completed successfully")
                    else:
                        log.error("Equalisation run failed — check alarms")
                finally:
                    self._running = False
                    if success:
                        self._update_idle_status()
                    else:
                        # Preserve error state — only update live voltages
                        update_cache(
                            trojan_v=self.monitor.get_trojan_voltage(),
                            lfp_v=self.monitor.get_lfp_voltage(),
                        )

        except Exception as e:
            log.exception("Error in periodic check: %s", e)

        return True


def main():
    """Entry point: start persistent service with GLib main loop."""
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
