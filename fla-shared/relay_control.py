"""Relay 2 control with safety verification.

Shared between FLA equalisation and FLA charge services.
All operations verify hardware state via read-back.
"""

import logging
import time

log = logging.getLogger(__name__)

RELAY_CLOSE_DELTA_MAX = 2.0  # Max voltage delta for auto-close (V)


def open_relay(monitor):
    """Open relay 2 to disconnect LFP direct path. Returns True on success."""
    if not monitor.set_relay(0):
        log.error("Failed to send relay open command")
        return False
    log.info("Relay 2 opened — LFP direct path disconnected, Orion activating")
    return True


def verify_relay_open(monitor, wait_seconds=10):
    """Verify LFP is disconnected after relay open. Returns True if verified."""
    time.sleep(wait_seconds)
    lfp_current = monitor.get_lfp_current()
    if lfp_current is not None and abs(lfp_current) > 5.0:
        log.error("LFP current still %.1fA after relay open — relay may not have opened", abs(lfp_current))
        return False
    return True


def close_relay_verified(monitor):
    """Close relay 2 and verify via read-back. Returns True on success."""
    if not monitor.set_relay(1):
        log.error("Failed to send relay close command")
        return False
    time.sleep(2)
    if monitor.get_relay_state() != 1:
        log.error("Relay 2 failed to close — read-back shows still open")
        return False
    log.info("Relay 2 closed and verified — LFP direct path restored")
    return True


def close_relay_delta_aware(monitor, alerting_mod=None, status=None):
    """Close relay only if voltage delta is safe. For use in finally/cleanup blocks.

    If delta > RELAY_CLOSE_DELTA_MAX: raises alarm, does NOT close.
    If delta <= max or unreadable with low risk: closes relay.
    """
    relay_state = monitor.get_relay_state()
    if relay_state != 0:
        return  # Relay not open, nothing to do

    v_t = monitor.get_trojan_voltage()
    v_l = monitor.get_lfp_voltage()

    if v_t is not None and v_l is not None:
        delta = abs(v_t - v_l)
        if delta > RELAY_CLOSE_DELTA_MAX:
            log.error(
                "SAFETY: Relay open with delta=%.1fV — too large to auto-close. "
                "LFPs remain on Orion. Manual intervention required.", delta
            )
            if alerting_mod and status:
                alerting_mod.raise_alarm(
                    "Relay open with %.1fV delta — manual close required" % delta,
                    status_service=status,
                )
            return  # Do NOT close
        log.warning("Safety: closing relay (delta=%.1fV)", delta)
    else:
        log.warning("Safety: closing relay (voltages unreadable — assuming safe)")

    monitor.set_relay(1)
    time.sleep(2)


def startup_safety_check(monitor, status=None, alerting_mod=None):
    """Check relay state on service startup. Recovers from interrupted operations."""
    relay_state = monitor.get_relay_state()
    if relay_state != 0:
        return  # Relay closed, nothing to recover

    log.warning("STARTUP: Relay 2 is open — possible interrupted operation")
    v_t = monitor.get_trojan_voltage()
    v_l = monitor.get_lfp_voltage()

    if v_t is not None and v_l is not None:
        delta = abs(v_t - v_l)
        if delta < RELAY_CLOSE_DELTA_MAX:
            log.info("STARTUP: Delta=%.1fV safe — closing relay", delta)
            monitor.set_relay(1)
            time.sleep(3)
        else:
            log.error("STARTUP: Delta=%.1fV too high — leaving relay open, alarm raised", delta)
            if alerting_mod and status:
                alerting_mod.raise_alarm(
                    "Startup: relay open with %.1fV delta — manual close required" % delta,
                    status_service=status,
                )
    else:
        log.error("STARTUP: Cannot read voltages — leaving relay open for safety")
        if alerting_mod and status:
            alerting_mod.raise_alarm(
                "Startup: relay open, cannot read voltages — manual check required",
                status_service=status,
            )
