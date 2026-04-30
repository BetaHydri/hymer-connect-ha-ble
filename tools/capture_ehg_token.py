"""Capture the EHG Remote Access Refresh Token from the Hymer Connect app.

Intercepts HTTP/HTTPS traffic via mitmproxy and scans for JWTs with
ett=access-refresh in request bodies, response bodies, HTTP headers, and
WebSocket messages.  A fast-path checks the known /remoteAccessToken
endpoint first; a generic JWT regex scanner catches tokens that appear
in unexpected locations.

Usage:
    1. Install mitmproxy:  pip install mitmproxy
    2. Run this script:    mitmdump -s capture_ehg_token.py --listen-port 8080
    3. Set your phone's Wi-Fi proxy to <PC_IP>:8080
    4. Install the mitmproxy CA cert: open http://mitm.it on the phone
    5. Open the Hymer Connect app (patched APK with cert pinning disabled)
    6. The token will be printed and saved automatically

The script auto-exits after capturing the token.

.AUTHOR Jan Tiedemann
.DATE 2026
"""

from __future__ import annotations

import base64
import json
import re
import sys
import os
from datetime import datetime, timezone
from pathlib import Path

from mitmproxy import ctx, http

# Target: POST /api/ehg/v1/vehicles/{urn}/remoteAccessToken
TARGET_PATH = "/remoteAccessToken"
# Alternative: look for the token in WebSocket UpdateTokens messages
SIGNALR_HOSTS = {"ehg-prod-signalr.service.signalr.net"}

# Generic JWT pattern: header.payload.signature (each part is base64url)
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")

OUTPUT_DIR = Path(__file__).parent
TOKEN_FILE = OUTPUT_DIR / "captured_ehg_token.txt"

_BANNER = """
╔══════════════════════════════════════════════════════════════════╗
║                                                                  ║
║   ✅  EHG REFRESH TOKEN CAPTURED SUCCESSFULLY!                   ║
║                                                                  ║
║   The token has been saved to:                                   ║
║   {path:<55s}  ║
║                                                                  ║
║   Copy this token into your HYMER Connect integration config     ║
║   in Home Assistant under "EHG Remote Access Refresh Token".     ║
║                                                                  ║
║   You can now:                                                   ║
║   1. Close this proxy (Ctrl+C)                                   ║
║   2. Remove the proxy settings from your phone's Wi-Fi           ║
║   3. Uninstall the patched APK and reinstall the original app    ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""


def _decode_jwt_payload(token: str) -> dict:
    """Decode the payload of a JWT without verification."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        # Add padding
        payload += "=" * (4 - len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return {}


def _is_refresh_token(token: str) -> bool:
    """Check if a JWT is an EHG remote access refresh token (ett=access-refresh)."""
    payload = _decode_jwt_payload(token)
    return payload.get("ett") == "access-refresh"


def _find_refresh_token(text: str | None) -> str | None:
    """Scan text for any JWT whose payload has ett=access-refresh."""
    if not text:
        return None
    for match in JWT_RE.finditer(text):
        candidate = match.group(0)
        if _is_refresh_token(candidate):
            return candidate
    return None


def _save_token(token: str) -> None:
    """Save the captured token and print success banner."""
    TOKEN_FILE.write_text(token, encoding="utf-8")

    payload = _decode_jwt_payload(token)
    vehicle = payload.get("urn", "unknown")
    client_id = payload.get("client_id", "unknown")

    print("\n" + "=" * 70)
    print(_BANNER.format(path=str(TOKEN_FILE)))
    print(f"   Vehicle:   {vehicle}")
    print(f"   Client ID: {client_id} (phone BLE MAC)")
    print(f"   Token type: {payload.get('ett', 'unknown')}")
    print(f"   Token length: {len(token)} chars")
    print(f"   Starts with: {token[:50]}...")
    print("=" * 70)
    print(f"\n   TOKEN:\n\n{token}\n")
    print("=" * 70)


class EhgTokenCapture:
    """mitmproxy addon that captures the EHG refresh token."""

    def __init__(self):
        self._found = False

    def _try_save(self, token: str, source: str) -> bool:
        """Validate and save a candidate token. Returns True if saved."""
        if token and _is_refresh_token(token):
            ctx.log.info(f"🎯 Found refresh token via {source}")
            _save_token(token)
            self._found = True
            return True
        return False

    def _scan_headers(self, flow: http.HTTPFlow) -> bool:
        """Scan request and response headers for JWTs."""
        for name, value in flow.request.headers.items(multi=True):
            token = _find_refresh_token(value)
            if token and self._try_save(token, f"request header '{name}'"):
                return True
        if flow.response:
            for name, value in flow.response.headers.items(multi=True):
                token = _find_refresh_token(value)
                if token and self._try_save(token, f"response header '{name}'"):
                    return True
        return False

    def request(self, flow: http.HTTPFlow) -> None:
        """Intercept HTTP requests to find the refresh token."""
        if self._found:
            return

        # Fast path: POST /remoteAccessToken — request body contains the token
        if (
            flow.request.method == "POST"
            and TARGET_PATH in flow.request.pretty_url
        ):
            try:
                body = flow.request.get_text()
                if body:
                    data = json.loads(body)
                    token = data.get("token", "")
                    if self._try_save(token, f"POST {flow.request.pretty_url} body['token']"):
                        return
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        # Generic: scan entire request body for JWTs
        try:
            body = flow.request.get_text()
            token = _find_refresh_token(body)
            if token and self._try_save(token, f"request body JWT scan ({flow.request.pretty_url})"):
                return
        except (UnicodeDecodeError, ValueError):
            pass

        # Scan request headers
        self._scan_headers(flow)

    def response(self, flow: http.HTTPFlow) -> None:
        """Scan response bodies and headers for the refresh token."""
        if self._found:
            return

        if flow.response and flow.response.content:
            # Generic: scan entire response body for JWTs
            try:
                text = flow.response.get_text()
                token = _find_refresh_token(text)
                if token and self._try_save(token, f"response body JWT scan ({flow.request.pretty_url})"):
                    return
            except (UnicodeDecodeError, ValueError):
                pass

        # Scan response headers (Authorization, Set-Cookie, custom headers)
        self._scan_headers(flow)

    def websocket_message(self, flow: http.HTTPFlow) -> None:
        """Check WebSocket messages for the refresh token."""
        if self._found:
            return

        if not flow.websocket:
            return

        msg = flow.websocket.messages[-1]
        if not msg.is_text:
            return

        # SignalR uses \x1e as record separator
        for part in msg.text.split("\x1e"):
            part = part.strip()
            if not part:
                continue
            try:
                parsed = json.loads(part)
            except json.JSONDecodeError:
                # Not valid JSON — still scan raw text for JWTs
                token = _find_refresh_token(part)
                if token and self._try_save(token, "WebSocket raw text JWT scan"):
                    return
                continue

            # Fast path: UpdateTokens invocation
            if parsed.get("target") == "UpdateTokens":
                args = parsed.get("arguments", [])
                if args and isinstance(args[0], dict):
                    ehg_token = args[0].get("ehgAccessToken", "")
                    if self._try_save(ehg_token, "UpdateTokens['ehgAccessToken']"):
                        return

            # Generic: scan entire SignalR message for JWTs
            token = _find_refresh_token(part)
            if token and self._try_save(token, "WebSocket message JWT scan"):
                return


addons = [EhgTokenCapture()]
