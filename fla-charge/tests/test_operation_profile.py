#!/usr/bin/env python3
"""Anti-drift guard for the charge Operation profile (ADR-0002).

Mirror of fla-equalisation/tests/test_operation_profile.py — the profile
is the ONLY place this service may differ from fla-equalisation in web
plumbing; these tests pin it to the service's own state and settings maps.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'fla-shared'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'fla-shared', 'tests'))

from helpers import dbus_mock_setup
dbus_mock_setup()

from operation_profile import PROFILE
from settings import SETTINGS_DEFS
import dbus_status_service as status_mod


class TestChargeProfileCompleteness(unittest.TestCase):
    def test_states_exactly_cover_the_state_enum(self):
        enum_values = {v for k, v in vars(status_mod).items()
                       if k.startswith("STATE_") and isinstance(v, int)}
        self.assertEqual(set(PROFILE.states.keys()), enum_values)
        self.assertEqual(PROFILE.error_state, status_mod.STATE_ERROR)

    def test_state_labels_match_the_status_service(self):
        self.assertEqual(PROFILE.states, status_mod.STATE_NAMES)

    def test_identity(self):
        self.assertEqual(PROFILE.name, "fla-charge")
        self.assertEqual(PROFILE.port, 8089)
        self.assertIn("Charge", PROFILE.title)

    def test_cross_origin_control_covers_both_dashboards_only(self):
        # The unified page on either port may control this service; any
        # other origin port must stay refused (CSRF guard).
        self.assertEqual(set(PROFILE.allowed_origin_ports), {8088, 8089})

    def test_settings_rows_are_valid_setting_keys(self):
        page = PROFILE.render_page()
        row_keys = set(re.findall(r'data-key="([^"]+)"', page))
        self.assertTrue(row_keys, "page has no settings rows")
        self.assertTrue(row_keys.issubset(set(PROFILE.settings_keys)),
                        "page rows not in schema: %s"
                        % (row_keys - set(PROFILE.settings_keys)))
        self.assertEqual(set(PROFILE.settings_keys), set(SETTINGS_DEFS))

    def test_page_shows_every_state_label(self):
        page = PROFILE.render_page()
        for label in PROFILE.states.values():
            self.assertIn(label, page)

    def test_old_web_server_module_is_gone(self):
        self.assertFalse(
            os.path.exists(os.path.join(os.path.dirname(__file__), '..', 'web_server.py')),
            "per-service web_server.py must be deleted (replaced by the shared engine)")


if __name__ == "__main__":
    unittest.main()
