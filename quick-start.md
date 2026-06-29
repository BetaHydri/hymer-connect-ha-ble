# Quick start

This guide is the fastest path to a working `HYMER Connect BLE` setup in Home Assistant.

Use this file if you are new to the integration and want the shortest practical path.
For deeper background, see the main [`README.md`](README.md).

## Before you start

You always need:

- a HYMER / EHG account email and password
- Home Assistant with HACS
- a vehicle with an SCU / SIU supported by the EHG app

Then choose **one** setup path.

## Pick your setup path

| Path | Choose this when | What you need |
| --- | --- | --- |
| **Path A — BLE + Cloud** | Your HA host has Bluetooth and you are physically at the vehicle | HA host with BLE, dealer QR activation token, press **CONNECTION** on SCU |
| **Path B — Cloud-only (mitmproxy)** | No BLE on HA host, but you can capture the token manually | EHG refresh token from mitmproxy |
| **Path C — Cloud-only (Android app)** | No BLE on HA host, but you have an Android phone and vehicle access | Android phone, dealer QR activation token, press **CONNECTION** on SCU |
| **Path D — Bootstrap only** | You want to create the config entry now and add the token later | login only |

## Install via HACS

1. Open **HACS**
2. Go to **Custom repositories**
3. Add `https://github.com/BetaHydri/hymer-connect-ha-ble` as **Integration**
4. Install **HYMER Connect BLE**
5. Restart Home Assistant

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
5. Submit and wait for the BLE pairing step
6. Press **CONNECTION** on the SCU touch panel within 2 minutes
7. Wait for pairing and token exchange to finish

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

1. Download the token extractor APK from the latest GitHub release
2. Install it on your Android phone
3. Enter email, password, and the dealer QR activation token
4. Press **Start** in the app
5. Press **CONNECTION** on the SCU touch panel when prompted
6. Copy the extracted EHG refresh token
7. Add the integration in Home Assistant and paste the token in the login step
8. Leave vehicle activation fields empty

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

## First checks after setup

After setup, check these items:

1. Open the integration and wait 1–2 minutes
2. Verify entities start receiving data
3. If using GPS, make sure **Find-My-RV** is enabled in the EHG app
4. If using the shipped dashboard, import `dashboards/hymer_connect.yaml`
5. Create the `Engine Running (Corrected)` helper described in [`dashboards/README.md`](dashboards/README.md)

## If something does not work

Start here:

- **BLE pairing problems:** [`README.md`](README.md#troubleshooting)
- **Dashboard setup:** [`dashboards/README.md`](dashboards/README.md)
- **Sensor reference:** [`docs/sensor-map.md`](docs/sensor-map.md)
- **Cloud / BLE internals:** the other files under [`docs/`](docs)

## What to read next

- [`README.md`](README.md) — full overview and advanced topics
- [`dashboards/README.md`](dashboards/README.md) — dashboard setup and helpers
- [`docs/sensor-map.md`](docs/sensor-map.md) — current bus / slot reference
