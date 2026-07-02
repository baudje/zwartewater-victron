#!/usr/bin/env python3
"""Tests for the shared web engine (Candidate 3 / ADR-0002).

One closed engine serves both services' dashboards, configured by a
per-service Operation profile. These tests exercise the engine through
its public surface only: real HTTP requests against an ephemeral port,
and the module-level call surface the services bind (update_cache,
check_run_now, check_abort, clear_abort, drain_pending_settings).
"""

import json
import os
import socket
import sys
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tests'))

from helpers import dbus_mock_setup
dbus_mock_setup()

from web_engine import OperationProfile, WebEngine


def make_profile(**overrides):
    """A minimal valid profile for engine tests."""
    fields = dict(
        name="fla-test",
        title="FLA Test",
        port=0,  # ephemeral — tests must never bind a real service port
        states={0: "Idle", 1: "Busy", 2: "Error"},
        error_state=2,
        settings_keys=["eq_voltage", "lfp_soc_min"],
        allowed_origin_ports=[8088, 8089],
        cache_fields={
            "state": None,
            "time_remaining": 0,
            "trojan_voltage": None,
            "lfp_voltage": None,
            "voltage_delta": None,
            "settings": {},
        },
        settings_rows=[
            {"key": "eq_voltage", "label": "EQ voltage", "unit": "V", "type": "f"},
            {"key": "lfp_soc_min", "label": "Min LFP SoC", "unit": "%", "type": "i"},
        ],
        panel_fields=[
            {"key": "trojan_voltage", "label": "Trojan FLA", "format": "v"},
        ],
        run_now_confirm="Start test now?",
    )
    fields.update(overrides)
    return OperationProfile(**fields)


class EngineHttpTestCase(unittest.TestCase):
    """Base: start an engine on an ephemeral port, tear it down after."""

    def setUp(self):
        self.engine = WebEngine(make_profile())
        self.server = self.engine.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()

    def get(self, path):
        return urllib.request.urlopen(self.base + path, timeout=5)


class TestServesDashboardPage(EngineHttpTestCase):
    def test_root_serves_the_unified_page(self):
        resp = self.get("/")
        body = resp.read().decode()
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.headers["Content-Type"])
        # The page is data-driven: it carries the dashboard ports and builds
        # everything else from each service's /api/config at runtime.
        self.assertIn("[8088, 8089]", body)
        self.assertIn("/api/config", body)
        self.assertIn("/api/status", body)
        self.assertIn("/api/log", body)
        self.assertNotIn("__PORTS__", body)

    def test_page_is_identical_regardless_of_profile(self):
        # "Loading either port shows the same page": nothing per-service may
        # leak into the page itself — only the ports, shared by both profiles.
        other = WebEngine(make_profile(
            name="fla-other", title="Other Op", port=0,
            states={0: "Idle", 9: "Error"}, error_state=9,
            settings_keys=["x"], settings_rows=[
                {"key": "x", "label": "X", "unit": "V", "type": "f"}],
            panel_fields=[]))
        self.assertEqual(self.engine._page, other._page)

    def test_page_is_self_contained(self):
        # Strictly no external hosts: the vessel may be offline. URLs are
        # built from location.hostname + the injected ports only.
        body = self.get("/").read().decode()
        self.assertNotIn("venus.local", body)
        self.assertNotIn("https://", body)
        self.assertNotIn("cdn", body.lower())


class TestConfigEndpoint(EngineHttpTestCase):
    def test_config_returns_the_profile_card_with_cors(self):
        resp = self.get("/api/config")
        self.assertEqual(resp.status, 200)
        # Read-only, world-readable: the unified page fetches BOTH services'
        # configs from whichever port it was loaded on.
        self.assertEqual(resp.headers["Access-Control-Allow-Origin"], "*")
        cfg = json.loads(resp.read().decode())
        self.assertEqual(cfg["title"], "FLA Test")
        self.assertEqual(cfg["states"]["1"], "Busy")
        self.assertEqual(cfg["error_state"], 2)
        self.assertEqual(cfg["settings_rows"][0]["key"], "eq_voltage")
        self.assertEqual(cfg["panel_fields"][0]["format"], "v")
        self.assertEqual(cfg["run_now_confirm"], "Start test now?")


