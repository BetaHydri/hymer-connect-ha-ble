"""BLE transport client for direct SCU communication.

Provides a local BLE path to the SCU as an alternative to the cloud SignalR
path. Uses the Nordic UART Service (NUS) over BLE GATT with TLS 1.0/1.1
encryption — the same PIA protobuf protocol used by the EHG app when
physically near the vehicle.

Requires:
  - bleak (BLE GATT client library)
  - Python ssl module with MemoryBIO (TLS over arbitrary transport)
  - BLE hardware (Pi 4 built-in or USB adapter)

Architecture:
  Phone (BLE) ──► SCU ──► CAN/LIN/PIA devices
  Pi/HA (BLE)  ──► SCU ──► CAN/LIN/PIA devices  ← THIS MODULE
  Pi/HA (cloud) ──► Azure SignalR ──► SCU         ← signalr_client.py

The BLE path is ~50ms latency vs ~500ms-2s for the cloud path.

Status: EXPERIMENTAL — not yet verified on real hardware.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import struct
import time
import zlib
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
WAKE_UP_COMMAND = bytes((0x0A,))
DEFAULT_GATT_MTU = 23


class BleTransportError(RuntimeError):
    """Raised when the BLE transport encounters an error."""


class TlsTransportError(RuntimeError):
    """Raised when the TLS layer over BLE fails."""


# ---------------------------------------------------------------------------
# TLS over MemoryBIO — drives TLS without a real socket
# ---------------------------------------------------------------------------

class _TlsOverBle:
    """TLS client using ssl.MemoryBIO for transport over BLE GATT."""

    def __init__(self) -> None:
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._context.check_hostname = False
        self._context.verify_mode = ssl.CERT_NONE
        self._context.minimum_version = APP_TLS_MIN_VERSION
        self._context.maximum_version = APP_TLS_MAX_VERSION
        self._context.set_ciphers(APP_TLS_CIPHERS)

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
    def scu_address(self) -> str:
        return self._scu_address

    async def scan_for_scu(self, timeout: float = DEFAULT_SCAN_TIMEOUT) -> list[dict]:
        """Scan for nearby SCU devices advertising the NUS service."""
        if not HAS_BLEAK:
            raise BleTransportError("bleak is not installed")
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
        results = []
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
        return results

    async def connect(self) -> None:
        """Connect to the SCU via BLE and set up NUS notifications."""
        if self._client is not None:
            return

        self._loop = asyncio.get_running_loop()
        client = BleakClient(self._scu_address, timeout=self._connect_timeout)
        await client.connect()

        services = await client.get_services()
        rx_char = None
        tx_char = None
        for service in services:
            for char in service.characteristics:
                uuid = str(char.uuid).lower()
                if uuid == UART_RX_UUID:
                    rx_char = char
                elif uuid == UART_TX_UUID:
                    tx_char = char

        if rx_char is None or tx_char is None:
            await client.disconnect()
            raise BleTransportError(
                f"SCU {self._scu_address} does not expose Nordic UART Service"
            )

        # Determine write mode (write-with-response preferred)
        props = {p.lower() for p in rx_char.properties}
        self._write_response = "write" in props
        mtu = getattr(client, "mtu_size", DEFAULT_GATT_MTU)
        self._write_chunk_size = max(20, min(242, mtu - 3))

        # Start receiving notifications
        await client.start_notify(UART_TX_UUID, self._on_uart_notify)

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

    async def _write_to_scu(self, data: bytes) -> None:
        """Write data to SCU via NUS RX characteristic in chunks."""
        if not data or self._client is None:
            return
        for offset in range(0, len(data), self._write_chunk_size):
            chunk = data[offset : offset + self._write_chunk_size]
            await self._client.write_gatt_char(
                UART_RX_UUID, chunk, response=self._write_response
            )

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
        self._loop.call_soon_threadsafe(
            self._uart_queue.put_nowait, bytes(data)
        )
