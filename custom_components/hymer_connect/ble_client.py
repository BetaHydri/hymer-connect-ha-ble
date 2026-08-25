"""BLE transport client for direct SCU communication.

Provides a local BLE path to the SCU as an alternative to the cloud SignalR
path. Uses the Nordic UART Service (NUS) over BLE GATT with TLS 1.0/1.1
encryption — the same PIA protobuf protocol used by the EHG app when
physically near the vehicle.

Requires:
  - bleak (BLE GATT client library)
  - Python ssl module with MemoryBIO (TLS over arbitrary transport)
  - BLE hardware (Pi 4 built-in BT 5.0 or USB adapter)

Architecture:
  Phone (BLE) ──► SCU ──► CAN/LIN/PIA devices
  Pi/HA (BLE)  ──► SCU ──► CAN/LIN/PIA devices  ← THIS MODULE
  Pi/HA (cloud) ──► Azure SignalR ──► SCU         ← signalr_client.py

The BLE path is ~50ms latency vs ~500ms-2s for the cloud path.

Pairing (one-time at vehicle):
  The SCU requires pressing the CONNECTION button on the vehicle's
  control panel display to allow a new BLE client to pair. This is the same
  button used when pairing a new smartphone with the EHG app. After the button
  press, the SCU enters pairing mode and accepts the PairMobileRequest. The SCU
  returns a new remoteAccessToken bound to the device's BLE address. The SCU
  supports multiple paired clients simultaneously (phone + Pi).

  Flow: User presses CONNECTION button on SCU → Pi connects via BLE/TLS →
        sends PairMobileRequest → SCU returns remoteAccessToken → stored locally

Credits:
  The PairMobileRequest/Response protobuf field layout and the full BLE pairing
  ceremony (activation token + confirmation token + SCU CONNECTION button +
  remote-access refresh token minting) were reverse-engineered by Dan Simms
  (dan-simms1/hymer-connect-ha) in the standalone hymer_token_tool. The protobuf
  field numbers, nesting structure, and frame encoding in this module are derived
  from his ble.py and scu.py implementation.

Status: EXPERIMENTAL — not yet verified on real hardware.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import random
import ssl
import struct
import time
import zlib
from dataclasses import asdict, dataclass
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

try:
    from bleak import BleakClient, BleakScanner
    HAS_BLEAK = True
except ImportError:
    HAS_BLEAK = False
    BleakClient = None  # type: ignore[assignment,misc]
    BleakScanner = None  # type: ignore[assignment,misc]

# Nordic UART Service (NUS) UUIDs — public Nordic Semiconductor standard
UART_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
UART_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # Phone/Pi → SCU (write)
UART_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # SCU → Phone/Pi (notify)

# Name substrings used to recognise an SCU/SIU in a BLE advertisement.
# The SCU advertises as "<BRAND> <serial>" (confirmed e.g. "HYMER 00013970"),
# so every supported EHG brand token is included here alongside the generic
# SCU/SIU/EHG markers. Kept lowercase for case-insensitive matching.
SCU_NAME_TOKENS: frozenset[str] = frozenset({
    "scu", "siu", "ehg",
    "hymer", "buerstner", "bürstner", "burstner", "dethleffs", "eriba",
    "lmc", "niesmann", "bischoff", "sunlight", "carado", "laika",
    "freeontour",
})

# SCU power management service
POWER_SERVICE_UUID = "fff40001-13c9-42f3-9d46-e1d1aa2a7232"
POWER_STATE_UUID = "fff40002-13c9-42f3-9d46-e1d1aa2a7232"
POWER_CONTROL_UUID = "fff40003-13c9-42f3-9d46-e1d1aa2a7232"
BONDING_STATE_UUID = "fff40004-13c9-42f3-9d46-e1d1aa2a7232"  # Bonding state check (challenge-response)

# BLE PIA frame format: 2-byte magic + 4-byte length + 4-byte CRC32 + payload
BLE_PIA_MAGIC = bytes((0xA0, 0xCB))
BLE_PIA_HEADER_SIZE = 10

# TLS configuration matching the EHG app's observable profile
APP_TLS_CIPHERS = "AES128-SHA:AES256-SHA"
APP_TLS_MIN_VERSION = ssl.TLSVersion.TLSv1
APP_TLS_MAX_VERSION = ssl.TLSVersion.TLSv1_1

# Connection defaults
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_TLS_TIMEOUT = 20.0
DEFAULT_SCAN_TIMEOUT = 8.0
DEFAULT_PAIR_TIMEOUT = 60.0  # pairing needs user to press CONNECTION button on SCU
DEFAULT_DISCONNECT_TIMEOUT = 5.0  # cleanup must not block HA config-entry setup
DEFAULT_NOTIFY_TIMEOUT = 8.0  # fail fast on a stale notify channel (hang ~= 11s)
WAKE_UP_COMMAND = bytes((0x0A,))
DEFAULT_GATT_MTU = 23

# PIA protocol version (matches EHG app 2.10.14)
APP_PIA_VERSION = "v0.32.0"


async def async_clear_bluez_bond(address: str) -> bool:
    """Remove a BlueZ bonding record for the given BLE address.

    Uses D-Bus Adapter1.RemoveDevice() to fully clear the bond.
    Returns True if the bond was removed, False if not found or failed.
    """
    if not address:
        return False
    try:
        from dbus_fast.aio import MessageBus
        from dbus_fast import BusType, Message, MessageType
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        dev_path = f"/org/bluez/hci0/dev_{address.replace(':', '_')}"
        msg = Message(
            destination="org.bluez",
            path="/org/bluez/hci0",
            interface="org.bluez.Adapter1",
            member="RemoveDevice",
            signature="o",
            body=[dev_path],
        )
        reply = await bus.call(msg)
        bus.disconnect()
        if reply.message_type == MessageType.ERROR:
            _LOGGER.debug("RemoveDevice %s: %s", address, reply.body)
            return False
        _LOGGER.info("Cleared BlueZ bond for %s", address)
        return True
    except Exception as err:
        _LOGGER.debug("Failed to clear BlueZ bond for %s: %s", address, err)
        return False


async def async_dbus_disconnect(address: str) -> bool:
    """Force a D-Bus Device1.Disconnect() on the given BLE address.

    Unlike BleakClient.disconnect(), a Device1.Disconnect() makes BlueZ drop
    every AcquireWrite/AcquireNotify file descriptor it still holds for the
    device. This is the only client-side way (short of restarting the
    bluetooth service) to release a write/notify channel that stayed acquired
    after an abrupt session teardown (org.bluez.Error.NotPermitted:
    "Write acquired" / "Notify acquired"). Keeps the bond intact.
    Returns True if the Disconnect() call succeeded.
    """
    if not address:
        return False
    try:
        from dbus_fast.aio import MessageBus
        from dbus_fast import BusType, Message, MessageType
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            dev_path = f"/org/bluez/hci0/dev_{address.replace(':', '_')}"
            msg = Message(
                destination="org.bluez",
                path=dev_path,
                interface="org.bluez.Device1",
                member="Disconnect",
            )
            reply = await bus.call(msg)
            if reply.message_type == MessageType.ERROR:
                _LOGGER.debug("Device1.Disconnect %s: %s", address, reply.body)
                return False
            _LOGGER.info(
                "Forced D-Bus Device1.Disconnect for %s "
                "(releases any stale write/notify channel)",
                address,
            )
            return True
        finally:
            bus.disconnect()
    except Exception as err:
        _LOGGER.debug("D-Bus Device1.Disconnect failed for %s: %s", address, err)
        return False

# ---------------------------------------------------------------------------
# Protobuf wire format helpers (minimal, no external dependency)
# ---------------------------------------------------------------------------
# Field numbers and nesting structure derived from Dan Simms'
# hymer_token_tool (dan-simms1/hymer-connect-ha), which decoded them
# from the decompiled EHG Hermes/React Native app bundle.

_WIRE_VARINT = 0
_WIRE_LEN = 2

# BleProtocol envelope
_BLE_PROTOCOL_REQUEST_FIELD = 1
_BLE_PROTOCOL_RESPONSE_FIELD = 2

# Request envelope
_REQUEST_ID_FIELD = 1
_REQUEST_VERSION_FIELD = 2
_REQUEST_TIMESTAMP_FIELD = 3
_REQUEST_USER_FIELD = 8

# UserRequestTopic branches
_USER_PAIR_MOBILE_DEVICE_FIELD = 4
_USER_PAIR_MOBILE_CONFIRMATION_FIELD = 6

# PairMobileRequest fields
_PAIR_REQ_ACTIVATION_TOKEN_FIELD = 1
_PAIR_REQ_CONFIRMATION_TOKEN_FIELD = 2
_PAIR_REQ_MOBILE_DEVICE_NAME_FIELD = 3
_PAIR_REQ_WAIT_FOR_CONFIRMATION_FIELD = 4

# PairMobileConfirmation
_PAIR_CONFIRM_SUCCESS_FIELD = 1

# Response envelope
_RESPONSE_ID_FIELD = 1
_RESPONSE_STATUS_FIELD = 2
_RESPONSE_TIMESTAMP_FIELD = 3
_RESPONSE_MOBILE_PAIR_FIELD = 9

# PairMobileResponse fields
_PAIR_RESP_ACCESS_TOKEN_FIELD = 1
_PAIR_RESP_REFRESH_TOKEN_FIELD = 2
_PAIR_RESP_CONFIRMATION_REQUIRED_FIELD = 3

# ConnectedComponent request topic (Request field 4) — same setValues topic the
# cloud encoder builds; only the outer BleProtocol wrapper differs on BLE.
_REQUEST_CONNECTED_COMPONENT_FIELD = 4

# PIA status codes (Response.status). Full enum in docs/ehg-app-ble-protocol.md.
# 0 NO_STATUS, 1 SUCCESS, 5 ACCESS_DENIED, 15 SCU_IS_NOT_ONLINE, ...
PIA_STATUS_SUCCESS = 1


def _encode_varint(value: int) -> bytes:
    """Encode a non-negative integer as a protobuf varint."""
    buf = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            buf.append(b | 0x80)
        else:
            buf.append(b)
            return bytes(buf)


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Decode a protobuf varint, return (value, next_offset)."""
    value = shift = 0
    while offset < len(data):
        b = data[offset]
        value |= (b & 0x7F) << shift
        offset += 1
        if not (b & 0x80):
            return value, offset
        shift += 7
    raise ValueError("unterminated protobuf varint")


