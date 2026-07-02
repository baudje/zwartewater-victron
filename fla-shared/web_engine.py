"""Shared web engine for the FLA service dashboards (Candidate 3).

One CLOSED engine serves both fla-equalisation and fla-charge, configured
by a per-service Operation profile (a plain data card) — composition, not
inheritance, per ADR-0002. The engine has no override hooks: everything
that may vary between the two services lives in the profile, so the web
plumbing cannot drift apart again.

Threading contract (unchanged from the old per-service web_server.py):
the service's GLib main loop writes the cache and drains pending settings;
the HTTP handler thread reads the cache and queues flags/settings. HTTP
handlers must never touch D-Bus.
"""

import json
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Lock, Thread

log = logging.getLogger(__name__)


class OperationProfile:
    """Declarative card describing one FLA operation's web identity.

    See CONTEXT.md ("Operation profile"): the genuinely-varying parts stay
    per-service in this card; the engine is closed over it.
    """

    REQUIRED_PLACEHOLDERS = ("__TITLE__", "__STATES__", "__ERROR_STATE__")

    def __init__(self, name, title, port, states, error_state,
                 settings_keys, cache_fields, html_template,
                 cache_aliases=None):
        self.cache_aliases = cache_aliases or {}
        self.name = name
        self.title = title
        self.port = port
        self.states = states
        self.error_state = error_state
        self.settings_keys = settings_keys
        self.cache_fields = cache_fields
        self.html_template = html_template
        self._validate()

    def _validate(self):
        """Fail at service startup, not when a page or endpoint is hit."""
        missing = [field for field in ("name", "title", "states",
                                       "settings_keys", "cache_fields",
                                       "html_template")
                   if not getattr(self, field)]
        if self.port is None:
            missing.append("port")
        if missing:
            raise ValueError("OperationProfile missing required fields: %s"
                             % ", ".join(missing))
        if self.error_state not in self.states:
            raise ValueError("error_state %r is not in states" % self.error_state)
        absent = [p for p in self.REQUIRED_PLACEHOLDERS
                  if p not in self.html_template]
        if absent:
            raise ValueError("html_template lacks placeholders: %s"
                             % ", ".join(absent))

    def render_page(self):
        """Substitute the profile's data into its HTML template so the
        page's states/title can never drift from the service's own maps."""
        # ensure_ascii=False: labels contain non-ASCII (em-dash) and the
        # page is served as UTF-8.
        states_json = json.dumps({str(k): v for k, v in self.states.items()},
                                 ensure_ascii=False)
        return (self.html_template
                .replace("__TITLE__", self.title)
                .replace("__STATES__", states_json)
                .replace("__ERROR_STATE__", str(self.error_state)))


