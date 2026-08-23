# BLE Communication Protocol

> **Audience:** Maintainers and reverse-engineering contributors. Normal users
> do not need this file to install or operate the integration.

Technical documentation for the HYMER Connect SCU BLE communication layer.

> **Just setting up or debugging BLE?** See the user-facing
> [`ble-troubleshooting.md`](ble-troubleshooting.md) — the BLE-direct path (Path A),
> adding BLE to an existing cloud-only setup via **Reconfigure**, what to watch out
> for on the HA host, enabling debug logging, and reading the pairing log.

## Architecture Overview

```
┌─────────────┐     BLE GATT (NUS)       ┌─────────────┐
│  RPi4 / HA  │◄────────────────────────►│     SCU     │
│  (bleak)    │   TLS 1.1 over NUS       │ (firmware   │
│             │   PIA Protobuf           │  1.12.0.0)  │
└─────────────┘                          └─────────────┘
      │                                        │
      │  Cloud (SignalR/HTTPS)                 │  LTE
      └───────────►Azure◄──────────────────────┘
```

### Protocol layering: PIA vs. the vehicle's field buses

PIA is **not** a vehicle field bus like LIN or CAN — it is an *application-layer*
protocol (Protobuf framed as `0xA0CB`+len+CRC32, carried over TLS). It sits on
top; the physical automotive buses sit underneath. The **SCU acts as a gateway**
that abstracts the messy real field buses behind one uniform PIA API:

```
HA integration / EHG app
        │  PIA (Protobuf over TLS) — transport = BLE (NUS) or cloud (SignalR)
        ▼
      ┌─────┐
      │ SCU │  gateway / protocol translator
      └─────┘
        │  SCU-internal field buses (never on the PIA wire)
        ├── LIN   (lin1, lin2, …)
        ├── CAN   (can2, …)
        ├── pin/GPIO (pin-6, pin-7, …)
        └── BLE   (HYMER Smart Sensors: tyre, gas-bottle, contact, temp/humidity)
        ▼
   heater · fridge · lights · satellite antenna · sensors · BLE accessories
```

