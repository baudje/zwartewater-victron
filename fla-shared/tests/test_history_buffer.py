#!/usr/bin/env python3
"""Tests for the in-memory history ring buffer (issue #26).

Pure logic: injected clock, no I/O, no D-Bus. Contents are intentionally
lost on service restart — live supervision, not archival.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from history_buffer import HistoryBuffer


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


class TestHistoryBuffer(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.buf = HistoryBuffer(fields=["trojan_voltage", "lfp_voltage"],
                                 capacity=5, min_interval=30.0, clock=self.clock)

    def test_samples_are_rate_limited(self):
        self.assertTrue(self.buf.sample({"trojan_voltage": 31.0, "lfp_voltage": 27.0}))
        self.clock.now += 10  # too soon
        self.assertFalse(self.buf.sample({"trojan_voltage": 31.1, "lfp_voltage": 27.0}))
        self.clock.now += 25  # 35s since accepted sample
        self.assertTrue(self.buf.sample({"trojan_voltage": 31.2, "lfp_voltage": 27.0}))
        w = self.buf.window()
        self.assertEqual(w["series"]["trojan_voltage"], [31.0, 31.2])

    def test_capacity_evicts_oldest_first(self):
        for i in range(8):
            self.buf.sample({"trojan_voltage": float(i), "lfp_voltage": 27.0})
            self.clock.now += 30
        w = self.buf.window()
        self.assertEqual(len(w["t"]), 5)
        self.assertEqual(w["series"]["trojan_voltage"], [3.0, 4.0, 5.0, 6.0, 7.0])

    def test_all_none_samples_are_skipped(self):
        self.assertFalse(self.buf.sample({"trojan_voltage": None, "lfp_voltage": None}))
        self.assertEqual(self.buf.window()["t"], [])

    def test_window_can_be_limited_to_recent_seconds(self):
        for i in range(4):
            self.buf.sample({"trojan_voltage": float(i), "lfp_voltage": 27.0})
            self.clock.now += 100
        w = self.buf.window(seconds=250)
        self.assertEqual(w["series"]["trojan_voltage"], [2.0, 3.0])

    def test_unknown_fields_in_sample_are_ignored(self):
        self.buf.sample({"trojan_voltage": 31.0, "lfp_voltage": 27.0, "bogus": 1})
        self.assertNotIn("bogus", self.buf.window()["series"])


if __name__ == "__main__":
    unittest.main()
