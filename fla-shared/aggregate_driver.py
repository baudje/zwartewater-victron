"""Start/stop the dbus-aggregate-batteries service."""

import logging
import subprocess
import time

log = logging.getLogger(__name__)

AGG_SERVICE_PATH = "/service/dbus-aggregate-batteries"


def stop():
    """Stop dbus-aggregate-batteries. Returns True on success."""
    log.info("Stopping dbus-aggregate-batteries...")
    result = subprocess.run(["svc", "-d", AGG_SERVICE_PATH], capture_output=True)
    if result.returncode != 0:
        log.error("Failed to stop aggregate driver: %s", result.stderr.decode())
        return False
    time.sleep(5)
    log.info("Aggregate driver stopped")
    return True


def start():
    """Start dbus-aggregate-batteries. Returns True on success."""
    log.info("Starting dbus-aggregate-batteries...")
    result = subprocess.run(["svc", "-u", AGG_SERVICE_PATH], capture_output=True)
    if result.returncode != 0:
        log.error("Failed to start aggregate driver: %s", result.stderr.decode())
        return False
    log.info("Aggregate driver started")
    return True
