"""Single-shot PIA protobuf field PROBE for HYMER Connect SCU.

Connects to the EHG cloud via SignalR and sends ONE trial PIA request carrying
a single operator-chosen sub-field of the UserRequestTopic (Request field 8) or
CommandRequestTopic (field 9) envelope, then captures the SCU's response. Used
to identify undocumented commands such as:

    - getPairedMobileDevices   (read-only, the ONLY reasonably safe target)
    - deleteMobileDevices      (destructive: removes a paired device)
    - deleteAllUsers / deleteUser  (DESTRUCTIVE: can wipe the account)

Known field numbers (confirmed):
    UserRequestTopic (field 8):  4 = pairMobileDevice, 6 = pairMobileDeviceConfirmation
    CommandRequestTopic (field 9): 2 = restart

SAFETY (why this is single-shot, not a sweep)
---------------------------------------------
The field numbers of deleteUser / deleteAllUsers are UNKNOWN. A blind 1-15 sweep
would therefore eventually send one of them and could wipe your account/pairings.
This tool follows the safe model used by dan-simms1/hymer-connect-ha's
`scu-user-topic` probe: it sends exactly ONE deliberately chosen field per run,
refuses to send without an explicit acknowledgement, refuses the pairing ceremony
fields (4/6) and restart (command 2), and NEVER accepts a range. Prefer probing
`getPairedMobileDevices` (read-only, empty payload) first.

Usage:
    Set environment variables:
        HYMER_USERNAME=your@email.com
        HYMER_PASSWORD=yourpassword
        HYMER_EHG_REFRESH_TOKEN=eyJraWQ...

    Then probe ONE field, e.g. a candidate for getPairedMobileDevices:
        python scan_pia_fields.py --envelope user --field 5 \
            --i-understand-may-be-destructive

.AUTHOR Jan Tiedemann
.DATE 2026
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import struct
import sys
import time
import argparse
from typing import Any

# Add parent directory to path so we can import the integration modules
_integration_dir = os.path.join(os.path.dirname(__file__), '..', 'custom_components', 'hymer_connect')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'custom_components'))

import types
hymer_pkg = types.ModuleType('hymer_connect')
hymer_pkg.__path__ = [os.path.abspath(_integration_dir)]
hymer_pkg.__package__ = 'hymer_connect'
sys.modules['hymer_connect'] = hymer_pkg

import aiohttp
from hymer_connect.api import HymerConnectApi
from hymer_connect.const import API_BASE_URL


# ---------------------------------------------------------------------------
# Protobuf encoding helpers (standalone, no pia_decoder import needed)
# ---------------------------------------------------------------------------

def _encode_varint(value: int) -> bytes:
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def _encode_field(field_number: int, wire_type: int, data: bytes) -> bytes:
    tag = _encode_varint((field_number << 3) | wire_type)
    return tag + data


def _encode_varint_field(field_number: int, value: int) -> bytes:
    return _encode_field(field_number, 0, _encode_varint(value))


def _encode_bytes_field(field_number: int, data: bytes) -> bytes:
    return _encode_field(field_number, 2, _encode_varint(len(data)) + data)


def _encode_str_field(field_number: int, value: str) -> bytes:
    return _encode_bytes_field(field_number, value.encode("utf-8"))


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while pos < len(data):
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not (b & 0x80):
            return result, pos
        shift += 7
    return result, pos


def _decode_protobuf(data: bytes) -> list[tuple[int, int, Any]]:
    """Decode raw protobuf into (field_number, wire_type, value) tuples."""
    fields: list[tuple[int, int, Any]] = []
    pos = 0
    while pos < len(data):
        try:
            tag, pos = _decode_varint(data, pos)
        except (IndexError, ValueError):
            break
        field_number = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:  # varint
            value, pos = _decode_varint(data, pos)
            fields.append((field_number, 0, value))
        elif wire_type == 2:  # length-delimited
            length, pos = _decode_varint(data, pos)
            if pos + length > len(data):
                break
            value = data[pos:pos + length]
            pos += length
            fields.append((field_number, 2, value))
        elif wire_type == 5:  # fixed32
            if pos + 4 > len(data):
                break
            value = struct.unpack_from("<f", data, pos)[0]
            pos += 4
            fields.append((field_number, 5, round(value, 2)))
        elif wire_type == 1:  # fixed64
            if pos + 8 > len(data):
                break
            pos += 8
            fields.append((field_number, 1, None))
        else:
            break
    return fields


def _try_decode_string(data: bytes) -> str | None:
    try:
        text = data.decode("utf-8")
        if text and all(c.isprintable() or c in "\r\n\t" for c in text):
            return text
    except (UnicodeDecodeError, ValueError):
        pass
    return None


def _dump_protobuf(data: bytes, indent: int = 0) -> str:
    """Recursively decode and pretty-print a protobuf message."""
    lines: list[str] = []
    prefix = "  " * indent
    fields = _decode_protobuf(data)
    for fn, wt, val in fields:
        if wt == 0:
            lines.append(f"{prefix}field {fn} (varint): {val}")
        elif wt == 2:
            text = _try_decode_string(val)
            if text and len(text) < 200:
                lines.append(f"{prefix}field {fn} (string): \"{text}\"")
            else:
                # Try to decode as nested protobuf
                try:
                    nested = _decode_protobuf(val)
                    if nested and all(f[0] > 0 for f in nested):
                        lines.append(f"{prefix}field {fn} (message, {len(val)} bytes):")
                        lines.append(_dump_protobuf(val, indent + 1))
                    else:
                        lines.append(f"{prefix}field {fn} (bytes, {len(val)}): {val[:50].hex()}")
                except Exception:
                    lines.append(f"{prefix}field {fn} (bytes, {len(val)}): {val[:50].hex()}")
        elif wt == 5:
            lines.append(f"{prefix}field {fn} (float32): {val}")
        elif wt == 1:
            lines.append(f"{prefix}field {fn} (fixed64)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# PIA request builders
# ---------------------------------------------------------------------------

APP_VERSION = "v0.32.0"

# Known field labels for display
USER_TOPIC_KNOWN = {
    4: "pairMobileDevice",
    6: "pairMobileDeviceConfirmation",
}

COMMAND_TOPIC_KNOWN = {
    2: "restart",
}

# Read-only sub-fields that are safe to probe with an empty payload. Everything
# else in these topics is either a write or an unknown (possibly destructive)
# command, so it must be chosen deliberately, one at a time.
SAFE_USAGE_NOTICE = """\
Nothing sent. This is a single-shot probe, not a scanner.

