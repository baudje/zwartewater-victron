#!/usr/bin/env python3
"""Anti-drift guard for the charge Operation profile (ADR-0002).

All contract assertions live in ProfileContractMixin
(fla-shared/tests/helpers.py) so both services are guarded by the same
invariants; this file only binds the charge-specific values.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'fla-shared'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'fla-shared', 'tests'))

from helpers import dbus_mock_setup, ProfileContractMixin
dbus_mock_setup()

from operation_profile import PROFILE
from settings import SETTINGS_DEFS
import dbus_status_service as status_mod


class TestChargeProfileCompleteness(ProfileContractMixin, unittest.TestCase):
    PROFILE = PROFILE
    SETTINGS_DEFS = SETTINGS_DEFS
    STATUS_MOD = status_mod
    EXPECTED_NAME = "fla-charge"
    EXPECTED_PORT = 8089
    EXPECTED_TITLE_SUBSTRING = "Charge"
    SERVICE_DIR = os.path.join(os.path.dirname(__file__), '..')


if __name__ == "__main__":
    unittest.main()
