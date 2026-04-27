# BLE Communication Protocol

Technical documentation for the HYMER Connect SCU BLE communication layer.

## Architecture Overview

`
┌─────────────┐     BLE GATT (NUS)      ┌─────────────┐
│  RPi4 / HA  │◄─────────────────────────►│     SCU     │
│  (bleak)    │   TLS 1.1 over NUS       │ (firmware   │
│             │   PIA Protobuf           │  1.12.0.0)  │
└─────────────┘                          └─────────────┘
      │                                        │
      │  Cloud (SignalR/HTTPS)                │  LTE
      └───────────►Azure◄─────────────────────┘
`

## BLE Services

### Discovered Services (without bonding)

5 GATT services visible immediately after connection (no CONNECTION button required):

| Service | UUID | Purpose |
|---------|------|---------|
| Power Service | `fff40001-13c9-42f3-9d46-e1d1aa2a7232` | SCU power management |
| Generic Access | `00001800-0000-1000-8000-00805f9b34fb` | Standard BLE device info |
| Bond Management | `0000181e-0000-1000-8000-00805f9b34fb` | BLE bonding management |
| Generic Attribute | `00001801-0000-1000-8000-00805f9b34fb` | Standard GATT service |
| Nordic UART (NUS) | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` | Data exchange (PIA protobuf over TLS) |

### Key Characteristics

| Characteristic | UUID | Properties | Notes |
|----------------|------|------------|-------|
| NUS RX | `6e400002` | Write, Write Without Response | RPi → SCU data |
| NUS TX | `6e400003` | Notify | SCU → RPi data |
| Power State | `fff40002` | Read | SCU power state |
| Power Control | `fff40003` | Write | Wake-up command (0x0A) |
| Bonding State | `fff40004` | Read/Write | Challenge-response bonding check. **Only visible after OS-level bonding** (not found without CONNECTION pressed) |

## Connection Sequence

### 1. BLE Bonding (JustWorks)
- Register D-Bus pairing agent (raw messages + introspection XML)
- Call `Device1.Pair()` via D-Bus → JustWorks bonding
- SCU must be in pairing mode (CONNECTION pressed on touch panel)
- Without bonding, SCU ignores TLS ClientHello

### 2. TLS Handshake
- TLS 1.0/1.1 over `ssl.MemoryBIO` (no real socket)
- Cipher: `AES128-SHA` or `AES256-SHA`
- OpenSSL 3.x requires `@SECLEVEL=0` to allow legacy protocols
- Clear `OP_NO_TLSv1` and `OP_NO_TLSv1_1` flags
- SCU sends ServerHello + Certificate + ServerHelloDone
- RPi sends ClientKeyExchange + ChangeCipherSpec + Finished

### 3. PIA Protobuf over TLS
All data after TLS handshake is encrypted. PIA frames use:
- Magic: `0xA0CB` (2 bytes)
- Payload length: 4 bytes big-endian
- CRC32: 4 bytes (computed over header with zeroed CRC + payload)
- Payload: protobuf message

### 4. PairMobileRequest
`
BleProtocol.request (field 1, LEN) {
  Request {
    field 1: request_id (varint, random 1-1000001)
    field 2: version (string, "v0.32.0")
    field 3: timestamp (varint, epoch seconds)
    field 8: User (LEN) {
      field 4: PairMobileDevice (LEN) {
        field 1: activation_token (string, QR code text)
        field 2: confirmation_token (string, from cloud API)
        field 3: mobile_device_name (string, "homeassistant")
        field 4: wait_for_confirmation (bool, true)
      }
    }
  }
}
`

### 5. PairMobileResponse (expected)
`
BleProtocol.response (field 2, LEN) {
  Response {
    field 1: request_id (varint)
    field 2: status (varint)
    field 3: timestamp (varint)
    field 9: mobilePair (LEN) {
      field 1: remote_access_token (string, short-lived)
      field 2: remote_access_refresh_token (string, LONG-LIVED)
      field 3: confirmation_required (bool)
    }
  }
}
`

### 6. PairMobileConfirmation
`
BleProtocol.request (field 1) {
  Request {
    field 1: request_id (varint)
    field 2: version (string)
    field 3: timestamp (varint)
    field 8: User {
      field 6: PairMobileConfirmation {
        field 1: success (bool, true)
      }
    }
  }
}
`

## GATT Write Pacing

Large payloads (>10 chunks at MTU=23, chunk_size=20) use **Write Without Response**
with 5ms inter-chunk delay. This matches the EHG app's Nordic BLE library behavior
(`.split()` method), which splits data across multiple Write Without Response packets.

Small payloads (≤10 chunks, e.g. TLS handshake) use Write With Response for
reliability.

**Why Write Without Response for large payloads:**
- Write With Response requires an ACK for every chunk → back-pressure
- 63 sequential Write-With-Response at MTU=23 caused ATT error 0x0e
- Write Without Response eliminates per-chunk ACK overhead
- 5ms pacing prevents NUS RX buffer overflow

**Reference:** [Nordic UART Service (NUS) docs](https://docs.nordicsemi.com/bundle/ncs-latest/page/nrf/libraries/bluetooth/services/nus.html)
— NUS RX (`6E400002`) supports both Write and Write Without Response.

## Bonding State Check (fff40004)

Challenge-response protocol used by the EHG app:
1. Write 4 random bytes to `fff40004`
2. Read back from `fff40004`
3. First 4 bytes of response = echo of challenge
4. 5th byte = bonding state (0 = not in pairing mode, non-zero = CONNECTION pressed)

**Important:** This characteristic is only visible after OS-level bonding is
established. Without bonding (CONNECTION not pressed), GATT service discovery
returns `fff40001` but does NOT expose `fff40004` — attempting to access it
throws `Characteristic fff40004-13c9-42f3-9d46-e1d1aa2a7232 was not found!`.

Verified via live testing (2026-04-27): GATT connects fine, 5 services visible,
but `fff40004` is absent until bonding succeeds.

## Token Delivery (confirmed via Hermes analysis)

The EHG refresh token is delivered in the BLE `PairMobileResponse`, NOT via
a cloud API call. Confirmed by string analysis of the Hermes JS bundle
(`index.android.bundle`, 13.4 MB):

- `setRemoteRefreshToken` — stores token after BLE response
- `remoteAccessRefreshToken` — protobuf field name
- `REMOTE_ACCESS_TOKEN_EXPIRED` — token lifecycle

## D-Bus Pairing Agent

HAOS constraints prevent using `bleak.pair()`, `bluetoothctl`, or
`dbus-fast ServiceInterface`. Solution: pure raw D-Bus messages.

`python
# Register agent with introspection XML
agent_handler → responds to Introspect (returns Agent1 XML)
agent_handler → auto-accepts all Agent1 methods (JustWorks)
RegisterAgent(agent_path, "NoInputNoOutput")
Device1.Pair()
UnregisterAgent(agent_path)
`

## Credits

- **Dan Simms** (`dan-simms1/hymer-connect-ha`) — PairMobileRequest/Response
  protobuf field layout, BLE pairing ceremony, `hymer_token_tool`
- **HYMER helpcenter video** — CONNECTION button flow
- **Nordic Semiconductor** — [NUS specification](https://docs.nordicsemi.com/bundle/ncs-latest/page/nrf/libraries/bluetooth/services/nus.html),
  [GATT Latency Client](https://docs.nordicsemi.com/bundle/ncs-latest/page/nrf/libraries/bluetooth/services/latency_client.html)
