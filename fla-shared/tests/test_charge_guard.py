"""Tests for DischargeGuard: trips when the isolated Trojan bank is being
discharged instead of charged (2026-10-03: 150 min at -50A logged as charging)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from charge_guard import DischargeGuard, DISCHARGE_POLLS, TailWindow, TAIL_POLLS


class TestDischargeGuard(unittest.TestCase):
    def test_trips_after_sustained_discharge(self):
        g = DischargeGuard()
        results = [g.tripped(-45.0) for _ in range(DISCHARGE_POLLS)]
        self.assertEqual(results, [False] * (DISCHARGE_POLLS - 1) + [True])

    def test_charging_never_trips(self):
        g = DischargeGuard()
        self.assertFalse(any(g.tripped(i) for i in [35.0, 5.0, 0.0, -2.0] * 5))

    def test_recovery_resets_the_count(self):
        g = DischargeGuard()
        for _ in range(DISCHARGE_POLLS - 1):
            g.tripped(-45.0)
        self.assertFalse(g.tripped(20.0))
        self.assertFalse(g.tripped(-45.0))

    def test_unreadable_reading_does_not_reset_the_count(self):
        # A flaky shunt (every few reads time out) must not mask a discharge.
        g = DischargeGuard()
        readings = [-45.0, None] * DISCHARGE_POLLS
        self.assertTrue(any(g.tripped(i) for i in readings))

    def test_unreadable_current_does_not_trip(self):
        g = DischargeGuard()
        self.assertFalse(any(g.tripped(None) for _ in range(DISCHARGE_POLLS * 2)))


class TestTailWindow(unittest.TestCase):
    def _window(self, samples):
        w = TailWindow()
        for at_target, current in samples:
            w.add(at_target, current)
        return w

    def test_one_low_sample_in_a_swinging_current_is_not_completion(self):
        # 2026-10-04: 9.3A on one poll while the current swung between 8 and 16A.
        w = self._window([(True, i) for i in (14.7, 12.9, 9.2, 13.8, 16.0, 9.3)])
        self.assertFalse(w.complete(10.0))

    def test_sustained_tail_current_at_target_completes(self):
        w = self._window([(True, i) for i in (9.5, 8.8, 9.9, 8.1, 9.0, 8.7)])
        self.assertTrue(w.complete(10.0))

    def test_needs_a_full_window(self):
        w = self._window([(True, 5.0)] * (TAIL_POLLS - 1))
        self.assertFalse(w.complete(10.0))

    def test_low_current_below_target_voltage_is_not_completion(self):
        w = self._window([(False, 5.0)] * TAIL_POLLS)
        self.assertFalse(w.complete(10.0))

    def test_swinging_voltage_counts_when_mostly_at_target(self):
        w = self._window([(t, 8.0) for t in (True, False, True, True, False, True)])
        self.assertTrue(w.complete(10.0))

    def test_net_discharge_is_not_completion(self):
        w = self._window([(True, -1.0)] * TAIL_POLLS)
        self.assertFalse(w.complete(10.0))

    def test_unreadable_current_is_skipped(self):
        w = self._window([(True, None)] * 3 + [(True, 5.0)] * (TAIL_POLLS - 1))
        self.assertFalse(w.complete(10.0))


if __name__ == '__main__':
    unittest.main()
