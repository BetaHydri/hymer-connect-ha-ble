# EHG App — BLE Protocol & Decompilation Analysis

> **Audience:** Maintainers and reverse-engineering contributors. Normal users
> can safely skip this file.

Findings from reverse-engineering the HYMER Connect (EHG) Android app v2.10.14.  
Source: `source/androidapp/com.ehg.hymerconnect/` (APK) and `_jadx_output/` (decompiled).

> This file documents the **wire format** of the pairing ceremony. The user-facing
> token lifecycle (which token is which, per-device minting, pairing-slot limits)
> lives in [`ehg-token-and-pairing.md`](ehg-token-and-pairing.md).

## App Architecture

| Layer | Technology | Location | Responsibility |
|-------|-----------|----------|----------------|
| Native BLE | Java (Android) | `SmartCaravanBleManager.java` | GATT connect, MTU, NUS read/write, bonding, power service |
| Bridge | React Native | `NativeBleModule.java` | JS ↔ Java via `LocalBroadcastManager` |
| Protocol | Hermes JS bytecode | `assets/index.android.bundle` (13.4 MB) | TLS 1.0/1.1, PIA protobuf, pairing logic, token storage |

The Java layer is a **pure byte pipe** — no TLS, no protobuf, no pairing logic.  
All protocol intelligence is in the compiled Hermes bundle.

## SmartCaravanBleManager.java — Key Reference

**Path:** `_jadx_output/sources/com/app/modules/ble/support/SmartCaravanBleManager.java`

### GATT Service & Characteristic UUIDs

| Service | UUID | Purpose |
|---------|------|---------|
| Nordic UART (NUS) | `6E400001-B5A3-F393-E0A9-E50E24DCCA9E` | Data exchange (PIA over TLS) |
| Power Service | `FFF40001-13C9-42F3-9D46-E1D1AA2A7232` | SCU power & bonding |
| Device Info (SIU only) | `0000180A-0000-1000-8000-00805F9B34FB` | SIU manufacturer/model/firmware |

| Characteristic | UUID | Properties | Notes |
|----------------|------|------------|-------|
| NUS RX (RPi→SCU) | `6E400002-...` | Write, WriteWithoutResponse | Data to SCU |
| NUS TX (SCU→RPi) | `6E400003-...` | Notify (SCU), Indicate (SIU) | Data from SCU |
| Power State | `FFF40002-...` | Read, Notify | SCU power state |
| Power Control | `FFF40003-...` | Write | Wake-up command (`0x0A`) |
| Bonding State | `FFF40004-...` | Read, Write | Challenge-response (only visible after bonding) |
| Manufacturer Name | `00002A29-...` | Read | SIU only |
| Firmware Revision | `00002A26-...` | Read | SIU only |
| Model Number | `00002A24-...` | Read | SIU only |

### Key Behaviors

- **MTU**: `requestMtu(245)` on init, `overrideMtu(245)` on Android 14+
- **Write mode**: `setWriteType(2)` = Write Without Response + `.split()` for auto-chunking
- **Connection**: `connect().retry(3, 100)` — 3 retries, 100ms between
- **Wake-up**: Write `0x0A` to Power Control characteristic
- **Smart Unit Type**: `SCU` (NUS + Power service) vs `SIU` (NUS + Device Info) vs `NONE`
- **SCU uses Notify**, SIU uses **Indicate** for UART TX

### Bonding State Check (fff40004)

```
1. Write 4 random bytes to fff40004
2. Read back from fff40004
3. Response bytes[0:4] = echo of challenge (validation)
4. Response byte[4] = bonding state
   - 0 = not in pairing mode (CONNECTION not pressed)
   - non-zero = pairing mode active
```

**Important:** `fff40004` is only visible after OS-level BLE bonding.  
Without bonding, GATT service discovery does NOT expose this characteristic.

## PIA Protobuf Protocol — Message Structure

### BLE PIA Frame Format

```
┌──────────┬───────────────┬──────────┬─────────────────┐
│ Magic    │ Payload Len   │ CRC32    │ Payload         │
│ 0xA0CB   │ 4 bytes BE    │ 4 bytes  │ Protobuf        │
│ (2 bytes)│               │          │                 │
└──────────┴───────────────┴──────────┴─────────────────┘
```

CRC32 is computed over (header with zeroed CRC field) + payload.

### Request Envelope (BleProtocol.request, field 1)

