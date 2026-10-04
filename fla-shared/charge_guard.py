"""Guards for the FLA charging loops (equalisation and absorption).

DischargeGuard — trips when the isolated Trojan bank is being discharged instead
of charged. 2026-10-03: ESS fed the boat from the isolated bank for 150 minutes
at -50A while the equalisation loop, reading abs(current), logged it as charging.

TailWindow — decides "tail current reached" on a few minutes of readings, not
one. 2026-10-04: the absorption ended on a single 9.3A sample while the current
was swinging between 8 and 16A; earlier equalisations "completed" after 2, 4 and
16 minutes the same way.
"""

from collections import deque

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


TAIL_POLLS = 6  # 30s polls -> a 3 minute window


class TailWindow:
    """The last TAIL_POLLS readings of (at target voltage?, current)."""

    def __init__(self):
        self._samples = deque(maxlen=TAIL_POLLS)

    def add(self, at_target, current):
        """Record one poll. An unreadable current (None) is skipped."""
        if current is not None:
            self._samples.append((bool(at_target), current))

    def mean_current(self):
        return sum(i for _, i in self._samples) / len(self._samples) if self._samples else None

    def complete(self, threshold):
        """True when the window is full, the bank was at the target voltage for
        most of it, and the mean current is a charge current below `threshold`."""
        if len(self._samples) < TAIL_POLLS:
            return False
        at_target = sum(1 for t, _ in self._samples if t)
        return at_target * 2 > TAIL_POLLS and 0 <= self.mean_current() < threshold
