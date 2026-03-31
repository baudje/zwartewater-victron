#!/usr/bin/env python3
"""FLA Charge Service for Venus OS.

Detects undercharged Trojan FLA batteries and runs a full bulk+absorption
charge cycle by temporarily disconnecting LFPs.
Runs as a persistent daemontools service with GLib main loop.
"""

import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/data/apps/fla-shared")
sys.path.insert(1, os.path.join("/data/apps/fla-shared", "ext", "velib_python"))

from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

from dbus_monitor import DbusMonitor
from temp_battery import TempBatteryService
from relay_control import (
    open_relay, verify_relay_open, verify_relay_still_open,
    close_relay_verified, close_relay_delta_aware, startup_safety_check,
)
from voltage_matching import wait_for_match
from aggregate_driver import stop as stop_aggregate, start as start_aggregate
from lock import acquire as acquire_lock, release as release_lock
from temp_compensation import compensate as temp_compensate
import alerting

from dbus_status_service import (
    StatusService, STATE_IDLE, STATE_PHASE1_SHARED, STATE_STOPPING_DRIVER,
    STATE_DISCONNECTING, STATE_PHASE2_BULK, STATE_PHASE3_ABSORPTION,
    STATE_COOLING_DOWN, STATE_VOLTAGE_MATCHING, STATE_RECONNECTING,
    STATE_RESTARTING_DRIVER, STATE_ERROR,
)
from settings import Settings
from web_server import start_web_server, update_cache, check_run_now, _cache

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
    """Check if AC input is active (shore power or generator)."""
    try:
        import dbus
        bus = monitor.bus
        obj = bus.get_object("com.victronenergy.vebus.ttyS4", "/Ac/ActiveIn/ActiveInput")
        iface = dbus.Interface(obj, "com.victronenergy.BusItem")
        value = iface.GetValue()
        # 0 = AC-in-1, 1 = AC-in-2, 240 = disconnected
        return int(value) in (0, 1)
    except Exception:
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
    temp_service = None
    aggregate_stopped = False
    original_dvcc_voltage = None
    original_battery_service = None

    if not acquire_lock("fla-charge"):
        log.info("Operation lock held — skipping")
        return False

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
                return False

            if int(elapsed) % 300 < 30:
                log.info("Phase 1: %.0f min, LFP SoC=%.0f%%, Trojan SoC=%.0f%%, I=%.0fA, max cell=%.3fV",
                         elapsed / 60, lfp_soc or 0, trojan_soc or 0, abs(charge_current or 0), max_cell_v or 0)

            time.sleep(30)

        # === PHASE 2: FLA-only bulk ===
        # Crash-safe: register at current bus voltage first
        current_voltage = v_lfp or v_trojan or 28.0
        temp_service = TempBatteryService(device_instance=100)
        temp_service.register(
            charge_voltage=current_voltage,
            charge_current=60.0,  # FLA recommended max bulk current
            discharge_current=0,
        )

        status.update(state=STATE_STOPPING_DRIVER)
        if not stop_aggregate():
            status.update(state=STATE_ERROR)
            alerting.raise_alarm("Failed to stop aggregate driver", status_service=status)
            return False
        aggregate_stopped = True

        # Switch DVCC to our temp battery service
        original_battery_service = monitor.get_battery_service_setting()
        log.info("Saving BatteryService setting: %s", original_battery_service)
        monitor.set_battery_service_setting("com.victronenergy.battery/100")
        time.sleep(5)

        status.update(state=STATE_DISCONNECTING)
        if not open_relay(monitor):
            status.update(state=STATE_ERROR)
            alerting.raise_alarm("Failed to open relay 2", status_service=status)
            return False

        if not verify_relay_open(monitor):
            status.update(state=STATE_ERROR)
            alerting.raise_alarm("LFP not disconnected after relay open", status_service=status)
            return False

        lfp_voltage_at_disconnect = monitor.get_lfp_voltage()
        if lfp_voltage_at_disconnect:
            log.info("LFP voltage at disconnect: %.2fV", lfp_voltage_at_disconnect)

        # Raise DVCC system limit and CVL — LFPs are disconnected, safe
        battery_temp = monitor.get_battery_temperature()
        abs_voltage = temp_compensate(settings.fla_bulk_voltage, battery_temp)
        original_dvcc_voltage = monitor.get_dvcc_max_charge_voltage()
        log.info("Saving DVCC MaxChargeVoltage: %.1fV", original_dvcc_voltage or 0)
        monitor.set_dvcc_max_charge_voltage(abs_voltage + 0.5)
        temp_service.set_charge_voltage(abs_voltage)
        log.info("CVL raised to FLA bulk voltage: %.2fV (base %.2fV, temp %s)",
                 abs_voltage, settings.fla_bulk_voltage,
                 "%.1f°C" % battery_temp if battery_temp is not None else "N/A")

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

            if v_trojan is not None and i_trojan is not None:
                temp_service.update_voltage_current(v_trojan, i_trojan)

            current_state = STATE_PHASE3_ABSORPTION if elapsed > 300 else STATE_PHASE2_BULK
            delta = round(abs(v_trojan - v_lfp), 2) if (v_trojan and v_lfp) else None
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

            # Verify relay still open — external close at 29.64V would damage LFPs
            if not verify_relay_still_open(monitor, settings.fla_bulk_voltage):
                status.update(state=STATE_ERROR)
                alerting.raise_alarm("Relay closed externally during absorption — aborting", status_service=status)
                return False

            # Orion failure detection
            if (lfp_voltage_at_disconnect is not None and v_lfp is not None
                    and v_lfp < lfp_voltage_at_disconnect - 0.5):
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

            if int(elapsed) % 300 < 30:
                log.info("Absorption: %.0f min, V=%.1fV, I=%.1fA",
                         elapsed / 60, v_trojan or 0, i_trojan or 0)

            time.sleep(30)

        # === PHASE 4: Voltage matching + reconnect ===
        status.update(state=STATE_VOLTAGE_MATCHING)
        update_cache(state=STATE_VOLTAGE_MATCHING)
        def _vm_cache_cb(**kwargs):
            update_cache(state=STATE_VOLTAGE_MATCHING, **kwargs)

        matched, delta = wait_for_match(
            monitor, temp_service, status, alerting,
            voltage_delta_max=settings.voltage_delta_max,
            timeout_hours=settings.voltage_match_timeout_hours,
            float_voltage=temp_compensate(settings.fla_float_voltage, battery_temp),
            cache_callback=_vm_cache_cb,
        )
        if not matched:
            return False

        status.update(state=STATE_RECONNECTING)
        if not close_relay_verified(monitor):
            alerting.raise_alarm("Failed to close relay 2", status_service=status)
            return False

        # Measure inrush
        time.sleep(1)
        inrush = monitor.get_lfp_current()
        status.update(
            inrush_current=abs(inrush) if inrush else None,
            reconnect_delta=delta,
        )
        log.info("Reconnect: inrush=%.1fA, delta=%.2fV", abs(inrush) if inrush else 0, delta or 0)
        time.sleep(2)

        # Cleanup
        temp_service.deregister()
        temp_service = None

        status.update(state=STATE_RESTARTING_DRIVER)
        if not start_aggregate():
            alerting.raise_alarm("Failed to restart aggregate driver", status_service=status)
            return False
        monitor.invalidate_services()

        write_last_charge()
        status.update(state=STATE_IDLE, time_remaining=0)
        alerting.clear_alarm(status_service=status)
        log.info("FLA charge completed successfully")
        return True

    except Exception as e:
        log.exception("Unexpected error: %s", e)
        alerting.raise_alarm("FLA charge error: %s" % e, status_service=status)
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
            try: temp_service.deregister()
            except Exception: pass
        close_relay_delta_aware(monitor, alerting, status)
        if aggregate_stopped:
            try: start_aggregate()
            except Exception: log.error("CRITICAL: Failed to restart aggregate driver")
        release_lock()