The field numbers for deleteUser / deleteAllUsers are UNKNOWN, so a blind sweep
could wipe your account. Choose ONE field deliberately and send it explicitly.

Known UserRequestTopic (Request field 8) sub-fields:
    4 = pairMobileDevice          (refused here - use the BLE pairing path)
    6 = pairMobileDeviceConfirmation (refused here)
    ? = getPairedMobileDevices    (read-only - the SAFE thing to hunt for)
    ? = deleteMobileDevices       (destructive - needs a mobileDeviceMac payload)
    ? = deleteUser / deleteAllUsers (DESTRUCTIVE - do not guess blindly)

Example (probe a candidate for the read-only getPairedMobileDevices):
    python scan_pia_fields.py --envelope user --field 5 \\
        --i-understand-may-be-destructive

Start with likely candidates near the known pairing fields (5, 7, 8, 10, 11) and
probe ONE at a time, with an EMPTY payload, watching for a response that carries
a repeated mobileDevices list.
"""

DESTRUCTIVE_REFUSAL = """\
Refusing to send. Some UserRequestTopic sub-fields are destructive
(deleteUser, deleteAllUsers) and their field numbers are UNKNOWN. Pass
--i-understand-may-be-destructive once you have chosen a single field
deliberately. Never sweep a range.
"""


def build_user_topic_probe(field_number: int, payload: bytes = b"") -> str:
    """Build a Request with UserRequestTopic (field 8) containing a sub-field probe.

    Request envelope:
        field 1: request_id
        field 2: version
        field 3: timestamp
        field 8: UserRequestTopic { field N: payload }
    """
    msg_id = random.randint(1, 10_000_000)
    ts = int(time.time())

    # UserRequestTopic: field N = payload
    user_topic = _encode_bytes_field(field_number, payload)

    # Request envelope
    request = _encode_varint_field(1, msg_id)
    request += _encode_bytes_field(2, APP_VERSION.encode("utf-8"))
    request += _encode_varint_field(3, ts)
    request += _encode_bytes_field(8, user_topic)

    # Outer envelope: field 2 = request (cloud transport uses field 2)
    outer = _encode_bytes_field(2, request)
    return base64.b64encode(outer).decode("ascii"), msg_id


def build_command_topic_probe(field_number: int, payload: bytes = b"") -> str:
    """Build a Request with CommandRequestTopic (field 9) containing a sub-field probe."""
    msg_id = random.randint(1, 10_000_000)
    ts = int(time.time())
    command_topic = _encode_bytes_field(field_number, payload)
    request = _encode_varint_field(1, msg_id)
    request += _encode_bytes_field(2, APP_VERSION.encode("utf-8"))
    request += _encode_varint_field(3, ts)
    request += _encode_bytes_field(9, command_topic)
    outer = _encode_bytes_field(2, request)
    return base64.b64encode(outer).decode("ascii"), msg_id


def build_toplevel_topic_probe(field_number: int, payload: bytes = b"") -> str:
    """Build a Request with a top-level topic field (not nested in field 8 or 9).

    Dan's code sends subscriptions on Request fields 4, 5, 9, 12, 15 directly.
    The user management commands might also be at the top level.
    """
    msg_id = random.randint(1, 10_000_000)
    ts = int(time.time())
    request = _encode_varint_field(1, msg_id)
    request += _encode_bytes_field(2, APP_VERSION.encode("utf-8"))
    request += _encode_varint_field(3, ts)
    request += _encode_bytes_field(field_number, payload)
    outer = _encode_bytes_field(2, request)
    return base64.b64encode(outer).decode("ascii"), msg_id


# ---------------------------------------------------------------------------
# SignalR minimal client (standalone, not using signalr_client.py to avoid HA deps)
# ---------------------------------------------------------------------------

SIGNALR_RECORD_SEPARATOR = "\x1e"
MSG_TYPE_INVOCATION = 1
MSG_TYPE_COMPLETION = 3
MSG_TYPE_PING = 6


class PiaFieldScanner:
    """Standalone SignalR client that sends PIA probes and captures responses."""

    def __init__(
        self,
        api: HymerConnectApi,
        session: aiohttp.ClientSession,
        vehicle_urn: str,
        scu_urn: str,
        ehg_refresh_token: str,
    ):
        self._api = api
        self._session = session
        self._vehicle_urn = vehicle_urn
        self._scu_urn = scu_urn
        self._ehg_refresh_token = ehg_refresh_token
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._responses: dict[int, dict] = {}  # request_id → response
        self._all_messages: list[dict] = []

    async def connect(self) -> None:
        """Negotiate and connect to SignalR (2-step negotiate like the real integration)."""
        # Step 1: Negotiate with scc-appcomm to get Azure SignalR URL + token
        negotiate1 = await self._api.signalr_negotiate()
        azure_url = negotiate1.get("url")
        signalr_token = negotiate1.get("accessToken")
        print(f"  Negotiate step 1: url={'yes' if azure_url else 'NO'}, token={'yes' if signalr_token else 'NO'}")

        if not azure_url or not signalr_token:
            raise RuntimeError("SignalR negotiate step 1 did not return url/accessToken")

        # Step 2: Negotiate with Azure SignalR to get connectionToken
        negotiate2_url = azure_url.replace("client/?", "client/negotiate?")
        headers = {
            "Authorization": f"Bearer {signalr_token}",
            "X-Requested-With": "XMLHttpRequest",
            "X-SignalR-User-Agent": (
                "Microsoft SignalR/6.0 "
                "(6.0.25; Unknown OS; Browser; Unknown Runtime Version)"
            ),
            "Content-Type": "text/plain;charset=UTF-8",
        }
        async with self._session.post(negotiate2_url, headers=headers, data="") as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(f"SignalR negotiate step 2 failed: {resp.status} {text[:200]}")
            negotiate2 = await resp.json()

        connection_token = negotiate2.get("connectionToken")
        if not connection_token:
            raise RuntimeError("SignalR negotiate step 2 did not return connectionToken")
        print(f"  Negotiate step 2: connectionToken={'yes' if connection_token else 'NO'}")

        # Step 3: Build WebSocket URL and connect
        ws_url = azure_url.replace("https://", "wss://")
        ws_url += f"&id={connection_token}&access_token={signalr_token}"
        self._ws = await self._session.ws_connect(ws_url)

        # Send SignalR handshake
        handshake = json.dumps({"protocol": "json", "version": 1}) + SIGNALR_RECORD_SEPARATOR
        await self._ws.send_str(handshake)

        # Read handshake response
        msg = await self._ws.receive()
        print(f"  SignalR handshake: {msg.data.strip(chr(0x1e))}")

        # Send UpdateTokens
        if self._ehg_refresh_token:
            update_tokens = {
                "arguments": [self._ehg_refresh_token],
                "target": "UpdateTokens",
                "type": MSG_TYPE_INVOCATION,
            }
            await self._ws.send_str(json.dumps(update_tokens) + SIGNALR_RECORD_SEPARATOR)
            print("  UpdateTokens sent")

    async def send_pia_request(self, b64_payload: str) -> None:
        """Send a PiaRequest message."""
        msg = {
            "arguments": [b64_payload],
            "target": "PiaRequest",
            "type": MSG_TYPE_INVOCATION,
        }
        await self._ws.send_str(json.dumps(msg) + SIGNALR_RECORD_SEPARATOR)

    async def listen_for_responses(self, timeout: float = 10.0) -> list[dict]:
        """Listen for PiaResponse messages until timeout."""
        messages = []
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    msg = await asyncio.wait_for(
                        self._ws.receive(),
                        timeout=min(remaining, 2.0),
                    )
                except asyncio.TimeoutError:
                    continue

                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue

                for part in msg.data.split(SIGNALR_RECORD_SEPARATOR):
                    part = part.strip()
                    if not part:
                        continue
                    try:
                        parsed = json.loads(part)
                    except json.JSONDecodeError:
                        continue

                    msg_type = parsed.get("type", 0)
                    if msg_type == MSG_TYPE_PING:
                        # Reply to ping
                        await self._ws.send_str(
                            json.dumps({"type": MSG_TYPE_PING}) + SIGNALR_RECORD_SEPARATOR
                        )
                        continue

                    target = parsed.get("target", "")
                    if target == "PiaResponse":
                        messages.append(parsed)
                        self._all_messages.append(parsed)

        except Exception as e:
            print(f"  Listen error: {e}")

        return messages

    async def close(self) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.close()


def decode_pia_response(b64_payload: str) -> dict[str, Any]:
    """Decode a PiaResponse payload and return structured info."""
    try:
        raw = base64.b64decode(b64_payload)
    except Exception:
        return {"error": "base64 decode failed"}

    result: dict[str, Any] = {"raw_hex": raw.hex()[:200], "size": len(raw)}

    # Walk outer protobuf
    outer = _decode_protobuf(raw)
    for fn, wt, val in outer:
        if wt == 2 and isinstance(val, bytes):
            # Try to find Response envelope
            inner = _decode_protobuf(val)
            for ifn, iwt, ival in inner:
                if ifn == 1 and iwt == 0:
                    result["request_id"] = ival
                elif ifn == 2 and iwt == 0:
                    result["status"] = ival
                elif ifn == 3 and iwt == 0:
                    result["timestamp"] = ival
                elif iwt == 2 and isinstance(ival, bytes):
                    # Payload field — dump it
                    result[f"response_field_{ifn}"] = {
                        "size": len(ival),
                        "hex": ival.hex()[:200],
                        "dump": _dump_protobuf(ival),
                    }

    return result


# ---------------------------------------------------------------------------
# Main scanner
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(
        description=(
            "Probe ONE PIA UserRequestTopic/CommandRequestTopic sub-field to identify "
            "an undocumented SCU command. Single-shot by design: it never sweeps a range, "
            "because the destructive delete* field numbers are unknown and a blind sweep "
            "could wipe the account."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--field", type=int, default=None,
        help="The SINGLE sub-field number to probe (e.g. 5). Required to send anything."
    )
    parser.add_argument(
        "--envelope", type=str, default="user",
        choices=["user", "command", "toplevel"],
        help="Envelope the field lives in: user (Request field 8), command (field 9), toplevel (direct). Default: user"
    )
    parser.add_argument(
        "--payload-hex", type=str, default="",
        help="Optional hex payload for the sub-field (e.g. an encoded mobileDeviceMac). Default: empty = read-only probe."
    )
    parser.add_argument(
        "--i-understand-may-be-destructive", dest="ack_destructive",
        action="store_true", default=False,
        help="Required acknowledgement. Some sub-fields (deleteUser/deleteAllUsers) are destructive and their numbers are UNKNOWN."
    )
    parser.add_argument(
        "--timeout", type=float, default=10.0,
        help="Seconds to wait for the SCU response (default: 10)"
    )
    parser.add_argument(
        "--brand", type=str, default="hymer",
        help="Vehicle brand (default: hymer)"
    )
    parser.add_argument(
        "--output", type=str, default="",
        help="Export the raw result to a JSON file"
    )
    args = parser.parse_args()

    # --- Safety gate 1: no field -> print guidance and exit without sending ---
    if args.field is None:
        print(SAFE_USAGE_NOTICE)
        return

    # --- Safety gate 2: refuse the pairing ceremony (use the BLE pairing path) ---
    if args.envelope == "user" and args.field in USER_TOPIC_KNOWN:
        print(
            f"Refusing: UserRequestTopic field {args.field} is "
            f"{USER_TOPIC_KNOWN[args.field]} (the pairing ceremony). Use the "
            "integration's BLE pairing path, not this probe."
        )
        sys.exit(2)

    # --- Safety gate 3: refuse restart ---
    if args.envelope == "command" and args.field == 2:
        print(
            "Refusing: CommandRequestTopic field 2 is restart (reboots the SCU). "
            "Use button.hymer_restart_scu if that is what you want."
        )
        sys.exit(2)

    # --- Safety gate 4: require explicit acknowledgement for every real send ---
    if not args.ack_destructive:
        print(DESTRUCTIVE_REFUSAL)
        sys.exit(2)

    # Decode the optional payload (empty = read-only probe, the only safe kind)
    try:
        payload = bytes.fromhex(args.payload_hex) if args.payload_hex else b""
    except ValueError:
        print(f"ERROR: --payload-hex is not valid hex: {args.payload_hex!r}")
        sys.exit(2)

    field_numbers = [args.field]  # kept for the JSON export block below

    # Credentials
    username = os.environ.get("HYMER_USERNAME", "")
    password = os.environ.get("HYMER_PASSWORD", "")
    refresh_token = os.environ.get("HYMER_EHG_REFRESH_TOKEN", "")

    if not username or not password:
        print("ERROR: Set HYMER_USERNAME and HYMER_PASSWORD environment variables")
        print("  $env:HYMER_USERNAME='your@email.com'")
        print("  $env:HYMER_PASSWORD='yourpassword'")
        sys.exit(1)

    if not refresh_token:
        token_file = os.path.join(os.path.dirname(__file__), "captured_ehg_token.txt")
        if os.path.exists(token_file):
            refresh_token = open(token_file).read().strip()
            print(f"Loaded refresh token from {token_file}")

    if not refresh_token:
        print("ERROR: No EHG refresh token available")
        print("  Set HYMER_EHG_REFRESH_TOKEN or place token in tools/captured_ehg_token.txt")
        sys.exit(1)

    print("=" * 80)
    print("  HYMER Connect PIA Field Probe (single-shot)")
    print("=" * 80)
    print(f"  Envelope: {args.envelope}")
    print(f"  Field: {args.field}")
    print(f"  Payload: {args.payload_hex or 'empty (read-only)'}")
    print(f"  Timeout: {args.timeout}s")
    print()

    results: dict[str, dict] = {}

    async with aiohttp.ClientSession() as session:
        # Authenticate
        print("Connecting to EHG cloud...")
        api = HymerConnectApi(session, brand=args.brand)
        await api.authenticate(username, password)
        print("  Authenticated")

        # Get vehicle info
        vehicles = await api.get_ehg_vehicles()
        if not vehicles:
            print("ERROR: No vehicles found")
            sys.exit(1)
        vehicle = vehicles[0]
        vehicle_urn = vehicle.get("urn", "")
        print(f"  Vehicle: {vehicle.get('name', '?')} ({vehicle_urn})")

        scc_vehicles = await api.get_vehicles()
        scu_urn = ""
        for v in scc_vehicles:
            scu_urn = v.get("smartUnitUrn", "")
            if scu_urn:
                break
        print(f"  SCU: {scu_urn}")

        # Connect SignalR
        print("  Connecting SignalR...")
        scanner = PiaFieldScanner(
            api=api,
            session=session,
            vehicle_urn=vehicle_urn,
            scu_urn=scu_urn,
            ehg_refresh_token=refresh_token,
        )
        await scanner.connect()
        print("  Connected!")
        print()

        # Drain initial messages (subscriptions, etc.)
        print("  Draining initial messages (3s)...")
        await scanner.listen_for_responses(timeout=3.0)
        print()

        # --- Send exactly ONE probe (single-shot by design) ---
        if args.envelope == "user":
            b64, msg_id = build_user_topic_probe(args.field, payload)
            envelope_label = "UserRequestTopic (Request.field_8)"
            label = USER_TOPIC_KNOWN.get(args.field, "???")
        elif args.envelope == "command":
            b64, msg_id = build_command_topic_probe(args.field, payload)
            envelope_label = "CommandRequestTopic (Request.field_9)"
            label = COMMAND_TOPIC_KNOWN.get(args.field, "???")
        else:
            b64, msg_id = build_toplevel_topic_probe(args.field, payload)
            envelope_label = "Request top-level"
            label = "???"

        print("─" * 80)
        print(f"  Probing {envelope_label} sub-field {args.field} ({label})")
        print(f"  payload={args.payload_hex or 'empty (read-only)'}  request_id={msg_id}")
        print("─" * 80)

        await scanner.send_pia_request(b64)
        responses = await scanner.listen_for_responses(timeout=args.timeout)

        matched = []
        for resp in responses:
            resp_args = resp.get("arguments", [])
            if resp_args and isinstance(resp_args[0], str):
                decoded = decode_pia_response(resp_args[0])
                if decoded.get("request_id") == msg_id:
                    matched.append(decoded)

        if matched:
            print("  ✅ RESPONSE (matching request_id):")
            results[f"{args.envelope}_field_{args.field}"] = {
                "envelope": envelope_label,
                "field": args.field,
                "label": label,
                "payload_hex": args.payload_hex,
                "responses": matched,
            }
            for m in matched:
                print(f"       Status: {m.get('status', '?')}")
                for k, v in m.items():
                    if k.startswith("response_field_"):
                        print(f"       {k}: {json.dumps(v, indent=8, default=str)[:800]}")
        else:
            print(
                "  ❌ no response with our request_id "
                "(SCU may be in standby, token invalid, or the field is a no-op)"
            )

        await scanner.close()

    # Summary
    print("=" * 80)
    print("  SCAN RESULTS SUMMARY")
    print("=" * 80)
    if results:
        for key, data in results.items():
            status_list = [r.get("status", "?") for r in data["responses"]]
            payload_fields = []
            for r in data["responses"]:
                payload_fields.extend(k for k in r if k.startswith("response_field_"))
            print(f"  {data['envelope']}.field_{data['field']}: "
                  f"label={data['label']}, status={status_list}, "
                  f"payload_fields={payload_fields or 'none'}")
    else:
        print("  No responses received from any probed field.")
        print("  This is expected if:")
        print("    - SCU is in standby (12V OFF)")
        print("    - EHG refresh token is invalid/expired")
        print("    - SignalR connection lost during scan")
    print("=" * 80)

    # Export
    if args.output:
        output_path = args.output
    else:
        output_path = os.path.join(os.path.dirname(__file__), "pia_field_scan_results.json")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "tool": "scan_pia_fields.py",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "envelope": args.envelope,
            "fields_scanned": field_numbers,
            "timeout_per_probe": args.timeout,
            "results": results,
        }, f, indent=2, default=str)
    print(f"\n  Results exported to: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())