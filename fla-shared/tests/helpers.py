"""Shared test infrastructure for FLA services.

Provides D-Bus mock setup and reusable mock objects for tests
that run on macOS without Venus OS.
"""

import logging
import sys
from unittest.mock import MagicMock


def dbus_mock_setup():
    """Mock all D-Bus and GLib modules so imports work on any platform."""
    for mod in [
        'dbus', 'dbus.mainloop.glib', 'dbus.exceptions', 'dbus.service',
        'gi', 'gi.repository', 'vedbus',
    ]:
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()

    # Redirect FileHandler to stderr (some modules create log files at import time)
    logging.FileHandler = lambda *a, **kw: logging.StreamHandler()


class MockMonitor:
    """Mock DbusMonitor with configurable return values."""

    def __init__(self, **kwargs):
        self._lfp_voltage = kwargs.get('lfp_voltage', 28.0)
        self._lfp_current = kwargs.get('lfp_current', 0.0)
        self._trojan_voltage = kwargs.get('trojan_voltage', 27.5)
        self._trojan_current = kwargs.get('trojan_current', 20.0)
        self._lfp_soc = kwargs.get('lfp_soc', 96.0)
        self._trojan_soc = kwargs.get('trojan_soc', 85.0)
        self._relay_state = kwargs.get('relay_state', 1)
        self._battery_temp = kwargs.get('battery_temp', 25.0)
        self._battery_service = kwargs.get('battery_service', "com.victronenergy.battery.aggregate")
        self._bms_instance = kwargs.get('bms_instance', -1)
        self._relay_set_calls = []
        self._invalidated = False

    def get_lfp_voltage(self):
        return self._lfp_voltage

    def get_lfp_current(self):
        return self._lfp_current

    def get_trojan_voltage(self):
        return self._trojan_voltage

    def get_trojan_current(self):
        return self._trojan_current

    def get_lfp_soc(self):
        return self._lfp_soc

    def get_trojan_soc(self):
        return self._trojan_soc

    def get_relay_state(self):
        return self._relay_state

    def set_relay(self, state):
        self._relay_set_calls.append(state)
        self._relay_state = state
        return True

    def get_battery_service_setting(self):
        return self._battery_service

    def set_battery_service_setting(self, value):
        self._battery_service = value
        return True

    def get_dvcc_max_charge_voltage(self):
        return 28.4

    def set_dvcc_max_charge_voltage(self, voltage):
        return True

    def get_battery_temperature(self):
        return self._battery_temp

    def get_bms_instance(self):
        return self._bms_instance

    def set_bms_instance(self, instance):
        self._bms_instance = instance
        return True

    def restart_systemcalc(self, system_timeout=300, system_poll=1.0, should_abort=None):
        return True

    def wait_for_service_instance(self, instance, prefix="com.victronenergy.battery",
                                  timeout_seconds=120, poll_interval=0.5, should_abort=None):
        return "com.victronenergy.battery.fla_equalisation"

    def wait_for_bms_selection(self, battery_service, bms_instance,
                               timeout_seconds=5, poll_interval=0.5):
        return True

    def invalidate_services(self):
        self._invalidated = True


class MockStatus:
    """Mock StatusService that records state transitions."""

    def __init__(self):
        self.states = []
        self.updates = []

    def update(self, **kwargs):
        self.updates.append(kwargs)
        if 'state' in kwargs:
            self.states.append(kwargs['state'])

    def register(self):
        pass

    def deregister(self):
        pass

    def set_alarm(self, level=2):
        pass

    def clear_alarm_path(self):
        pass


class ProfileContractMixin:
    """Anti-drift contract for an Operation profile (ADR-0002), shared by
    both services' test_operation_profile.py so a new invariant added here
    guards BOTH profiles at once (per-service copies of these assertions
    had already drifted within one PR).

    Subclasses set: PROFILE, SETTINGS_DEFS, STATUS_MOD, EXPECTED_NAME,
    EXPECTED_PORT, EXPECTED_TITLE_SUBSTRING, SERVICE_DIR.
    """

    def test_states_exactly_cover_the_state_enum(self):
        enum_values = {v for k, v in vars(self.STATUS_MOD).items()
                       if k.startswith("STATE_") and isinstance(v, int)}
        self.assertEqual(set(self.PROFILE.states.keys()), enum_values)
        self.assertEqual(self.PROFILE.error_state, self.STATUS_MOD.STATE_ERROR)

    def test_state_labels_match_the_status_service(self):
        # One source of truth: the profile must carry STATE_NAMES itself,
        # not a hand-copied variant.
        self.assertEqual(self.PROFILE.states, self.STATUS_MOD.STATE_NAMES)

    def test_identity(self):
        self.assertEqual(self.PROFILE.name, self.EXPECTED_NAME)
        self.assertEqual(self.PROFILE.port, self.EXPECTED_PORT)
        self.assertIn(self.EXPECTED_TITLE_SUBSTRING, self.PROFILE.title)

    def test_cross_origin_control_covers_both_dashboards_only(self):
        # The unified page on either port may control this service; any
        # other origin port must stay refused (CSRF guard).
        self.assertEqual(set(self.PROFILE.allowed_origin_ports), {8088, 8089})

    # Setting keys a profile may intentionally leave off the panel.
    # run_now is a D-Bus pseudo-setting (the RunNow trigger) in both
    # services — the panel's Run Now button covers it. Override to extend.
    HIDDEN_SETTINGS = frozenset({"run_now"})

    def test_settings_schema_matches_and_rows_cover_it(self):
        self.assertEqual(set(self.PROFILE.settings_keys), set(self.SETTINGS_DEFS))
        row_keys = {r["key"] for r in self.PROFILE.settings_rows}
        # Every setting must be editable from the panel unless explicitly
        # hidden — a key silently missing here is invisible to the operator.
        self.assertEqual(row_keys,
                         set(self.SETTINGS_DEFS) - set(self.HIDDEN_SETTINGS))
        for row in self.PROFILE.settings_rows:
            self.assertTrue(row.get("label"), "row %s has no label" % row["key"])
            self.assertIn(row.get("type"), ("f", "i", "b"))

    def test_config_is_json_serializable(self):
        # The unified page consumes this card verbatim from /api/config.
        import json
        cfg = json.loads(json.dumps(self.PROFILE.config()))
        self.assertEqual(cfg["title"], self.PROFILE.title)
        self.assertEqual(set(cfg["states"].keys()),
                         {str(k) for k in self.PROFILE.states})

    def test_log_file_points_at_the_service_log(self):
        # The dashboard log card tails this path on the Cerbo.
        self.assertEqual(self.PROFILE.log_file,
                         "/data/log/%s.log" % self.EXPECTED_NAME)

    def test_run_history_file_is_on_the_data_partition(self):
        self.assertEqual(self.PROFILE.run_history_file,
                         "/data/apps/%s/run-history.jsonl" % self.EXPECTED_NAME)

    def test_old_web_server_module_is_gone(self):
        import os
        self.assertFalse(
            os.path.exists(os.path.join(self.SERVICE_DIR, 'web_server.py')),
            "per-service web_server.py must be deleted (replaced by the shared engine)")
