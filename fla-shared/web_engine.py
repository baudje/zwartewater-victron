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
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


class OperationProfile:
    """Declarative card describing one FLA operation's web identity.

    See CONTEXT.md ("Operation profile"): the genuinely-varying parts stay
    per-service in this card; the engine is closed over it. Since the
    unified dashboard, the page itself is SHARED (fla-shared/unified_page.py)
    and fully data-driven: this card's settings_rows/panel_fields/states are
    served via GET /api/config and rendered by the page at runtime, so
    nothing per-service can leak into (or drift inside) the HTML.
    """

    # Value-format names the unified page's JS knows how to render.
    PANEL_FORMATS = ("v", "a", "pct", "days_or_due", "text")

    def __init__(self, name, title, port, states, error_state,
                 settings_keys, cache_fields, settings_rows, panel_fields,
                 cache_aliases=None, allowed_origin_ports=None,
                 run_now_message=None, run_now_confirm=None, log_file=None):
        # Optional path to this service's log; served (tail only, bounded)
        # via GET /api/log for the dashboard's log card.
        self.log_file = log_file
        self.cache_aliases = cache_aliases or {}
        # Optional callable(settings_dict) -> str for the /api/run-now
        # reply, so a service can name its start precondition (e.g. the EQ
        # SoC gate) with live threshold values.
        self.run_now_message = run_now_message
        # Optional browser confirm() text for the Run Now button; may embed
        # {setting_key} placeholders the page fills from live settings.
        self.run_now_confirm = run_now_confirm
        # Ports whose pages may issue cross-origin control POSTs (the two
        # dashboard ports). Origins on other ports — or other hosts — are
        # refused; see WebEngine._origin_allowed.
        self.allowed_origin_ports = allowed_origin_ports or [port]
        self.name = name
        self.title = title
        self.port = port
        self.states = states
        self.error_state = error_state
        self.settings_keys = settings_keys
        self.cache_fields = cache_fields
        # Editable settings shown on this operation's panel:
        # {key, label, unit, type} with type "f" (float), "i" (int) or
        # "b" (yes/no select stored as int).
        self.settings_rows = settings_rows
        # Extra status rows shown on this operation's panel:
        # {key: cache field, label, format: one of PANEL_FORMATS}.
        self.panel_fields = panel_fields
        self._validate()

    def _validate(self):
        """Fail at service startup, not when a page or endpoint is hit."""
        missing = [field for field in ("name", "title", "states",
                                       "settings_keys", "cache_fields",
                                       "settings_rows")
                   if not getattr(self, field)]
        if self.port is None:
            missing.append("port")
        if self.panel_fields is None:
            missing.append("panel_fields")
        if missing:
            raise ValueError("OperationProfile missing required fields: %s"
                             % ", ".join(missing))
        if self.error_state not in self.states:
            raise ValueError("error_state %r is not in states" % self.error_state)
        # The page's abort-button gate is `0 < state < error_state`, so the
        # error state must be the highest-numbered — a state appended above
        # it would silently hide Abort while live.
        if self.error_state != max(self.states):
            raise ValueError("error_state %r must be the highest state (max is %r)"
                             % (self.error_state, max(self.states)))
        bad_rows = [r["key"] for r in self.settings_rows
                    if r["key"] not in self.settings_keys]
        if bad_rows:
            raise ValueError("settings_rows keys not in settings schema: %s"
                             % ", ".join(bad_rows))
        bad_fields = [f["key"] for f in self.panel_fields
                      if f["key"] not in self.cache_fields]
        if bad_fields:
            raise ValueError("panel_fields keys not in cache_fields: %s"
                             % ", ".join(bad_fields))
        bad_formats = [f["format"] for f in self.panel_fields
                       if f["format"] not in self.PANEL_FORMATS]
        if bad_formats:
            raise ValueError("unknown panel_fields formats: %s (known: %s)"
                             % (", ".join(bad_formats), ", ".join(self.PANEL_FORMATS)))

    def config(self):
        """The JSON-safe card the unified page renders a panel from."""
        return {
            "name": self.name,
            "title": self.title,
            "port": self.port,
            "states": {str(k): v for k, v in self.states.items()},
            "error_state": self.error_state,
            "settings_rows": self.settings_rows,
            "panel_fields": self.panel_fields,
            "run_now_confirm": self.run_now_confirm,
        }


