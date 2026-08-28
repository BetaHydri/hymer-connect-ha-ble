# EHG token & BLE pairing

> **Audience:** All users. This is the single, authoritative explanation of the
> two tokens involved, how each setup path obtains the long-lived one, why every
> pairing mints its own token, and the SCU's pairing-slot limits. Other documents
> link here instead of repeating it.

Setup and daily operation are covered elsewhere — this file only owns the
**token model** and the **pairing concepts**:

- New install, step by step → [`quick-start.md`](../quick-start.md)
- BLE setup, Configure vs Reconfigure, log reading, host tuning →
  [`ble-troubleshooting.md`](ble-troubleshooting.md)
- Cloud connection lifecycle and token refresh timing →
  [`signalr-connection.md`](signalr-connection.md)
- BLE wire protocol (NUS/GATT, TLS-over-BLE, PIA framing) →
  [`ble-communication.md`](ble-communication.md)
- Decompiled pairing ceremony / protobuf field layout →
  [`ehg-app-ble-protocol.md`](ehg-app-ble-protocol.md)

## Which token is which? (read this first)

Two different tokens are involved, and confusing them is the most common setup
mistake. **The dealer QR activation token is NOT the EHG refresh token.**

- **Dealer QR activation token** — a code from your **dealer handover paperwork**
  (a paper document, *not* a sticker on the vehicle). You provide it during BLE
  pairing (Path A) or the Android app (Path C) to prove physical access to the
  vehicle. It *is* saved in the config entry and reused automatically on a
  re-pair, but it only bootstraps the pairing — it is **not** the token that
  authenticates the cloud / SignalR connection. **Since v2.91.3 the Reconfigure
  form no longer pre-fills it, so you cannot read the code back there** — keep
  your handover paperwork.
- **EHG refresh token** — the long-lived token the integration actually stores
  and uses for cloud / SignalR data (`ett=access-refresh`, no expiry). You never
  paste the QR code as the refresh token; the refresh token is **obtained for
  you** by BLE pairing (Path A), captured with mitmproxy (Path B), or extracted
  by the Android app (Path C).

**In short: the QR code helps *obtain* the refresh token — it is not the refresh
token itself.**

This is why the config flow treats the QR token as **optional**: it is only
required (and only enforced) when you enable the BLE data path (Path A).
Cloud-only setups (Paths B and D) supply the refresh token directly, so the flow
lets you leave the QR field empty and falls back to cloud-only mode.

## All token types

The integration juggles **four** tokens. Confusing them causes silent failures.
Only the **EHG refresh token** is something you obtain and keep; the rest are
minted and refreshed automatically.

| Token | `ett` | Source | Lifetime | Used for |
| --- | --- | --- | --- | --- |
| OAuth2 access token | — | `POST /api/v2/oauth/token` (username + password) | ~1 h | REST API calls, SignalR `UpdateTokens` `accessToken` |
| OAuth2 refresh token | — | same endpoint | long-lived | refreshing the OAuth2 access token on a 401 |
| SignalR negotiate JWT | — | `POST scc-appcomm/datahub/negotiate` | ~1 h | the WebSocket URL `access_token` parameter |
| **EHG remote-access refresh token** | **`access-refresh`** | **BLE pairing / capture (this is the one you obtain)** | **never expires** | minting the EHG remote-access token below |
| EHG remote-access token | `access` | `POST /api/ehg/v1/vehicles/{urn}/remoteAccessToken` | ~15–30 min | SignalR `UpdateTokens` `ehgAccessToken` — required for remote commands |

Refresh timing (managed for you):

- **OAuth2 access token** — auto-refreshed on a 401 via the API retry path.
- **SignalR negotiate JWT** — not refreshable; the connection is proactively
  recycled at ~50 min before it expires.
- **EHG remote-access token** — refreshed every ~15 min while connected.

Once obtained, the **EHG refresh token** is stored in the config entry and
**survives HACS updates**.

## How each path obtains the refresh token

| Path | Use when | How the refresh token is obtained |
| --- | --- | --- |
| **A — BLE + Cloud** | HA host has Bluetooth and you are at the vehicle | The integration pairs over Bluetooth (press **CONNECTION**) and extracts the token automatically. |
| **B — Cloud-only (mitmproxy)** | No BLE on the HA host; capture the token manually | Capture it from EHG app traffic — see [`../tools/README.md`](../tools/README.md). |
| **C — Cloud-only (Android app)** | No BLE, but you have an Android phone + vehicle access | The [token-extractor APK](https://github.com/BetaHydri/hymer-connect-ha-ble/releases/latest/download/ehg-token-extractor.apk) pairs on your phone and shows the token to copy/paste (**Android-only**; sideload, use once, then uninstall). |
| **D — Bootstrap only** | Create the config entry now, add the token later | Login only; add the token via Reconfigure afterwards. |

> **📵 Apple / iOS is not supported for token acquisition.** The token-extractor
> app is Android-only, and the mitmproxy method (Path B) has only ever been
> validated by capturing traffic from an Android device. If you only own Apple
> devices, borrow or use a spare Android phone to obtain the token once.

## Per-device tokens & pairing slots

The token returned in the pairing response (`PairMobileResponse`) is **bound to
the pairing device's BLE identity** (the device name / MAC used in the pairing
request). This has practical consequences:

