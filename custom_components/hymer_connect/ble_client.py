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
                self._buf.clear()
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
    ) -> None:
        if not HAS_BLEAK:
            raise BleTransportError(
                "bleak is not installed. Install with: pip install bleak"
            )
        self._scu_address = scu_address
        self._connect_timeout = connect_timeout
        self._tls_timeout = tls_timeout
        self._on_pia_response = on_pia_response

        self._client: BleakClient | None = None
        self._tls: _TlsOverBle | None = None
        self._frame_acc = _FrameAccumulator()
        self._uart_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = False
        self._tls_established = False
        self._write_response = True
        self._write_chunk_size = 20

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

    async def scan_for_scu(
        self,
        timeout: float = DEFAULT_SCAN_TIMEOUT,
        hass: Any | None = None,
    ) -> list[dict]:
        """Scan for nearby SCU devices advertising the NUS service.

        When *hass* is provided (running inside Home Assistant), uses HA's
        managed Bluetooth scanner (habluetooth) which avoids conflicts with
        the BlueZ adapter.  Falls back to a raw BleakScanner.discover() for
        standalone / tool usage.
        """
        results: list[dict] = []

        # ── HA-native path ──────────────────────────────────────────────
        if hass is not None:
            try:
                from homeassistant.components.bluetooth import async_discovered_service_info

                _LOGGER.debug("BLE scan via HA Bluetooth integration")
                for info in async_discovered_service_info(hass):
                    name = info.name or ""
                    uuids = [str(u).lower() for u in (info.service_uuids or [])]
                    if (
                        UART_SERVICE_UUID.lower() in uuids
                        or "hymer" in name.lower()
                        or "scu" in name.lower()
                    ):
                        results.append({
                            "address": info.address,
                            "name": name,
                            "rssi": info.rssi,
                            "service_uuids": uuids,
                        })
                results.sort(key=lambda x: x.get("rssi") or -999, reverse=True)
                _LOGGER.debug(
                    "HA BLE scan matched %d SCU candidates: %s",
                    len(results),
                    [(r["address"], r["name"], r["rssi"]) for r in results],
                )
                return results
            except Exception:  # noqa: BLE001
                _LOGGER.debug("HA Bluetooth API unavailable, falling back to BleakScanner")

        # ── Standalone fallback (tools / non-HA) ────────────────────────
        if not HAS_BLEAK:
            raise BleTransportError("bleak is not installed")
        _LOGGER.debug("BLE scan starting (timeout=%.1fs)", timeout)
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
        _LOGGER.debug("BLE scan found %d total devices", len(discovered))
        for _, (device, adv) in discovered.items():
            name = device.name or adv.local_name or ""
            uuids = [str(u).lower() for u in (adv.service_uuids or [])]
            if UART_SERVICE_UUID.lower() in uuids or "hymer" in name.lower() or "scu" in name.lower():
                results.append({
                    "address": device.address,
                    "name": name,
                    "rssi": adv.rssi,
                    "service_uuids": uuids,
                })
        results.sort(key=lambda x: x.get("rssi") or -999, reverse=True)
        _LOGGER.debug("BLE scan matched %d SCU candidates: %s", len(results),
                      [(r["address"], r["name"], r["rssi"]) for r in results])
        return results

    async def connect(self) -> None:
        """Connect to the SCU via BLE and set up NUS notifications."""
        if self._client is not None:
            return

        self._loop = asyncio.get_running_loop()
        await self._connect_inner()

    async def _connect_inner(self, retry: bool = True) -> None:
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
        client = BleakClient(self._scu_address, timeout=self._connect_timeout)
        try:
            await client.connect()
        except Exception as connect_err:
            # If bonded but connection fails with "device disconnected", the
            # bond keys are stale/corrupt. Clear them and retry once.
            if is_already_bonded and retry and "disconnect" in str(connect_err).lower():
                _LOGGER.warning(
                    "Bonded device rejected connection — clearing stale bond via D-Bus RemoveDevice: %s",
                    connect_err,
                )
                # BleakClient.unpair() doesn't reliably clear bonds.
                # Use D-Bus Adapter1.RemoveDevice() directly.
                try:
                    from dbus_fast.aio import MessageBus
                    from dbus_fast import BusType, Message, MessageType
                    rm_bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
                    try:
                        dev_path = f"/org/bluez/hci0/dev_{self._scu_address.replace(':', '_')}"
                        rm_msg = Message(
                            destination="org.bluez",
                            path="/org/bluez/hci0",
                            interface="org.bluez.Adapter1",
                            member="RemoveDevice",
                            signature="o",
                            body=[dev_path],
                        )
                        reply = await rm_bus.call(rm_msg)
                        if reply.message_type == MessageType.ERROR:
                            _LOGGER.debug("RemoveDevice failed: %s", reply.body)
                        else:
                            _LOGGER.info("Removed stale bond for %s via D-Bus", self._scu_address)
                    finally:
                        rm_bus.disconnect()
                except Exception as rm_err:
                    _LOGGER.debug("D-Bus RemoveDevice failed: %s", rm_err)
                # RemoveDevice removes the device from BlueZ's object tree.
                # In HA's managed BLE environment (habluetooth), a raw
                # BleakScanner re-scan does NOT work — the adapter is
                # controlled by habluetooth and won't start a new discovery.
                # Instead, raise back to the caller's retry loop.  By the
                # next attempt, habluetooth's passive scanner will have
                # re-discovered the device advertisement.
                _LOGGER.info(
                    "Stale bond cleared for %s — raising to retry loop "
                    "(habluetooth will re-discover the device)",
                    self._scu_address,
                )
                raise BleTransportError(
                    f"Stale bond cleared for {self._scu_address} — retry needed"
                ) from connect_err
            raise
        _LOGGER.debug("BLE GATT connected to %s", self._scu_address)

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
            try:
                if hasattr(client, "_acquire_mtu"):
                    await client._acquire_mtu()
                    _LOGGER.debug("BLE MTU acquired via _acquire_mtu()")
            except Exception as mtu_err:
                _LOGGER.debug("BLE MTU acquisition failed (non-critical): %s", mtu_err)

            mtu = getattr(client, "mtu_size", DEFAULT_GATT_MTU)
            self._write_chunk_size = max(20, min(242, mtu - 3))

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

            # Try OS-level bonding if CONNECTION was pressed on the SCU.
            # The SCU requires bonding before it will respond to TLS.
            #
            # bleak's client.pair() calls Device1.Pair() on D-Bus but does NOT
            # register a pairing agent. BlueZ waits ~8s for an agent response
            # that never comes → AuthenticationCanceled. We use bluetoothctl
            # instead, which has a built-in NoInputNoOutput agent for JustWorks
            # pairing (same as the Android "Koppeln?" dialog).
            #
            # Stale bonding records were already cleared before connect().
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

            # Bonding is required before TLS/notify will work.
            # If bonding failed, don't attempt start_notify (it will fail
            # with ATT 0x0e because the failed Pair() corrupts the GATT
            # session). Raise immediately with a clear message.
            if not bonded:
                raise BleTransportError(
                    "BLE bonding rejected by SCU — "
                    "press CONNECTION (Verbindung) on the SCU touch panel, "
                    "then retry within 2 minutes"
                )

            # bluetoothctl pairing may have disrupted the GATT connection.
            # Reconnect to get a fresh GATT session with the new bond.
            if not client.is_connected:
                _LOGGER.debug("Reconnecting after successful bonding")
                client = BleakClient(self._scu_address, timeout=self._connect_timeout)
                await client.connect()
                _LOGGER.debug("BLE GATT reconnected after bonding")

            # Start receiving NUS TX notifications
            await client.start_notify(UART_TX_UUID, self._on_uart_notify)
        except Exception as err:
            # Guarantee the raw BleakClient is disconnected on any setup failure
            # so BlueZ releases the GATT session and notify acquisition.
            _LOGGER.debug("BLE setup failed, disconnecting raw client: %s", err)
            try:
                await client.stop_notify(UART_TX_UUID)
            except Exception:
                pass
            try:
                await client.disconnect()
            except Exception:
                pass

            # "Notify acquired" / "NotPermitted" means BlueZ kept a stale
            # notify acquisition from a prior session that didn't clean up.
            # The only reliable fix is a full disconnect + reconnect so BlueZ
            # starts a fresh D-Bus GATT session without the stale state.
            err_str = str(err)
            if retry and ("Notify acquired" in err_str or "NotPermitted" in err_str):
                _LOGGER.warning(
                    "Stale BlueZ notify acquisition — disconnecting and "
                    "reconnecting with fresh GATT session: %s", err,
                )
                await asyncio.sleep(1.0)  # let BlueZ settle
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
        await self._write_to_scu(outbound)

        # Complete handshake
        deadline = self._loop.time() + self._tls_timeout
        while not self._tls.handshake_done:
            incoming = await self._next_uart_data(deadline)
            outbound, _plaintext = self._tls.feed_encrypted(incoming)
            await self._write_to_scu(outbound)

        self._tls_established = True
        _LOGGER.info("BLE TLS session established with SCU %s", self._scu_address)

    async def send_pia_command(self, b64_payload: str) -> None:
        """Send a PIA protobuf command (base64-encoded) over BLE TLS."""
        import base64
        if not self._tls_established:
            raise BleTransportError("TLS not established")
        raw = base64.b64decode(b64_payload)
        pia_frame = encode_ble_pia_frame(raw)
        encrypted = self._tls.encrypt(pia_frame)
        await self._write_to_scu(encrypted)

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
        await self._write_to_scu(encrypted)

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
            try:
                await client.stop_notify(UART_TX_UUID)
            except Exception:
                pass
            try:
                await client.disconnect()
            except Exception:
                pass
        _LOGGER.info("BLE disconnected from SCU %s", self._scu_address)

    async def _write_to_scu(self, data: bytes) -> None:
        """Write data to SCU via NUS RX characteristic in chunks.

        Uses Write Without Response for large payloads (matching the EHG app's
        Nordic BLE library .split() behavior). Write Without Response avoids
        per-chunk ACK overhead that can cause ATT errors on rapid multi-chunk
        writes. Small payloads (≤10 chunks) use Write With Response for
        reliability.

        See: https://docs.nordicsemi.com/bundle/ncs-latest/page/nrf/libraries/bluetooth/services/nus.html
        NUS RX supports both Write and Write Without Response.
        """
        if not data or self._client is None:
            return
        total_chunks = (len(data) + self._write_chunk_size - 1) // self._write_chunk_size
        # Large payloads: use Write Without Response (like EHG app's .split())
        # + pacing to avoid buffer overflow
        use_write_without_response = total_chunks > 10
        use_response = self._write_response if not use_write_without_response else False
        write_mode = "WriteReq" if use_response else "WriteCmd(no-resp)"
        _LOGGER.debug(
            "BLE TX %d bytes → %d chunks, mode=%s, pace=%s",
            len(data), total_chunks, write_mode,
            "5ms" if use_write_without_response else "none",
        )
        for i, offset in enumerate(range(0, len(data), self._write_chunk_size)):
            chunk = data[offset : offset + self._write_chunk_size]
            await self._client.write_gatt_char(
                UART_RX_UUID, chunk, response=use_response
            )
            # Pace large writes to avoid ATT buffer overflow on SCU
            if use_write_without_response and i < total_chunks - 1:
                await asyncio.sleep(0.005)  # 5ms between chunks (faster without ACK overhead)

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

    async def _pair_via_bluetoothctl(self) -> bool:
        """Pair with the SCU using a D-Bus pairing agent (raw messages only).

        Registers an agent path with BlueZ, handles all agent method calls
        via add_message_handler (including Introspect so BlueZ can find the
        object), then calls Device1.Pair().
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
    <method name="Cancel"/>
  </interface>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect">
      <arg name="xml" type="s" direction="out"/>
    </method>
  </interface>
</node>"""

        _LOGGER.debug("D-Bus agent: registering NoInputNoOutput agent for %s", addr)

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

                # Handle all Agent1 methods — auto-accept everything
                if msg.interface == "org.bluez.Agent1":
                    _LOGGER.debug("D-Bus agent: auto-accepting %s", msg.member)
                    reply = Message.new_method_return(msg)
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
