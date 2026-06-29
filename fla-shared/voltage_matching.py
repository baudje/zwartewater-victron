"""Reconnect hold loop — holds the main bus at float, reconnects the LFP bank
when the Trojan<->LFP delta converges.

Shared between FLA equalisation and FLA charge services.

Reconnect is a *stable hold*, not a race. While the relay is open the temp
battery pins the bus to float (≈ the isolated LFP voltage) EVERY cycle, so the
bus never free-falls. The relay is auto-closed ONLY when the delta is within
RELAY_CLOSE_DELTA_MAX. If the bus cannot be brought within threshold, the loop
enters an indefinite safe-hold (float pinned, relay open, alarm re-asserted) and
never returns into the caller's teardown — the only exits are convergence or an
out-of-band manual operator action (restore shore power, or close relay 2 from
the GUI, which collapses the delta the loop then sees as convergence).
"""

import logging
import time

from relay_control import RELAY_CLOSE_DELTA_MAX

log = logging.getLogger(__name__)

POLL_INTERVAL = 2       # seconds between reconnect polls (was 30 — bus is held now)
SETTLE_TIMEOUT = 120    # seconds of non-convergence before declaring safe-hold
MAX_NONE_CYCLES = 10    # consecutive None shunt reads (~20s) before safe-hold


def wait_for_match(monitor, temp_service, status, alerting_mod,
                   voltage_delta_max=RELAY_CLOSE_DELTA_MAX, float_voltage=None,
                   cache_callback=None, max_cycles=None, timeout_hours=None):
    """Hold the bus at float and wait for |V_trojan - V_lfp| <= voltage_delta_max.

    Re-pins float_voltage on the temp battery every cycle so the bus is actively
    held while the relay is open. Returns (True, delta) on convergence.

    On sustained non-convergence (elapsed > SETTLE_TIMEOUT) or sustained
    unreadable shunts (>= MAX_NONE_CYCLES), enters a safe-hold: keeps looping
    with float pinned, relay open, buzzer re-asserted — it does NOT return, so
    the caller never reaches teardown while the LFP is isolated. The firm
    invariant is that the relay is NEVER auto-closed while delta > threshold.

    max_cycles is a TEST-ONLY bound: production passes None (truly indefinite
    safe-hold). When set, the loop returns (False, delta) after that many cycles
    so unit tests of the safe-hold terminate deterministically.

    timeout_hours is accepted but IGNORED — the old 4h convergence timeout was
    replaced by the indefinite safe-hold. Retained so existing call sites that
    still pass it keep working.
    """
    log.info("Reconnect hold: pinning bus to float %s, closing when delta <= %.2fV",
             "%.2fV" % float_voltage if float_voltage is not None else "N/A",
             voltage_delta_max)
    match_start = time.time()
    delta = None
    none_count = 0
    safe_hold = False
    cycle = 0

    while True:
        cycle += 1
        if max_cycles is not None and cycle > max_cycles:
            return False, delta  # test-only bound; production never sets max_cycles

        # Re-pin the hold every cycle so drift / a resume re-asserts float cleanly.
        if float_voltage is not None:
            temp_service.set_charge_voltage(float_voltage)

        elapsed = time.time() - match_start
        v_trojan = monitor.get_trojan_voltage()
        v_lfp = monitor.get_lfp_voltage()

        # Delta unknown — cannot decide to close; keep holding, never close blind.
        if v_trojan is None or v_lfp is None:
            none_count += 1
            status.update(time_remaining=0, trojan_v=v_trojan, lfp_v=v_lfp)
            if none_count >= MAX_NONE_CYCLES:
                if not safe_hold:
                    log.error("Shunt unreadable for %d cycles during reconnect — "
                              "entering safe-hold (bus held at float, relay open)", none_count)
                    if alerting_mod and status:
                        alerting_mod.raise_alarm(
                            "Reconnect: shunt unreadable — bus held, manual intervention required",
                            status_service=status,
                        )
                    safe_hold = True
                elif alerting_mod:
                    alerting_mod.activate_buzzer()
            time.sleep(POLL_INTERVAL)
            continue

        none_count = 0
        delta = abs(v_trojan - v_lfp)
        status.update(time_remaining=0, trojan_v=v_trojan, lfp_v=v_lfp)
        temp_service.update_voltage_current(v_trojan, monitor.get_trojan_current())
        if cache_callback:
            cache_callback(trojan_v=v_trojan, lfp_v=v_lfp,
                           voltage_delta=delta, time_remaining=0)

        if delta <= voltage_delta_max:
            log.info("Voltage converged: Trojan=%.2fV, LFP=%.2fV, delta=%.2fV",
                     v_trojan, v_lfp, delta)
            return True, delta

        # Not converged. A transient undershoot during the initial 31V->27V
        # settle must not false-trigger, so only declare safe-hold once the bus
        # has had SETTLE_TIMEOUT to settle.
        if elapsed > SETTLE_TIMEOUT:
            if not safe_hold:
                log.error("Bus not within %.2fV after %.0fs — entering safe-hold "
                          "(float pinned, relay open). Trojan=%.2fV, LFP=%.2fV, delta=%.2fV",
                          voltage_delta_max, elapsed, v_trojan, v_lfp, delta)
                if alerting_mod and status:
                    alerting_mod.raise_alarm(
                        "Reconnect not converging — bus held at float, manual intervention required",
                        status_service=status,
                    )
                safe_hold = True
            elif alerting_mod:
                alerting_mod.activate_buzzer()
        elif int(elapsed) % 30 < POLL_INTERVAL:
            log.info("Reconnect hold: Trojan=%.2fV, LFP=%.2fV, delta=%.2fV (%.0fs)",
                     v_trojan, v_lfp, delta, elapsed)

        time.sleep(POLL_INTERVAL)