class TestStatusEndpoint(EngineHttpTestCase):
    def test_status_returns_cache_json_with_cors(self):
        resp = self.get("/api/status")
        data = json.loads(resp.read().decode())
        self.assertEqual(resp.status, 200)
        # Initial cache mirrors the profile's cache_fields.
        self.assertIsNone(data["state"])
        self.assertEqual(data["time_remaining"], 0)
        self.assertEqual(data["settings"], {})
        # Cross-origin read must be allowed (unified page polls both ports).
        self.assertEqual(resp.headers["Access-Control-Allow-Origin"], "*")

    def test_cache_updates_are_visible_via_status(self):
        self.engine.update_cache(state=1, trojan_voltage=29.6)
        data = json.loads(self.get("/api/status").read().decode())
        self.assertEqual(data["state"], 1)
        self.assertEqual(data["trojan_voltage"], 29.6)


class ControlTestCase(EngineHttpTestCase):
    def post(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        return urllib.request.urlopen(req, timeout=5)


class TestRunNowControl(ControlTestCase):
    def test_run_now_post_sets_flag_consumed_once(self):
        self.assertFalse(self.engine.check_run_now())
        resp = self.post("/api/run-now")
        self.assertEqual(resp.status, 200)
        self.assertIn("message", json.loads(resp.read().decode()))
        # Check-and-clear: True exactly once.
        self.assertTrue(self.engine.check_run_now())
        self.assertFalse(self.engine.check_run_now())

    def test_clear_run_now_discards_a_queued_request(self):
        # A run can start from the D-Bus RunNow setting too; the service
        # clears any web-queued request at start so it can't fire twice.
        self.post("/api/run-now")
        self.engine.clear_run_now()
        self.assertFalse(self.engine.check_run_now())


class TestAbortControl(ControlTestCase):
    def test_abort_persists_until_cleared(self):
        self.assertFalse(self.engine.check_abort())
        resp = self.post("/api/abort")
        self.assertEqual(resp.status, 200)
        # Unlike run-now, abort is NOT consumed by checking: the Takeover's
        # should_abort may poll it many times during one wait.
        self.assertTrue(self.engine.check_abort())
        self.assertTrue(self.engine.check_abort())
        self.engine.clear_abort()
        self.assertFalse(self.engine.check_abort())


class TestSettingsEndpoint(ControlTestCase):
    def test_valid_setting_is_queued_and_drained(self):
        resp = self.post("/api/setting", {"key": "eq_voltage", "value": 31.5})
        self.assertTrue(json.loads(resp.read().decode())["ok"])
        # The GLib thread drains the queue atomically; a second drain is empty.
        self.assertEqual(self.engine.drain_pending_settings(), [("eq_voltage", 31.5)])
        self.assertEqual(self.engine.drain_pending_settings(), [])
        # The UI sees the new value immediately via the status cache.
        data = json.loads(self.get("/api/status").read().decode())
        self.assertEqual(data["settings"]["eq_voltage"], 31.5)

    def test_unknown_setting_key_is_refused(self):
        resp = self.post("/api/setting", {"key": "not_a_setting", "value": 1})
        self.assertFalse(json.loads(resp.read().decode())["ok"])
        self.assertEqual(self.engine.drain_pending_settings(), [])


class TestCrossOriginControl(ControlTestCase):
    """The unified page is served from one port but controls both services;
    the browser preflights its JSON POSTs with OPTIONS.

    Reads are world-readable (*), but WRITES only accept origins on the
    same host as the request at the dashboard ports — a malicious website
    visited from a browser on the vessel LAN must not be able to drive
    Run Now / Abort cross-origin (CSRF)."""

    def post_from(self, path, origin, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        req = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Content-Type": "application/json", "Origin": origin})
        try:
            return urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as e:
            return e

    def allowed_origin(self):
        # Same host the page would be served from, at a dashboard port.
        return "http://127.0.0.1:8089"

    def test_options_preflight_allows_dashboard_origin(self):
        req = urllib.request.Request(
            self.base + "/api/setting", method="OPTIONS",
            headers={"Origin": self.allowed_origin(),
                     "Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "Content-Type"})
        resp = urllib.request.urlopen(req, timeout=5)
        self.assertIn(resp.status, (200, 204))
        # The specific origin is echoed — never a wildcard on control paths.
        self.assertEqual(resp.headers["Access-Control-Allow-Origin"],
                         self.allowed_origin())
        self.assertEqual(resp.headers["Vary"], "Origin")
        self.assertIn("POST", resp.headers["Access-Control-Allow-Methods"])
        self.assertIn("Content-Type", resp.headers["Access-Control-Allow-Headers"])
        # Cache the preflight so repeated control POSTs skip the extra
        # round-trip on the single-threaded server.
        self.assertTrue(int(resp.headers["Access-Control-Max-Age"]) >= 60)

    def test_options_preflight_refuses_foreign_origin(self):
        for origin in ("http://evil.example",          # wrong host + port
                       "http://evil.example:8089",     # right port, wrong host
                       "http://127.0.0.1:9999"):       # right host, wrong port
            req = urllib.request.Request(
                self.base + "/api/abort", method="OPTIONS",
                headers={"Origin": origin,
                         "Access-Control-Request-Method": "POST"})
            try:
                resp = urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError as e:
                resp = e
            self.assertEqual(resp.status, 403, "preflight not refused for %s" % origin)
            self.assertIsNone(resp.headers["Access-Control-Allow-Origin"])

    def test_post_from_foreign_origin_is_refused_and_has_no_effect(self):
        resp = self.post_from("/api/run-now", "http://evil.example")
        self.assertEqual(resp.status, 403)
        self.assertIsNone(resp.headers["Access-Control-Allow-Origin"])
        self.assertFalse(self.engine.check_run_now(),
                         "foreign-origin POST must not set the run-now flag")
        resp = self.post_from("/api/abort", "http://evil.example")
        self.assertEqual(resp.status, 403)
        self.assertFalse(self.engine.check_abort())

    def test_post_from_dashboard_origin_echoes_origin(self):
        for path, body in (("/api/run-now", None),
                           ("/api/abort", None),
                           ("/api/setting", {"key": "eq_voltage", "value": 31.0})):
            resp = self.post_from(path, self.allowed_origin(), body)
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["Access-Control-Allow-Origin"],
                             self.allowed_origin(),
                             "origin not echoed on POST %s" % path)

    def test_post_without_origin_still_works(self):
        # Non-browser clients (curl, local tooling) send no Origin header;
        # CORS is a browser control and must not break them.
        resp = self.post("/api/run-now")
        self.assertEqual(resp.status, 200)
        self.assertTrue(self.engine.check_run_now())

    def test_status_read_stays_world_readable(self):
        resp = self.get("/api/status")
        self.assertEqual(resp.headers["Access-Control-Allow-Origin"], "*")

    def test_ipv6_literal_origin_is_allowed(self):
        # Browsing http://[fd00::2]:8088 sends a bracketed Host; the origin
        # check must compare hostnames, not raw Host-header prefixes, or the
        # page's own Abort button 403s under IPv6.
        class FakeReq:
            headers = {"Origin": "http://[fd00::2]:8089", "Host": "[fd00::2]:8088"}
        self.assertTrue(self.engine._origin_allowed(FakeReq()))

    def test_foreign_host_still_refused_with_ipv6(self):
        class FakeReq:
            headers = {"Origin": "http://[fd00::9]:8089", "Host": "[fd00::2]:8088"}
        self.assertFalse(self.engine._origin_allowed(FakeReq()))


class TestMalformedRequests(EngineHttpTestCase):
    """A misbehaving LAN client must not be able to stall or crash the
    single-threaded server that also serves the Abort button."""

    def _raw(self, request_bytes):
        s = socket.create_connection(self.server.server_address, timeout=5)
        try:
            s.sendall(request_bytes)
            s.settimeout(3)
            chunks = b""
            while True:
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                chunks += chunk
            return chunks
        finally:
            s.close()

    def test_non_numeric_content_length_gets_json_error(self):
        resp = self._raw(b"POST /api/setting HTTP/1.1\r\n"
                         b"Host: 127.0.0.1\r\n"
                         b"Content-Type: application/json\r\n"
                         b"Content-Length: abc\r\n\r\n")
        self.assertIn(b'"ok": false', resp)
        # The server survives to answer the next request.
        self.assertEqual(self.get("/api/status").status, 200)

    def test_negative_content_length_does_not_block_the_server(self):
        # Old behavior: rfile.read(-1) waited for socket EOF, freezing the
        # dashboard (including Abort) for as long as the client held on.
        resp = self._raw(b"POST /api/setting HTTP/1.1\r\n"
                         b"Host: 127.0.0.1\r\n"
                         b"Content-Type: application/json\r\n"
                         b"Content-Length: -1\r\n\r\n")
        self.assertIn(b'"ok": false', resp)
        self.assertEqual(self.get("/api/status").status, 200)


class TestUpdateCacheContract(unittest.TestCase):
    """No HTTP needed — update_cache is the GLib-thread call surface."""

    def setUp(self):
        self.engine = WebEngine(make_profile())

    def cache(self):
        return self.engine._cache  # asserted via /api/status elsewhere

    def test_voltage_matching_aliases_map_to_cache_fields(self):
        # voltage_matching's cache_callback passes trojan_v=/lfp_v= — the
        # engine must map them onto the full cache field names.
        self.engine.update_cache(trojan_v=27.8, lfp_v=26.9, voltage_delta=0.9)
        self.assertEqual(self.cache()["trojan_voltage"], 27.8)
        self.assertEqual(self.cache()["lfp_voltage"], 26.9)
        self.assertEqual(self.cache()["voltage_delta"], 0.9)

    def test_none_values_do_not_blank_last_known(self):
        self.engine.update_cache(trojan_voltage=29.6)
        self.engine.update_cache(trojan_voltage=None)
        self.assertEqual(self.cache()["trojan_voltage"], 29.6)

    def test_profile_specific_aliases_are_honoured(self):
        # e.g. EQ call sites pass last_eq= / days_until= — the profile maps
        # them onto its cache fields; the engine merges them with the base
        # aliases.
        engine = WebEngine(make_profile(
            cache_fields={"state": None, "settings": {},
                          "trojan_voltage": None, "lfp_voltage": None,
                          "voltage_delta": None,
                          "last_equalisation": None},
            cache_aliases={"last_eq": "last_equalisation"}))
        engine.update_cache(last_eq="2026-07-02", trojan_v=27.8)
        self.assertEqual(engine._cache["last_equalisation"], "2026-07-02")
        self.assertEqual(engine._cache["trojan_voltage"], 27.8)

    def test_unknown_field_is_rejected(self):
        # Typo guard: a misspelled field must fail loudly, not silently
        # publish a key the page never reads.
        with self.assertRaises(ValueError):
            self.engine.update_cache(trojan_volts=29.6)


class TestLogEndpoint(EngineHttpTestCase):
    def setUp(self):
        import tempfile
        f = tempfile.NamedTemporaryFile("w", delete=False, suffix=".log")
        f.write("".join("entry %d\n" % i for i in range(300)))
        f.close()
        self.addCleanup(os.unlink, f.name)
        self.engine = WebEngine(make_profile(log_file=f.name))
        self.server = self.engine.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def test_log_returns_last_lines_with_cors(self):
        resp = self.get("/api/log?lines=5")
        self.assertEqual(resp.headers["Access-Control-Allow-Origin"], "*")
        data = json.loads(resp.read().decode())
        self.assertEqual(data["lines"], ["entry %d" % i for i in range(295, 300)])

    def test_requested_lines_are_capped_server_side(self):
        data = json.loads(self.get("/api/log?lines=999999").read().decode())
        self.assertLessEqual(len(data["lines"]), 200)

    def test_default_without_query(self):
        data = json.loads(self.get("/api/log").read().decode())
        self.assertEqual(len(data["lines"]), 50)

    def test_profile_without_log_file_yields_empty(self):
        engine = WebEngine(make_profile())  # no log_file
        server = engine.start()
        try:
            url = "http://127.0.0.1:%d/api/log" % server.server_address[1]
            data = json.loads(urllib.request.urlopen(url, timeout=5).read().decode())
            self.assertEqual(data["lines"], [])
        finally:
            server.shutdown()


class TestRunNowMessageHook(ControlTestCase):
    """A service can put its start-precondition in the run-now reply (the
    old EQ page told the operator 'SoC must be >= N%'); the hook receives
    the current settings so the threshold stays live."""

    def setUp(self):
        self.engine = WebEngine(make_profile(
            run_now_message=lambda s: "queued (SoC must be >= %d%%)"
                                      % int(s.get("lfp_soc_min") or 95)))
        self.server = self.engine.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def test_run_now_reply_uses_the_profile_hook(self):
        self.engine.update_cache(settings={"lfp_soc_min": 95})
        resp = self.post("/api/run-now")
        self.assertIn("SoC must be >= 95%", json.loads(resp.read().decode())["message"])


class TestProfileValidation(unittest.TestCase):
    """A profile missing a required field must fail at service startup
    (construction), not when the page or endpoint is first hit."""

    def test_missing_field_raises_naming_the_field(self):
        with self.assertRaises(ValueError) as ctx:
            make_profile(states=None)
        self.assertIn("states", str(ctx.exception))

    def test_error_state_must_be_a_known_state(self):
        with self.assertRaises(ValueError):
            make_profile(error_state=99)

    def test_error_state_must_be_the_highest_state(self):
        # The page's abort-button gate is `0 < state < ERROR_STATE`; a state
        # numbered above error would silently hide Abort while live.
        with self.assertRaises(ValueError):
            make_profile(states={0: "Idle", 1: "Busy", 2: "Error", 3: "Later"},
                         error_state=2)

    def test_alias_targets_must_exist_in_cache_fields(self):
        # 'Fail at service startup, not when hit': an alias pointing at a
        # missing cache field must not wait for voltage_matching's callback
        # to raise mid-hand-back on the worker thread.
        with self.assertRaises(ValueError) as ctx:
            WebEngine(make_profile(cache_aliases={"last_eq": "missing_field"}))
        self.assertIn("missing_field", str(ctx.exception))

    def test_base_alias_targets_are_required_too(self):
        # The engine's own trojan_v/lfp_v aliases point at trojan_voltage/
        # lfp_voltage — every profile must carry those fields.
        with self.assertRaises(ValueError):
            WebEngine(make_profile(cache_fields={"state": None, "settings": {}}))

    def test_settings_rows_must_use_schema_keys(self):
        # A row pointing at a non-existent setting would render an input
        # whose saves are refused at the queue — fail at startup instead.
        with self.assertRaises(ValueError):
            make_profile(settings_rows=[
                {"key": "not_a_setting", "label": "X", "unit": "V", "type": "f"}])

    def test_panel_fields_must_use_cache_fields(self):
        # A panel row for a field the service never publishes would render
        # a permanently empty '-' — fail at startup instead.
        with self.assertRaises(ValueError):
            make_profile(panel_fields=[
                {"key": "not_a_cache_field", "label": "X", "format": "v"}])

    def test_panel_field_formats_must_be_known(self):
        with self.assertRaises(ValueError):
            make_profile(panel_fields=[
                {"key": "trojan_voltage", "label": "X", "format": "nope"}])


if __name__ == "__main__":
    unittest.main()
