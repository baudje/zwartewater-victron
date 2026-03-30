"""Tests for alerting.py — buzzer, alarm, and D-Bus alerting."""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))
from helpers import dbus_mock_setup
dbus_mock_setup()

import alerting


class TestRaiseAlarm(unittest.TestCase):

    @patch('alerting.activate_buzzer')
    def test_activates_buzzer(self, mock_buzzer):
        alerting.raise_alarm("test alarm")
        mock_buzzer.assert_called_once()

    @patch('alerting.activate_buzzer')
    def test_sets_alarm_on_status(self, mock_buzzer):
        status_service = MagicMock()
        alerting.raise_alarm("test alarm", status_service=status_service)
        status_service.set_alarm.assert_called_once_with(2)

    @patch('alerting.activate_buzzer')
    def test_none_status_no_crash(self, mock_buzzer):
        # Should not raise when status_service is None
        alerting.raise_alarm("test alarm", status_service=None)


class TestClearAlarm(unittest.TestCase):

    @patch('alerting.deactivate_buzzer')
    def test_deactivates_buzzer(self, mock_buzzer):
        alerting.clear_alarm()
        mock_buzzer.assert_called_once()

    @patch('alerting.deactivate_buzzer')
    def test_clears_status(self, mock_buzzer):
        status_service = MagicMock()
        alerting.clear_alarm(status_service=status_service)
        status_service.clear_alarm_path.assert_called_once()


class TestBuzzer(unittest.TestCase):

    @patch('alerting.get_bus')
    def test_activate_calls_set_value(self, mock_get_bus):
        mock_bus = MagicMock()
        mock_get_bus.return_value = mock_bus
        mock_obj = MagicMock()
        mock_bus.get_object.return_value = mock_obj
        mock_iface = MagicMock()

        with patch('alerting.dbus.Interface', return_value=mock_iface):
            alerting.activate_buzzer()

        mock_iface.SetValue.assert_called_once()

    @patch('alerting.get_bus')
    def test_activate_handles_exception(self, mock_get_bus):
        import dbus
        mock_get_bus.side_effect = dbus.exceptions.DBusException("connection refused")
        # Should not raise
        alerting.activate_buzzer()


if __name__ == '__main__':
    unittest.main()
