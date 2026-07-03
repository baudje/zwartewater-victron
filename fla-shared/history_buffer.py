"""In-memory ring buffer for run-scoped dashboard graphs (issue #26).

Pure logic: fixed capacity, injected clock, no I/O, no D-Bus. Deliberately
memory-only — continuous sampling must cause zero flash wear on the Cerbo,
and the buffer exists for live supervision, not archival (VRM covers
long-term trends). Contents are lost on service restart by design.
"""

import time
from collections import deque


class HistoryBuffer:
    """Fixed-capacity time series of a declared field set.

    sample() is rate-limited (min_interval) so callers can feed it from
    every status-update site without thinking about cadence; all-None
    samples are skipped so idle gaps don't fill the window with blanks.
    """

    def __init__(self, fields, capacity=2880, min_interval=30.0, clock=time.time):
        self.fields = list(fields)
        self.min_interval = min_interval
        self._clock = clock
        self._t = deque(maxlen=capacity)
        self._series = {f: deque(maxlen=capacity) for f in self.fields}
        self._last_sample = None

    def sample(self, values):
        """Record one sample of the declared fields. Returns True if stored
        (rate limit passed and at least one value present)."""
        now = self._clock()
        if self._last_sample is not None and now - self._last_sample < self.min_interval:
            return False
        if all(values.get(f) is None for f in self.fields):
            return False
        self._t.append(round(now, 1))
        for f in self.fields:
            self._series[f].append(values.get(f))
        self._last_sample = now
        return True

    def window(self, seconds=None):
        """The series as JSON-safe lists, optionally only the last `seconds`."""
        t = list(self._t)
        start = 0
        if seconds is not None and t:
            cutoff = self._clock() - seconds
            start = next((i for i, ts in enumerate(t) if ts >= cutoff), len(t))
        return {
            "t": t[start:],
            "series": {f: list(self._series[f])[start:] for f in self.fields},
        }

    def __len__(self):
        return len(self._t)
