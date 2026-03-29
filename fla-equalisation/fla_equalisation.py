#!/usr/bin/env python3
"""FLA Equalisation Service for Venus OS.

Automates periodic Trojan L16H-AC equalisation on vessel Zwartewater.
Runs as a persistent daemontools service with GLib main loop.
Checks conditions every 60 seconds, visible on Cerbo GUI Device List.
"""

import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Add velib_python to path
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext", "velib_python"))

from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

from dbus_battery_service import TempBatteryService
from dbus_monitor import DbusMonitor
from dbus_status_service import (
    StatusService, STATE_IDLE, STATE_STOPPING_DRIVER, STATE_DISCONNECTING,
    STATE_EQUALISING, STATE_COOLING_DOWN, STATE_VOLTAGE_MATCHING,
    STATE_RECONNECTING, STATE_RESTARTING_DRIVER, STATE_ERROR,
)
from settings import Settings
from alerting import raise_alarm, clear_alarm
from web_server import start_web_server

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
AGG_SERVICE_PATH = "/service/dbus-aggregate-batteries"
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


def stop_aggregate_driver():
    """Stop dbus-aggregate-batteries service."""
    log.info("Stopping dbus-aggregate-batteries...")
    result = subprocess.run(["svc", "-d", AGG_SERVICE_PATH], capture_output=True)
    if result.returncode != 0:
        log.error("Failed to stop aggregate driver: %s", result.stderr.decode())
        return False
    time.sleep(5)
    log.info("Aggregate driver stopped")
    return True


