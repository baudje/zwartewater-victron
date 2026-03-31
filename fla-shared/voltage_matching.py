"""Voltage matching loop — waits for Trojan/LFP delta to converge before reconnect.

Shared between FLA equalisation and FLA charge services.
"""

import logging
import time

log = logging.getLogger(__name__)


def wait_for_match(monitor, temp_service, status, alerting_mod,
                   voltage_delta_max=1.0, timeout_hours=4.0,
                   float_voltage=None, cache_callback=None):
    """Wait for |V_trojan - V_lfp| < voltage_delta_max.

    If float_voltage is provided, reduces CVL to that voltage first.
    Returns (True, delta) on success, (False, delta) on timeout/error.
    """
    if float_voltage is not None:
        log.info("Reducing CVL to float voltage %.1fV", float_voltage)
        temp_service.set_charge_voltage(float_voltage)

    log.info("Waiting for voltage convergence (delta < %.1fV)", voltage_delta_max)
    match_start = time.time()
    match_timeout = timeout_hours * 3600
    delta = None
    lfp_none_count = 0

    while True:
        elapsed = time.time() - match_start
        v_trojan = monitor.get_trojan_voltage()
        v_lfp = monitor.get_lfp_voltage()

        # SmartShunt Trojan responsive check
        if v_trojan is None:
            log.error("SmartShunt Trojan unresponsive during voltage matching")
            if alerting_mod and status:
                alerting_mod.raise_alarm(
                    "SmartShunt Trojan unresponsive during voltage matching",
                    status_service=status,
                )
            return False, delta

        # SmartShunt LFP responsive check
        if v_trojan is not None and v_lfp is None:
            lfp_none_count += 1
            if lfp_none_count >= 10:  # 10 × 30s = 5 minutes
                log.error("SmartShunt LFP unresponsive for 5 minutes during voltage matching")
                if alerting_mod and status:
                    alerting_mod.raise_alarm(
                        "SmartShunt LFP unresponsive during voltage matching",
                        status_service=status,
                    )
                return False, delta
        else:
            lfp_none_count = 0

        if v_trojan is not None and v_lfp is not None:
            delta = abs(v_trojan - v_lfp)
            remaining = max(0, match_timeout - elapsed)
            status.update(time_remaining=remaining, trojan_v=v_trojan, lfp_v=v_lfp)
            temp_service.update_voltage_current(v_trojan, monitor.get_trojan_current())

            if cache_callback:
                cache_callback(trojan_v=v_trojan, lfp_v=v_lfp,
                               voltage_delta=delta, time_remaining=remaining)

            if delta <= voltage_delta_max:
                log.info("Voltage converged: Trojan=%.2fV, LFP=%.2fV, delta=%.2fV",
                         v_trojan, v_lfp, delta)
                return True, delta

            if int(elapsed) % 300 < 30:
                log.info("Voltage matching: Trojan=%.2fV, LFP=%.2fV, delta=%.2fV (%.0f min)",
                         v_trojan, v_lfp, delta, elapsed / 60)

        if elapsed > match_timeout:
            log.error("Voltage delta did not converge after %.0f hours", elapsed / 3600)
            if alerting_mod and status:
                alerting_mod.raise_alarm(
                    "Voltage delta did not converge after %.0f hours. "
                    "Trojan=%.2fV, LFP=%.2fV, delta=%.2fV. "
                    "LFPs remain disconnected — manual intervention required."
                    % (elapsed / 3600, v_trojan or 0, v_lfp or 0, delta or 0),
                    status_service=status,
                )
            return False, delta

        time.sleep(30)
