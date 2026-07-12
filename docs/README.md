# `docs/` — Documentation Index

This folder holds the in-depth reference and reverse-engineering documentation for
**HYMER Connect BLE**. Most users only need the top-level
[`README.md`](../README.md) and the [`quick-start.md`](../quick-start.md) guide —
the files here are for looking up bus/slot meanings, adding translations, and
understanding the protocol internals.

## Reference (users & contributors)

| Document | What it covers | Audience |
| --- | --- | --- |
| [`sensor-map.md`](sensor-map.md) | The **canonical bus/slot reference** — current meaning of every known `(bus_id, sensor_id)` slot per vehicle, units, transforms, plus the pinned-mapping and auto-slot `{n}` template rules for multi-device buses. | Users looking up a sensor · contributors mapping a new vehicle |
| [`translations.md`](translations.md) | When (and when not) to edit `strings.json` and `translations/en.json` after adding an entity to a brand overlay — translation-key style vs. the direct-name stepped-switch select driver. | Contributors editing sensor maps / opening PRs |

## Protocol & connection internals (maintainers)

| Document | What it covers | Audience |
| --- | --- | --- |
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
- [`../dashboards/README.md`](../dashboards/README.md) — dashboard import and required helpers
- [`../CHANGELOG.md`](../CHANGELOG.md) — release history
