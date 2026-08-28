# Tools

> **Most users do not need these tools.** For normal installation and setup,
> start with the repository root [`README.md`](../README.md). This file is mainly
> for advanced troubleshooting, token capture fallback paths, and contributor
> workflows.

Only the items listed below are shipped with the repository. Everything else
under `tools/` (including local trace artifacts under `tools/traces/`) is
gitignored.

## Shipped scripts at a glance

| Script | Purpose | Run as |
| --- | --- | --- |
| [`ehg-token-app/`](ehg-token-app/) | Standalone Android app (Kotlin, single-activity) that performs the **full BLE token-extraction flow on the phone itself**: login → confirmation token → BLE scan → SCU bond → TLS-over-NUS handshake → PairMobileRequest → receive refresh token → display for copy/paste. **Use this when your HA host has no BLE hardware** (and Path A's in-integration BLE pairing is not an option). | **Download the prebuilt [`ehg-token-extractor.apk`](https://github.com/BetaHydri/hymer-connect-ha-ble/releases/latest/download/ehg-token-extractor.apk)** (always the latest release, auto-built and attached to every release) and install it on a phone in the vehicle — or open the [source](ehg-token-app/) in Android Studio and build it yourself. |
| [`Start-EhgTokenCapture.ps1`](Start-EhgTokenCapture.ps1) | One-click PowerShell wrapper around the mitmproxy capture: checks prerequisites (Python, mitmproxy, Node.js, apk-mitm), starts the proxy, prints connection instructions, and exits when the token is captured. **Use this on Windows for the cloud-only fallback path (Path B).** Path A (BLE pairing) extracts the token automatically inside the integration. | `pwsh tools\Start-EhgTokenCapture.ps1` |
| [`capture_ehg_token.py`](capture_ehg_token.py) | mitmproxy addon that scans intercepted HTTP/WebSocket traffic for the EHG Remote Access Refresh Token (`ett=access-refresh`). Saves it to `traces/captured_ehg_token.txt` and auto-exits. **Use this directly on Linux/macOS for the cloud-only fallback path.** *Note: this addon does not yet also capture the OAuth `Authorization: Basic` client header — paste that one manually if needed.* | `mitmdump -s tools/capture_ehg_token.py --listen-port 8080` |
| [`discover_sensors.py`](discover_sensors.py) | Connects to the EHG cloud via SignalR using your captured token, subscribes to all PIA sensor data for a configurable window (default 120 s), and prints a complete `(bus_id, sensor_id) → value` table cross-referenced with the known sensor map. **Use this to identify unmapped slots on a non-S600 vehicle** before opening a brand-overlay PR. | `python tools/discover_sensors.py --duration 120` |
| [`scan_pia_fields.py`](scan_pia_fields.py) | **Single-shot** PIA protobuf field probe. The device-management field numbers are now **resolved** from the decompiled codec (`getPairedMobileDevices`=5, `deleteMobileDevices`=3, `deleteUser`=1, `deleteAllUsers`=2). Use **`--getpaired`** for a **safe read-only** listing of the SCU's paired BLE devices (sends field 5, decodes `Response.mobileDevices`=10). Any other field is single-shot and **refuses without `--i-understand-may-be-destructive`**; it never sweeps a range and blocks the pairing (4/6) and restart (command 2) fields. **Reverse-engineering tool; delete*/getPaired* are schema-verified but unconfirmed on a live SCU.** | `python tools/scan_pia_fields.py --getpaired` |
| [`convert_dan_metadata.py`](convert_dan_metadata.py) | Converts a *local* EHG runtime-metadata extraction directory (produced by [@dan-simms1's upstream extractor](https://github.com/dan-simms1/hymer-connect-ha)) into a starting `sensor_maps/<brand>.json` overlay. Detailed below. | `python tools/convert_dan_metadata.py self-test` |
| [`decode_cmds.py`](decode_cmds.py) | Compact offline protobuf decoder for base64-encoded PIA command payloads captured from mitmproxy logs. Useful for inspecting individual SignalR messages (heater commands, light controls, etc.) at the protobuf field level. **Developer tool, not for daily use.** | `python tools/decode_cmds.py` |
| [`mitm_hymer_ws.py`](mitm_hymer_ws.py) | mitmproxy addon that intercepts live SignalR WebSocket traffic between the EHG app and cloud, decodes PIA protobuf payloads in real time, and logs all sensor data to timestamped JSONL files. **Used during early development to reverse-engineer bus/slot mappings.** | `mitmweb -s tools/mitm_hymer_ws.py --listen-port 8080` |
| [`analyze_ehg_apk.py`](analyze_ehg_apk.py) | Extracts the base APK + Hermes `index.android.bundle` from locally-downloaded EHG `.xapk` files, then scans for the OAuth client credential and compares it across apps/versions. Confirms the shipped `Authorization: Basic` default is the shared app client credential (`ehg-prod-mobile-app-technical-user`), not a user token. **Reverse-engineering tool.** Reads/writes under `source/androidapp/` (gitignored). | `python tools/analyze_ehg_apk.py` |
| [`analyze_ehg_devices.py`](analyze_ehg_devices.py) | Extracts EHG component/device identifier strings from the Hermes bundles and diffs them across app versions (old→new) and brands (Hymer↔Eriba) to spot newly supported appliances or bus/slot mappings. For an authoritative component→kind catalog, prefer [@dan-simms1's](https://github.com/dan-simms1/hymer-connect-ha) `metadata_overlay.py::_classify_component`. **Reverse-engineering tool.** | `python tools/analyze_ehg_devices.py` |

The two non-BLE capture scripts (`Start-EhgTokenCapture.ps1` + `capture_ehg_token.py`) are alternatives — use the PowerShell wrapper on Windows, or invoke the Python addon directly on Linux/macOS. Both write to `traces/captured_ehg_token.txt`.

> **BLE-specific note:** The mitmproxy capture scripts are only needed for **Path B** (cloud-only setup with mitmproxy and a patched APK). For **Path A** (BLE + Cloud, *recommended*), the integration's built-in BLE pairing flow extracts the EHG refresh token automatically by talking to the SCU directly — no mitmproxy, no patched APK, no extra tools. The standalone `ehg-token-app/` is a third option for users without BLE hardware on the HA host but with a BLE-capable phone in the vehicle. The OAuth client header (v2.61.0-alpha.6+) is captured manually for now.

## `convert_dan_metadata.py` — Brand overlay generator

Converts a **local** EHG runtime-metadata extraction directory — produced by
[HYMER Connect Metadata Edition](https://github.com/dan-simms1/hymer-connect-ha)
by [@dan-simms1](https://github.com/dan-simms1) — into a
[`sensor_maps/<brand>.json`](../custom_components/hymer_connect/sensor_maps/)
overlay file. This is intended for users whose vehicle is **not** a HYMER
Grand Canyon S 600 / S 700 (sub-brands such as Bürstner, Carado, Dethleffs,
Eriba variants, LMC, Laika, Niesmann+Bischoff, Sunlight, Freeontour, …) where
the shared, observation-gated `base.json` does not yet cover a component the
vehicle reports. In almost all cases a confirmed mapping is then folded into
`base.json` / `lights.json` (fixed EHG components) rather than shipped as a
per-brand overlay.

### Provenance rules

1. **You** lawfully obtain the EHG APK / bundle.
2. **You** run the upstream extractor's `prepare_runtime_metadata.py` locally
   (see
   [HYMER Connect Metadata Edition](https://github.com/dan-simms1/hymer-connect-ha))
   to produce a metadata directory containing `sensor_labels.json`,
   `component_kinds.json`, `control_catalog.json`, `coverage_audit.json`, and
   (optionally) `support_matrix.json` / `vehicle_catalog.json`.
3. **You** run this converter against that local directory to emit a brand
   overlay.
4. **Neither** the input metadata nor `oauth_client.json` may be committed to
   this repo. The `.gitignore` already blocks the common file names, but
   please double-check before opening a PR.

### Pin to a released tag of the upstream extractor

The upstream metadata format is reasonably stable but is not a public API.
Pin your extraction to a
[released tag](https://github.com/dan-simms1/hymer-connect-ha/releases)
rather than the upstream `main` branch so the field names this converter
expects remain valid. If the upstream project publishes a formal schema,
adjust `SCHEMA_MAP` at the top of
[`convert_dan_metadata.py`](convert_dan_metadata.py) in one place.

### Conservative emission policy

The converter intentionally emits a minimal, safe subset of the source
metadata:

| Coverage class | Output |
| --- | --- |
| `known_read_only` | `sensor` or `binary_sensor` (datatype-driven) |
| `known_writable` + `kind=light` | `lights` section |
| `known_writable` + `control_catalog` entry | `switches` section |
| `inferred` | skipped (or emitted with `enabled: false` if `--include-inferred`) |
| `suppressed` | always skipped |
| `kind` in {fridge, heater, boiler, ac} | **not** auto-emitted; a `_climate_templates_required` marker is written instead — hand-port from the climate templates in `sensor_maps/base.json` |

### Usage

```pwsh
# 1. Verify the converter logic on synthetic in-memory fixtures.
python tools\convert_dan_metadata.py self-test

# 2. Convert your own local extraction.
python tools\convert_dan_metadata.py convert `
    --input  C:\path\to\your\local\dan_metadata `
    --output custom_components\hymer_connect\sensor_maps\<brand>.json `
    --brand  <brand> `
    --vehicle-id <optional support_matrix key>
```

`--include-inferred` re-enables the conservative-skip behaviour for inferred
slots: they are emitted with `enabled: false` and `_inferred: true` so a
maintainer can review and promote individual entries.

### Reviewing the output before merging

The generated file is a **starting point**, not a final overlay:

* Re-name auto-generated entity ids to match the conventions in
  [`base.json`](../custom_components/hymer_connect/sensor_maps/base.json) (and
  [`lights.json`](../custom_components/hymer_connect/sensor_maps/lights.json) for
  lights).
* Refine `device_class` / `icon` choices — the converter only applies the few
  unambiguous unit-to-class mappings.
* Fill in any `_climate_templates_required` entries by hand using the
  `truma_heater` / `fridge` blocks in `hymer.json` as a template.
* Test on a real vehicle (12 V on for passive sensors) before opening a PR.
* Strip the `_generated_by` and `_source_vehicle_id` header keys once you have
  curated the file.

### Credits

This converter consumes the metadata extraction tooling shipped with
[**HYMER Connect Metadata Edition**](https://github.com/dan-simms1/hymer-connect-ha)
by [@dan-simms1](https://github.com/dan-simms1) — a sibling Home Assistant
integration that uses the same EHG cloud stack with a metadata-driven
approach. This repository ships only the converter; it does not redistribute
any APK-derived data or vendor credentials. Users supply their own
extraction output locally before running the converter.
