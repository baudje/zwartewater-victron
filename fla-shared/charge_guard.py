"""Trips when the isolated Trojan bank is being discharged instead of charged.

2026-10-03: ESS fed the boat from the isolated bank for 150 minutes at -50A
while the equalisation loop, reading abs(current), logged it as charging.
"""

DISCHARGE_LIMIT_A = -5.0  # below this the bank is feeding loads, not shunt noise
DISCHARGE_POLLS = 4       # consecutive 30s polls (~2 min): rides out a load peak
# A run that ends on its timeout only counts if the bank was held at the target
# voltage this long (30s polls -> 30 min); one sample touching it is not a charge.
MIN_POLLS_AT_TARGET = 60


class DischargeGuard:
    def __init__(self):
        self._count = 0

    def tripped(self, current):
        """Feed one current reading (A, + = charging). True once the bank has
        been discharging for DISCHARGE_POLLS readings in a row. An unreadable
        reading (None) neither counts nor resets, so a flaky shunt cannot mask
        a discharge."""
        if current is None:
            pass
        elif current < DISCHARGE_LIMIT_A:
            self._count += 1
        else:
            self._count = 0
        return self._count >= DISCHARGE_POLLS
