# EHG App — BLE Protocol & Decompilation Analysis

> **Audience:** Maintainers and reverse-engineering contributors. Normal users
> can safely skip this file.

Findings from reverse-engineering the HYMER Connect (EHG) Android app v2.10.14.  
Source: `source/androidapp/com.ehg.hymerconnect/` (APK) and `_jadx_output/` (decompiled).

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
    // field ??: mobileDevices (response to getPairedMobileDevices)
    // field ??: nonCommunicatingComponents
    // field ??: fotaState
    // field ??: metadata
}
```

### UserRequestTopic (Request field 8) — All Known Commands

| Field # | Command | Sub-fields | Status |
|---------|---------|------------|--------|
| 4 | `pairMobileDevice` | activation_token(1), confirmation_token(2), mobile_device_name(3), wait_for_confirmation(4) | **Confirmed & implemented** |
| 6 | `pairMobileDeviceConfirmation` | success(1) | **Confirmed & implemented** |
| ?? | `getPairedMobileDevices` | (none?) | Found in app, field # unknown |
| ?? | `deleteMobileDevices` | mobileDeviceMac (string) | Found in app, field # unknown |
| ?? | `deleteUser` | (unknown) | Found in app, field # unknown |
| ?? | `deleteAllUsers` | (unknown) | Found in app, field # unknown |

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
4. `deleteMobileDevices` takes a `mobileDeviceMac` (string) parameter

### SCU Pairing Behavior

- SCU remembers paired device **names** (not just MAC addresses)
- Re-sending `PairMobileRequest` with an already-paired name → empty response (no `mobilePair` field) or timeout
- SCU has limited pairing slots (likely 4–5 devices)
- Each paired device gets its **own personal refresh token** — pairing a new device (e.g. the token-extractor APK alongside the official EHG app) does not invalidate the tokens of the others. The extracted token is portable and is reused in Home Assistant, but keep one token live on only one device at a time
- Fix: use unique device name per attempt (`ha-{timestamp}`)

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
individual paired BLE devices. The only way to access it is via the PIA protobuf
`getPairedMobileDevices` / `deleteMobileDevices` commands (field numbers unknown).

"Verbindung trennen" removes the **entire vehicle** from the user's account —
it does NOT selectively remove a single paired BLE device.

## Extracting Unknown Field Numbers

The protobuf field numbers for `deleteMobileDevices`, `getPairedMobileDevices`, etc.
are embedded in compiled Hermes bytecode. Three approaches to extract:

1. **Hermes decompiler** — use `hbcdump` or `hermes-dec` on `index.android.bundle`
   - **Done:** the decompiled bundle now lives at `source/androidapp/_archive_old_app/_hermes_decompiled/index.js` and was used to extract the full `(componentId, slot)` catalog (labels/modes/datatypes/enums) — see [`ehg-app-metadata.md`](ehg-app-metadata.md). The device-management field numbers (`deleteMobileDevices` / `getPairedMobileDevices`) were still not pinned down from it.
2. **MITM capture** — intercept BLE traffic while the EHG app performs an unpair
3. **Brute-force** — try field numbers 1–15 systematically (protobuf fields are small integers)

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