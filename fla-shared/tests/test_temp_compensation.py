"""Tests for temp_compensation.compensate() — pure function, no D-Bus."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from temp_compensation import compensate


class TestCompensate(unittest.TestCase):

    def test_none_temperature_returns_base(self):
        self.assertEqual(compensate(31.5, None), 31.5)

    def test_reference_temp_no_offset(self):
        self.assertEqual(compensate(31.5, 25.0), 31.5)

    def test_cold_increases_voltage(self):
        # 15°C: offset = (25 - 15) * 0.005 * 12 = +0.6
        self.assertEqual(compensate(31.5, 15.0), 32.1)

    def test_hot_decreases_voltage(self):
        # 35°C: offset = (25 - 35) * 0.005 * 12 = -0.6
        self.assertEqual(compensate(31.5, 35.0), 30.9)

    def test_very_cold_capped_at_max(self):
        # -10°C: offset = (25 - (-10)) * 0.005 * 12 = +2.1 → 31.5 + 2.1 = 33.6, capped at 32.4
        self.assertEqual(compensate(31.5, -10.0), 32.4)

    def test_very_hot_capped_at_min(self):
        # 50°C: offset = (25 - 50) * 0.005 * 12 = -1.5 → 24.5 - 1.5 = 23.0, capped at 24.0
        self.assertEqual(compensate(24.5, 50.0), 24.0)

    def test_result_rounded_to_2_decimals(self):
        # 18°C: offset = (25 - 18) * 0.005 * 12 = 0.42 → 29.64 + 0.42 = 30.06
        self.assertEqual(compensate(29.64, 18.0), 30.06)

    def test_custom_cell_count(self):
        # 20°C, 6 cells: offset = (25 - 20) * 0.005 * 6 = 0.15 → 31.5 + 0.15 = 31.65
        self.assertEqual(compensate(31.5, 20.0, cells=6), 31.65)

    def test_absorption_at_20c(self):
        # 20°C: offset = (25 - 20) * 0.005 * 12 = 0.3 → 29.64 + 0.3 = 29.94
        self.assertEqual(compensate(29.64, 20.0), 29.94)

    def test_float_at_30c(self):
        # 30°C: offset = (25 - 30) * 0.005 * 12 = -0.3 → 27.0 - 0.3 = 26.7
        self.assertEqual(compensate(27.0, 30.0), 26.7)

    def test_extreme_cold_minus_40(self):
        # -40°C: offset = (25 - (-40)) * 0.005 * 12 = +3.9 → 29.0 + 3.9 = 32.9, capped at 32.4
        self.assertEqual(compensate(29.0, -40.0), 32.4)

    def test_extreme_hot_plus_60(self):
        # 60°C: offset = (25 - 60) * 0.005 * 12 = -2.1 → 29.0 - 2.1 = 26.9
        self.assertEqual(compensate(29.0, 60.0), 26.9)


if __name__ == '__main__':
    unittest.main()
