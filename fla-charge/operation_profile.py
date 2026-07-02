"""Operation profile for the FLA charge dashboard (ADR-0002).

The single declarative card that distinguishes this service's web UI from
fla-equalisation's. The shared engine (fla-shared/web_engine.py) is closed
and configured by this card; the unified page (fla-shared/unified_page.py)
is shared and data-driven — it renders this card from GET /api/config, so
the panel can never disagree with the state machine or the settings schema.
"""

import os
import sys

sys.path.insert(1, os.path.join(os.path.dirname(__file__), "..", "fla-shared"))

from web_engine import OperationProfile
from settings import SETTINGS_DEFS
from dbus_status_service import STATE_NAMES, STATE_ERROR

PROFILE = OperationProfile(
    name="fla-charge",
    title="FLA Charge",
    port=8089,
    states=STATE_NAMES,
    error_state=STATE_ERROR,
    settings_keys=list(SETTINGS_DEFS),
    log_file="/data/log/fla-charge.log",
    # Cross-origin control is limited to pages served by the Cerbo itself:
    # this dashboard and its peer (fla-equalisation on 8088).
    allowed_origin_ports=[8088, 8089],
    cache_fields={
        "state": None,
        "time_remaining": 0,
        "trojan_voltage": None,
        "lfp_voltage": None,
        "voltage_delta": None,
        "trojan_soc": None,
        "lfp_soc": None,
        "trojan_current": None,
        "last_charge": None,
        "settings": {},
    },
    # Extra status rows on this operation's panel (system-wide voltages and
    # SoCs live in the unified page's shared header).
    panel_fields=[
        {"key": "last_charge", "label": "Last charge", "format": "text"},
        {"key": "trojan_current", "label": "Trojan current", "format": "a"},
    ],
    settings_rows=[
        {"key": "enabled", "label": "Enabled", "unit": "", "type": "b"},
        {"key": "trojan_soc_trigger", "label": "Trojan SoC trigger", "unit": "%", "type": "i"},
        {"key": "lfp_soc_transition", "label": "LFP SoC transition", "unit": "%", "type": "i"},
        {"key": "lfp_cell_voltage_disconnect", "label": "LFP cell V disconnect", "unit": "V", "type": "f"},
        {"key": "current_taper_threshold", "label": "Current taper threshold", "unit": "A", "type": "f"},
        {"key": "fla_bulk_voltage", "label": "FLA bulk voltage", "unit": "V", "type": "f"},
        {"key": "fla_absorption_complete_current", "label": "Absorption complete I", "unit": "A", "type": "f"},
        {"key": "fla_absorption_max_hours", "label": "Absorption max hours", "unit": "hrs", "type": "f"},
        {"key": "fla_float_voltage", "label": "FLA float voltage", "unit": "V", "type": "f"},
        {"key": "voltage_delta_max", "label": "Max reconnect delta", "unit": "V", "type": "f"},
        {"key": "voltage_match_timeout_hours", "label": "Voltage match timeout", "unit": "hrs", "type": "f"},
        {"key": "phase1_timeout_hours", "label": "Phase 1 timeout", "unit": "hrs", "type": "f"},
    ],
    run_now_confirm="Start FLA charge now?",
)
