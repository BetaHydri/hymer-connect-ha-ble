# `docs/` — Documentation Index

This folder holds the in-depth reference and reverse-engineering documentation for
**HYMER Connect BLE**. Most users only need the top-level
[`README.md`](../README.md) and the [`quick-start.md`](../quick-start.md) guide —
the files here are for looking up bus/slot meanings, adding translations, and
understanding the protocol internals.

## Reference (users & contributors)

| Document | What it covers | Audience |
| --- | --- | --- |
| [`ehg-token-and-pairing.md`](ehg-token-and-pairing.md) | **The single, authoritative token/pairing reference** — which token is which (dealer QR vs. EHG refresh token), the full four-token model, how each setup path obtains the refresh token, per-device token minting, and the SCU's pairing-slot limits. | Users setting up · anyone confused by tokens |
| [`sensor-map.md`](sensor-map.md) | The **canonical bus/slot reference** — current meaning of every known `(bus_id, sensor_id)` slot per vehicle, units, transforms, plus the pinned-mapping and auto-slot `{n}` template rules for multi-device buses. Includes a **[Bus coverage by vehicle](sensor-map.md#bus-coverage-by-vehicle)** table and a **[Complete bus index](sensor-map.md#complete-bus-index-mapped-buses)** of all mapped buses. | Users looking up a sensor · contributors mapping a new vehicle |
| [`contributing-overlays.md`](contributing-overlays.md) | **How to map a new vehicle** — discovering your `(bus, slot)` pairs (converter, discovery tool, debug logging, log reading) and adding entries to the shared `base.json` / `lights.json` (or, rarely, a brand-specific `sensor_maps/<brand>.json` overlay) step by step (field reference, decision matrix, worked examples, common mistakes). | Contributors adding a brand/model |
| [`translations.md`](translations.md) | When (and when not) to edit `strings.json` and `translations/en.json` after adding an entity to a sensor map — translation-key style vs. the direct-name stepped-switch select driver. | Contributors editing sensor maps / opening PRs |
| [`climate-temperature-source.md`](climate-temperature-source.md) | How to change which temperature the Truma climate card **displays** (e.g. a HYMER Smart Sensor) via the `climate.truma_heater.temp_sensor` JSON field — and why this is **display-only** and does **not** change how the heater regulates. | Users customising the climate card |

## Protocol & connection internals (maintainers)

| Document | What it covers | Audience |
| --- | --- | --- |
| [`ble-troubleshooting.md`](ble-troubleshooting.md) | **BLE setup & troubleshooting** for users — the BLE-direct path (Path A), adding BLE to an existing cloud-only setup via **Reconfigure**, what to watch out for on the HA host, enabling debug logging from the integration, reading the pairing log stage-by-stage, and clearing a stale bond. | Users setting up or debugging BLE |
| [`signalr-connection.md`](signalr-connection.md) | The cloud path: SignalR/Azure connection lifecycle, token refresh strategy, reconnection logic, traffic budgets, and lessons from production issues. | Maintainers · advanced troubleshooters |
| [`ble-communication.md`](ble-communication.md) | The local path: SCU BLE communication layer — NUS GATT, TLS-over-BLE handshake, bonding, and the PIA read mirror. | Maintainers · reverse-engineering contributors |

## Reverse-engineering references (advanced)

| Document | What it covers | Audience |
| --- | --- | --- |
| [`ehg-app-ble-protocol.md`](ehg-app-ble-protocol.md) | Findings from decompiling the HYMER Connect (EHG) Android app v2.10.14 — app architecture and the BLE protocol as implemented by the official app. | Reverse-engineering contributors |
| [`ehg-app-metadata.md`](ehg-app-metadata.md) | Large reference table extracted from the EHG APK 2.10.14: all component/bus definitions (127 components, 929+ slot definitions) used to bootstrap brand overlays. | Advanced contributors |
| [`external-sensors.md`](external-sensors.md) | SIU (Smart Interface Unit) external-sensor ecosystem analysis — tyre pressure, gas, temperature/humidity, contact sensors — extracted from the EHG app Hermes bundle. | Advanced contributors mapping SIU sensors |

## See also

- [`../README.md`](../README.md) — full project overview, setup paths, and troubleshooting
- [`../quick-start.md`](../quick-start.md) — shortest path to a working setup
- [`../tools/README.md`](../tools/README.md) — token capture, sensor discovery, and contributor tooling
- [**EHG token-extractor APK**](https://github.com/BetaHydri/hymer-connect-ha-ble/releases/latest/download/ehg-token-extractor.apk) — sideload-only Android helper (unsigned) to read your EHG refresh token for the cloud-only path; uninstall it again once the token is saved. See [`ehg-token-and-pairing.md`](ehg-token-and-pairing.md).
- [`../dashboards/README.md`](../dashboards/README.md) — dashboard import and required helpers
- [`../CHANGELOG.md`](../CHANGELOG.md) — release history
