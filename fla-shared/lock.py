"""File-based operation lock for FLA services.

Prevents equalisation and charge from running simultaneously.
Lock file at /data/apps/fla-shared/operation.lock
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

LOCK_FILE = "/data/apps/fla-shared/operation.lock"
SERVICE_MARKERS = {
    "fla-equalisation": "fla_equalisation.py",
    "fla-charge": "fla_charge.py",
}


def acquire(service_name):
    """Acquire the operation lock atomically. Returns True if acquired, False if held by another."""
    # Clean stale locks first (PID no longer running)
    if Path(LOCK_FILE).exists():
        try:
            info = json.loads(Path(LOCK_FILE).read_text())
            pid = info.get("pid")
            if pid and _pid_exists(pid) and _pid_matches_service(pid, info.get("service")):
                log.debug("Lock held by %s since %s (PID %s)",
                          info.get("service"), info.get("started"), pid)
                return False
            log.warning("Stale lock from PID %s — clearing", pid)
            Path(LOCK_FILE).unlink(missing_ok=True)
        except (json.JSONDecodeError, OSError):
            Path(LOCK_FILE).unlink(missing_ok=True)

    # Atomic creation with O_EXCL — prevents TOCTOU race
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            content = json.dumps({
                "service": service_name,
                "started": datetime.now().isoformat(),
                "pid": os.getpid(),
            })
            os.write(fd, content.encode())
        finally:
            os.close(fd)
        log.info("Operation lock acquired by %s", service_name)
        return True
    except FileExistsError:
        # Another process acquired between our stale check and open
        log.debug("Lock acquired by another process during race window")
        return False
    except OSError as e:
        log.error("Failed to acquire lock: %s", e)
        return False


def release():
    """Release the operation lock."""
    try:
        Path(LOCK_FILE).unlink(missing_ok=True)
        log.info("Operation lock released")
    except OSError as e:
        log.warning("Failed to release lock: %s", e)


def is_locked():
    """Check if the lock is held by a live process."""
    if not Path(LOCK_FILE).exists():
        return False
    try:
        info = json.loads(Path(LOCK_FILE).read_text())
        pid = info.get("pid")
        if pid and (not _pid_exists(pid) or not _pid_matches_service(pid, info.get("service"))):
            return False  # Stale — acquire() will clean it
        return True
    except (json.JSONDecodeError, OSError):
        return False


def holder():
    """Return info about the lock holder, or empty dict."""
    try:
        return json.loads(Path(LOCK_FILE).read_text())
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return {}


def _pid_exists(pid):
    """Check if a process with given PID exists."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def _pid_matches_service(pid, service_name):
    """Best-effort check that a live PID still belongs to the expected FLA service."""
    marker = SERVICE_MARKERS.get(service_name)
    if not marker:
        return True

    proc_cmdline = Path("/proc") / str(pid) / "cmdline"
    try:
        cmdline = proc_cmdline.read_bytes().decode("utf-8", errors="ignore").replace("\x00", " ")
    except OSError:
        return True

    return marker in cmdline
