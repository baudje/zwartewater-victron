"""Tests for the Takeover module (DVCC handoff / reconnect orchestration)."""

import os
import sys
import json
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))
from helpers import dbus_mock_setup, MockMonitor, MockStatus

dbus_mock_setup()

import takeover


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        # Redirect the snapshot file to a temp path per test.
        self._tmp = os.path.join(os.path.dirname(__file__), "_snap_test.json")
        self._patch = patch.object(takeover, "SNAPSHOT_FILE", self._tmp)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(lambda: os.path.exists(self._tmp) and os.unlink(self._tmp))

    def test_save_then_load_roundtrip(self):
        ok = takeover.save_originals("com.victronenergy.battery/277", -1, 32.0)
        self.assertTrue(ok)
        got = takeover.load_originals()
        self.assertEqual(got["battery_service"], "com.victronenergy.battery/277")
        self.assertEqual(got["bms_instance"], -1)
        self.assertEqual(got["max_charge_voltage"], 32.0)

    def test_load_missing_returns_none(self):
        self.assertIsNone(takeover.load_originals())

    def test_load_corrupt_returns_none(self):
        with open(self._tmp, "w") as f:
            f.write("{not json")
        self.assertIsNone(takeover.load_originals())

    def test_delete_removes_file(self):
        takeover.save_originals("com.victronenergy.battery/277", -1, 32.0)
        takeover.delete_originals()
        self.assertFalse(os.path.exists(self._tmp))
        takeover.delete_originals()  # idempotent — no raise


if __name__ == '__main__':
    unittest.main()
