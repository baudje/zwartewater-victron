"""Trips when the isolated Trojan bank is being discharged instead of charged.

2026-10-03: ESS fed the boat from the isolated bank for 150 minutes at -50A
while the equalisation loop, reading abs(current), logged it as charging.
"""

DISCHARGE_LIMIT_A = -5.0  # below this the bank is feeding loads, not shunt noise
DISCHARGE_POLLS = 4       # consecutive 30s polls (~2 min): rides out a load peak


class DischargeGuard:
    def __init__(self):
        self._count = 0

    def tripped(self, current):
        """Feed one current reading (A, + = charging). True once the bank has
        been discharging for DISCHARGE_POLLS consecutive readings."""
        if current is not None and current < DISCHARGE_LIMIT_A:
            self._count += 1
        else:
            self._count = 0
        return self._count >= DISCHARGE_POLLS
