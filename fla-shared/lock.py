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


def acquire(service_name):
    """Acquire the operation lock. Returns True if acquired, False if held by another."""
    if is_locked():
        holder_info = holder()
        log.debug("Lock held by %s since %s", holder_info.get("service"), holder_info.get("started"))
        return False
    try:
        Path(LOCK_FILE).write_text(json.dumps({
            "service": service_name,
            "started": datetime.now().isoformat(),
            "pid": os.getpid(),
        }))
        log.info("Operation lock acquired by %s", service_name)
        return True
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
    """Check if the lock is held. Also cleans stale locks (PID no longer running)."""
    if not Path(LOCK_FILE).exists():
        return False
    try:
        info = json.loads(Path(LOCK_FILE).read_text())
        pid = info.get("pid")
        if pid and not _pid_exists(pid):
            log.warning("Stale lock from PID %s — clearing", pid)
            release()
            return False
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