class FlaChargeService:
    """Persistent service that checks conditions and runs FLA charge."""

    def __init__(self):
        self.settings = Settings()
        self.monitor = DbusMonitor(lfp_instance=277, trojan_instance=279)
        self.status = StatusService()
        self.status.register()
        self._running = False
        startup_safety_check(self.monitor, self.status, alerting)
        self._update_idle_status()
        log.info("FLA charge service started — checking every %ds", CHECK_INTERVAL_SEC)

    def _update_idle_status(self):
        v_trojan = self.monitor.get_trojan_voltage()
        v_lfp = self.monitor.get_lfp_voltage()
        trojan_soc = self.monitor.get_trojan_soc()
        lfp_soc = self.monitor.get_lfp_soc()
        delta = round(abs(v_trojan - v_lfp), 2) if (v_trojan and v_lfp) else None
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
        pending = _cache.pop("pending_settings", None)
        if not pending: return
        for key, value in pending.items():
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
        if self._running: return True
        try:
            self._apply_pending_settings()
            if check_run_now():
                self.settings._write("run_now", 1)
            self._update_idle_status()
            if should_run(self.settings, self.monitor):
                self._running = True
                _cache["run_now_requested"] = False
                success = False
                try:
                    success = run_charge(self.settings, self.monitor, self.status)
                    if success:
                        log.info("FLA charge completed successfully")
                    else:
                        log.error("FLA charge failed — check alarms")
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
