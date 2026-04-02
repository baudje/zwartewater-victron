"""Basic auth helpers for the Cerbo-hosted web dashboards."""

import base64
import binascii
import json
import logging
import os
import secrets

log = logging.getLogger(__name__)

AUTH_FILE = "/data/apps/fla-shared/webui-auth.json"
DEFAULT_USERNAME = "victron"


def ensure_credentials(path=AUTH_FILE):
    """Return persistent credentials, creating them on first use."""
    if os.path.exists(path):
        try:
            with open(path) as f:
                creds = json.load(f)
            if creds.get("username") and creds.get("password"):
                return creds
        except (OSError, json.JSONDecodeError):
            log.warning("Web UI auth file unreadable — regenerating")

    creds = {
        "username": DEFAULT_USERNAME,
        "password": secrets.token_urlsafe(24),
    }
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(creds, f)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)
    log.warning("Created web UI credentials file at %s", path)
    return creds


def is_authorized(headers, credentials):
    """Return True when the request contains valid HTTP Basic auth."""
    if not credentials:
        return True

    auth_header = headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Basic "):
        return False

    try:
        decoded = base64.b64decode(auth_header[6:], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False

    username, sep, password = decoded.partition(":")
    if not sep:
        return False

    return (
        secrets.compare_digest(username, credentials["username"])
        and secrets.compare_digest(password, credentials["password"])
    )


def send_unauthorized(handler, realm):
    """Send an HTTP 401 challenge."""
    handler.send_response(401)
    handler.send_header("WWW-Authenticate", 'Basic realm="%s"' % realm)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.end_headers()
    handler.wfile.write(b"Authentication required\n")
