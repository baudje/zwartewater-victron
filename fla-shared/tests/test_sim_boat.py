"""SimBoat must keep offering what the services call on DbusMonitor."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))
from helpers import dbus_mock_setup

dbus_mock_setup()
from dbus_monitor import DbusMonitor
from sim_boat import SimBoat

# DbusMonitor methods the simulator deliberately does not offer.
NOT_SIMULATED = ["wait_for_system_service"]   # only called inside restart_systemcalc


class TestSimBoatContract(unittest.TestCase):
    def test_simboat_has_every_public_dbus_monitor_method(self):
        missing = [n for n in dir(DbusMonitor)
                   if not n.startswith("_") and callable(getattr(DbusMonitor, n))
                   and not hasattr(SimBoat, n)]
        self.assertEqual(missing, NOT_SIMULATED)


if __name__ == '__main__':
    unittest.main()