class WebEngine:
    """Closed HTTP engine for one FLA service, configured by its profile."""

    def __init__(self, profile):
        self.profile = profile
        # The SAME page is served at both ports; it discovers each panel's
        # content from /api/config at runtime. Only the port list (shared
        # by both profiles) is baked in.
        from unified_page import render_unified_page
        self._page = render_unified_page(sorted(profile.allowed_origin_ports))
        # Per-instance copy so two engines in one process never share state.
        self._cache = {k: (dict(v) if isinstance(v, dict) else v)
                       for k, v in profile.cache_fields.items()}
        self._aliases = dict(self.CACHE_ALIASES)
        self._aliases.update(profile.cache_aliases)
        # Fail at service startup, not mid-hand-back on the worker thread:
        # every alias must point at a real cache field.
        bad_targets = sorted(t for t in self._aliases.values() if t not in self._cache)
        if bad_targets:
            raise ValueError("cache alias targets not in cache_fields: %s"
                             % ", ".join(bad_targets))
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
            self._send_json(req, self._cache, read_only=True)
        elif req.path == "/api/config":
            self._send_json(req, self.profile.config(), read_only=True)
        elif req.path == "/api/log" or req.path.startswith("/api/log?"):
            self._send_json(req, {"lines": self._log_lines(req.path)}, read_only=True)
        else:
            req.send_response(404)
            req.end_headers()

    def _origin_allowed(self, req):
        """CSRF guard for the control endpoints.

        Reads are world-readable, but writes must not be drivable by an
        arbitrary website visited from a browser on the vessel LAN. The
        unified page is always served from the Cerbo itself, so a browser
        write is legitimate only when its Origin is the SAME HOST the
        client addressed (works for venus.local and raw-IP browsing alike)
        at one of the dashboard ports. Requests without an Origin header
        (curl, local tooling — not subject to CSRF) are allowed."""
        origin = req.headers.get("Origin")
        if origin is None:
            return True
        try:
            parts = urlsplit(origin)
            origin_port = parts.port or (443 if parts.scheme == "https" else 80)
            # Parse the Host header the same way (handles IPv6 brackets and
            # case), instead of naive string splitting.
            request_host = urlsplit("//" + (req.headers.get("Host") or "")).hostname
        except ValueError:
            return False
        # Known residual gap, accepted: DNS rebinding (Origin and Host both
        # reflect the attacker's rebound name, so they match). Closing it
        # needs a pinned hostname allowlist, which would break raw-IP
        # browsing; on this LAN-only, unauthenticated-by-design deployment
        # the same-host check is the chosen trade-off.
        return (parts.scheme in ("http", "https")
                and parts.hostname is not None
                and parts.hostname == request_host
                and origin_port in self.profile.allowed_origin_ports)

    def _refuse_origin(self, req):
        # 403 with NO CORS headers: the browser both blocks the read and
        # the server has refused the action.
        req.send_response(403)
        req.send_header("Content-Type", "application/json")
        req.end_headers()
        req.wfile.write(json.dumps({"ok": False, "error": "origin not allowed"}).encode())

    def _handle_post(self, req):
        if not self._origin_allowed(req):
            self._refuse_origin(req)
            return
        if req.path == "/api/run-now":
            self._run_now_requested = True
            message = "RunNow requested — will start at next check"
            if self.profile.run_now_message:
                try:
                    message = self.profile.run_now_message(
                        dict(self._cache.get("settings") or {}))
                except Exception:
                    pass  # never let a hook error break the control path
            self._send_json(req, {"message": message})
        elif req.path == "/api/abort":
            self._abort_requested = True
            self._send_json(req, {"message": "Abort requested — will stop at next check cycle"})
        elif req.path == "/api/setting":
            try:
                # Parse and clamp inside the try: a malformed or negative
                # Content-Length must yield a JSON error, not an uncaught
                # exception or an rfile.read(-1) that blocks the single
                # HTTP thread (and with it the Abort button) until EOF.
                length = max(0, int(req.headers.get("Content-Length", 0)))
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

    LOG_LINES_DEFAULT = 50
    LOG_LINES_MAX = 200

    def _log_lines(self, path):
        """Bounded tail of the service log (empty when none is configured)."""
        if not self.profile.log_file:
            return []
        from log_tail import tail
        lines = self.LOG_LINES_DEFAULT
        query = urlsplit(path).query
        for part in query.split("&"):
            if part.startswith("lines="):
                try:
                    lines = int(part[len("lines="):])
                except ValueError:
                    pass
        lines = max(1, min(lines, self.LOG_LINES_MAX))
        return tail(self.profile.log_file, lines=lines)

    def _handle_options(self, req):
        """CORS preflight for the cross-port control POSTs. Only origins on
        this host at the dashboard ports are approved (echoed, never *)."""
        if not self._origin_allowed(req) or "Origin" not in req.headers:
            self._refuse_origin(req)
            return
        req.send_response(204)
        self._send_write_cors_headers(req)
        req.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        req.send_header("Access-Control-Allow-Headers", "Content-Type")
        # Let the browser cache the preflight so repeated control POSTs
        # skip the extra round-trip on this single-threaded server.
        req.send_header("Access-Control-Max-Age", "600")
        req.end_headers()

    def _send_json(self, req, payload, read_only=False):
        req.send_response(200)
        req.send_header("Content-Type", "application/json")
        if read_only:
            # Reads carry no control authority — world-readable is fine and
            # lets the unified page poll both services from either port.
            req.send_header("Access-Control-Allow-Origin", "*")
        else:
            self._send_write_cors_headers(req)
        req.end_headers()
        req.wfile.write(json.dumps(payload).encode())

    def _send_write_cors_headers(self, req):
        # Echo the (already validated) specific origin — never a wildcard
        # on state-changing paths.
        origin = req.headers.get("Origin")
        if origin:
            req.send_header("Access-Control-Allow-Origin", origin)
            req.send_header("Vary", "Origin")