```protobuf
message Request {
    uint32 request_id = 1;       // random 1–10,000,000
    string version = 2;          // "v0.32.0"
    uint64 timestamp = 3;        // epoch seconds
    // field 4-7: other topics (unknown)
    UserRequestTopic user = 8;
    CommandRequestTopic command = 9;  // also used for refresh (empty bytes)
}
```

### Response Envelope (BleProtocol.response, field 2)

```protobuf
message Response {
    uint32 request_id = 1;
    uint32 status = 2;
    uint64 timestamp = 3;
    // field 4-8: other fields
    PairMobileResponse mobilePair = 9;
    MobileDevices mobileDevices = 10;   // reply to getPairedMobileDevices (UserRequestTopic field 5)
    // field ??: nonCommunicatingComponents
    // field ??: fotaState
    // field ??: metadata
}
```

### UserRequestTopic (Request field 8) — All Commands

All field numbers below were **resolved from the decompiled protobuf codec** (`UserRequestTopic.encode()`
wire tags, `field = tag >> 3`), self-validated by the three already-known values (4, 6, and
`Response.mobilePair` = 9). "Resolved" = schema-verified; the `delete*` / `getPaired*` commands are **not
yet confirmed against a live SCU** (the app never calls them, so there was no reference implementation).

| Field # | Command | Payload | Status |
|---------|---------|---------|--------|
| 1 | `deleteUser` | `User` message | Resolved (codec) — unverified live |
| 2 | `deleteAllUsers` | `google.protobuf.Empty` | Resolved — app's "Remove all users" sends this |
| 3 | `deleteMobileDevices` | `User` message (targets in `User.devices`) | **Implemented** (gated HA unpair UI) — SCU ACKs `status=1` but **silently discards** on every firmware tested so far (#26); no per-device removal yet |
| 4 | `pairMobileDevice` | activation_token(1), confirmation_token(2), mobile_device_name(3), wait_for_confirmation(4) | **Confirmed & implemented** |
| 5 | `getPairedMobileDevices` | `google.protobuf.Empty` (no args) — read-only | **Confirmed & implemented** (live-verified 2026-08-28) |
| 6 | `pairMobileDeviceConfirmation` | success(1) | **Confirmed & implemented** |

### CommandRequestTopic (Request field 9) — All Known Commands

| Field # | Command | Sub-fields | Status |
|---------|---------|------------|--------|
| 1 | `btleBonding` | (unknown) | Found in app |
| 2 | `restart` | cold(1) = bool | **Confirmed & implemented** |
| ?? | `factoryReset` | (unknown) | Found in app |
| ?? | `telemetryErase` | (unknown) | Found in app |
| ?? | `realtimeMode` | (unknown) | Found in app |
| ?? | `troubleshootReport` | (unknown) | Found in app |

### PairMobileResponse (Response field 9)

```protobuf
message PairMobileResponse {
    string remote_access_token = 1;          // short-lived
    string remote_access_refresh_token = 2;  // LONG-LIVED (the EHG token)
    bool confirmation_required = 3;
}
```

### Device-management message shapes (resolved from decompiled codec)

```protobuf
// Payload of deleteUser (field 1) and deleteMobileDevices (field 3)
message User {
    string uuid = 1;                     // EHG USER ACCOUNT id (not a device id)
    bool   isMainUser = 2;
    string expireDate = 3;
    repeated MobileDevice devices = 4;   // for deleteMobileDevices: the device(s) to remove
}

message MobileDevice {
    string mobileDeviceMac  = 1;         // ha-xxxxx entries: = host BT controller (hci0) address, not random
    string mobileDeviceName = 2;         // e.g. "ha-12345"
    string userUuid         = 3;         // the OWNING user account — MANY devices can share one uuid
}

// Reply carried in Response.mobileDevices (field 10), from getPairedMobileDevices (field 5)
message MobileDevices {
    repeated MobileDevice values = 1;
}
```

## Unpair / Device Management Flow

### EHG App UI (from i18n string keys)

| String Key | Purpose |
|------------|---------|
| `UNPAIR_MODAL.TEXT_WITH_NAME` | Confirm dialog showing device name |
| `UNPAIR_MODAL.CONFIRM_LABEL` | Confirm button |
| `UNPAIR_MODAL.CANCEL_LABEL` | Cancel button |
| `UNPAIR_MODAL.SUCCESS_TEXT` | Unpair succeeded |
| `UNPAIR_MODAL.FAILED_TEXT` / `FAILED_HEADER` | Unpair failed |
| `UNPAIR_MODAL.NO_BLE_TEXT` | **BLE required** for unpair |
| `UNPAIR_MODAL.NO_INTERNET_TEXT` | Internet also required |
| `UNPAIRING_SCU` | SCU must be in active state (12V on) |
| `UNPAIR_PREVENTED_MESSAGE` | Some conditions prevent unpair |
| `UNPAIR_SUCCESS_MESSAGE` | Success confirmation |

### Key Functions (from Hermes string table)

| Function | Purpose |
|----------|---------|
| `unpairVehicle` | Main unpair entry point |
| `unpairDevice` | Device-level unpair |
| `handleUnpairSuccessModalDismissed` | Success handler |
| `onUnpaired` | Callback after unpair |
| `requestDeleteAllUsers` | Sends deleteAllUsers PIA command |
| `deletePairedDeviceEmulator` | Mock/emulator for testing |

### Requirements for Unpairing

1. **BLE connection required** — cannot unpair via cloud/SignalR alone
2. **SCU must be active** (12V on, not in standby)
3. **Internet required** — possibly for cloud-side cleanup
4. `deleteMobileDevices` (UserRequestTopic field 3) takes a **`User`** message whose `devices` list holds the `MobileDevice{mobileDeviceMac, mobileDeviceName, userUuid}` entries to remove — **not** a bare MAC string

> **Catch-22 for full-table SCUs.** Because unpair runs *over* BLE (no cloud
> fallback), it needs a **stable** bonded session. If the pairing table is already
> full and the SCU **rejects/drops the BLE session within seconds**, you can't use
> the in-HA unpair to free a slot — freeing the slot needs the very link the full
> table is refusing. Same for deep-standby / weak-signal drops. Work-arounds: wake
> the SCU fully and retry close-up (one delete may squeeze through), unpair from an
> already-paired phone via the EHG app, or a HYMER support/factory pairing reset. See
> [`ehg-token-and-pairing.md`](ehg-token-and-pairing.md#paired-ble-device-management-in-home-assistant).

### SCU Pairing Behavior

- SCU remembers paired device **names** (not just MAC addresses)
- Re-sending `PairMobileRequest` with an already-paired name → empty response (no `mobilePair` field) or timeout
- SCU has limited pairing slots — a small table; **at least 7 confirmed on firmware ASW 1.49.7** ([issue #25](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/25); no rejection seen to mark the ceiling, so the earlier "4–5" was only an estimate). A **full** table rejects new pairings
- `userUuid` identifies the **EHG user account**, not the device — one account can hold several slots, and a second enrolled account (guest access) carries a different uuid. Unpair by device (MAC/name), not by uuid
- Each paired device gets its **own personal refresh token** — pairing a new device (e.g. the token-extractor APK alongside the official EHG app) does not invalidate the tokens of the others. The extracted token is portable and is reused in Home Assistant, but keep one token live on only one device at a time
- Name strategy: **stable, reused device name** (`ha-xxxxx`) — since **v2.95.6** the name is generated once on the first successful pair, persisted in the config entry (`ble_pair_name`) and reused on every re-pair, so the SCU always sees the same `(MAC, name)` slot and recognises the returning host instead of piling up a new slot each time (before v2.95.6 a fresh random `ha-{timestamp}` was sent per attempt). This matters because per-device removal is not honoured on any tested firmware ([issue #26](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/26)), so orphaned slots cannot be cleaned up individually.

## EHG App UI — Vehicle Management

### "Mein Fahrzeug" Menu

| Menu Item | Purpose |
|-----------|----------|
| Gastzugänge | **User account invitations** (family members). NOT BLE device management. Max 2 devices per person. |
| Service Termine | Service appointments |
| Meine Ausstattung | Vehicle equipment/options |
| Benachrichtigungen | Push notification settings |
| Systemaktivierung und Neustart | SCU restart / activation |
| Connect Smart Unit | BLE pairing entry point |
| Verbindung trennen | **Nuclear option** — disconnects ENTIRE vehicle from account (all users, all devices) |
| Sensor hinzufügen | Add external BLE sensors |

### Important: No BLE Device Management UI

The SCU's internal paired BLE device list (e.g. "homeassistant", "ha-xxxxx") is
**not visible anywhere** in the EHG app. There is no UI to selectively delete
individual paired BLE devices. The only way to reach it is via the PIA protobuf
`getPairedMobileDevices` (UserRequestTopic field 5) / `deleteMobileDevices` (field 3)
commands — which **the app itself never calls** (they exist in its protobuf schema but
have zero call-sites), so there is no in-app path, hidden or otherwise.

"Verbindung trennen" removes the **entire vehicle** from the user's account —
it does NOT selectively remove a single paired BLE device.

> **This integration now provides the per-device UI the app lacks.** Since
> v2.94.0 a read-only "Log paired BLE devices" button + `sensor.*_paired_ble_devices`
> expose the list, and since v2.95.0 a two-step **`select` (pick) + `button`
> (approve)** sends a gated single-device `deleteMobileDevices` over BLE — intended to
> free one pairing slot, though the SCU firmwares tested so far ACK it and silently
> discard it (#26), so it does not yet actually remove a device. All BLE-only (cloud
> replies `ACCESS_DENIED`). See
> [`ehg-token-and-pairing.md`](ehg-token-and-pairing.md#paired-ble-device-management-in-home-assistant).

## Device-Management Field Numbers — RESOLVED (2026-08-28)

The `getPairedMobileDevices` / `deleteMobileDevices` (and `deleteUser` / `deleteAllUsers`)
field numbers were **resolved by reading the protobuf codec directly** out of the decompiled
Hermes bundle (`source/androidapp/_archive_old_app/_hermes_decompiled/index.js`). The
`UserRequestTopic.encode()` function writes each sub-field's wire tag; `field = tag >> 3`.
The extraction is self-validating — the three already-known values (`pairMobileDevice` = 4,
`pairMobileDeviceConfirmation` = 6, `Response.mobilePair` = 9) all came out correct.

| Command | field | wire tag | payload |
|---------|-------|----------|---------|
| `deleteUser` | 1 | 10 | `User` |
| `deleteAllUsers` | 2 | 18 | `Empty` |
| `deleteMobileDevices` | 3 | 26 | `User` (targets in `User.devices`) |
| `pairMobileDevice` | 4 | 34 | `PairMobileRequest` |
| `getPairedMobileDevices` | 5 | 42 | `Empty` (no args) |
| `pairMobileDeviceConfirmation` | 6 | 50 | `PairMobileConfirmation` |

The `getPairedMobileDevices` reply comes back in **`Response.mobileDevices` (field 10)** =
`MobileDevices { repeated MobileDevice values = 1 }`.

> **MITM capture is no longer needed for these two.** Because the app never calls
> `getPairedMobileDevices` / `deleteMobileDevices` (schema-only, zero call-sites), a traffic
> capture of the EHG app could never have revealed them anyway — the numbers came from the
> compiled schema. **Live status (2026-08-28):** `getPairedMobileDevices` (field 5) is now
> **confirmed on a real SCU** — it returned the full paired list (`Response.mobileDevices`,
> field 10) over BLE, and is shipped as the "Log paired BLE devices" button +
> `sensor.*_paired_ble_devices`. `deleteMobileDevices` (field 3) is **implemented** behind the
> gated `select` + `button` unpair UI, but its live result is now confirmed **negative**: on
> two different vehicles/firmwares (retrofit ASW 1.49.7 in #26 and a factory Grand Canyon
> S 600) the SCU returns `status=1` yet **silently keeps the device** — full-record and
> MAC-only delete frames were both ACKed and discarded, table unchanged. v2.95.2 verifies
> and reports "Slot NOT freed"; no firmware honouring per-device removal has been seen yet.
> Both are BLE-only; over cloud the SCU replies `ACCESS_DENIED` (status 5).

> ⚠️ **Do NOT blindly sweep field numbers 1–15.** `deleteUser` (1) and `deleteAllUsers` (2)
> live in the same `UserRequestTopic`, so a range sweep will delete users / wipe the account.
> `tools/scan_pia_fields.py` is single-shot: it sends exactly one deliberately-chosen field,
> refuses without `--i-understand-may-be-destructive` (except the read-only `--getpaired`
> shortcut), and blocks the pairing (4/6) and restart (command 2) fields.

## Other BLE Module Files (jadx output)

| File | Purpose |
|------|---------|
| `NativeBleModule.java` | React Native bridge (JS ↔ Java BLE) |
| `BondingObserverCallback.java` | Bond state broadcasts (11=bonding, 12=bonded, 10=failed) |
| `ConnectionObserverCallback.java` | Connection state tracking, SIU RSSI events |
| `BLEUtils.java` | UUID construction from short values, random byte generation |
| `BleDeviceStorage.java` | Device persistence |
| `Scanner.java` | BLE device scanning |
| `DeviceBleConfig.java` | Device BLE configuration |
| `SmartUnitType.java` | Enum: `SCU`, `SIU`, `NONE` |

**Path prefix:** `_jadx_output/sources/com/app/modules/ble/`