- **Every BLE pairing mints its own personal refresh token.** The EHG app on your
  phone holds a different token than Home Assistant, and pairing a new device does
  **not** invalidate the tokens already issued to other devices.
- **Cloud-only paths reuse a helper device's token.** With Path B (mitmproxy) or
  Path C (the APK) you pair a helper device and **reuse that same token** in Home
  Assistant. Best practice: don't keep one extracted token live on more than one
  device at a time — uninstall the APK once HA has the token.
- **Path A mints its own token.** A HA host in the vehicle with BLE hardware pairs
  directly and mints its **own** token, so no reuse is involved.
- **The SCU has a limited number of pairing slots** (typically ~4–5 devices) and
  remembers paired device **names**, not just MAC addresses. Re-sending a pairing
  request with an already-paired name returns an empty response; the integration
  uses a unique device name per attempt to avoid this.

There is no UI in the EHG app to list or delete the SCU's individual paired BLE
devices. "Verbindung trennen" in the app removes the **entire vehicle** from the
account (all users, all devices) — it is not a per-device unpair. This integration
fills that gap over BLE (see below).

## Paired BLE device management (in Home Assistant)

The integration exposes the SCU's internal paired-device table — which the EHG app
never shows — through three entities (all **BLE-only**; over the cloud path the SCU
replies `ACCESS_DENIED`, so they only work while a bonded BLE session is up with the
SCU awake / 12V on). All are **disabled by default** — enable them in the entity
settings when you need them.

| Entity | Type | What it does |
| --- | --- | --- |
| `sensor.*_paired_ble_devices` | sensor (diagnostic) | State = **count** of paired devices; `attributes.devices` = the full list (`name` / `mac` / `uuid`). Auto-populates a few seconds after a BLE connect. |
| `button.*_log_paired_ble_devices` | button (diagnostic) | Read-only refresh — re-reads the list (`getPairedMobileDevices`) and writes it to the log and the sensor. Never changes anything. |
| `select.*_ble_device_to_unpair` | select (config) | **Records** which device you want to remove. Picking an option does **not** touch the SCU. |
| `button.*_unpair_selected_ble_device` | button (config) | **Approves + executes** the removal of the selected device (`deleteMobileDevices`). **Destructive** — frees one pairing slot. |

**Two-step unpair (deliberate by design).** Removing a device is split so it cannot
happen by accident: the **select only records** the target, and the separate
**"Unpair selected BLE device" button** performs the actual deletion. That button is
disabled by default and only becomes *available* when BLE is connected **and** a
device is selected. To free a slot: enable the entities → make sure BLE is connected
(SCU awake) → pick the device in the select → press **Unpair selected BLE device**.
The list refreshes automatically and the freed slot is then available for a new
pairing. This is the intended way to clear a stale `ha-xxxxx` entry when the SCU's
pairing table is full and rejects new pairings.

**Where to use them — no template needed.** These are ordinary entities, not
template helpers. After you enable them you can operate them straight from the
**device page** (Settings → Devices & Services → your HYMER device) or from
**Settings → Entities** — the select shows a dropdown and the button has a press
action there. Putting them on a dashboard is **optional**; if you want a card, add a
standard **Entities** (or **Tile**) card — no YAML templating and no Jinja. The
select's option list is filled by the integration, not by a template. Example card:

```yaml
type: entities
title: HYMER — paired BLE devices
entities:
  - entity: sensor.hymer_paired_ble_devices
  - entity: button.hymer_log_paired_ble_devices
  - entity: select.hymer_ble_device_to_unpair
  - entity: button.hymer_unpair_selected_ble_device
```

> Adjust the entity IDs to match your install (they are prefixed with your device
> name). The unpair button stays greyed-out until BLE is connected **and** a device
> is selected in the dropdown.

The underlying protocol commands are `getPairedMobileDevices` (UserRequestTopic
field 5, read-only — **live-confirmed 2026-08-28**) and `deleteMobileDevices`
(field 3, a `User{devices:[…]}` payload). See
[`ehg-app-ble-protocol.md`](ehg-app-ble-protocol.md).

## Verified BLE hardware

The BLE dual-path is verified on **Raspberry Pi 4** hardware using the Pi's
built-in Bluetooth adapter (no external dongle required). **Since v2.91.8 an
external USB Bluetooth dongle / any second adapter (`hci1`, …) also works** — the
integration resolves the SCU's BlueZ device path across all adapters instead of
assuming `hci0`. Other BLE-capable HA hosts should work but are unverified. For
host requirements (`bluetoothctl`, legacy TLS) and recovery steps, see
[`ble-troubleshooting.md`](ble-troubleshooting.md).

## See also

- [`quick-start.md`](../quick-start.md) — the four setup paths, step by step
- [`ble-troubleshooting.md`](ble-troubleshooting.md) — BLE setup and log reading
- [`../tools/README.md`](../tools/README.md) — token capture and discovery tooling