Note that **BLE is a dual role** for the SCU: on the *upstream* side its BLE radio
is a **peripheral** exposing the NUS GATT service to the app/HA (this is the "BLE
(NUS)" transport above); on the *downstream* side the same radio acts as a **BLE
central** that pairs with HYMER Smart Sensors (HSS) — the wireless tyre-pressure,
gas-bottle, contact and temperature/humidity sensors — and aggregates their
readings into the very same PIA `(bus, slot)` space (buses 70/71/73/74). To us
these look like any other component; we never see the raw BLE advertisements, only
the decoded PIA slots.

We only ever speak PIA and address components logically as `(bus_id, slot_id)`.
The underlying LIN/CAN/pin/BLE wiring is never exposed as traffic — it only appears
as a **label**: PIA field 10 (`connectedComponentInstance`) carries strings such
as `lin1`, `lin2`, `can2`, `pin-6`, telling us which field bus a component is
physically wired to. The decoder even uses this label to tell apart components
that share the same `(bus, slot)` numbering — this is exactly how the multiple HSS
sensors on one BLE bus are separated into per-device entities; see
[`sensor-map.md`](sensor-map.md#pinned-sensor-mappings-and-auto-slot-templates-v2640)
(`connectedComponentInstance`).

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
- Without bonding, SCU ignores TLS ClientHello (20s timeout, no ServerHello)
- If already bonded at BlueZ level, `Pair()` is skipped entirely (v2.61.2+)
- Transient GATT failures on a bonded device retry once (2s delay) before
  clearing the bond (v2.61.1+) — prevents unnecessary bond destruction

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
| 23 (default) | 20 bytes | 63 chunks — works with 100ms WriteReq / 20ms WriteCmd pacing |
| 245 (EHG app) | 242 bytes | 6 chunks — works reliably |

The integration calls `_acquire_mtu()` on the bleak client to negotiate
a higher MTU, matching the EHG app behavior. However, on HA's RPi4 with
BlueZ D-Bus, the `MTU` property is often unavailable, leaving MTU at 23.

## GATT Write Pacing (v2.61.0+)

At MTU=23, large payloads require many 20-byte chunks. Without inter-chunk
pacing, the SCU's NUS RX buffer overflows after ~10-15 rapid chunks,
causing `ATT error: 0x0e (Unlikely Error)` and an immediate BLE disconnect.

| Write mode | Pacing | Used for |
|------------|--------|----------|
| Write With Response (WriteReq) | **100ms** per chunk | TLS handshake, PairMobileRequest (63 chunks) |
| Write Without Response (WriteCmd) | **20ms** per chunk | PIA subscriptions (70 chunks) |
| Any mode, ≤10 chunks | No pacing | Small PIA commands |

Pacing is only applied for writes with >10 chunks. The EHG app avoids this
problem by negotiating MTU=245 (6 chunks for PairMobileRequest), but the
integration works reliably at MTU=23 with the above pacing values.

**History:** WriteReq pacing was initially 10ms (ATT 0x0e after ~10 chunks),
increased to 50ms in v2.61.0, then to 100ms after vehicle testing showed
ATT 0x0e at chunk 16/18 of the 342-byte TLS CertificateVerify message
(2026-05-13). WriteCmd pacing was initially 5ms (ATT 0x0e after ~15 chunks),
increased to 20ms in v2.61.0.

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

Verified via live testing (2026-05-13): Without bonding, GATT connects, 5 services
discovered, `start_notify` on NUS TX succeeds, but TLS ClientHello is silently
ignored by the SCU (20s timeout). Bonding is mandatory for TLS.

## Token Delivery (confirmed via Hermes analysis)

The EHG refresh token is delivered in the BLE `PairMobileResponse`, NOT via
a cloud API call. Confirmed by string analysis of the Hermes JS bundle
(`index.android.bundle`, 13.4 MB):

- `setRemoteRefreshToken` — stores token after BLE response
- `remoteAccessRefreshToken` — protobuf field name
- `REMOTE_ACCESS_TOKEN_EXPIRED` — token lifecycle

> **Per-device tokens:** The token returned in `PairMobileResponse` is bound to
> the pairing device's BLE identity (the `mobileDeviceMac` / device name used in
> `PairMobileRequest`). Each BLE pairing therefore mints its **own** personal
> refresh token for the same vehicle — the official EHG app holds a different
> token than Home Assistant, and pairing a new device does not invalidate the
> tokens already issued to others (subject to the SCU's limited pairing slots
> below). The token is portable: the token-extractor APK pairs as its own device,
> and the extracted token is then **reused** in Home Assistant. Best practice is
> to keep one extracted token live on only one device at a time (uninstall the
> APK once HA has the token).

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
commands (lights, switches, heater, fridge, boiler, climate) and PIA
subscription requests.

### BLE Subscriptions

The coordinator sends the same 7 PIA subscription requests + refresh command
over BLE that SignalR uses (`build_subscription_requests()` + `build_refresh_command()`).
This should unlock all ~130 sensors via BLE — previously only ~28 were pushed
autonomously by the SCU without explicit subscriptions.

After the **initial setup** (OAuth2 login + EHG token exchange require internet),
BLE can operate fully offline — sensor streaming and control commands work
without any cloud connectivity.  A fresh installation always requires internet
for the OAuth2 handshake and `confirmationToken` API call during BLE pairing.

### Write Commands (REMOVED in v2.62.24)

> ⚠️ **Historical only.** As of v2.62.24 the integration **does not send any
> writes over BLE**. All commands (lights, switches, heater, fridge, boiler)
> are routed via the cloud / SignalR path with a single reconnect-retry on
> failure. The text below describes how the BLE write path was *intended* to
> work in v2.62.16 → v2.62.23 and is kept for archival / future-firmware
> reference. See the **Why removed** subsection at the end.

The coordinator's `_send_with_retry()` built the PIA protobuf payload locally
(same `build_light_command()` / `build_multi_sensor_command()` used for SignalR)
and sent it via `ble_client.send_pia_command(b64_payload)`, which:

1. Base64-decoded the payload
2. Wrapped it in a BLE PIA frame (magic `0xA0CB` + length + CRC32)
3. Encrypted via TLS
4. Wrote to NUS RX (`6e400002`) using chunked GATT writes

The SCU was expected to process the command identically to a cloud-received
PIA command and respond via NUS TX notifications. In practice (SCU firmware
1.12.0.0) **no SCU state change ever occurred from a BLE write**, regardless
of TLS handshake quality, ACK timeout, or `connectedComponentInstance`
(CCValue field 10).

#### ACK-based Cloud Safety Net (historical)

v2.62.17 → v2.62.23 waited up to a tunable timeout (1.5–5.0 s, default 2.5 s)
for the SCU to echo back a PIA response after a BLE write, then fell back to
cloud. Post-mortem analysis showed the apparent BLE ACKs were in fact cloud
echoes relayed back over BLE ~500 ms after the SignalR send — the BLE write
itself never reached the SCU's command handler.

#### Why removed

> ⚠️ **Superseded by v2.66.0/v2.67.0 — the conclusion below was wrong.** The
> "SCU silently drops every BLE write" verdict was a **client-side encoding
> bug**, not a firmware limit: our command encoders wrapped the PIA `Request`
> in protobuf **field 2** (the cloud/DataHub envelope), but over BLE the frame
> payload *is* `BleProtocol`, where field 2 = `response`. The SCU therefore
> parsed every write as a *response*, matched no outstanding request, and
> discarded it without an ACK. Root cause found by **Dan Simms**. The BLE write
> path was re-enabled in v2.66.0 (field-1 rewrap + write-with-response +
> request_id ACK) and **confirmed working on a Grand Canyon S 600, SCU fw
> 1.12.0.0** — the exact firmware the text below calls impossible. As of
> v2.67.0 it is on by default with automatic cloud fallback. The
> "0/5 writes" test below failed because those builds were still emitting the
> field-2 envelope; the ACK safety net was also reading cloud echoes, not BLE
> ACKs, which masked the real cause. Kept for historical context only.

Decisive test (2026-05-21, SCU firmware 1.12.0.0, cloud fallback OFF, ACK
timeout 4 s): **0/5** writes accepted across the fridge (bus 34), Truma
heater (bus 58) and lights (buses 12/19). The EHG app on LTE confirmed no
SCU state change. Conclusion: the SCU silently drops every inbound BLE
`setValues` frame on this firmware. The BLE write path has been removed
from `coordinator._send_with_retry`; the PIA encoder, `send_pia_command`,
and the instance-cache seeder remain in place so the BLE-first leg can be
restored as a localised change if a future SCU firmware unlocks BLE writes.

#### Cross-check: official EHG Android app uses the same path

After v2.62.24 shipped we ran a parallel jadx investigation of the EHG
Android app (`com.ehg.hymerconnect`) to determine whether HA was missing a
separate write characteristic, an additional GATT service, or a different
payload envelope. The decompiled Java/Kotlin sources confirm:

- EHG writes to the **same NUS RX characteristic `6e400002`** we used.
- It writes the **same opaque TLS-encrypted PIA bytes** — payload assembly
  happens entirely on the JavaScript / Hermes side and the native code just
  passes the encrypted blob through the standard Nordic BLE library.
- There is no hidden second GATT service, no separate write characteristic,
  and no extra outer envelope.

In other words, the official EHG app is equally affected by the SCU's
firmware-side drop — it only *appears* to work because phones almost always
have LTE available, so the EHG app silently falls back to the cloud just
like our pre-v2.62.24 builds did. Our cloud-only v2.62.24/v2.62.25 is
therefore the correct and only viable architecture for SCU firmware
1.12.0.0. **If a future firmware update unlocks BLE writes, both the EHG
app and this integration should benefit at the same time** — watch user
reports.

#### BLE write code path version history (for restoration)

For anyone restoring the BLE-first leg in a future release, the last
tag with the full BLE write code path was **`v2.62.23`** (commit `e0c0477`).

| Version  | Date       | BLE write change |
|----------|------------|------------------|
| v2.62.16 | 2026-05-20 | BLE write code path active. ACK timeout hardcoded 1.5 s. |
| v2.62.17 | 2026-05-20 | ACK timeout 1.5 s → 3.0 s; bus/slot-keyed ACK matching. |
| v2.62.18 | 2026-05-20 | `cloud_fallback` option added (`CONF_CLOUD_FALLBACK`, default `True`). |
| v2.62.19 | 2026-05-20 | Added CCValue field 9 (`connectedComponentIndex`) — **wrong**, reverted in v2.62.20. |
| v2.62.20 | 2026-05-20 | Reverted field 9; added BLE wire hex-dump logging. |
| v2.62.21 | 2026-05-21 | Per-bus `_BUS_INSTANCE_CACHE`; emit CCValue field 10 (`connectedComponentInstance`). |
| v2.62.22 | 2026-05-21 | User-tunable ACK timeout (`CONF_BLE_ACK_TIMEOUT`, default 2.5 s, range 1.0–5.0 s). |
| v2.62.23 | 2026-05-21 | `_seed_instance_cache_walk()` populates cache from both cloud and BLE PIA frames. **Last release with a BLE write code path.** |
| v2.62.24 | 2026-05-21 | **BLE write path removed.** Cloud-only writes. `cloud_fallback` + `ble_ack_timeout` options deprecated (still in `const.py`). |
| v2.62.25 | 2026-05-21 | Removed deprecated BLE-write constants from `const.py`; cosmetic log cleanup. |
| v2.66.0  | 2026-08-22 | **BLE write path restored** behind an opt-in option. Root cause was the field-2 (cloud) vs field-1 (BLE) `BleProtocol` envelope — writes now rewrap to `BleProtocol.request` + write-with-response, judged on a real `request_id` ACK. Credit: Dan Simms. |
| v2.66.2  | 2026-08-22 | Same field-1 rewrap applied to the BLE subscription/refresh path. |
| v2.67.0  | 2026-08-23 | BLE write path **confirmed on a Grand Canyon S 600 (fw 1.12.0.0)** and turned **on by default** (opt-out), with automatic cloud fallback. |

Configurable BLE write options that existed in v2.62.18 → v2.62.23:

- `cloud_fallback` (boolean, default `True`) — if `False`, BLE-only mode; if
  `True`, fall back to cloud on ACK timeout.
- `ble_ack_timeout` (float seconds, default 2.5, range 1.0–5.0) — how long
  to wait for a BLE PIA response before falling back to cloud.

Both keys were deleted in v2.62.25. Home Assistant silently ignores unknown
keys in saved options dicts, so old config entries continue to load.

Code still present in the codebase that would help a future restoration:

- `pia_decoder.build_light_command()` / `build_multi_sensor_command()` — PIA
  payload encoders, used for cloud writes today; identical bytes work on
  BLE if the firmware ever cooperates.
- `pia_decoder._BUS_INSTANCE_CACHE` + `_seed_instance_cache_walk()` —
  passively primed from every inbound PIA frame; would feed CCValue
  field 10 (`connectedComponentInstance`) into outbound writes.
- `ble_client.send_pia_command()` — BLE PIA framing + TLS encrypt + GATT
  write to NUS RX; still wired and used for nothing today.
- `coordinator._send_via_ble()` — currently a no-op stub returning `False`;
  reinstate the original body to bring BLE writes back.

Restoration cost is a localised change in `coordinator._send_with_retry`
plus re-adding the two Options fields — ≈150 lines total.

## Credits

- **Dan Simms** (`dan-simms1/hymer-connect-ha`) — PairMobileRequest/Response
  protobuf field layout, BLE pairing ceremony, `hymer_token_tool`
- **HYMER helpcenter video** — CONNECTION button flow
- **Nordic Semiconductor** — [NUS specification](https://docs.nordicsemi.com/bundle/ncs-latest/page/nrf/libraries/bluetooth/services/nus.html),
  [GATT Latency Client](https://docs.nordicsemi.com/bundle/ncs-latest/page/nrf/libraries/bluetooth/services/latency_client.html)
