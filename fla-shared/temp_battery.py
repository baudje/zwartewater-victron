"""Temporary D-Bus battery service for DVCC CVL control.

Launches a separate subprocess to avoid D-Bus root path conflicts
with the status service running in the main process.
"""

import logging
import os
import signal
import subprocess
import time

import lock

log = logging.getLogger(__name__)

CVL_FILE = "/tmp/fla_temp_cvl"
PROCESS_NAME = "temp_battery_process.py"
PROCESS_SCRIPT = os.path.join(os.path.dirname(__file__), PROCESS_NAME)
# Match the launched interpreter running our script by absolute path. A bare
# filename with pgrep/pkill -f would also hit any unrelated process that merely
# mentions it (an editor, a manual run, a tail/grep on the path) — and we SIGKILL
# what matches, so the matcher must be specific to the process we spawn.
PROCESS_MATCH = "python3 " + PROCESS_SCRIPT


def recover_orphan_temp_battery():
    """Kill a stray temp battery subprocess left running with no operation lock.

    The temp battery subprocess registers com.victronenergy.battery.fla_temp.
    If it outlives its operation — e.g. dbus-daemon is restarted mid-handoff by
    a firmware update — it keeps that name registered in a half-dead state,
    which hangs every Victron dbusmonitor scan (systemcalc, the aggregate
    driver) and takes the whole DVCC chain down. The operation lock guarantees
    only one operation runs at a time, so a temp_battery_process.py running with
    no lock held is definitively an orphan. Call at service startup. Returns
    True if an orphan was found and killed."""
    if lock.is_locked():
        return False  # A real operation owns the temp battery — leave it alone
    try:
        found = subprocess.run(["pgrep", "-f", PROCESS_MATCH], capture_output=True)
    except OSError as e:
        log.warning("Orphan temp-battery check failed (pgrep): %s", e)
        return False
    if found.returncode != 0 or not found.stdout.strip():
        return False  # Nothing running — nothing to recover
    log.warning("Orphaned temp battery subprocess running with no operation lock "
                "— killing to unblock D-Bus discovery")
    try:
        subprocess.run(["pkill", "-9", "-f", PROCESS_MATCH], capture_output=True)
    except OSError as e:
        log.error("Failed to kill orphan temp battery: %s", e)
        return False
    return True


class TempBatteryService:
    """Manages a temp battery service subprocess for DVCC control."""

    def __init__(self, device_instance=100, trojan_instance=279):
        self._process = None
        self._device_instance = device_instance
        self._trojan_instance = trojan_instance
        self._registered = False

    def register(self, charge_voltage, charge_current, discharge_current=0):
        """Launch the temp battery service subprocess.

        Returns True only if the subprocess is still alive after startup.
        D-Bus discovery is verified separately by the caller.
        """
        try:
            self._process = subprocess.Popen(
                ["python3", PROCESS_SCRIPT, str(charge_voltage), str(charge_current),
                 str(self._trojan_instance)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1)
            if self._process.poll() is not None:
                log.error(
                    "Temp battery subprocess exited during startup (code %s)",
                    self._process.returncode,
                )
                self._process = None
                self._registered = False
                return False

            self._registered = True
            log.info(
                "Temp battery subprocess started (PID %d): CVL=%.1fV, CCL=%.1fA",
                self._process.pid, charge_voltage, charge_current,
            )
            return True
        except Exception as e:
            log.error("Failed to start temp battery subprocess: %s", e)
            self._process = None
            self._registered = False
            return False

    def update_voltage_current(self, voltage, current):
        """Voltage/current updated automatically by the subprocess from SmartShunt."""
        pass  # Subprocess reads SmartShunt directly

    def set_charge_voltage(self, voltage):
        """Update the CVL by writing to the shared file."""
        if not self._registered:
            return
        try:
            with open(CVL_FILE, "w") as f:
                f.write(str(voltage))
            log.info("CVL update written: %.2fV", voltage)
        except OSError as e:
            log.warning("Failed to write CVL file: %s", e)

    def deregister(self):
        """Stop the subprocess."""
        if not self._registered or self._process is None:
            return
        try:
            self._process.send_signal(signal.SIGTERM)
            self._process.wait(timeout=5)
            log.info("Temp battery subprocess stopped (PID %d)", self._process.pid)
        except subprocess.TimeoutExpired:
            self._process.kill()
            log.warning("Temp battery subprocess killed (PID %d)", self._process.pid)
        except Exception as e:
            log.warning("Error stopping subprocess: %s", e)
        try:
            os.unlink(CVL_FILE)
        except OSError:
            pass
        self._process = None
        self._registered = False
