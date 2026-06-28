"""Relay 2 control with safety verification.

Shared between FLA equalisation and FLA charge services.
All operations verify hardware state via read-back.
"""

import logging
import time

import lock

log = logging.getLogger(__name__)

RELAY_CLOSE_DELTA_MAX = 1.0  # Max voltage delta for auto-close (V)
LFP_SAFE_CVL = 28.4          # Max CVL safe for LFPs (3.55V × 8 cells)
RELAY_OPEN_MIN_DIVERGENCE = 0.15  # Min LFP<->Trojan divergence (V) proving isolation


def open_relay(monitor):
    """Open relay 2 to disconnect LFP direct path. Returns True on success."""
    if not monitor.set_relay(0):
        log.error("Failed to send relay open command")
        return False
    log.info("Relay 2 opened — LFP direct path disconnected, Orion activating")
    return True


def verify_relay_open(monitor, settle_seconds=10, poll_attempts=10,
                      poll_interval=2.0, min_divergence=RELAY_OPEN_MIN_DIVERGENCE):
    """Verify the LFP bank is isolated from the main bus after opening relay 2.

    The old check used LFP current (< 5A) as the disconnection proxy, which is
    invalid in this topology: opening relay 2 activates the Orion DC-DC, which
    CC-charges the LFP at its configured current (~15A on SmartShunt 277), so
    LFP current stays high even when the relay opened correctly. Require two
    signals the Orion can't spoof:

      1. the GX relay reads open (/Relay/1/State == 0), and
      2. the LFP voltage has diverged from the Trojan/main-bus voltage — once
         isolated the Orion holds the LFP at a different voltage than the main
         bus, whereas a welded relay forces the two to track each other. This
         is the physical isolation proof the current check was meant to give.

    The divergence threshold is derived from observed values (~0.03V while the
    banks are connected vs ~0.36V once isolated). Returns True only if both
    signals hold within the poll window.
    """
    time.sleep(settle_seconds)

    relay_state = monitor.get_relay_state()
    if relay_state != 0:
        log.error("Relay 2 read-back is %s, not open — cannot verify disconnection", relay_state)
        return False

    # Wait for the Orion to pull the isolated LFP bank away from the main-bus
    # voltage; a welded relay would keep the two locked together.
    for _ in range(poll_attempts):
        v_lfp = monitor.get_lfp_voltage()
        v_trojan = monitor.get_trojan_voltage()
        if v_lfp is not None and v_trojan is not None:
            divergence = abs(v_lfp - v_trojan)
            if divergence >= min_divergence:
                log.info("Relay open verified: state=0, LFP/Trojan divergence %.2fV", divergence)
                return True
        time.sleep(poll_interval)

    log.error("LFP/Trojan voltage did not diverge >= %.2fV after relay open — "
              "banks may still be connected (welded relay?); aborting", min_divergence)
    return False


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
        log.error(
            "SAFETY: Cannot read voltages — leaving relay open for safety. "
            "Manual intervention required."
        )
        if alerting_mod and status:
            alerting_mod.raise_alarm(
                "Relay open, voltages unreadable — manual close required",
                status_service=status,
            )
        return  # Do NOT close

    if not monitor.set_relay(1):
        log.error("SAFETY: Failed to send relay close command during cleanup")
        if alerting_mod and status:
            alerting_mod.raise_alarm(
                "Relay close command failed — manual close required",
                status_service=status,
            )
        return
    time.sleep(2)
    if monitor.get_relay_state() != 1:
        log.error("SAFETY: Relay read-back still open after close command")
        if alerting_mod and status:
            alerting_mod.raise_alarm(
                "Relay close failed (read-back) — manual close required",
                status_service=status,
            )


def verify_relay_still_open(monitor, current_cvl):
    """Verify relay is still open during high-voltage charging.

    Called every iteration in EQ/charge loops. If relay is found closed
    while CVL > LFP safe voltage, LFPs are exposed to dangerous voltage.
    Returns True if safe (relay open), False if unsafe (relay closed at high CVL).
    """
    relay_state = monitor.get_relay_state()
    if relay_state == 0:
        return True  # Relay open — safe

    # Relay is closed but CVL is above LFP safe voltage — CRITICAL
    if current_cvl > LFP_SAFE_CVL:
        log.error(
            "CRITICAL: Relay 2 closed externally while CVL=%.1fV! "
            "LFPs exposed to dangerous voltage. Aborting immediately.", current_cvl
        )
        return False

    # Relay closed but CVL is safe (e.g., during voltage matching at 27.0V)
    return True


def startup_safety_check(monitor, status=None, alerting_mod=None):
    """Check relay state on service startup. Recovers from interrupted operations."""
    # If the operation lock is held, the OTHER FLA service is mid-operation and
    # owns the relay (legitimately open during a charge/equalisation handoff).
    # Closing it here would stomp on a live run — leave it alone. Only a stale
    # lock (crashed holder) frees this path to recover an interrupted operation.
    if lock.is_locked():
        log.info("STARTUP: operation lock held by another service — skipping relay recovery")
        return

    relay_state = monitor.get_relay_state()
    if relay_state != 0:
        return  # Relay closed, nothing to recover

    log.warning("STARTUP: Relay 2 is open — possible interrupted operation")
    v_t = monitor.get_trojan_voltage()
    v_l = monitor.get_lfp_voltage()

    if v_t is not None and v_l is not None:
        delta = abs(v_t - v_l)
        if delta <= RELAY_CLOSE_DELTA_MAX:
            log.info("STARTUP: Delta=%.1fV safe — closing relay", delta)
            if not monitor.set_relay(1):
                log.error("STARTUP: Failed to send relay close command")
                if alerting_mod and status:
                    alerting_mod.raise_alarm(
                        "Startup: relay close command failed — manual close required",
                        status_service=status,
                    )
                return
            time.sleep(3)
            if monitor.get_relay_state() != 1:
                log.error("STARTUP: Relay read-back still open after close command")
                if alerting_mod and status:
                    alerting_mod.raise_alarm(
                        "Startup: relay close failed (read-back) — manual close required",
                        status_service=status,
                    )
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
