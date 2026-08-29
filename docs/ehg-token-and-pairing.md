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
- **The SCU has a limited number of pairing slots** — a *small* table, but the exact
  ceiling is **firmware-dependent** and not well-calibrated. **At least 7** slots were
  confirmed on firmware **ASW 1.49.7** ([issue #25](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/25)),
  with no rejection observed to mark the true limit; the earlier "~4–5" was only an
  estimate. A **full** table rejects new pairings. The SCU remembers paired device
  **names**, not just MAC addresses — it keys its `PairedMobileDevices` table on the
  **(MAC, name)** pair. Since **v2.95.6** the integration presents a **stable, reused**
  device name (`ha-xxxxx`): the name is generated **once** on the first successful pair,
  persisted in the config entry (`ble_pair_name`), and **reused on every subsequent
  re-pair** (both the automatic coordinator path and the reconfigure dialog). So a
  returning Home Assistant host always maps to the **same slot** and is recognised,
  instead of consuming a new slot with a fresh random name each time — which matters
  because per-device removal is not honoured on any tested firmware (see #26 below), so
  orphaned slots cannot be cleaned up individually. Existing installs adopt a stable name
  on their next pair. (Before v2.95.6 a new random `ha-<time>` name was sent per attempt.)

> **The stable name only helps the SCU's slot table — not the Bluetooth link itself.**
> The BLE bond is keyed on **MAC + Long-Term Key** (Bluetooth Core Spec / SMP); the
> device name is keyed only into the SCU's `(MAC, name)` slot table and matters solely
> during a token *mint*. A returning, already-bonded host re-connects with **no** name
> at all (bond + stored token = BLE reads). See
> [`ehg-app-ble-protocol.md`](ehg-app-ble-protocol.md#scu-pairing-behavior) for the
> two-layer detail. So if BLE won't connect, chase the **bond**, not the name.

There is no UI in the EHG app to list or delete the SCU's individual paired BLE
devices. "Verbindung trennen" in the app removes the **entire vehicle** from the
account (all users, all devices) — it is not a per-device unpair. This integration
fills that gap over BLE (see below).

## Paired BLE device management (in Home Assistant)

The integration exposes the SCU's internal paired-device table — which the EHG app
never shows — through **four entities** (all **BLE-only**; over the cloud path the SCU
replies `ACCESS_DENIED`, so they only work while a bonded BLE session is up with the
SCU awake / 12V on). **All four are disabled by default** (they are diagnostic / config
entities) — enable them per entity under **Settings → Devices & Services → your HYMER
device → the entity → ⚙ → *Enable***, or via **Settings → Entities**, when you need them.

> [!IMPORTANT]
> **Per-device removal does not currently free a slot on any SCU firmware tested so
> far.** The SCU acknowledges `deleteMobileDevices` with `status=1` (SUCCESS) but then
> **silently keeps the device** — the paired table is unchanged. This "ack-then-discard"
> was confirmed on **two** different vehicles/firmwares: a retrofit kit (ASW 1.49.7,
> [issue #26](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/26)) **and** a
> factory Grand Canyon S 600 — the full-record *and* the MAC-only delete frame were both
> ACKed and discarded, the table stayed unchanged. Since **v2.95.2** the unpair button
> **verifies** the result and reports the truth (*"Slot NOT freed"*) instead of a false
> success; **v2.95.4** adds an automatic MAC-only retry as a diagnostic. **Today the only
> reliable way to clear the SCU's pairing table is the EHG app's "Verbindung trennen"**
> (which wipes *all* paired devices at once); afterwards you re-pair Home Assistant (and
> any phone) from scratch. Listing the table (`getPairedMobileDevices`) and the
> diagnostics work fine — only the actual per-device *removal* is not honoured yet.

| Entity | Type | What it does |
| --- | --- | --- |
| `sensor.*_paired_ble_devices` | sensor (diagnostic) | State = **count** of paired devices; `attributes.devices` = the full list (`name` / `mac` / `uuid`). Auto-populates a few seconds after a BLE connect. |
| `button.*_log_paired_ble_devices` | button (diagnostic) | Read-only refresh — re-reads the list (`getPairedMobileDevices`) and writes it to the log and the sensor. Never changes anything. |
| `select.*_ble_device_to_unpair` | select (config) | **Records** which device you want to remove. Picking an option does **not** touch the SCU. |
| `button.*_unpair_selected_ble_device` | button (config) | **Approves + executes** the removal of the selected device (`deleteMobileDevices`). Intended to free one pairing slot — but **the SCU firmwares tested so far ACK the request and silently keep the device** (see the note above, #26). Since v2.95.2 it verifies and reports *"Slot NOT freed"* rather than a false success. |

> **What the `uuid` column means.** `uuid` identifies the **EHG user account**, not
> the device — one account can occupy several slots, and a second account (e.g. a
> family member added via *Gastzugänge*) shows a **different** `uuid`. On
> [issue #25](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/25)'s SCU, 6
> of 7 slots shared one account uuid and 1 belonged to a second account. So unpair by
> **device** (MAC / name), **not** by uuid — but the uuid is handy to see at a glance
> which slots are yours vs. a partner's. Two hints for spotting your own slot: an
> `ha-xxxxx` entry's MAC is the **host's Bluetooth controller address** (e.g. the
> Pi's `hci0`), not a random value, so you can match it with certainty; and a
> token-extraction helper permanently occupies a slot (it can even appear literally
> as `ehg-token-extractor`), making it a prime unpair candidate.

**Two-step unpair (deliberate by design).** Removing a device is split so it cannot
happen by accident: the **select only records** the target, and the separate
**"Unpair selected BLE device" button** performs the actual deletion. That button is
disabled by default and only becomes *available* when BLE is connected **and** a
device is selected. To free a slot: enable the entities → make sure BLE is connected
(SCU awake) → pick the device in the select → press **Unpair selected BLE device**.
The list refreshes automatically. **Note:** on every SCU firmware tested so far this does
**not** actually free the slot — the SCU ACKs but discards the removal (see the important
note at the top of this section, #26), and the button reports *"Slot NOT freed"*. It
remains the intended mechanism for a stale `ha-xxxxx` entry and will start working the
moment a firmware honours `deleteMobileDevices`, but **for now use the EHG app's
"Verbindung trennen"** to clear a full table.

> [!WARNING]
> **This needs a *stable* bonded BLE session — it can't fix itself when BLE won't
> hold.** Unpairing runs *over* the BLE link (there is no cloud fallback — the SCU
> answers `ACCESS_DENIED` on the cloud path). So it only works if a bonded session
> stays up long enough to send `deleteMobileDevices` and read the refreshed list.
> If your BLE **never completes or drops within seconds** — e.g. the SCU is in deep
> standby, the signal is weak, or a **full pairing table makes the SCU reject/drop
> the session before you can act** — then the in-HA unpair **cannot help you**, by
> design: freeing a slot needs BLE, but BLE is exactly what's failing (a
> chicken-and-egg). The `sensor`/select/button will simply stay empty or greyed out.
>
> **Work-arounds when the in-HA unpair can't get a stable link:**
> 1. Wake the SCU fully first (12V on, **ignition on** on Mercedes-based ML-T, and/or
>    the physical **CONNECTION** button on the SCU) and try again close to the SCU —
>    a longer-lived session may be enough to fire one delete.
> 2. Free a slot from a phone that is **already paired**, using the official EHG app's
>    unpair flow, or unpair a device you no longer use.
> 3. As a last resort, a HYMER **support / factory pairing reset** clears the table.
>
> This is the situation for full-table SCUs whose BLE only holds for a few seconds:
> the feature exists, but it can't be the escape hatch for the very condition
> (full table) that is dropping the link.

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