class WebEngine:
    """Closed HTTP engine for one FLA service, configured by its profile."""

    def __init__(self, profile):
        self.profile = profile
        self._page = profile.render_page()
        # Per-instance copy so two engines in one process never share state.
        self._cache = {k: (dict(v) if isinstance(v, dict) else v)
                       for k, v in profile.cache_fields.items()}
        self._aliases = dict(self.CACHE_ALIASES)
        self._aliases.update(profile.cache_aliases)
        self._run_now_requested = False
        self._abort_requested = False
        self._pending_settings = []
        self._pending_settings_lock = Lock()

    # Short kwarg names used by shared callers (voltage_matching's
    # cache_callback) mapped onto the full cache field names. Identical for
    # both services, so it lives in the engine, not the profile.
    CACHE_ALIASES = {"trojan_v": "trojan_voltage", "lfp_v": "lfp_voltage"}

    def update_cache(self, **kwargs):
        """Update cache fields from the service's GLib thread.

        None values are skipped (a poll that failed to read a value must
        not blank the last known one — same semantics as the old
        per-service update_cache). Unknown fields raise: a typo must fail
        loudly, not silently publish a key the page never reads."""
        for key, value in kwargs.items():
            key = self._aliases.get(key, key)
            if key not in self._cache:
                raise ValueError("Unknown cache field: %s" % key)
            if value is None:
                continue
            self._cache[key] = value

    def start(self):
        """Start serving on the profile's port from a daemon thread."""
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                engine._handle_get(self)

            def do_POST(self):
                engine._handle_post(self)

            def do_OPTIONS(self):
                engine._handle_options(self)

        server = HTTPServer(("0.0.0.0", self.profile.port), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        log.info("Web UI (%s) started on port %d",
                 self.profile.name, server.server_address[1])
        return server

    # --- HTTP handling (runs on the HTTP thread — cache only, no D-Bus) ---

    def _handle_get(self, req):
        if req.path in ("/", "/index.html"):
            req.send_response(200)
            req.send_header("Content-Type", "text/html; charset=utf-8")
            req.end_headers()
            req.wfile.write(self._page.encode())
        elif req.path == "/api/status":
            self._send_json(req, self._cache)
        else:
            req.send_response(404)
            req.end_headers()

    def _handle_post(self, req):
        if req.path == "/api/run-now":
            self._run_now_requested = True
            self._send_json(req, {"message": "RunNow requested — will start at next check"})
        elif req.path == "/api/abort":
            self._abort_requested = True
            self._send_json(req, {"message": "Abort requested — will stop at next check cycle"})
        elif req.path == "/api/setting":
            length = int(req.headers.get("Content-Length", 0))
            try:
                data = json.loads(req.rfile.read(length))
                key, value = data["key"], data["value"]
                self.queue_setting(key, value)
                msg = {"ok": True, "key": key, "value": value}
            except Exception as e:
                msg = {"ok": False, "error": str(e)}
            self._send_json(req, msg)
        else:
            req.send_response(404)
            req.end_headers()

    # --- Service-facing call surface (runs on the GLib thread) ---

    def check_run_now(self):
        """Check and clear the run-now flag."""
        if self._run_now_requested:
            self._run_now_requested = False
            return True
        return False

    def clear_run_now(self):
        """Discard a queued run-now request without acting on it (used when
        a run starts by other means, so the request can't fire twice)."""
        self._run_now_requested = False

    def check_abort(self):
        """Read (without clearing) the abort flag — polled repeatedly as
        the Takeover's should_abort during long waits."""
        return self._abort_requested

    def clear_abort(self):
        """Clear the abort flag once the operation has handled it."""
        self._abort_requested = False

    def queue_setting(self, key, value):
        """Queue a setting update for the GLib thread to apply to D-Bus.
        Raises ValueError for keys not in the profile's settings schema."""
        if key not in self.profile.settings_keys:
            raise ValueError("Unknown setting key: %s" % key)
        with self._pending_settings_lock:
            self._pending_settings.append((key, value))
        # Reflect immediately in the status cache so the UI shows the new
        # value on the next poll (range validation still happens on the
        # GLib side before the D-Bus write).
        if isinstance(self._cache.get("settings"), dict):
            self._cache["settings"][key] = value

    def drain_pending_settings(self):
        """Atomically take all pending setting updates (GLib thread)."""
        with self._pending_settings_lock:
            pending, self._pending_settings = self._pending_settings, []
        return pending

    def _handle_options(self, req):
        """CORS preflight for the cross-port control POSTs."""
        req.send_response(204)
        self._send_cors_headers(req)
        req.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        req.send_header("Access-Control-Allow-Headers", "Content-Type")
        req.end_headers()

    def _send_json(self, req, payload):
        req.send_response(200)
        req.send_header("Content-Type", "application/json")
        self._send_cors_headers(req)
        req.end_headers()
        req.wfile.write(json.dumps(payload).encode())

    def _send_cors_headers(self, req):
        # The unified page is loaded from ONE service's port but must read
        # and control BOTH services — every API response allows cross-origin.
        req.send_header("Access-Control-Allow-Origin", "*")
