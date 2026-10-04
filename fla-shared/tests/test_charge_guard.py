"""Tests for DischargeGuard: trips when the isolated Trojan bank is being
discharged instead of charged (2026-10-03: 150 min at -50A logged as charging)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from charge_guard import DischargeGuard, DISCHARGE_POLLS


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


if __name__ == '__main__':
    unittest.main()
