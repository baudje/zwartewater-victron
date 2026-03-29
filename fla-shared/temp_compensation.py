"""Trojan L16H-AC temperature compensation for charge voltages.

Per datasheet: ±0.005V/cell per °C deviation from 25°C.
For 24V system (12 cells): ±0.06V/°C.
"""

import logging

log = logging.getLogger(__name__)

REFERENCE_TEMP = 25.0        # °C — datasheet reference
COMP_PER_CELL = 0.005        # V/cell/°C
FLA_CELLS = 12               # 4× 6V batteries in series
MAX_EQ_VOLTAGE = 32.4        # Trojan datasheet absolute max EQ (2.70V/cell × 12)
MIN_CHARGE_VOLTAGE = 24.0    # Below this makes no sense


def compensate(base_voltage, temperature, cells=FLA_CELLS):
    """Apply temperature compensation to a charge voltage.

    Returns compensated voltage, capped to safe range.
    If temperature is None, returns base_voltage unchanged.
    """
    if temperature is None:
        return base_voltage

    offset = (REFERENCE_TEMP - temperature) * COMP_PER_CELL * cells
    compensated = base_voltage + offset

    # Cap to safe range
    compensated = max(MIN_CHARGE_VOLTAGE, min(MAX_EQ_VOLTAGE, compensated))

    if abs(offset) > 0.1:
        log.info("Temp compensation: %.1f°C, base=%.2fV, offset=%+.2fV, result=%.2fV",
                 temperature, base_voltage, offset, compensated)

    return round(compensated, 2)
