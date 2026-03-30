"""Tests for aggregate_driver.py — stop/start dbus-aggregate-batteries."""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import aggregate_driver


class TestStop(unittest.TestCase):

    @patch('aggregate_driver.time.sleep')
    @patch('aggregate_driver.subprocess.run')
    def test_success(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr=b'')
        result = aggregate_driver.stop()
        self.assertTrue(result)

    @patch('aggregate_driver.time.sleep')
    @patch('aggregate_driver.subprocess.run')
    def test_failure(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=1, stderr=b'service not found')
        result = aggregate_driver.stop()
        self.assertFalse(result)

    @patch('aggregate_driver.time.sleep')
    @patch('aggregate_driver.subprocess.run')
    def test_calls_svc_d(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr=b'')
        aggregate_driver.stop()
        mock_run.assert_called_once_with(
            ["svc", "-d", "/service/dbus-aggregate-batteries"],
            capture_output=True,
        )


class TestStart(unittest.TestCase):

    @patch('aggregate_driver.subprocess.run')
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr=b'')
        result = aggregate_driver.start()
        self.assertTrue(result)

    @patch('aggregate_driver.subprocess.run')
    def test_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr=b'failed')
        result = aggregate_driver.start()
        self.assertFalse(result)

    @patch('aggregate_driver.subprocess.run')
    def test_calls_svc_u(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr=b'')
        aggregate_driver.start()
        mock_run.assert_called_once_with(
            ["svc", "-u", "/service/dbus-aggregate-batteries"],
            capture_output=True,
        )


if __name__ == '__main__':
    unittest.main()
