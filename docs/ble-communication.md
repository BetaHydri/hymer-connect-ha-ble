# BLE Communication Protocol

Technical documentation for the HYMER Connect SCU BLE communication layer.

## Architecture Overview

`
┌─────────────┐     BLE GATT (NUS)       ┌─────────────┐
│  RPi4 / HA  │◄────────────────────────►│     SCU     │
│  (bleak)    │   TLS 1.1 over NUS       │ (firmware   │
│             │   PIA Protobuf           │  1.12.0.0)  │
└─────────────┘                          └─────────────┘
      │                                        │
      │  Cloud (SignalR/HTTPS)                 │  LTE
      └───────────►Azure◄──────────────────────┘
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

## MTU Negotiation

The EHG app requests **MTU 245** (`requestMtu(245)` in `SmartCaravanBleManager.java`),
giving 242-byte chunks. This is critical for large payloads:

| MTU | Chunk size | PairMobileRequest (1253 bytes) |
|-----|-----------|-------------------------------|
| 23 (default) | 20 bytes | 63 chunks — caused ATT error 0x0e |
| 245 (EHG app) | 242 bytes | 6 chunks — works reliably |

The integration now calls `_acquire_mtu()` on the bleak client to negotiate
a higher MTU, matching the EHG app behavior.

## GATT Write Mode

Large payloads (>10 chunks) use **Write Without Response** with 5ms inter-chunk
pacing. This matches the EHG app's Nordic BLE library behavior:
- `setWriteType(2)` = Write Without Response
- `.split()` = auto-chunk at negotiated MTU

Small payloads (≤10 chunks, e.g. TLS handshake) use Write With Response for
reliability.

**Reference:** [Nordic UART Service (NUS) docs](https://docs.nordicsemi.com/bundle/ncs-latest/page/nrf/libraries/bluetooth/services/nus.html)
— NUS RX (`6E400002`) supports both Write and Write Without Response.

## EHG App Architecture (from decompilation)

The EHG app's BLE stack has two layers:

1. **Java native** (`SmartCaravanBleManager.java`) — pure transport:
   - GATT connect, service discovery, MTU negotiation (245)
   - NUS TX notifications → hex string → `LocalBroadcastManager` → JS
   - NUS RX writes ← hex string ← `LocalBroadcastManager` ← JS
   - Write Without Response + `.split()` for auto-chunking
   - No TLS, no protobuf — just a byte pipe

2. **Hermes JS bundle** (`index.android.bundle`, 13.4 MB) — protocol:
   - TLS 1.0/1.1 handshake (AES128-SHA)
   - PIA protobuf encoding/decoding
   - PairMobileRequest/Response handling
   - Token storage (`setRemoteRefreshToken`)

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

## Runtime PIA Commands over BLE (v2.61.0+)

After TLS is established, the BLE path supports **full bidirectional PIA
communication** — not just pairing and sensor streaming, but also write
commands (lights, switches, heater, fridge, boiler, climate).

The coordinator's `_send_with_retry()` builds the PIA protobuf payload locally
(same `build_light_command()` / `build_multi_sensor_command()` used for SignalR)
and sends it via `ble_client.send_pia_command(b64_payload)`, which:

1. Base64-decodes the payload
2. Wraps it in a BLE PIA frame (magic `0xA0CB` + length + CRC32)
3. Encrypts via TLS
4. Writes to NUS RX (`6e400002`) using chunked GATT writes

The SCU processes the command identically to a cloud-received PIA command and
responds via NUS TX notifications. The response is decoded by the BLE listen
loop and updates sensor state immediately.

### ACK-based Cloud Safety Net

After sending a command via BLE, the coordinator waits up to **2 seconds** for
the SCU to echo back a PIA response (confirming it processed the command).
If no response arrives within the timeout, the same command is automatically
re-sent via the cloud/SignalR path as a safety net.  Commands are idempotent
(set-value, not toggle), so a duplicate is harmless.

```
BLE send → wait 2s for PIA response
    ├─ Response received  → ✅ confirmed, done
    └─ Timeout (no ACK)   → ⚠️ re-send via cloud
```

## Credits

- **Dan Simms** (`dan-simms1/hymer-connect-ha`) — PairMobileRequest/Response
  protobuf field layout, BLE pairing ceremony, `hymer_token_tool`
- **HYMER helpcenter video** — CONNECTION button flow
- **Nordic Semiconductor** — [NUS specification](https://docs.nordicsemi.com/bundle/ncs-latest/page/nrf/libraries/bluetooth/services/nus.html),
  [GATT Latency Client](https://docs.nordicsemi.com/bundle/ncs-latest/page/nrf/libraries/bluetooth/services/latency_client.html)
