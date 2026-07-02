"""Operation profile for the FLA equalisation dashboard (ADR-0002).

The single declarative card that distinguishes this service's web UI from
fla-charge's. The shared engine (fla-shared/web_engine.py) is closed and
configured by this card; the unified page (fla-shared/unified_page.py) is
shared and data-driven — it renders this card from GET /api/config, so the
panel can never disagree with the state machine or the settings schema.
"""

import os
import sys

sys.path.insert(1, os.path.join(os.path.dirname(__file__), "..", "fla-shared"))

from web_engine import OperationProfile
from settings import SETTINGS_DEFS
from dbus_status_service import STATE_NAMES, STATE_ERROR

PROFILE = OperationProfile(
    name="fla-equalisation",
    title="FLA Equalisation",
    port=8088,
    states=STATE_NAMES,
    error_state=STATE_ERROR,
    settings_keys=list(SETTINGS_DEFS),
    # Cross-origin control is limited to pages served by the Cerbo itself:
    # this dashboard and its peer (fla-charge on 8089).
    allowed_origin_ports=[8088, 8089],
    cache_fields={
        "state": None,
        "time_remaining": 0,
        "trojan_voltage": None,
        "lfp_voltage": None,
        "voltage_delta": None,
        "last_equalisation": None,
        "days_until_next": None,
        "inrush_current": None,
        "reconnect_delta": None,
        "settings": {},
    },
    cache_aliases={"last_eq": "last_equalisation", "days_until": "days_until_next"},
    # Extra status rows on this operation's panel (system-wide voltages and
    # SoCs live in the unified page's shared header).
    panel_fields=[
        {"key": "last_equalisation", "label": "Last equalisation", "format": "text"},
        {"key": "days_until_next", "label": "Next due in", "format": "days_or_due"},
        {"key": "inrush_current", "label": "Last inrush current", "format": "a"},
        {"key": "reconnect_delta", "label": "Last reconnect delta", "format": "v"},
    ],
    settings_rows=[
        {"key": "eq_voltage", "label": "EQ voltage", "unit": "V", "type": "f"},
        {"key": "eq_current_complete", "label": "Complete current", "unit": "A", "type": "f"},
        {"key": "eq_timeout_hours", "label": "Max duration", "unit": "hrs", "type": "f"},
        {"key": "float_voltage", "label": "Float voltage", "unit": "V", "type": "f"},
        {"key": "voltage_delta_max", "label": "Max reconnect delta", "unit": "V", "type": "f"},
        # Was editable only via D-Bus before the unified page; the contract
        # test (rows must cover the schema) surfaced the gap.
        {"key": "voltage_match_timeout_hours", "label": "Voltage match timeout", "unit": "hrs", "type": "f"},
        {"key": "days_between", "label": "Interval", "unit": "days", "type": "i"},
        {"key": "start_hour", "label": "Start hour", "unit": ":00", "type": "i"},
        {"key": "end_hour", "label": "End hour", "unit": ":00", "type": "i"},
        {"key": "lfp_soc_min", "label": "Min LFP SoC", "unit": "%", "type": "i"},
        {"key": "enabled", "label": "Enabled", "unit": "", "type": "b"},
    ],
    # Browser confirm() before Run Now; {lfp_soc_min} is filled from the
    # live settings by the page.
    run_now_confirm="Start FLA equalisation now?\n(LFP SoC must be >= {lfp_soc_min}%)",
    # Name the start precondition in the run-now reply (with the live
    # threshold), so curl/scripted callers learn why a run may not start.
    run_now_message=lambda s: (
        "RunNow requested — will start at next check (SoC must be >= %d%%)"
        % int(s.get("lfp_soc_min") or 95)),
)