def _encode_key(field: int, wire_type: int) -> bytes:
    return _encode_varint((field << 3) | wire_type)


def _encode_varint_field(field: int, value: int) -> bytes:
    return _encode_key(field, _WIRE_VARINT) + _encode_varint(value)


def _encode_bool_field(field: int, value: bool) -> bytes:
    return _encode_varint_field(field, 1 if value else 0)


def _encode_string_field(field: int, value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _encode_key(field, _WIRE_LEN) + _encode_varint(len(encoded)) + encoded


def _encode_bytes_field(field: int, value: bytes) -> bytes:
    return _encode_key(field, _WIRE_LEN) + _encode_varint(len(value)) + value


def _decode_len_delimited(data: bytes, offset: int) -> tuple[bytes, int]:
    length, offset = _decode_varint(data, offset)
    end = offset + length
    if end > len(data):
        raise ValueError("protobuf length-delimited field overruns buffer")
    return data[offset:end], end


def _skip_field(data: bytes, offset: int, wire_type: int) -> int:
    if wire_type == _WIRE_VARINT:
        _, offset = _decode_varint(data, offset)
    elif wire_type == _WIRE_LEN:
        _, offset = _decode_len_delimited(data, offset)
    elif wire_type == 1:  # 64-bit
        offset += 8
    elif wire_type == 5:  # 32-bit
        offset += 4
    else:
        raise ValueError(f"unsupported wire type {wire_type}")
    return offset


# ---------------------------------------------------------------------------
# PairMobileRequest / Response builders
# ---------------------------------------------------------------------------

@dataclass
class PairMobileResponse:
    """Decoded SCU PairMobileResponse."""

    remote_access_token: str
    remote_access_refresh_token: str
    confirmation_required: bool
    request_id: int | None = None
    status: int | None = None
    timestamp: int | None = None


def _build_pair_mobile_request_payload(
    activation_token: str,
    confirmation_token: str,
    mobile_device_name: str,
) -> bytes:
    """Build the inner PairMobileRequest protobuf."""
    return b"".join((
        _encode_string_field(_PAIR_REQ_ACTIVATION_TOKEN_FIELD, activation_token),
        _encode_string_field(_PAIR_REQ_CONFIRMATION_TOKEN_FIELD, confirmation_token),
        _encode_string_field(_PAIR_REQ_MOBILE_DEVICE_NAME_FIELD, mobile_device_name),
        _encode_bool_field(_PAIR_REQ_WAIT_FOR_CONFIRMATION_FIELD, True),
    ))


def _build_pair_mobile_confirmation_payload(success: bool = True) -> bytes:
    """Build the PairMobileConfirmation protobuf."""
    return _encode_bool_field(_PAIR_CONFIRM_SUCCESS_FIELD, success)


def build_pair_mobile_frame(
    activation_token: str,
    confirmation_token: str,
    mobile_device_name: str,
) -> bytes:
    """Build a complete BLE PIA frame containing PairMobileRequest.

    Protobuf nesting:
      BleProtocol.request(1) → Request → User(8) → PairMobileDevice(4)
    """
    request_id = math.ceil(random.random() * 1_000_000) + 1
    timestamp = round(time.time())

    pair_mobile = _build_pair_mobile_request_payload(
        activation_token, confirmation_token, mobile_device_name,
    )
    user_topic = _encode_bytes_field(_USER_PAIR_MOBILE_DEVICE_FIELD, pair_mobile)
    request_msg = b"".join((
        _encode_varint_field(_REQUEST_ID_FIELD, request_id),
        _encode_string_field(_REQUEST_VERSION_FIELD, APP_PIA_VERSION),
        _encode_varint_field(_REQUEST_TIMESTAMP_FIELD, timestamp),
        _encode_bytes_field(_REQUEST_USER_FIELD, user_topic),
    ))
    ble_protocol = _encode_bytes_field(_BLE_PROTOCOL_REQUEST_FIELD, request_msg)
    return encode_ble_pia_frame(ble_protocol)


def build_pair_mobile_confirmation_frame(success: bool = True) -> bytes:
    """Build a BLE PIA frame containing PairMobileConfirmation."""
    request_id = math.ceil(random.random() * 1_000_000) + 1
    timestamp = round(time.time())

    confirm = _build_pair_mobile_confirmation_payload(success)
    user_topic = _encode_bytes_field(_USER_PAIR_MOBILE_CONFIRMATION_FIELD, confirm)
    request_msg = b"".join((
        _encode_varint_field(_REQUEST_ID_FIELD, request_id),
        _encode_string_field(_REQUEST_VERSION_FIELD, APP_PIA_VERSION),
        _encode_varint_field(_REQUEST_TIMESTAMP_FIELD, timestamp),
        _encode_bytes_field(_REQUEST_USER_FIELD, user_topic),
    ))
    ble_protocol = _encode_bytes_field(_BLE_PROTOCOL_REQUEST_FIELD, request_msg)
    return encode_ble_pia_frame(ble_protocol)


def build_user_request_frame(user_field_number: int, payload: bytes = b"") -> bytes:
    """Build a BLE PIA frame with an arbitrary UserRequestTopic field number.

    Used for brute-forcing unknown field numbers (deleteMobileDevices,
    getPairedMobileDevices, etc.) by sending requests with different field
    numbers and observing SCU responses.
    """
    request_id = math.ceil(random.random() * 1_000_000) + 1
    timestamp = round(time.time())

    user_topic = _encode_bytes_field(user_field_number, payload) if payload else b""
    if not payload:
        # Empty message for fields like getPairedMobileDevices (no args)
        user_topic = _encode_bytes_field(user_field_number, b"")
    request_msg = b"".join((
        _encode_varint_field(_REQUEST_ID_FIELD, request_id),
        _encode_string_field(_REQUEST_VERSION_FIELD, APP_PIA_VERSION),
        _encode_varint_field(_REQUEST_TIMESTAMP_FIELD, timestamp),
        _encode_bytes_field(_REQUEST_USER_FIELD, user_topic),
    ))
    ble_protocol = _encode_bytes_field(_BLE_PROTOCOL_REQUEST_FIELD, request_msg)
    return encode_ble_pia_frame(ble_protocol)


def build_delete_mobile_device_frame(
    user_field_number: int, mac_address: str
) -> bytes:
    """Build a BLE PIA frame for deleteMobileDevices with a MAC address.

    The deleteMobileDevices sub-message has a single field 'mobileDeviceMac'
    (string). We assume mobileDeviceMac is field 1 inside the sub-message.
    """
    # Inner payload: mobileDeviceMac = field 1, string
    inner = _encode_string_field(1, mac_address)
    return build_user_request_frame(user_field_number, inner)


def _rewrap_cloud_payload_as_ble_request(raw: bytes) -> tuple[bytes, int | None]:
    """Rewrap a cloud PIA payload for BLE transport.

    The cloud/DataHub encoders (``build_light_command`` etc.) wrap the PIA
    ``Request`` in top-level field 2 — correct for the cloud envelope, but on
    BLE field 2 is ``BleProtocol.response``. The SCU then parses the command as
    a response, finds no matching outstanding request and discards it silently.
    This unwraps the outer field-2 ``Request`` and rewraps it in field 1
    (``BleProtocol.request``), the same wrapper the pairing path already uses.

    Returns the field-1-wrapped frame payload plus the Request's request_id
    (for ACK correlation), or ``None`` id when it cannot be parsed.

    Reference: dan-simms1/hymer-connect-ha FINDINGS-ble-writes.md (2026-08-22).
    """
    request_msg: bytes | None = None
    offset = 0
    while offset < len(raw):
        key, offset = _decode_varint(raw, offset)
        fn, wt = key >> 3, key & 7
        if fn == _BLE_PROTOCOL_RESPONSE_FIELD and wt == _WIRE_LEN:
            request_msg, offset = _decode_len_delimited(raw, offset)
        else:
            offset = _skip_field(raw, offset, wt)
    if request_msg is None:
        raise BleTransportError("cloud payload has no field-2 Request to rewrap")

    request_id: int | None = None
    offset = 0
    while offset < len(request_msg):
        key, offset = _decode_varint(request_msg, offset)
        fn, wt = key >> 3, key & 7
        if fn == _REQUEST_ID_FIELD and wt == _WIRE_VARINT:
            request_id, offset = _decode_varint(request_msg, offset)
        else:
            offset = _skip_field(request_msg, offset, wt)

    return _encode_bytes_field(_BLE_PROTOCOL_REQUEST_FIELD, request_msg), request_id


def _extract_response_id_status(payload: bytes) -> tuple[int | None, int | None]:
    """Extract (request_id, status) from a BleProtocol.response frame payload.

    Returns (None, None) when the payload is not a response (e.g. a sensor
    push carried on the same notify pipe).
    """
    response_payload: bytes | None = None
    offset = 0
    while offset < len(payload):
        key, offset = _decode_varint(payload, offset)
        fn, wt = key >> 3, key & 7
        if fn == _BLE_PROTOCOL_RESPONSE_FIELD and wt == _WIRE_LEN:
            response_payload, offset = _decode_len_delimited(payload, offset)
        else:
            offset = _skip_field(payload, offset, wt)
    if response_payload is None:
        return None, None

    request_id = status = None
    offset = 0
    while offset < len(response_payload):
        key, offset = _decode_varint(response_payload, offset)
        fn, wt = key >> 3, key & 7
        if wt == _WIRE_VARINT:
            val, offset = _decode_varint(response_payload, offset)
            if fn == _RESPONSE_ID_FIELD:
                request_id = val
            elif fn == _RESPONSE_STATUS_FIELD:
                status = val
        else:
            offset = _skip_field(response_payload, offset, wt)
    return request_id, status


def decode_generic_response(frame: bytes) -> dict:
    """Decode a BLE PIA frame as a generic Response, returning all fields.

    Returns dict with keys: request_id, status, timestamp, and any
    LEN-delimited fields as {field_number: raw_bytes}.
    """
    payload = decode_ble_pia_frame(frame)

    # Unwrap BleProtocol → Response
    response_payload: bytes | None = None
    offset = 0
    while offset < len(payload):
        key, offset = _decode_varint(payload, offset)
        fn, wt = key >> 3, key & 7
        if fn == _BLE_PROTOCOL_RESPONSE_FIELD and wt == _WIRE_LEN:
            response_payload, offset = _decode_len_delimited(payload, offset)
        else:
            offset = _skip_field(payload, offset, wt)
    if response_payload is None:
        return {"error": "no Response in BleProtocol", "raw": payload.hex()}

    result: dict = {"fields": {}}
    offset = 0
    while offset < len(response_payload):
        key, offset = _decode_varint(response_payload, offset)
        fn, wt = key >> 3, key & 7
        if wt == _WIRE_VARINT:
            val, offset = _decode_varint(response_payload, offset)
            if fn == _RESPONSE_ID_FIELD:
                result["request_id"] = val
            elif fn == _RESPONSE_STATUS_FIELD:
                result["status"] = val
            elif fn == _RESPONSE_TIMESTAMP_FIELD:
                result["timestamp"] = val
            else:
                result["fields"][f"varint_{fn}"] = val
        elif wt == _WIRE_LEN:
            val, offset = _decode_len_delimited(response_payload, offset)
            result["fields"][f"len_{fn}"] = val.hex()
            # Try to decode as UTF-8 for readability
            try:
                result["fields"][f"str_{fn}"] = val.decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                pass
        else:
            offset = _skip_field(response_payload, offset, wt)

    return result


def _decode_jwt_payload(token: str) -> dict:
    """Decode the payload of a JWT without signature verification.

    Returns the decoded payload dict, or empty dict on any error.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload_b64 = parts[1]
        # Add padding for base64url
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        decoded = base64.urlsafe_b64decode(payload_b64)
        return json.loads(decoded)
    except Exception:
        return {}


def _validate_refresh_token(token: str) -> bool:
    """Validate that a token looks like a valid EHG refresh JWT.

    Checks:
      1. Non-empty string
      2. Starts with 'eyJ' (base64url-encoded JSON header)
      3. Has 3 dot-separated parts (header.payload.signature)
      4. Payload contains ett=access-refresh
    """
    if not token or len(token) < 50:
        _LOGGER.warning("Token validation failed: too short (%d chars)", len(token) if token else 0)
        return False
    if not token.startswith("eyJ"):
        _LOGGER.warning("Token validation failed: does not start with 'eyJ' (got '%s...')", token[:10])
        return False
    parts = token.split(".")
    if len(parts) != 3:
        _LOGGER.warning("Token validation failed: expected 3 JWT parts, got %d", len(parts))
        return False
    payload = _decode_jwt_payload(token)
    ett = payload.get("ett", "")
    if ett != "access-refresh":
        _LOGGER.warning(
            "Token validation warning: ett='%s' (expected 'access-refresh'). "
            "Token may still work — storing it.",
            ett,
        )
        # Don't reject — the token might still be valid with a different ett
        # value in newer SCU firmware. Log the warning for diagnostics.
    return True


def decode_pair_mobile_response(frame: bytes) -> PairMobileResponse:
    """Decode a BLE PIA frame containing the SCU's PairMobileResponse.

    Protobuf nesting:
      BleProtocol.response(2) → Response → mobilePair(9) → tokens
    """
    payload = decode_ble_pia_frame(frame)

    # Unwrap BleProtocol → Response
    response_payload: bytes | None = None
    offset = 0
    while offset < len(payload):
        key, offset = _decode_varint(payload, offset)
        fn, wt = key >> 3, key & 7
        if fn == _BLE_PROTOCOL_RESPONSE_FIELD and wt == _WIRE_LEN:
            response_payload, offset = _decode_len_delimited(payload, offset)
        else:
            offset = _skip_field(payload, offset, wt)
    if response_payload is None:
        raise BleTransportError("BleProtocol does not contain a Response")

    # Unwrap Response → mobilePair
    request_id = status = timestamp = None
    mobile_pair_payload: bytes | None = None
    offset = 0
    while offset < len(response_payload):
        key, offset = _decode_varint(response_payload, offset)
        fn, wt = key >> 3, key & 7
        if wt == _WIRE_VARINT:
            val, offset = _decode_varint(response_payload, offset)
            if fn == _RESPONSE_ID_FIELD:
                request_id = val
            elif fn == _RESPONSE_STATUS_FIELD:
                status = val
            elif fn == _RESPONSE_TIMESTAMP_FIELD:
                timestamp = val
        elif fn == _RESPONSE_MOBILE_PAIR_FIELD and wt == _WIRE_LEN:
            mobile_pair_payload, offset = _decode_len_delimited(response_payload, offset)
        else:
            offset = _skip_field(response_payload, offset, wt)
    if mobile_pair_payload is None:
        raise BleTransportError("Response does not contain mobilePair field")

    # Unwrap mobilePair → tokens
    access_token = ""
    refresh_token = ""
    confirmation_required = False
    offset = 0
    while offset < len(mobile_pair_payload):
        key, offset = _decode_varint(mobile_pair_payload, offset)
        fn, wt = key >> 3, key & 7
        if wt == _WIRE_LEN:
            val, offset = _decode_len_delimited(mobile_pair_payload, offset)
            text = val.decode("utf-8")
            if fn == _PAIR_RESP_ACCESS_TOKEN_FIELD:
                access_token = text
            elif fn == _PAIR_RESP_REFRESH_TOKEN_FIELD:
                refresh_token = text
        elif wt == _WIRE_VARINT:
            val, offset = _decode_varint(mobile_pair_payload, offset)
            if fn == _PAIR_RESP_CONFIRMATION_REQUIRED_FIELD:
                confirmation_required = bool(val)
        else:
            offset = _skip_field(mobile_pair_payload, offset, wt)

    # Validate the refresh token before returning
    if refresh_token:
        if _validate_refresh_token(refresh_token):
            payload = _decode_jwt_payload(refresh_token)
            _LOGGER.info(
                "BLE pairing: refresh token validated — ett='%s', urn='%s', len=%d",
                payload.get("ett", "?"),
                payload.get("urn", "?"),
                len(refresh_token),
            )
        else:
            _LOGGER.warning(
                "BLE pairing: refresh token failed validation (len=%d, starts='%s...'). "
                "Storing anyway — downstream exchange will confirm if usable.",
                len(refresh_token),
                refresh_token[:20] if len(refresh_token) > 20 else refresh_token,
            )
    else:
        _LOGGER.warning("BLE pairing: mobilePair response contained no refresh token")

    if access_token:
        if _validate_refresh_token(access_token):
            payload = _decode_jwt_payload(access_token)
            _LOGGER.debug(
                "BLE pairing: access token — ett='%s', len=%d",
                payload.get("ett", "?"),
                len(access_token),
            )

    return PairMobileResponse(
        remote_access_token=access_token,
        remote_access_refresh_token=refresh_token,
        confirmation_required=confirmation_required,
        request_id=request_id,
        status=status,
        timestamp=timestamp,
    )


class BleTransportError(RuntimeError):
    """Raised when the BLE transport encounters an error."""


class TlsTransportError(RuntimeError):
    """Raised when the TLS layer over BLE fails."""


# ---------------------------------------------------------------------------
# TLS over MemoryBIO — drives TLS without a real socket
# ---------------------------------------------------------------------------

class _TlsOverBle:
    """TLS client using ssl.MemoryBIO for transport over BLE GATT.

    The SCU firmware only speaks TLS 1.0/1.1 with legacy ciphers.
    Modern Python/OpenSSL (3.12+, OpenSSL 3.x) disables these by default.
    We must explicitly lower the security level to allow them.
    """

    def __init__(self) -> None:
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._context.check_hostname = False
        self._context.verify_mode = ssl.CERT_NONE

        # Lower OpenSSL security level to 0 to allow TLS 1.0/1.1 + legacy ciphers.
        # This is required because the SCU firmware does not support TLS 1.2+.
        # Security level 0 permits all ciphers and protocols.
        # This MUST be set BEFORE setting min/max version and ciphers.
        try:
            self._context.set_ciphers("@SECLEVEL=0:" + APP_TLS_CIPHERS)
        except ssl.SSLError:
            # Fallback: try without security level prefix (older OpenSSL)
            self._context.set_ciphers(APP_TLS_CIPHERS)

        # Clear OP_NO_TLSv1 and OP_NO_TLSv1_1 flags that OpenSSL 3.x sets by default
        self._context.options &= ~ssl.OP_NO_TLSv1
        self._context.options &= ~ssl.OP_NO_TLSv1_1

        self._context.minimum_version = APP_TLS_MIN_VERSION
        self._context.maximum_version = APP_TLS_MAX_VERSION

        _LOGGER.debug(
            "BLE TLS context: min=%s max=%s ciphers=%s",
            APP_TLS_MIN_VERSION, APP_TLS_MAX_VERSION, APP_TLS_CIPHERS,
        )

        self._incoming = ssl.MemoryBIO()
        self._outgoing = ssl.MemoryBIO()
        self._sslobj = self._context.wrap_bio(
            self._incoming, self._outgoing, server_hostname=None
        )
        self._handshake_done = False

    @property
    def handshake_done(self) -> bool:
        return self._handshake_done

    def begin_handshake(self) -> bytes:
        """Start the TLS handshake, return outbound records."""
        return self._pump()

    def feed_encrypted(self, data: bytes) -> tuple[bytes, list[bytes]]:
        """Feed encrypted data from SCU, return (outbound_records, plaintext_chunks)."""
        self._incoming.write(data)
        outbound = self._pump()
        plaintext = self._read_plaintext()
        return outbound, plaintext

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt plaintext for sending to SCU."""
        if not self._handshake_done:
            raise TlsTransportError("TLS handshake not complete")
        self._sslobj.write(plaintext)
        return self._drain_outgoing()

    def _pump(self) -> bytes:
        """Advance TLS state machine, return outbound records."""
        if not self._handshake_done:
            try:
                self._sslobj.do_handshake()
                self._handshake_done = True
                _LOGGER.info("BLE TLS handshake complete: %s %s",
                             self._sslobj.version(), self._sslobj.cipher())
            except ssl.SSLWantReadError:
                pass
            except ssl.SSLError as err:
                raise TlsTransportError(f"TLS handshake failed: {err}") from err
        return self._drain_outgoing()

    def _read_plaintext(self) -> list[bytes]:
        chunks: list[bytes] = []
        if not self._handshake_done:
            return chunks
        while True:
            try:
                data = self._sslobj.read(16384)
                if not data:
                    break
                chunks.append(data)
            except ssl.SSLWantReadError:
                break
            except ssl.SSLZeroReturnError:
                break
        return chunks

    def _drain_outgoing(self) -> bytes:
        parts: list[bytes] = []
        while True:
            chunk = self._outgoing.read()
            if not chunk:
                return b"".join(parts)
            parts.append(chunk)


# ---------------------------------------------------------------------------
# BLE PIA Frame encoding/decoding
# ---------------------------------------------------------------------------

def encode_ble_pia_frame(payload: bytes) -> bytes:
    """Encode a PIA protobuf payload into the BLE PIA frame format."""
    provisional_header = BLE_PIA_MAGIC + len(payload).to_bytes(4, "big") + b"\x00\x00\x00\x00"
    crc = zlib.crc32(provisional_header + payload) & 0xFFFFFFFF
    header = BLE_PIA_MAGIC + len(payload).to_bytes(4, "big") + crc.to_bytes(4, "big")
    return header + payload


def decode_ble_pia_frame(frame: bytes) -> bytes:
    """Decode a BLE PIA frame, verify CRC, return payload."""
    if len(frame) < BLE_PIA_HEADER_SIZE:
        raise BleTransportError("Frame too short")
    if not frame.startswith(BLE_PIA_MAGIC):
        raise BleTransportError("Invalid BLE PIA magic")
    payload_len = int.from_bytes(frame[2:6], "big")
    stored_crc = int.from_bytes(frame[6:10], "big")
    payload = frame[BLE_PIA_HEADER_SIZE:]
    if len(payload) != payload_len:
        raise BleTransportError(f"Payload length mismatch: expected {payload_len}, got {len(payload)}")
    check_header = BLE_PIA_MAGIC + payload_len.to_bytes(4, "big") + b"\x00\x00\x00\x00"
    expected_crc = zlib.crc32(check_header + payload) & 0xFFFFFFFF
    if expected_crc != stored_crc:
        _LOGGER.warning("BLE PIA CRC mismatch: expected 0x%08x, got 0x%08x", expected_crc, stored_crc)
    return payload


# ---------------------------------------------------------------------------
# BLE PIA frame accumulator
# ---------------------------------------------------------------------------

class _FrameAccumulator:
    """Accumulate plaintext bytes and extract complete BLE PIA frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        frames: list[bytes] = []
        while True:
            idx = self._buf.find(BLE_PIA_MAGIC)
            if idx < 0:
                # No complete magic found. A notification boundary can split
                # the 2-byte magic, so keep the last (len(MAGIC) - 1) bytes as
                # a potential partial magic prefix for the next feed() call
                # instead of dropping them.
                keep = len(BLE_PIA_MAGIC) - 1
                if keep and len(self._buf) > keep:
                    del self._buf[:-keep]
                return frames
            if idx > 0:
                del self._buf[:idx]
            if len(self._buf) < BLE_PIA_HEADER_SIZE:
                return frames
            payload_len = int.from_bytes(self._buf[2:6], "big")
            frame_len = BLE_PIA_HEADER_SIZE + payload_len
            if len(self._buf) < frame_len:
                return frames
            frames.append(bytes(self._buf[:frame_len]))
            del self._buf[:frame_len]

    def clear(self) -> None:
        self._buf.clear()


# ---------------------------------------------------------------------------
# SCU BLE Client — main transport class
# ---------------------------------------------------------------------------

class ScuBleClient:
    """BLE GATT client for direct SCU communication.

    Connects to the SCU via Nordic UART Service, establishes a TLS session,
    and exchanges PIA protobuf messages — the same protocol the EHG app uses
    when physically near the vehicle.

    Usage:
        client = ScuBleClient(scu_address="XX:XX:XX:XX:XX:XX")
        await client.connect()
        await client.establish_tls()
        # Now send/receive PIA messages
        await client.send_pia_command(pia_request_b64)
        await client.disconnect()
    """

    def __init__(
        self,
        scu_address: str,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        tls_timeout: float = DEFAULT_TLS_TIMEOUT,
        on_pia_response: Callable[[str], None] | None = None,
        hass: Any | None = None,
    ) -> None:
        if not HAS_BLEAK:
            raise BleTransportError(
                "bleak is not installed. Install with: pip install bleak"
            )
        self._scu_address = scu_address
        self._connect_timeout = connect_timeout
        self._tls_timeout = tls_timeout
        self._on_pia_response = on_pia_response
        self._hass = hass

        self._client: BleakClient | None = None
        self._tls: _TlsOverBle | None = None
        self._frame_acc = _FrameAccumulator()
        self._uart_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = False
        self._tls_established = False
        self._write_response = True
        self._write_chunk_size = 20
        # request_id -> Future[int] for BLE write-command ACK correlation.
        self._pending_writes: dict[int, asyncio.Future] = {}

    @property
    def connected(self) -> bool:
        return self._connected and self._tls_established

    @property
    def ble_connected(self) -> bool:
        """True when BLE GATT is connected (before TLS handshake)."""
        return self._connected

    @property
    def scu_address(self) -> str:
        return self._scu_address

    @staticmethod
    def _is_scu_candidate(name: str, uuids: list[str]) -> bool:
        """Return True when an advertised device looks like an SCU.

        Matches on the Nordic UART Service UUID (primary signal) or on a
        recognisable name substring. Kept intentionally broad so unusual
        firmware advertising variants are not filtered out.
        """
        lname = (name or "").lower()
        if UART_SERVICE_UUID.lower() in uuids:
            return True
        return any(tag in lname for tag in SCU_NAME_TOKENS)

    async def scan_for_scu(
        self,
        timeout: float = DEFAULT_SCAN_TIMEOUT,
        hass: Any | None = None,
        *,
        active_fallback: bool = True,
        retries: int = 2,
    ) -> list[dict]:
        """Scan for nearby SCU devices advertising the NUS service.

        When *hass* is provided (running inside Home Assistant), first uses
        HA's managed Bluetooth scanner (habluetooth) which reads the passive
        advertisement cache without conflicting with the BlueZ adapter. If
        that cache yields no candidates and *active_fallback* is True, an
        active ``BleakScanner.discover()`` is run as a second stage — this is
        important because the passive cache can be empty even when the SCU is
        pairable (it only advertises intermittently, especially in standby).

        For standalone / tool usage (no *hass*), an active scan is used
        directly. Up to *retries* extra active scans are performed while no
        candidate is found, since the SCU advertisement is intermittent.
        """
        # ── HA-native passive cache path ────────────────────────────────
        if hass is not None:
            try:
                from homeassistant.components.bluetooth import async_discovered_service_info

                _LOGGER.debug("BLE scan via HA Bluetooth integration (passive cache)")
                seen = 0
                results: list[dict] = []
                for info in async_discovered_service_info(hass):
                    seen += 1
                    name = info.name or ""
                    uuids = [str(u).lower() for u in (info.service_uuids or [])]
                    if self._is_scu_candidate(name, uuids):
                        results.append({
                            "address": info.address,
                            "name": name,
                            "rssi": info.rssi,
                            "service_uuids": uuids,
                        })
                results.sort(key=lambda x: x.get("rssi") or -999, reverse=True)
                _LOGGER.debug(
                    "HA BLE passive cache: %d devices seen, %d SCU candidates: %s",
                    seen,
                    len(results),
                    [(r["address"], r["name"], r["rssi"]) for r in results],
                )
                if results:
                    return results
                if not active_fallback:
                    return results
                _LOGGER.debug(
                    "HA passive cache had no SCU candidate — "
                    "falling back to an active BLE scan"
                )
            except Exception:  # noqa: BLE001
                _LOGGER.debug("HA Bluetooth API unavailable, falling back to BleakScanner")

        # ── Active scan (standalone tools OR HA passive-cache miss) ──────
        return await self._active_scan_for_scu(timeout=timeout, retries=retries)

    async def _active_scan_for_scu(
        self, *, timeout: float, retries: int
    ) -> list[dict]:
        """Run one or more active ``BleakScanner.discover()`` passes."""
        if not HAS_BLEAK:
            raise BleTransportError("bleak is not installed")

        attempts = max(1, retries + 1)
        for attempt in range(1, attempts + 1):
            _LOGGER.debug(
                "Active BLE scan %d/%d starting (timeout=%.1fs)",
                attempt, attempts, timeout,
            )
            try:
                discovered = await BleakScanner.discover(
                    timeout=timeout, return_adv=True
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Active BLE scan %d failed: %s", attempt, err)
                continue

            results: list[dict] = []
            for _, (device, adv) in discovered.items():
                name = device.name or adv.local_name or ""
                uuids = [str(u).lower() for u in (adv.service_uuids or [])]
                if self._is_scu_candidate(name, uuids):
                    results.append({
                        "address": device.address,
                        "name": name,
                        "rssi": adv.rssi,
                        "service_uuids": uuids,
                    })
            results.sort(key=lambda x: x.get("rssi") or -999, reverse=True)
            _LOGGER.debug(
                "Active BLE scan %d/%d: %d devices seen, %d SCU candidates: %s",
                attempt, attempts, len(discovered), len(results),
                [(r["address"], r["name"], r["rssi"]) for r in results],
            )
            if results:
                return results

        return []

    async def _create_connected_client(self) -> BleakClient:
        """Create and connect a BleakClient, preferring HA's retry connector.

        When running inside Home Assistant (self._hass is set), uses
        bleak-retry-connector's establish_connection() for reliable
        connection establishment with automatic retries and backoff.
        Falls back to raw BleakClient for standalone/tool usage.
        """
        if self._hass is not None:
            try:
                from homeassistant.components.bluetooth import (
                    async_ble_device_from_address,
                )
                from bleak_retry_connector import establish_connection

                ble_device = async_ble_device_from_address(
                    self._hass, self._scu_address, connectable=True
                )
                if ble_device:
                    _LOGGER.debug(
                        "Using HA BLE connector for %s", self._scu_address
                    )
                    return await establish_connection(
                        BleakClient,
                        ble_device,
                        f"HYMER SCU {self._scu_address}",
                        max_attempts=2,
                    )
                _LOGGER.debug(
                    "BLE device %s not in HA registry — "
                    "falling back to raw BleakClient",
                    self._scu_address,
                )
            except ImportError:
                _LOGGER.debug(
                    "bleak-retry-connector not available — "
                    "using raw BleakClient"
                )
            except Exception as err:
                _LOGGER.debug(
                    "HA BLE connector failed, falling back to "
                    "raw BleakClient: %s",
                    err,
                )
        # Standalone / fallback: raw BleakClient
        client = BleakClient(self._scu_address, timeout=self._connect_timeout)
        try:
            await client.connect()
        except (Exception, asyncio.CancelledError):
            try:
                await asyncio.wait_for(
                    client.disconnect(), timeout=DEFAULT_DISCONNECT_TIMEOUT
                )
            except Exception:
                pass
            raise
        return client

    async def connect(self) -> None:
        """Connect to the SCU via BLE and set up NUS notifications."""
        if self._client is not None:
            return

        self._loop = asyncio.get_running_loop()
        await self._connect_inner()

    async def _connect_inner(self, retry: bool = True, gatt_attempt: int = 1) -> None:
        """Inner connect logic, handles stale BlueZ notify recovery."""
        # Clear stale BlueZ bonding records BEFORE connecting — but only
        # if the device is NOT already successfully bonded. Calling unpair()
        # on a bonded device removes the keys and breaks active connections.
        _LOGGER.debug("Checking BlueZ bond status for %s", self._scu_address)
        is_already_bonded = False
        try:
            from dbus_fast.aio import MessageBus
            from dbus_fast import BusType, Message, MessageType
            tmp_bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            try:
                dev_path = f"/org/bluez/hci0/dev_{self._scu_address.replace(':', '_')}"
                try:
                    introspection = await tmp_bus.introspect("org.bluez", dev_path)
                    dev_obj = tmp_bus.get_proxy_object("org.bluez", dev_path, introspection)
                    props_iface = dev_obj.get_interface("org.freedesktop.DBus.Properties")
                    paired = await props_iface.call_get("org.bluez.Device1", "Paired")
                    is_already_bonded = bool(paired.value) if paired else False
                except Exception:
                    pass  # Device not known to BlueZ — that's fine
            finally:
                tmp_bus.disconnect()
        except Exception:
            pass

        if is_already_bonded:
            _LOGGER.debug("Device %s is already bonded — skipping unpair", self._scu_address)
        else:
            # Device is not bonded — nothing to clear. Do NOT call
            # BleakClient.unpair() here: it invokes Adapter1.RemoveDevice()
            # which removes the device from BlueZ's object tree entirely,
            # making it unfindable for subsequent connection attempts until
            # a full BLE re-scan rediscovers it.
            _LOGGER.debug("Device %s is not bonded — no stale records to clear", self._scu_address)

        _LOGGER.debug("BLE connecting to %s (timeout=%.1fs)", self._scu_address, self._connect_timeout)
        client = None
        try:
            client = await self._create_connected_client()
        except Exception as connect_err:
            err_low = str(connect_err).lower()
            # A transient GATT failure (weak signal, or the SCU still booting
            # after a restart) shows up as "device disconnected" / "failed to
            # discover services" — NOT as an authentication/encryption error.
            # These must NOT destroy the bond: a fresh bond needs physical
            # access to the vehicle plus a CONNECTION button press. Only a real
            # auth/encryption error means the bond keys are actually stale.
            is_transient_gatt = (
                "disconnect" in err_low
                or "discover services" in err_low
                or "not connected" in err_low
                or "not found" in err_low
            )
            is_bond_key_error = (
                "authentication" in err_low
                or "encrypt" in err_low
                or "insufficient" in err_low
            )
            if is_already_bonded and is_bond_key_error:
                # Genuine bad bond → clear it via D-Bus RemoveDevice so a fresh
                # pairing can be established.
                _LOGGER.warning(
                    "Bonded device rejected with an authentication/encryption "
                    "error — clearing stale bond via D-Bus RemoveDevice: %s",
                    connect_err,
                )
                await async_clear_bluez_bond(self._scu_address)
                _LOGGER.info(
                    "Stale bond cleared for %s — raising to retry loop "
                    "(habluetooth will re-discover the device)",
                    self._scu_address,
                )
                raise BleTransportError(
                    f"Stale bond cleared for {self._scu_address} — retry needed"
                ) from connect_err
            if is_already_bonded and is_transient_gatt:
                # Transient — retry a few times with growing delay, KEEPING the
                # bond. After that, hand back to the coordinator so it backs off
                # and retries BLE later; the bond survives untouched.
                if gatt_attempt < 3:
                    delay = 2.0 * gatt_attempt
                    _LOGGER.warning(
                        "Bonded device not reachable (attempt %d/3 — likely weak "
                        "signal or the SCU is still booting). Retrying in %.0fs, "
                        "keeping the bond intact: %s",
                        gatt_attempt, delay, connect_err,
                    )
                    if client:
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
                    await asyncio.sleep(delay)
                    await self._connect_inner(retry=retry, gatt_attempt=gatt_attempt + 1)
                    return
                _LOGGER.warning(
                    "Bonded device still unreachable after 3 attempts — keeping "
                    "the bond and falling back to cloud; BLE will be retried "
                    "later: %s",
                    connect_err,
                )
                raise BleTransportError(
                    f"SCU {self._scu_address} temporarily unreachable "
                    "(bond preserved) — will retry"
                ) from connect_err
            raise
        _LOGGER.debug("BLE GATT connected to %s", self._scu_address)

        # Set when BlueZ reports a leaked write/notify acquisition from a prior
        # session (NotPermitted "Write/Notify acquired"). Such a session can
        # only be healed by a full D-Bus Device1.Disconnect + reconnect.
        stale_acquire_seen = False

        try:
            # Get services — handle both plain BleakClient (has get_services())
            # and HA's HaBleakClientWrapper (uses .services property)
            if hasattr(client, "get_services"):
                services = await client.get_services()
            else:
                services = client.services
                if services is None:
                    # Some wrappers need a connect first, then services populate
                    await asyncio.sleep(0.5)
                    services = client.services
            if services is None:
                raise BleTransportError(
                    f"SCU {self._scu_address}: could not discover GATT services"
                )
            svc_list = list(services)
            _LOGGER.debug(
                "BLE GATT services discovered: %d services — %s",
                len(svc_list),
                ", ".join(str(s.uuid) for s in svc_list),
            )
            rx_char = None
            tx_char = None
            for service in svc_list:
                for char in service.characteristics:
                    uuid = str(char.uuid).lower()
                    if uuid == UART_RX_UUID:
                        rx_char = char
                    elif uuid == UART_TX_UUID:
                        tx_char = char

            if rx_char is None or tx_char is None:
                raise BleTransportError(
                    f"SCU {self._scu_address} does not expose Nordic UART Service"
                )

            # Determine write mode (write-with-response preferred)
            props = {p.lower() for p in rx_char.properties}
            self._write_response = "write" in props

            # Request MTU 245 to match EHG app (requestMtu(245)).
            # Default MTU=23 gives 20-byte chunks = 63 chunks for PairMobileRequest.
            # MTU=245 gives 242-byte chunks = only 6 chunks — eliminates ATT 0x0e.
            mtu = DEFAULT_GATT_MTU
            try:
                # HA wraps the real bleak backend in HaBleakClientWrapper.
                # _acquire_mtu() may live on the wrapper OR only on the
                # wrapped backend depending on habluetooth/bleak version —
                # try the wrapper first, then fall back to the backend.
                acquire = getattr(client, "_acquire_mtu", None)
                if acquire is None:
                    backend = getattr(client, "_backend", None)
                    acquire = getattr(backend, "_acquire_mtu", None)
                if acquire is not None:
                    await acquire()
                    _LOGGER.debug("BLE MTU acquired via _acquire_mtu()")
                else:
                    _LOGGER.debug(
                        "BLE client has no _acquire_mtu() — "
                        "trying D-Bus MTU negotiation"
                    )
                    mtu = await self._negotiate_mtu_dbus() or DEFAULT_GATT_MTU
            except Exception as mtu_err:
                # A leaked write channel from a prior aborted session surfaces
                # here as NotPermitted "Write acquired". Flag it so the outer
                # handler forces a full D-Bus Device1.Disconnect + reconnect
                # instead of silently limping on at MTU 23 (which then fails
                # later with UNLIKELY_ERROR on start_notify).
                mtu_err_str = str(mtu_err)
                if (
                    "acquired" in mtu_err_str.lower()
                    or "NotPermitted" in mtu_err_str
                ):
                    stale_acquire_seen = True
                    _LOGGER.warning(
                        "BLE MTU acquisition hit a stale BlueZ write/notify "
                        "channel (%s) — will force a fresh GATT session",
                        mtu_err,
                    )
                else:
                    _LOGGER.debug(
                        "BLE MTU acquisition failed (non-critical): %s", mtu_err
                    )

            mtu = max(mtu, getattr(client, "mtu_size", DEFAULT_GATT_MTU))
            self._write_chunk_size = max(20, min(242, mtu - 3))
            if mtu <= DEFAULT_GATT_MTU:
                # Not an error: MTU 23 is a fully-supported fallback. Writes
                # switch to 20-byte Write-With-Response chunks, which are
                # reliable (just slightly slower). Logged at INFO so it does
                # not surface in HA's custom-integration error panel.
                _LOGGER.info(
                    "BLE MTU stayed at the %d-byte default; writes will use "
                    "20-byte Write-With-Response chunks (reliable, slightly "
                    "slower). This is normal on many Bluetooth adapters and "
                    "proxies and does not affect functionality.",
                    mtu,
                )

            # Check SCU bonding state before attempting pair.
            # The fff40004 characteristic tells us if CONNECTION was pressed.
            try:
                challenge = bytes(random.getrandbits(8) for _ in range(4))
                await client.write_gatt_char(BONDING_STATE_UUID, challenge)
                response = await client.read_gatt_char(BONDING_STATE_UUID)
                if response and len(response) >= 5 and response[:4] == challenge:
                    bond_state = response[4]
                    _LOGGER.info(
                        "SCU bonding state: %d (%s)",
                        bond_state,
                        "pairing mode ACTIVE" if bond_state else "not in pairing mode",
                    )
                else:
                    _LOGGER.debug("SCU bonding state check: no valid response")
            except Exception as bs_err:
                _LOGGER.debug("SCU bonding state check failed: %s", bs_err)

            # OS-level bonding (Pair()) is needed for the SCU to accept
            # TLS/notify.  But a FAILED Pair() corrupts the GATT session,
            # so we only attempt it when the device is NOT already bonded.
            # If already bonded at BlueZ level, the keys are valid and
            # we can proceed directly to notify + TLS.
            if is_already_bonded:
                _LOGGER.debug(
                    "Device %s is already bonded at BlueZ level — "
                    "skipping Pair()",
                    self._scu_address,
                )
                bonded = True
            else:
                # bleak's client.pair() calls Device1.Pair() on D-Bus but
                # does NOT register a pairing agent. BlueZ waits ~8s for an
                # agent response that never comes → AuthenticationCanceled.
                # We use bluetoothctl instead, which has a built-in
                # NoInputNoOutput agent for JustWorks pairing.
                bonded = False
                try:
                    _LOGGER.debug("Attempting BLE bonding via bluetoothctl agent")
                    bonded = await self._pair_via_bluetoothctl()
                    if bonded:
                        _LOGGER.info("BLE bonding successful with SCU %s", self._scu_address)
                    else:
                        _LOGGER.info("BLE bonding via bluetoothctl did not succeed")
                except Exception as bond_err:
                    _LOGGER.warning("BLE bonding failed: %s", bond_err)

                # A failed Pair() corrupts the GATT session — start_notify
                # will fail with ATT 0x0e.  Raise immediately.
                if not bonded:
                    raise BleTransportError(
                        "BLE bonding rejected by SCU — "
                        "press CONNECTION (Verbindung) on the SCU touch panel, "
                        "then retry within 2 minutes"
                    )

            # bluetoothctl pairing changes the encryption layer.
            # The pre-bonding GATT session's service handles are invalidated
            # even if the BleakClient still reports is_connected=True.
            # Always reconnect to get a fresh GATT session with valid services.
            if bonded and not is_already_bonded:
                _LOGGER.debug("Reconnecting after fresh bonding to refresh GATT services")
                try:
                    await client.disconnect()
                except Exception:
                    pass
                # The SCU's GATT server needs time to re-expose services after
                # the encryption change. Too short a settle leaves start_notify
                # hanging for ~11s and then failing with UNLIKELY_ERROR.
                await asyncio.sleep(1.5)  # let BlueZ + SCU settle
                client = await self._create_connected_client()
                _LOGGER.debug("BLE GATT reconnected after bonding")

            # Start receiving NUS TX notifications. Bound this with a timeout:
            # a stale BlueZ notify acquisition makes start_notify hang for ~11s
            # before UNLIKELY_ERROR — fail fast so the outer handler can force a
            # Device1.Disconnect + fresh-session reconnect instead.
            try:
                await asyncio.wait_for(
                    client.start_notify(UART_TX_UUID, self._on_uart_notify),
                    timeout=DEFAULT_NOTIFY_TIMEOUT,
                )
            except (asyncio.TimeoutError, TimeoutError) as notify_timeout:
                stale_acquire_seen = True
                raise BleTransportError(
                    "start_notify timed out — stale BlueZ notify channel"
                ) from notify_timeout
        except (Exception, asyncio.CancelledError) as err:
            # Guarantee the raw BleakClient is disconnected on any setup failure
            # so BlueZ releases the GATT session and notify acquisition.
            _LOGGER.debug("BLE setup failed, disconnecting raw client: %s", err)
            try:
                await asyncio.wait_for(
                    client.stop_notify(UART_TX_UUID),
                    timeout=DEFAULT_DISCONNECT_TIMEOUT,
                )
            except Exception:
                pass
            try:
                await asyncio.wait_for(
                    client.disconnect(), timeout=DEFAULT_DISCONNECT_TIMEOUT
                )
            except Exception:
                pass

            if isinstance(err, asyncio.CancelledError):
                raise

            # "Notify acquired" / "Write acquired" / "NotPermitted" means BlueZ
            # kept a stale write/notify acquisition from a prior session that
            # didn't clean up. It also surfaces one step later as an
            # UNLIKELY_ERROR on start_notify (the MTU acquire already failed and
            # was flagged in stale_acquire_seen). A plain BleakClient.disconnect
            # does NOT release the leaked file descriptor — only a D-Bus
            # Device1.Disconnect (or restarting bluetooth) does. So force one,
            # then reconnect with a fresh GATT session.
            err_str = str(err)
            is_stale_acquire = (
                stale_acquire_seen
                or "Notify acquired" in err_str
                or "Write acquired" in err_str
                or "NotPermitted" in err_str
                or "UNLIKELY_ERROR" in err_str
            )
            if retry and is_stale_acquire:
                _LOGGER.warning(
                    "Stale BlueZ write/notify acquisition — forcing D-Bus "
                    "Device1.Disconnect and reconnecting with a fresh GATT "
                    "session: %s", err,
                )
                await async_dbus_disconnect(self._scu_address)
                await asyncio.sleep(1.5)  # let BlueZ release the FDs and settle
                await self._connect_inner(retry=False)
                return

            raise

        self._client = client
        self._connected = True
        _LOGGER.info("BLE connected to SCU %s (MTU=%d, chunk=%d)",
                      self._scu_address, mtu, self._write_chunk_size)

    async def establish_tls(self) -> None:
        """Perform TLS handshake over the BLE GATT NUS channel."""
        if not self._connected:
            raise BleTransportError("Not connected to SCU")

        self._tls = _TlsOverBle()
        self._frame_acc.clear()

        # Send ClientHello
        outbound = self._tls.begin_handshake()
        # Force Write-With-Response for ALL TLS handshake writes.
        # At MTU=23 (chunk=20), the KeyExchange is ~340 bytes = 18 chunks.
        # Write-Without-Response at this rate overflows the SCU's NUS RX
        # buffer — chunks are silently dropped, the TLS record is corrupted,
        # and the SCU never responds (30s timeout). Write-With-Response adds
        # ~30ms per chunk but guarantees delivery.
        await self._write_to_scu(outbound, force_response=True)

        # Complete handshake
        deadline = self._loop.time() + self._tls_timeout
        while not self._tls.handshake_done:
            incoming = await self._next_uart_data(deadline)
            outbound, _plaintext = self._tls.feed_encrypted(incoming)
            await self._write_to_scu(outbound, force_response=True)

        self._tls_established = True
        _LOGGER.info("BLE TLS session established with SCU %s", self._scu_address)

    async def send_pia_command(self, b64_payload: str) -> None:
        """Send a PIA subscription/refresh request over BLE TLS.

        The pia_decoder payload is wrapped in field 2 (the cloud DataHub
        envelope); over BLE field 2 is BleProtocol.response, so it must be
        rewrapped as BleProtocol.request (field 1) or the SCU parses it as a
        response and ignores it — the same fix as the write path. Sent
        write-with-response so a multi-chunk subscription burst is not
        truncated at low MTU. (Root cause credit: Dan Simms.)
        """
        import base64
        if not self._tls_established:
            raise BleTransportError("TLS not established")
        raw = base64.b64decode(b64_payload)
        ble_payload, _request_id = _rewrap_cloud_payload_as_ble_request(raw)
        pia_frame = encode_ble_pia_frame(ble_payload)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "BLE PIA SEND %s: plaintext=%d B framed=%d B hex=%s",
                self._scu_address, len(raw), len(pia_frame), raw.hex(),
            )
        encrypted = self._tls.encrypt(pia_frame)
        await self._write_to_scu(encrypted, force_response=True)

    def _resolve_pending_write(self, payload: bytes) -> None:
        """Resolve a waiting write-ACK future if this payload is its response."""
        request_id, status = _extract_response_id_status(payload)
        if request_id is None:
            return
        future = self._pending_writes.get(request_id)
        if future is not None and not future.done():
            future.set_result(status if status is not None else PIA_STATUS_SUCCESS)

    async def send_setvalue_with_ack(
        self, b64_payload: str, *, timeout: float
    ) -> int | None:
        """Send a write command over BLE and wait for its matching ACK.

        Rewraps the cloud PIA payload as a ``BleProtocol.request`` (field 1) and
        writes it with response (both required for the SCU to parse a BLE write;
        see dan-simms1/hymer-connect-ha BLE_RUNBOOK.md). Returns the PIA
        ``status`` from the matching ``BleProtocol.response`` (1 = SUCCESS), or
        ``None`` on timeout / no request_id to correlate.
        """
        import base64
        if not self._tls_established:
            raise BleTransportError("TLS not established")
        raw = base64.b64decode(b64_payload)
        ble_payload, request_id = _rewrap_cloud_payload_as_ble_request(raw)
        pia_frame = encode_ble_pia_frame(ble_payload)
        if request_id is None:
            # No id to correlate — send fire-and-forget, report no ACK.
            encrypted = self._tls.encrypt(pia_frame)
            await self._write_to_scu(encrypted, force_response=True)
            return None

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_writes[request_id] = future
        try:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "BLE setValues SEND %s: request_id=%d framed=%d B",
                    self._scu_address, request_id, len(pia_frame),
                )
            encrypted = self._tls.encrypt(pia_frame)
            await self._write_to_scu(encrypted, force_response=True)
            status = await asyncio.wait_for(future, timeout=timeout)
            _LOGGER.info(
                "BLE setValues ACK: request_id=%d status=%s", request_id, status
            )
            return status
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "BLE setValues no ACK within %.1fs (request_id=%d) — cloud fallback",
                timeout, request_id,
            )
            return None
        finally:
            self._pending_writes.pop(request_id, None)

    async def listen(self) -> None:
        """Listen for PIA responses from the SCU and dispatch them."""
        import base64
        if not self._tls_established:
            raise BleTransportError("TLS not established")

        while self._connected:
            try:
                incoming = await asyncio.wait_for(
                    self._uart_queue.get(), timeout=30.0
                )
            except asyncio.TimeoutError:
                continue

            outbound, plaintext_chunks = self._tls.feed_encrypted(incoming)
            if outbound:
                await self._write_to_scu(outbound)

            for chunk in plaintext_chunks:
                for frame in self._frame_acc.feed(chunk):
                    try:
                        payload = decode_ble_pia_frame(frame)
                        if _LOGGER.isEnabledFor(logging.DEBUG):
                            _LOGGER.debug(
                                "BLE PIA RECV %s: plaintext=%d B hex=%s",
                                self._scu_address, len(payload), payload.hex(),
                            )
                        if self._pending_writes:
                            self._resolve_pending_write(payload)
                        if self._on_pia_response:
                            self._on_pia_response(base64.b64encode(payload).decode())
                    except BleTransportError as err:
                        _LOGGER.warning("BLE PIA frame decode error: %s", err)

    async def pair_mobile(
        self,
        activation_token: str,
        confirmation_token: str,
        mobile_device_name: str = "homeassistant",
        timeout: float = DEFAULT_PAIR_TIMEOUT,
    ) -> PairMobileResponse:
        """Perform the SCU mobile-device pairing ceremony over BLE/TLS.

        This mirrors the EHG app's pairing flow:
          1. User presses CONNECTION button on SCU control panel
          2. Send PairMobileRequest (activation token + confirmation token)
          3. SCU processes the request and returns tokens
          4. Send PairMobileConfirmation(success=true)

        Args:
            activation_token: The QR code text from the vehicle sticker.
            confirmation_token: One-time token from POST /confirmationToken.
            mobile_device_name: Friendly name for this device (default: "homeassistant").
            timeout: Seconds to wait for SCU pairing response (default: 60s).

        Returns:
            PairMobileResponse with remote_access_token and remote_access_refresh_token.
        """
        if not self._tls_established:
            raise BleTransportError("TLS not established — call connect() and establish_tls() first")

        _LOGGER.info(
            "Starting BLE pairing with SCU %s \u2014 press CONNECTION button on SCU, waiting up to %ds",
            self._scu_address, int(timeout),
        )

        # Build and send PairMobileRequest
        _LOGGER.debug("Building PairMobileRequest (device_name=%s, activation_token_len=%d)",
                      mobile_device_name, len(activation_token))
        pair_frame = build_pair_mobile_frame(
            activation_token, confirmation_token, mobile_device_name,
        )
        _LOGGER.debug("PairMobileRequest frame: %d bytes", len(pair_frame))
        encrypted = self._tls.encrypt(pair_frame)
        _LOGGER.debug("Sending encrypted PairMobileRequest: %d bytes", len(encrypted))
        # Force Write With Response for PairMobileRequest to guarantee data
        # integrity. At MTU=23 (chunk=20) this is 63 chunks — without ACKs
        # the SCU's NUS RX buffer overflows and silently drops data, causing
        # a 120s timeout with no response.
        await self._write_to_scu(encrypted, force_response=True)

        # Wait for PairMobileResponse (user must press CONNECTION button on SCU)
        response_frame = await self._receive_next_frame(timeout)
        pair_response = decode_pair_mobile_response(response_frame)

        _LOGGER.info(
            "BLE pairing response received from SCU %s (status=%s, confirmation_required=%s)",
            self._scu_address, pair_response.status, pair_response.confirmation_required,
        )

        # Send confirmation
        confirm_frame = build_pair_mobile_confirmation_frame(success=True)
        encrypted = self._tls.encrypt(confirm_frame)
        await self._write_to_scu(encrypted)
        _LOGGER.info("BLE pairing confirmation sent to SCU %s", self._scu_address)

        if pair_response.remote_access_refresh_token:
            _LOGGER.info("BLE pairing successful — remote-access refresh token obtained")
        else:
            _LOGGER.warning("BLE pairing response did not contain a refresh token")

        return pair_response

    async def scan_user_field(
        self,
        field_number: int,
        payload: bytes = b"",
        timeout: float = 10.0,
    ) -> dict | None:
        """Send a UserRequestTopic with the given field number and wait for response.

        Used to brute-force unknown protobuf field numbers. Returns the decoded
        generic response dict, or None on timeout (no response = invalid field).
        """
        if not self._tls_established:
            raise BleTransportError("TLS not established")

        frame = build_user_request_frame(field_number, payload)
        encrypted = self._tls.encrypt(frame)
        _LOGGER.info("BLE field scan: sending UserRequestTopic field=%d (%d bytes)", field_number, len(encrypted))
        await self._write_to_scu(encrypted)

        try:
            response_frame = await self._receive_next_frame(timeout)
            result = decode_generic_response(response_frame)
            _LOGGER.info(
                "BLE field scan: field=%d got response — status=%s, fields=%s",
                field_number, result.get("status"), list(result.get("fields", {}).keys()),
            )
            return result
        except BleTransportError:
            _LOGGER.info("BLE field scan: field=%d — no response (timeout)", field_number)
            return None

    async def brute_force_user_fields(
        self,
        field_range: range | None = None,
        timeout_per_field: float = 8.0,
    ) -> dict[int, dict]:
        """Try all UserRequestTopic field numbers and log responses.

        Skips known fields (4=pairMobileDevice, 6=pairMobileDeviceConfirmation).
        Returns {field_number: response_dict} for fields that got a response.
        """
        if field_range is None:
            field_range = range(1, 16)

        skip = {4, 6}  # Known fields — don't probe
        results = {}

        for fn in field_range:
            if fn in skip:
                _LOGGER.info("BLE field scan: skipping known field %d", fn)
                continue
            # Pace probes to avoid ATT 0x0e (GATT write buffer overflow).
            # The SCU's NUS RX buffer needs time to process each request.
            await asyncio.sleep(1.0)
            result = await self.scan_user_field(fn, timeout=timeout_per_field)
            if result is not None:
                results[fn] = result

        _LOGGER.info(
            "BLE field scan complete: %d/%d fields responded — %s",
            len(results), len(field_range) - len(skip), list(results.keys()),
        )
        return results

    async def try_delete_mobile_device(
        self,
        mac_address: str,
        field_number: int,
        timeout: float = 10.0,
    ) -> dict | None:
        """Try to delete a mobile device from the SCU's paired list.

        Args:
            mac_address: BLE MAC of the device to remove (e.g. "DC:A6:32:7B:F1:88").
            field_number: The UserRequestTopic field number for deleteMobileDevices.
            timeout: Seconds to wait for response.
        """
        if not self._tls_established:
            raise BleTransportError("TLS not established")

        frame = build_delete_mobile_device_frame(field_number, mac_address)
        encrypted = self._tls.encrypt(frame)
        _LOGGER.info(
            "BLE delete device: sending deleteMobileDevices(field=%d, mac=%s) — %d bytes",
            field_number, mac_address, len(encrypted),
        )
        await self._write_to_scu(encrypted)

        try:
            response_frame = await self._receive_next_frame(timeout)
            result = decode_generic_response(response_frame)
            _LOGGER.info(
                "BLE delete device: response — status=%s, fields=%s",
                result.get("status"), list(result.get("fields", {}).keys()),
            )
            return result
        except BleTransportError:
            _LOGGER.info("BLE delete device: no response (timeout)")
            return None

    async def _receive_next_frame(self, timeout: float) -> bytes:
        """Wait for the next complete BLE PIA frame from the SCU."""
        deadline = self._loop.time() + timeout
        acc = _FrameAccumulator()
        while True:
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                raise BleTransportError(
                    "Timed out waiting for SCU response — "
                    "did you press the CONNECTION button "
                    "on the SCU control panel?"
                )
            try:
                incoming = await asyncio.wait_for(
                    self._uart_queue.get(), timeout=remaining,
                )
            except asyncio.TimeoutError as err:
                raise BleTransportError(
                    "Timed out waiting for SCU response — "
                    "did you press the CONNECTION button "
                    "on the SCU control panel?"
                ) from err

            outbound, plaintext_chunks = self._tls.feed_encrypted(incoming)
            if outbound:
                await self._write_to_scu(outbound)

            for chunk in plaintext_chunks:
                frames = acc.feed(chunk)
                if frames:
                    return frames[0]

    async def disconnect(self) -> None:
        """Disconnect from the SCU."""
        self._connected = False
        self._tls_established = False
        self._tls = None
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.stop_notify(UART_TX_UUID)
            except Exception:
                pass
            try:
                await client.disconnect()
            except Exception:
                pass
        _LOGGER.info("BLE disconnected from SCU %s", self._scu_address)

    async def check_bonding_state(self) -> int | None:
        """Check the SCU's bonding state via the fff40004 characteristic.

        The EHG app uses a challenge-response protocol:
        1. Write 4 random bytes to fff40004
        2. Read back from fff40004
        3. First 4 bytes of response = echo of challenge
        4. 5th byte = bonding state (0 = not in pairing mode, 1+ = pairing mode)

        Returns the bonding state byte, or None if the characteristic
        is not available or the challenge-response fails.
        """
        if not self._client or not self._connected:
            return None

        try:
            challenge = bytes(random.getrandbits(8) for _ in range(4))
            await self._client.write_gatt_char(BONDING_STATE_UUID, challenge)
            response = await self._client.read_gatt_char(BONDING_STATE_UUID)

            if response and len(response) >= 5:
                echo = response[:4]
                state = response[4]
                if echo == challenge:
                    _LOGGER.debug("SCU bonding state: %d (challenge matched)", state)
                    return state
                else:
                    _LOGGER.debug(
                        "SCU bonding state challenge mismatch: sent %s, got %s",
                        challenge.hex(), echo.hex(),
                    )
            return None
        except Exception as err:
            _LOGGER.debug("Could not read SCU bonding state: %s", err)
            return None

    async def _write_to_scu(self, data: bytes, *, force_response: bool = False) -> None:
        """Write data to SCU via NUS RX characteristic in chunks.

        Uses Write Without Response for large payloads (matching the EHG app's
        Nordic BLE library .split() behavior). Write Without Response avoids
        per-chunk ACK overhead that can cause ATT errors on rapid multi-chunk
        writes. Small payloads (≤10 chunks) use Write With Response for
        reliability.

        When force_response=True, always uses Write With Response regardless
        of payload size. Use this for critical one-shot messages like
        PairMobileRequest where data integrity is more important than speed.
        At MTU=23 (chunk=20), large payloads need many chunks — without ACKs,
        the SCU's NUS RX buffer can overflow and silently drop data.

        Pacing is applied for ALL large writes (>10 chunks) regardless of
        write mode. Write-With-Response already adds ~30ms ACK overhead per
        chunk, but the SCU's NUS RX processing at MTU=23 still can't keep up
        with rapid back-to-back writes — ATT error 0x0e ("Unlikely Error")
        after ~10-15 chunks. A 50ms inter-chunk delay gives the SCU's NUS
        service time to drain its RX buffer and reassemble the PIA frame.
        Total time for 63 chunks at 50ms = ~3.2s (acceptable for pairing).

        Vehicle test 2026-05-13: 50ms WriteReq pacing fails at chunk 16/18
        of the 342-byte TLS CertificateVerify message (ATT 0x0e). The
        Write-With-Response ACK adds ~30ms, so effective interval was ~80ms.
        Increased WriteReq pacing to 100ms (effective ~130ms) which gives
        the SCU NUS RX enough drain time for large TLS handshake payloads.

        See: https://docs.nordicsemi.com/bundle/ncs-latest/page/nrf/libraries/bluetooth/services/nus.html
        NUS RX supports both Write and Write Without Response.
        """
        if not data or self._client is None:
            return
        total_chunks = (len(data) + self._write_chunk_size - 1) // self._write_chunk_size
        # Large payloads: use Write Without Response (like EHG app's .split())
        # + pacing to avoid buffer overflow.
        # Exception: force_response=True overrides this for critical messages.
        if force_response:
            use_response = True
        else:
            use_write_without_response = total_chunks > 10
            use_response = self._write_response if not use_write_without_response else False
        write_mode = "WriteReq" if use_response else "WriteCmd(no-resp)"
        # Pace ALL large writes (>10 chunks), regardless of write mode.
        # Write-With-Response adds ATT-level ACK but the SCU's NUS RX
        # processing still overflows at ~10-15 rapid chunks (ATT 0x0e).
        # 100ms delay per chunk at MTU=23 = ~6.3s for 63 chunks (WriteReq).
        # 20ms for WriteCmd (no ACK overhead).
        pace = total_chunks > 10
        pace_ms = 100 if use_response else 20  # WriteReq needs more time (ACK + drain)
        _LOGGER.debug(
            "BLE TX %d bytes → %d chunks, mode=%s, pace=%s",
            len(data), total_chunks, write_mode,
            f"{pace_ms}ms" if pace else "none",
        )
        for i, offset in enumerate(range(0, len(data), self._write_chunk_size)):
            chunk = data[offset : offset + self._write_chunk_size]
            try:
                await self._client.write_gatt_char(
                    UART_RX_UUID, chunk, response=use_response
                )
            except Exception as err:
                _LOGGER.warning(
                    "BLE TX chunk %d/%d failed after %d successful writes: %s",
                    i + 1, total_chunks, i, err,
                )
                raise
            # Pace large writes to avoid ATT buffer overflow on SCU
            if pace and i < total_chunks - 1:
                await asyncio.sleep(pace_ms / 1000)

    async def _next_uart_data(self, deadline: float) -> bytes:
        """Wait for the next UART notification from SCU."""
        remaining = deadline - self._loop.time()
        if remaining <= 0:
            raise BleTransportError("Timed out waiting for SCU BLE data")
        try:
            return await asyncio.wait_for(self._uart_queue.get(), timeout=remaining)
        except asyncio.TimeoutError as err:
            raise BleTransportError("Timed out waiting for SCU BLE data") from err

    def _on_uart_notify(self, _sender: Any, data: Any) -> None:
        """Handle incoming NUS TX notification from SCU."""
        if self._loop is None:
            return
        payload = bytes(data)
        _LOGGER.debug("BLE UART RX: %d bytes", len(payload))
        self._loop.call_soon_threadsafe(
            self._uart_queue.put_nowait, payload
        )

    async def _negotiate_mtu_dbus(self) -> int | None:
        """Try to read the negotiated ATT MTU from BlueZ via D-Bus.

        BlueZ negotiates MTU automatically during the ATT connection. We can
        read the result from the org.bluez.Device1.MTU property on D-Bus.
        Returns the MTU value, or None if unavailable.
        """
        try:
            from dbus_fast.aio import MessageBus
            from dbus_fast import BusType, Message, MessageType, Variant
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            dev_path = f"/org/bluez/hci0/dev_{self._scu_address.replace(':', '_')}"
            msg = Message(
                destination="org.bluez",
                path=dev_path,
                interface="org.freedesktop.DBus.Properties",
                member="Get",
                signature="ss",
                body=["org.bluez.Device1", "MTU"],
            )
            reply = await bus.call(msg)
            bus.disconnect()
            if reply.message_type == MessageType.ERROR:
                _LOGGER.debug("D-Bus MTU read failed: %s", reply.body)
                return None
            if reply.body and isinstance(reply.body[0], Variant):
                mtu_val = reply.body[0].value
                _LOGGER.info("BLE MTU from D-Bus: %d", mtu_val)
                return int(mtu_val)
            return None
        except Exception as err:
            _LOGGER.debug("D-Bus MTU negotiation failed: %s", err)
            return None

    async def _pair_via_bluetoothctl(self) -> bool:
        """Pair with the SCU using a D-Bus pairing agent (raw messages only).

        Registers an agent path with BlueZ, handles all agent method calls
        via add_message_handler (including Introspect so BlueZ can find the
        object), then calls Device1.Pair().

        The agent is device-locked to this SCU (foreign devices that pair in
        the same window are rejected) and also answers the legacy PIN/passkey
        callbacks, which some BlueZ/SCU combinations select instead of
        RequestConfirmation - omitting them makes Device1.Pair() fail with an
        unknown-method error.
        """
        try:
            from dbus_fast.aio import MessageBus
            from dbus_fast import BusType, Message, MessageType
        except ImportError:
            _LOGGER.warning("dbus-fast not available — falling back to bleak pair()")
            if self._client:
                await self._client.pair()
                return True
            return False

        addr = self._scu_address
        agent_path = "/org/bluez/agent_hymer"
        device_path = f"/org/bluez/hci0/dev_{addr.replace(':', '_')}"

        # Introspection XML for the agent — tells BlueZ what methods we support
        AGENT_INTROSPECT_XML = """<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
"http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="org.bluez.Agent1">
    <method name="Release"/>
    <method name="RequestConfirmation">
      <arg name="device" type="o" direction="in"/>
      <arg name="passkey" type="u" direction="in"/>
    </method>
    <method name="AuthorizeService">
      <arg name="device" type="o" direction="in"/>
      <arg name="uuid" type="s" direction="in"/>
    </method>
    <method name="RequestAuthorization">
      <arg name="device" type="o" direction="in"/>
    </method>
    <method name="RequestPinCode">
      <arg name="device" type="o" direction="in"/>
      <arg name="pincode" type="s" direction="out"/>
    </method>
    <method name="RequestPasskey">
      <arg name="device" type="o" direction="in"/>
      <arg name="passkey" type="u" direction="out"/>
    </method>
    <method name="Cancel"/>
  </interface>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect">
      <arg name="xml" type="s" direction="out"/>
    </method>
  </interface>
</node>"""

        _LOGGER.debug("D-Bus agent: registering NoInputNoOutput agent for %s", addr)

        # Device-locking: BlueZ passes the device object path as the first arg of
        # the pairing callbacks. We only answer for our SCU so a stray device
        # pairing in the same window cannot hijack our auto-accept.
        device_suffix = "dev_" + addr.replace(":", "_").upper()
        _DEVICE_ARG_METHODS = frozenset(
            {
                "RequestConfirmation",
                "RequestAuthorization",
                "AuthorizeService",
                "RequestPinCode",
                "RequestPasskey",
            }
        )

        def _is_our_device(body: Any) -> bool:
            if not body:
                return True  # Release/Cancel carry no device argument
            return str(body[0]).upper().endswith(device_suffix)

        bus = None
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

            # Handle ALL method calls to the agent path — including Introspect
            def agent_handler(msg: Message) -> bool:
                if msg.message_type != MessageType.METHOD_CALL:
                    return False
                if msg.path != agent_path:
                    return False

                # Handle Introspect — BlueZ needs this to find our agent
                if (msg.interface == "org.freedesktop.DBus.Introspectable"
                        and msg.member == "Introspect"):
                    reply = Message.new_method_return(msg, "s", [AGENT_INTROSPECT_XML])
                    bus.send_message(reply)
                    return True

                if msg.interface == "org.bluez.Agent1":
                    member = msg.member
                    # Reject pairing callbacks for any device that is not our SCU.
                    if member in _DEVICE_ARG_METHODS and not _is_our_device(msg.body):
                        _LOGGER.debug(
                            "D-Bus agent: rejecting %s for foreign device %s",
                            member, msg.body[0] if msg.body else "?",
                        )
                        reply = Message.new_error(
                            msg, "org.bluez.Error.Rejected", "not our device"
                        )
                        bus.send_message(reply)
                        return True
                    # Legacy JustWorks fallbacks: some adapter/SCU combos select
                    # PIN/passkey instead of RequestConfirmation. These expect a
                    # return value, so an empty method_return would be malformed.
                    if member == "RequestPinCode":
                        reply = Message.new_method_return(msg, "s", ["0000"])
                    elif member == "RequestPasskey":
                        reply = Message.new_method_return(msg, "u", [0])
                    else:
                        reply = Message.new_method_return(msg)
                    _LOGGER.debug("D-Bus agent: accepting %s", member)
                    bus.send_message(reply)
                    return True

                return False

            bus.add_message_handler(agent_handler)

            # Register agent with AgentManager1
            register_msg = Message(
                destination="org.bluez",
                path="/org/bluez",
                interface="org.bluez.AgentManager1",
                member="RegisterAgent",
                signature="os",
                body=[agent_path, "NoInputNoOutput"],
            )
            reply = await bus.call(register_msg)
            if reply.message_type == MessageType.ERROR:
                _LOGGER.warning("D-Bus agent: RegisterAgent failed: %s %s",
                                reply.error_name, reply.body)
                return False
            _LOGGER.debug("D-Bus agent: registered, calling Device1.Pair()")

            # Call Pair on the device
            pair_msg = Message(
                destination="org.bluez",
                path=device_path,
                interface="org.bluez.Device1",
                member="Pair",
            )
            try:
                reply = await asyncio.wait_for(bus.call(pair_msg), timeout=15.0)
                if reply.message_type == MessageType.ERROR:
                    err_name = reply.error_name or ""
                    err_body = str(reply.body) if reply.body else ""
                    if "AlreadyExists" in err_name:
                        _LOGGER.debug("D-Bus agent: already paired")
                        return True
                    _LOGGER.warning("D-Bus agent: Pair failed: %s %s", err_name, err_body)
                    return False
                _LOGGER.info("D-Bus agent: pairing successful with %s", addr)
                return True
            except asyncio.TimeoutError:
                _LOGGER.warning("D-Bus agent: Pair timed out after 15s")
                return False

        except Exception as err:
            _LOGGER.warning("D-Bus agent pairing error: %s", err)
            return False
        finally:
            if bus:
                try:
                    unreg_msg = Message(
                        destination="org.bluez",
                        path="/org/bluez",
                        interface="org.bluez.AgentManager1",
                        member="UnregisterAgent",
                        signature="o",
                        body=[agent_path],
                    )
                    await bus.call(unreg_msg)
                except Exception:
                    pass
                bus.disconnect()
