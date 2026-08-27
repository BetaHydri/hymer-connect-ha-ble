# Quick start

This guide is the fastest path to a working `HYMER Connect BLE` setup in Home Assistant.

Use this file if you are new to the integration and want the shortest practical path.
For deeper background, see the main [`README.md`](README.md).

## Before you start

You always need:

- a HYMER / EHG account email and password
- Home Assistant with HACS
- a vehicle with an SCU / SIU supported by the EHG app

For most users you also need:

- a **dealer QR activation token** — a code you get from **your dealer**
  (it is part of your vehicle handover paperwork, *not* a sticker on the
  vehicle). It is required for the two recommended setups that pair against the
  vehicle: **Path A (BLE)** and **Path C (Android app)**. It is **not** needed
  for Path B (mitmproxy) or Path D (bootstrap only). See
  [Which token is which?](#which-token-is-which-read-this-first) below for why.

Then choose **one** setup path.

## Which token is which? (read this first)

Two different tokens are involved, and confusing them is the most common setup
mistake. **The dealer QR activation token is NOT the EHG refresh token.**

- **Dealer QR activation token** — a code from your **dealer handover paperwork**
  (a paper document, *not* a sticker on the vehicle). You provide it during BLE
  pairing (Path A) or the Android app (Path C) to prove physical access to the
  vehicle. It *is* saved in the config entry (you can view it again later via
  **Reconfigure**), but it only bootstraps the pairing — it is **not** the token
  that authenticates the cloud / SignalR connection.
- **EHG refresh token** — the long-lived token the integration actually stores
  and uses for cloud / SignalR data. You never paste the QR code as the refresh
  token; the refresh token is **obtained for you** by BLE pairing (Path A),
  captured with mitmproxy (Path B), or extracted by the Android app (Path C).

**In short: the QR code helps *obtain* the refresh token — it is not the refresh
token itself.** For the full explanation, see
[README → Obtaining the EHG Refresh Token](README.md#obtaining-the-ehg-refresh-token).

This is why the config flow treats the QR token as **optional**: it is only
required (and only enforced) when you enable the BLE data path (Path A). Cloud-only
setups (Paths B and D) supply the refresh token directly, so the flow lets you
leave the QR field empty and falls back to cloud-only mode.

> **Every BLE pairing mints its own personal refresh token**, bound to the
> pairing device's BLE identity, so the EHG app on your phone holds a different
> token than Home Assistant. In the cloud-only paths you pair a helper device
> (the token-extractor APK, Path C, or a mitmproxy capture, Path B) and **reuse
> that same token** in Home Assistant. Best practice: don't run one extracted
> token on more than one device at once — uninstall the APK once HA has the token.
> With Path A (a HA host in the vehicle with BLE hardware + the Bluetooth
> integration), Home Assistant pairs directly and mints its **own** token, so no
> reuse is involved. The BLE dual-path has only been tested on **Raspberry Pi 4**
> hardware so far, using the Pi's built-in Bluetooth adapter (no external USB BLE
> dongle needed); other BLE-capable HA hosts should work but are unverified.

## Pick your setup path

| Path | Choose this when | What you need |
| --- | --- | --- |
| **Path A — BLE + Cloud** | Your HA host has Bluetooth and you are physically at the vehicle | HA host with BLE, dealer QR activation token, press **CONNECTION** on SCU |
| **Path B — Cloud-only (mitmproxy)** | No BLE on HA host, but you can capture the token manually | EHG refresh token from mitmproxy |
| **Path C — Cloud-only (Android app)** | No BLE on HA host, but you have an Android phone and vehicle access | Android phone, dealer QR activation token, press **CONNECTION** on SCU |
| **Path D — Bootstrap only** | You want to create the config entry now and add the token later | login only |

## Install via HACS

HYMER Connect is in the **HACS default store** ([hacs/default #7793](https://github.com/hacs/default/pull/7793)),
so no custom repository is needed:

1. Open **HACS**
2. Search for **HYMER Connect**
3. Click **Download**
4. Restart Home Assistant

## Path A — BLE + Cloud setup

This is the recommended setup when your HA host is inside the vehicle or otherwise within BLE range.

1. Enable the **Bluetooth** integration in Home Assistant
2. Add the **HYMER Connect BLE** integration
3. In **Login**, enter:
   - brand
   - email
   - password
4. In **Vehicle activation**, enter:
   - dealer QR activation token
   - optional SCU Bluetooth address
   - keep BLE enabled
5. Submit and wait for the **BLE pairing step** to appear
6. **Close the EHG app on your phone first** — do not pair while the phone is
   actively connected over BLE to the same SCU, as a competing connection can
   block the host's bond
7. **Press CONNECTION** on the SCU touch panel, then **submit the pairing step
   within ~25 seconds** and **do not close the dialog**. The pairing window can be
   as short as **~30 seconds** on some SCUs (e.g. B-MC I 680, SCU 1.13.0.0) — so
   press CONNECTION and submit promptly, rather than pressing CONNECTION only once
   the dialog is already waiting. The integration keeps retrying, but the attempt
   right after your press is the one most likely to succeed.
8. Wait for pairing and token exchange to finish

> The host's built-in pairing agent is **device-locked** to your SCU and handles
> both the JustWorks confirmation and the **legacy PIN/passkey** exchange
> automatically — you never enter a PIN. For the full pairing-sequence best
> practices, host requirements (`bluetoothctl`, legacy TLS), and recovery steps,
> see [`docs/ble-troubleshooting.md`](docs/ble-troubleshooting.md).

After that:

- the EHG refresh token is stored automatically
- BLE provides local low-latency sensor reads
- cloud / SignalR provides full write support and remote access

## Path B — Cloud-only with mitmproxy

Use this if your HA host has no BLE hardware.

1. Capture the EHG refresh token with the mitmproxy method
2. Add the integration in Home Assistant
3. Enter brand, email, password, and the EHG refresh token
4. Leave vehicle activation fields empty
5. Finish setup

For the detailed capture workflow, see:

- [`README.md`](README.md#obtaining-the-ehg-refresh-token)
- [`tools/README.md`](tools/README.md)

## Path C — Cloud-only with Android app

Use this if your HA host has no BLE hardware but you do have an Android phone and physical access to the vehicle.

1. Download [`ehg-token-extractor.apk`](https://github.com/BetaHydri/hymer-connect-ha-ble/releases/latest/download/ehg-token-extractor.apk) from the latest GitHub release (it is attached automatically to every release)
2. **Sideload** it on your Android phone — the APK is **not signed** and **not on Google Play**, so allow *"Install unknown apps"* for your browser/file manager when prompted
3. Enter email, password, and the dealer QR activation token
4. Press **Start** in the app
5. Press **CONNECTION** on the SCU touch panel when prompted
6. Copy the extracted EHG refresh token
7. Add the integration in Home Assistant and paste the token in the login step
8. Leave vehicle activation fields empty
9. You only need the app once — after the token is saved in Home Assistant you can **uninstall it again**

> **📵 Apple / iOS is not supported for token acquisition.** The token-extractor
> app is **Android-only**, and the mitmproxy method (Path B) has only ever been
> validated by capturing traffic from an **Android** device. Neither path has
> been tested on an iPhone/iPad. If you only own Apple devices, borrow or use a
> spare **Android phone** to obtain the token once — after it is saved in Home
> Assistant you no longer need the Android device.

## Path D — Bootstrap only

Use this if you want to create the entry now and finish pairing later.

1. Add the integration
2. Enter brand, email, password
3. Leave the EHG refresh token empty
4. Leave vehicle activation fields empty
5. Finish setup

Result:

- the config entry is created
- sensor data will not flow yet
- later use **Reconfigure** to add BLE or paste a token

> Since **v2.76.7**, turning on **Enable BLE direct path** connects right away —
> no integration reload needed — and a dropped BLE link recovers on its own via
> a background watchdog, even while the cloud connection stays healthy.

## First checks after setup

After setup, check these items:

1. Open the integration and wait 1–2 minutes
2. Verify entities start receiving data
3. If using GPS, make sure **Find-My-RV** is enabled in the EHG app
4. If using the shipped dashboard, import `dashboards/hymer_connect.yaml`
5. Create the `Engine Running (Corrected)` helper described in [`dashboards/README.md`](dashboards/README.md)

## If something does not work

Start here:

- **BLE setup & pairing problems (Path A / adding BLE later):** [`docs/ble-troubleshooting.md`](docs/ble-troubleshooting.md)
- **Moved HA to a new host and BLE stopped?** Re-pair via **Reconfigure** — tick **Re-pair over BLE (mint a new EHG token)** (v2.84.0+); you don't need to touch the token fields: [Add BLE to an existing cloud-only setup](docs/ble-troubleshooting.md#add-ble-to-an-existing-cloud-only-setup)
- **BLE bond broken but cloud still fine?** Use **Configure → Reset BLE pairing only** (v2.91.5+) — it clears just the Bluetooth bond and keeps your EHG token/cloud: [Reset or re-pair BLE — which do I need?](docs/ble-troubleshooting.md#reset-or-re-pair-ble--which-do-i-need)
- **Other troubleshooting:** [`README.md`](README.md#troubleshooting)
- **Dashboard setup:** [`dashboards/README.md`](dashboards/README.md)
- **Sensor reference:** [`docs/sensor-map.md`](docs/sensor-map.md)
- **Cloud / BLE internals:** the other files under [`docs/`](docs)

## What to read next

- [`README.md`](README.md) — full overview and advanced topics
- [`dashboards/README.md`](dashboards/README.md) — dashboard setup and helpers
- [`docs/sensor-map.md`](docs/sensor-map.md) — current bus / slot reference