def start_aggregate_driver():
    """Start dbus-aggregate-batteries service."""
    log.info("Starting dbus-aggregate-batteries...")
    result = subprocess.run(["svc", "-u", AGG_SERVICE_PATH], capture_output=True)
    if result.returncode != 0:
        log.error("Failed to start aggregate driver: %s", result.stderr.decode())
        return False
    log.info("Aggregate driver started")
    return True


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
    temp_service = None
    aggregate_stopped = False

    try:
        # Step 1: Stop aggregate driver
        status.update(state=STATE_STOPPING_DRIVER)
        if not stop_aggregate_driver():
            status.update(state=STATE_ERROR)
            raise_alarm("Failed to stop aggregate driver", status_service=status)
            return False
        aggregate_stopped = True

        # Step 2: Register temporary battery service
        temp_service = TempBatteryService(device_instance=100)
        temp_service.register(
            charge_voltage=settings.eq_voltage,
            charge_current=120.0,
            discharge_current=0,
        )

        # Step 3: Open relay 2 (disconnect LFP direct path)
        status.update(state=STATE_DISCONNECTING)
        if not monitor.set_relay(0):
            status.update(state=STATE_ERROR)
            raise_alarm("Failed to open relay 2", status_service=status)
            return False
        log.info("Relay 2 opened — LFP direct path disconnected, Orion activating")

        # Step 4: Verify LFP disconnected
        time.sleep(10)
        lfp_current = monitor.get_lfp_current()
        if lfp_current is not None and abs(lfp_current) > 5.0:
            status.update(state=STATE_ERROR)
            raise_alarm(
                "LFP current still %.1fA after relay open — relay may not have opened" % abs(lfp_current),
                status_service=status,
            )
            return False

        # Step 5: Equalisation — monitor Trojan current
        status.update(state=STATE_EQUALISING)
        log.info("Starting equalisation at %.1fV", settings.eq_voltage)
        eq_start = time.time()
        eq_timeout = settings.eq_timeout_hours * 3600

        while True:
            elapsed = time.time() - eq_start

            v_trojan = monitor.get_trojan_voltage()
            i_trojan = monitor.get_trojan_current()
            if v_trojan is not None and i_trojan is not None:
                temp_service.update_voltage_current(v_trojan, i_trojan)

            v_lfp = monitor.get_lfp_voltage()
            remaining = max(0, eq_timeout - elapsed)
            status.update(time_remaining=remaining, trojan_v=v_trojan, lfp_v=v_lfp)

            if v_trojan is None:
                status.update(state=STATE_ERROR)
                raise_alarm("SmartShunt Trojan (279) unresponsive during equalisation", status_service=status)
                return False

            if i_trojan is not None and abs(i_trojan) < settings.eq_current_complete:
                log.info(
                    "Equalisation complete: current %.1fA < %.1fA threshold (%.0f min)",
                    abs(i_trojan), settings.eq_current_complete, elapsed / 60,
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

        # Step 6: Reduce CVL to float
        status.update(state=STATE_COOLING_DOWN)
        log.info("Reducing CVL to float voltage %.1fV", settings.float_voltage)
        temp_service.set_charge_voltage(settings.float_voltage)

        # Step 7: Voltage matching
        status.update(state=STATE_VOLTAGE_MATCHING)
        log.info("Waiting for voltage convergence (delta < %.1fV)", settings.voltage_delta_max)
        match_start = time.time()
        match_timeout = settings.voltage_match_timeout_hours * 3600

        while True:
            elapsed = time.time() - match_start

            v_trojan = monitor.get_trojan_voltage()
            v_lfp = monitor.get_lfp_voltage()

            if v_trojan is None:
                status.update(state=STATE_ERROR)
                raise_alarm("SmartShunt Trojan (279) unresponsive during voltage matching",
                    status_service=status)
                return False

            if v_trojan is not None and v_lfp is not None:
                delta = abs(v_trojan - v_lfp)
                remaining = max(0, match_timeout - elapsed)
                status.update(time_remaining=remaining, trojan_v=v_trojan, lfp_v=v_lfp)
                temp_service.update_voltage_current(v_trojan, monitor.get_trojan_current())

                if delta < settings.voltage_delta_max:
                    log.info("Voltage converged: Trojan=%.2fV, LFP=%.2fV, delta=%.2fV",
                        v_trojan, v_lfp, delta)
                    break

                if int(elapsed) % 300 < 30:
                    log.info("Voltage matching: Trojan=%.2fV, LFP=%.2fV, delta=%.2fV (%.0f min)",
                        v_trojan, v_lfp, delta, elapsed / 60)

            if elapsed > match_timeout:
                status.update(state=STATE_ERROR)
                raise_alarm(
                    "Voltage delta did not converge after %.0f hours. "
                    "Trojan=%.2fV, LFP=%.2fV, delta=%.2fV. "
                    "LFPs remain disconnected — manual intervention required."
                    % (elapsed / 3600, v_trojan or 0, v_lfp or 0,
                       delta if (v_trojan and v_lfp) else 0),
                    status_service=status)
                return False

            time.sleep(30)

        # Step 8: Close relay 2
        status.update(state=STATE_RECONNECTING)
        if not monitor.set_relay(1):
            raise_alarm("Failed to close relay 2", status_service=status)
            return False
        log.info("Relay 2 closed — LFP direct path restored")
        time.sleep(5)

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
        if temp_service is not None:
            try:
                temp_service.deregister()
            except Exception:
                pass

        relay_state = monitor.get_relay_state()
        if relay_state == 0:
            log.warning("Safety: relay still open in cleanup — closing")
            monitor.set_relay(1)
            time.sleep(2)

        if aggregate_stopped:
            try:
                start_aggregate_driver()
            except Exception:
                log.error("CRITICAL: Failed to restart aggregate driver in cleanup")


class FlaEqualisationService:
    """Persistent service that checks conditions and runs equalisation."""

    def __init__(self):
        self.settings = Settings()
        self.monitor = DbusMonitor(lfp_instance=277, trojan_instance=279)
        self.status = StatusService()
        self.status.register()
        self._running = False
        self._update_idle_status()
        log.info("FLA equalisation service started — checking every %ds", CHECK_INTERVAL_SEC)

    def _update_idle_status(self):
        """Update status display with idle info."""
        days = days_until_next(self.settings)
        last = read_last_equalisation()
        v_trojan = self.monitor.get_trojan_voltage()
        v_lfp = self.monitor.get_lfp_voltage()
        self.status.update(
            state=STATE_IDLE,
            trojan_v=v_trojan,
            lfp_v=v_lfp,
        )

    def _check(self):
        """Periodic check — called by GLib timer. Returns True to keep running."""
        if self._running:
            return True  # Already running an equalisation, skip

        try:
            # Update idle status with live voltages
            if self.status._service["/State"] == STATE_IDLE:
                self._update_idle_status()

            if should_run(self.settings, self.monitor):
                self._running = True
                try:
                    success = run_equalisation(self.settings, self.monitor, self.status)
                    if success:
                        log.info("Equalisation run completed successfully")
                    else:
                        log.error("Equalisation run failed — check alarms")
                finally:
                    self._running = False
                    self._update_idle_status()

        except Exception as e:
            log.exception("Error in periodic check: %s", e)

        return True  # Always return True to keep the timer running


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
