"""Temporary D-Bus battery service for DVCC CVL control.

Launches a separate subprocess to avoid D-Bus root path conflicts
with the status service running in the main process.
"""

import logging
import os
import signal
import subprocess
import time

log = logging.getLogger(__name__)

CVL_FILE = "/tmp/fla_eq_cvl"
PROCESS_SCRIPT = os.path.join(os.path.dirname(__file__), "temp_battery_process.py")


class TempBatteryService:
    """Manages a temp battery service subprocess for DVCC control."""

    def __init__(self, device_instance=100):
        self._process = None
        self._device_instance = device_instance
        self._registered = False

    def register(self, charge_voltage, charge_current, discharge_current=0):
        """Launch the temp battery service subprocess."""
        try:
            self._process = subprocess.Popen(
                ["python3", PROCESS_SCRIPT, str(charge_voltage), str(charge_current)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self._registered = True
            log.info(
                "Temp battery subprocess started (PID %d): CVL=%.1fV, CCL=%.1fA",
                self._process.pid, charge_voltage, charge_current,
            )
            # Give it time to register on D-Bus
            time.sleep(3)
        except Exception as e:
            log.error("Failed to start temp battery subprocess: %s", e)

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
