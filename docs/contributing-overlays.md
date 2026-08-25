# Contributing sensor mappings & brand overlays

This guide is for contributors who want to **map a new EHG vehicle** or improve an
existing mapping. It covers discovering your vehicle's `(bus, slot)` pairs,
enabling debug logging, reading BLE/SignalR logs, and adding entries to the shared
`base.json` / `lights.json` (or, rarely, a brand-specific `sensor_maps/<brand>.json`
overlay).

> Most users never need this file — the integration works out of the box. Start
> with the [main README](../README.md) and the [quick-start guide](../quick-start.md).
> Come here only when you want to **contribute** mappings for your brand/model.

## Contents

- [How you can help](#how-you-can-help)
  - [Bootstrap a brand overlay with the converter](#-bootstrap-a-brand-overlay-with-the-converter-v2490)
  - [Option 1: Run the Sensor Discovery Tool](#option-1-run-the-sensor-discovery-tool-recommended)
  - [Option 2: Export from Home Assistant](#option-2-export-from-home-assistant)
  - [Option 3: Enable debug logging](#option-3-enable-debug-logging)
  - [Reading BLE sensor logs](#reading-ble-sensor-logs-dual-path-decoding)
  - [Open a GitHub issue](#open-a-github-issue)
- [Step-by-step: Adding a mapping](#step-by-step-adding-a-mapping)
  - [Field reference](#field-reference--all-available-fields-per-entity-type)
  - [Decision matrix](#decision-matrix--when-do-you-need-which-fields)
  - [Steps 1–4](#step-1-identify-your-vehicles-busslot-pairs)
  - [Common mistakes](#common-mistakes)
  - [Complete example overlay](#example-complete-brand-overlay-for-a-fictional-bestvan-s1)
  - [Translations](#translations)

---

## How you can help

If you have a different EHG vehicle and want to help expand compatibility:

### 🚀 Bootstrap a brand overlay with the converter (v2.49.0+)

If your brand isn't a HYMER Grand Canyon S 600/S 700, you can **generate a starting `sensor_maps/<brand>.json`** instead of writing it by hand. This repo ships [`../tools/convert_dan_metadata.py`](../tools/convert_dan_metadata.py) ([docs](../tools/README.md)). It is a **two-step pipeline** — the converter only consumes input, it does not extract from an APK itself:

1. **First run the upstream extractor** to produce a *local* runtime-metadata directory. The extractor is part of [**HYMER Connect Metadata Edition**](https://github.com/dan-simms1/hymer-connect-ha) by [@dan-simms1](https://github.com/dan-simms1) (see its `scripts/prepare_runtime_metadata.py`). You supply your own EHG APK; nothing APK-derived is committed.
2. **Then convert it** with `convert_dan_metadata.py convert --input ... --output sensor_maps/<brand>.json --brand <brand>`. The output is a **starting point**: read-only sensors and clearly-defined switches/lights are auto-emitted; climate/fridge/boiler/heater are *not* (a `_climate_templates_required` marker is written for hand-porting from the shared `base.json`). Review, and if the mapping is a fixed EHG component (the common case) fold it into `base.json` / `lights.json` following those conventions; test, then open a PR.

### Option 1: Run the Sensor Discovery Tool (recommended)

The `tools/discover_sensors.py` script connects to the EHG cloud, subscribes to your vehicle's SCU, and captures a complete `(bus_id, sensor_id) → value` mapping table. It supports all EHG brands and auto-exports results as JSON.

**Prerequisites:** Python 3.10+, `aiohttp` (`pip install aiohttp`), your EHG credentials, and the EHG refresh token (see [quick-start.md](../quick-start.md) and [`tools/README.md`](../tools/README.md) for the current capture paths).

```bash
# Clone the repo
git clone https://github.com/BetaHydri/hymer-connect-ha-ble.git
cd hymer-connect-ha-ble

# Install dependency
pip install aiohttp

# Set credentials (PowerShell)
$env:HYMER_USERNAME = 'your@email.com'
$env:HYMER_PASSWORD = 'yourpassword'
$env:HYMER_EHG_REFRESH_TOKEN = 'eyJ...'  # your captured token

# Set credentials (bash/zsh)
export HYMER_USERNAME='your@email.com'
export HYMER_PASSWORD='yourpassword'
export HYMER_EHG_REFRESH_TOKEN='eyJ...'

# Run discovery — replace 'eriba' with your brand
python tools/discover_sensors.py --brand eriba --duration 180
```

**Supported brands:** `hymer`, `eriba`, `buerstner`, `dethleffs`, `lmc`, `niesmann-bischoff`, `sunlight`, `carado`, `laika`

**While the script runs** (3 minutes by default), toggle lights, open/close the fridge door, change fridge mode, and trigger any other actions in the EHG app to generate sensor updates.

The script produces:

1. A printed table showing all `(bus_id, sensor_id)` pairs with values and mapped/unmapped status
2. A JSON file (`tools/sensor_discovery_<brand>.json`) — **attach this file to a GitHub issue**

Use `--output <path>` to customize the export filename, and `--duration <seconds>` to adjust the collection time.

> **Note:** This is a standalone tool that connects to the EHG cloud directly. It does NOT modify your Home Assistant installation or the integration in any way.

### Option 2: Export from Home Assistant

1. Go to **Developer Tools → States** in your Home Assistant
2. Filter for your device name (e.g. `camper_eriba`, `hymer`)
3. Copy/paste all entities with their current values into a GitHub issue

### Option 3: Enable debug logging

There are **two ways** to turn on debug logging. Pick whichever suits you:

#### 3a. One-click toggle in the integration UI (easiest)

Home Assistant ships a built-in **"Enable debug logging"** button for this integration — no YAML needed:

1. Go to **Settings → Devices & Services → HYMER Connect BLE**.
2. Open the **⋮ (three-dot) menu** in the top-right and choose **"Enable debug logging"**.
3. A yellow **"Debug logging enabled"** banner appears. Reproduce the issue.
4. Open the same ⋮ menu again and choose **"Disable debug logging"** — HA then **automatically downloads** the captured log for you.

<p align="center">
  <img src="https://raw.githubusercontent.com/BetaHydri/hymer-connect-ha-ble/master/images/debug-logging-button.png" alt="Enable/disable debug logging from the integration's three-dot menu" width="90%">
</p>

> This one-click toggle raises **all** `custom_components.hymer_connect.*` loggers to `debug` for the duration and reverts them afterwards. It's the quickest way to grab a log for a bug report. For **fine-grained control** over individual loggers (e.g. keep `pia_decoder` quiet, or add the low-level `bleak`/BlueZ loggers), use the `configuration.yaml` method below instead.

#### 3b. Via `configuration.yaml` (fine-grained control)

> 1. Edit `configuration.yaml` — easiest with the **File editor** or **Studio Code Server** add-on.
> 2. Add one of the `logger:` blocks below, then **restart Home Assistant** (Settings → System → ⋮ → Restart) or reload the YAML config.
> 3. Reproduce the issue, then download the log via **Settings → System → Logs → Download full log** (or grab `/config/home-assistant.log`).

##### Production (recommended)

Keep this in your `configuration.yaml` permanently. It gives enough visibility to spot problems without flooding the log:

```yaml
logger:
  default: warning
  logs:
    # --- HYMER Connect integration ---
    custom_components.hymer_connect: warning
    custom_components.hymer_connect.signalr_client: info
    custom_components.hymer_connect.coordinator: info
    custom_components.hymer_connect.ble_client: info
    custom_components.hymer_connect.pia_decoder: warning
    # --- BLE stack (bleak / BlueZ) ---
    bleak: warning                          # suppress bleak's INFO-level GATT chatter
```

##### Full troubleshooting

Use this temporarily when investigating issues (BLE pairing, token exchange, command routing). It covers **every layer** from the BLE adapter up to SignalR authentication:

```yaml
logger:
  default: warning
  logs:
    # --- HYMER Connect integration ---
    custom_components.hymer_connect: warning
    custom_components.hymer_connect.api: debug               # OAuth2 + EHG token exchange
    custom_components.hymer_connect.ble_client: debug         # BLE bonding, TLS, GATT writes
    custom_components.hymer_connect.coordinator: debug         # command routing, ACK, fallback
    custom_components.hymer_connect.signalr_client: info       # connection lifecycle, UpdateTokens
    custom_components.hymer_connect.config_flow: debug         # pairing ceremony progress
    custom_components.hymer_connect.pia_decoder: warning       # set to debug only for sensor mapping
    # --- BLE stack (bleak / BlueZ) ---
    bleak: warning                                             # suppress GATT read/write noise
    bleak.backends.bluezdbus.client: info                      # D-Bus calls, MTU negotiation, adapter errors
```

> **Tip:** Switch back to the production profile after troubleshooting. The `pia_decoder: debug` and `bleak.backends.bluezdbus.client: info` loggers are very verbose and will grow your HA log quickly.

##### Logger reference

| Logger | Level | What it shows |
|--------|-------|---------------|
| **Integration loggers** | | |
| `hymer_connect` | `warning` | General integration warnings and errors |
| `api` | `debug` | OAuth2 token refresh status, EHG refresh→access token exchange (vehicle URN, token lengths, response keys on failure) |
| `coordinator` | `info` | Command routing (BLE-first with cloud/SignalR fallback since v2.67.0, reconnect-retry on the cloud leg), REST polling, SignalR reconnect scheduling. A BLE write the SCU ACKs logs `Command sent over BLE`; otherwise the command falls back to the cloud. |
| `coordinator` | `debug` | BLE subscription events (sensor pushes only), path skip reasons (BLE not connected), connection mode changes |
| `signalr_client` | `info` | Connection lifecycle, reconnects, UpdateTokens status, SCU reconnect events |
| `signalr_client` | `debug` | Every SignalR message (very verbose) |
| `ble_client` | `info` | BLE connect/disconnect, bonding results, TLS status, GATT write success |
| `ble_client` | `debug` | GATT services, D-Bus agent, write mode/pacing, chunk details, TLS handshake |
| `pia_decoder` | `debug` | Every decoded PIA sensor value (very verbose — use sparingly) |
| `config_flow` | `warning` | BLE pairing attempt progress (🟢/🔴 status) |
| **BLE stack loggers** | | |
| `bleak` | `warning` | Suppress bleak's default INFO-level GATT read/write chatter that clutters the log |
| `bleak.backends.bluezdbus.client` | `info` | Low-level BlueZ D-Bus method calls, MTU negotiation results, adapter-level errors. Only needed when `ble_client: debug` doesn't show enough detail (e.g. GATT handle errors, BlueZ service resolution failures) |

**What to look for when troubleshooting commands (v2.67.0+ — BLE-first with automatic cloud fallback):**

| Log message | Meaning |
|-------------|---------|
| `Command sent over BLE (..., status=1)` | Write delivered over the local BLE link and ACKed by the SCU (no cloud needed) |
| `BLE write not accepted (status=...) — falling back to cloud` | SCU did not ACK the BLE write; the command is re-sent via SignalR |
| `Cloud command sent (attempt 1/2, ...)` | Normal successful command via SignalR |
| `Cloud command failed (attempt 1/2) — forcing reconnect` | SignalR send returned False; coordinator marks the connection unhealthy and retries once |
| `Command failed after reconnect+retry` | Both SignalR attempts failed — raised as HomeAssistantError to the caller |
| `BLE sensor push: ...` (debug) | BLE read mirror is delivering a sensor update; no write traffic |

**What to look for when troubleshooting token exchange / authentication:**

| Log message | Meaning |
|-------------|---------|
| `🟢 BLE bonding SUCCESSFUL on attempt N` | CONNECTION was pressed, JustWorks bonding worked |
| `TLS handshake complete` | TLS 1.1 session established over BLE NUS |
| `Sending encrypted PairMobileRequest: N bytes` | Pairing request sent to SCU |
| `BLE pairing response received from SCU` | SCU answered the pairing request |
| `refresh token validated — ett='access-refresh'` | Token obtained and validated |
| `BLE pairing: mobilePair response contained no refresh token` | SCU responded but didn't include a token (pairing slot full or rejected) |
| `Exchanging EHG refresh token for access token` | `api.py` starting the refresh→access exchange |
| `EHG access token obtained (len=N)` | Exchange succeeded |
| `remoteAccessToken response did not contain 'token' key` | Server returned unexpected JSON — check `body_preview` in the log |
| `OAuth2 token refresh: 200 OK but no 'access_token'` | OAuth server returned 200 but unexpected body — check `Keys=` and `body_preview` |
| `Token refresh failed 4xx: ...` | OAuth2 refresh rejected by server (credentials changed?) |
| `Obtained fresh EHG access token` | `signalr_client` got a usable token for UpdateTokens |
| `Failed to get remote access token: ...` | EHG exchange failed — check preceding `api` logs |
| `UpdateTokens SUCCESS` | SignalR authenticated — sensors should flow |
| `UpdateTokens failed: status=...` | SignalR rejected the tokens — check token validity |

#### Reading BLE sensor logs (dual-path decoding)

When BLE is connected, the integration receives the **same PIA sensor data twice**: once locally over BLE (~50 ms) and once via the SignalR cloud path (~0.5–2 s). Both feed the identical decoder. This is expected — it is not duplication or an error.

**Startup handshake** — a healthy BLE session logs this sequence once:

```text
ble_client   BLE connected to SCU C5:D9:A0:14:C5:37 (MTU=23, chunk=20)
ble_client   BLE TLS handshake complete: TLSv1.1 ('AES128-SHA', 'SSLv3', 128)
coordinator  BLE direct path established to SCU C5:D9:A0:14:C5:37 (mode=ble)
coordinator  Sending 7 PIA subscription requests over BLE
coordinator  BLE direct path active — running alongside SignalR (both paths: ~130 sensors, BLE ~50ms / SignalR ~500ms–2s)
signalr_client  UpdateTokens SUCCESS for urn:ehg:vehicle:...
```

**Per-update BLE chain** — every live BLE sensor value produces **three lines** in order. The MAC (`C5:D9:A0:14:C5:37`) is only the log prefix identifying the SCU — the actual value lives in the `hex=...` payload:

```text
ble_client   BLE UART RX: 85 bytes                                         ← encrypted frame arrived over BLE
ble_client   BLE PIA RECV C5:D9:A0:14:C5:37: plaintext=29 B hex=...8841    ← decrypted PIA payload (MAC = sender, not data)
pia_decoder  RAW PIA bus=8 | sid=2 | ... f6/wt5=17.1 | f10/wt2=...lin2     ← protobuf field decoded from those bytes
pia_decoder  DISCOVERY mapped (8,2) solar_voltage: 17.2 → 17.1             ← value assigned to an entity
```

The trailing float in the hex (`cdcc8841` little-endian = `0x4188cccd` = **17.1**) is the real reading — proof that genuine sensor values, not just the MAC, travel over BLE.

**Reading the fields:**

| Token | Meaning |
|-------|---------|
| `bus=8 \| sid=3` | `(bus_id, sensor_id)` pair — matches the `sensor_maps/*.json` overlay |
| `f6/wt5=2.5` | field 6, wiretype 5 (32-bit float) → decoded value `2.5` |
| `f3/wt0=13000` | field 3, wiretype 0 (varint/int) → raw `13000` (÷1000 → 13.0 V) |
| `f4/wt2=hex:...("Diesel")` | field 4, wiretype 2 (length-delimited string) |
| `f10/wt2=hex:...("lin2")` | transport tag (`lin1`/`lin2`/`can0`/`can2`) — which internal SCU bus carried it |
| `DISCOVERY mapped (8,2) solar_voltage: A → B` | mapped entity changed from `A` to `B` |
| `RAW PIA ... (no DISCOVERY line)` | value received but unchanged, or `(bus,sid)` not in the sensor map yet |

**Cloud (SignalR) equivalent** — the same value arriving via cloud looks like this; the base64 `arguments[0]` is the identical PIA protobuf:

```text
signalr_client  SignalR message: type=1 target=PiaResponse ... raw={... "arguments": ["GhsQARi9...", ...]}
pia_decoder     RAW PIA bus=8 | sid=2 | ... f6/wt5=17.4 | f10/wt2=...lin2
signalr_client  PiaResponse: 1 fields updated, keys=['solar_voltage']
```

The large periodic `PiaResponse: 129 fields updated` block is the **full state refresh** the SCU sends every few minutes; the small 1–2 field messages are the live deltas.

### Open a GitHub issue

Regardless of which option you use, **open a GitHub issue** with:

- Your vehicle brand, model, and base vehicle (Sprinter/Ducato/Transit/Crafter)
- The JSON sensor dump (from the discovery tool) or entity list (from HA)
- Which sensors work and which show "Unavailable"
- Any correlations you noticed between EHG app actions and sensor changes

This helps map sensor IDs for different vehicle configurations and benefits all users.

---

## Step-by-step: Adding a mapping

### Where mappings live now

Almost every EHG component sits on a **fixed component id** (the same bus/slot on
every brand), so the integration keeps all mappings in two shared, **observation-gated**
files:

- **`base.json`** — **all fixed EHG components**: chassis, habitation, heaters,
  fridges, batteries, solar, water, HSS accessories, etc.
- **`lights.json`** — **all interior lights**.

Because both files are observation-gated, an entity is created only once a vehicle
actually reports that bus — so adding a mapping here never produces phantom
entities on vehicles that lack the device.

- **`hymer.json`**, **`eriba.json`**, etc. — the per-brand files are now **empty
  stubs** that carry only the brand's vehicle list. You add to a brand file **only**
  if you find a genuinely brand-specific mapping — the same EHG component sitting on
  a *different* bus, or needing a *different* name, than it does elsewhere. That is
  rare; in almost all cases your new mapping belongs in `base.json` (or `lights.json`
  for a light).

When a brand file *does* define a `bus,slot` that also exists in `base.json`, the
brand mapping wins for that brand. This lets you:

- Override a name, `device_class` or unit for one brand
- Add a component that genuinely lives on a different bus for that brand
- Support multi-device buses with auto-slot templates

### JSON structure overview

```json
{
  "_doc": "HYMER Grand Canyon S 600/S 700, ML-T 570 CrossOver sensor mappings.",
  "_schema_version": "1.0",
  "sensors": {
    "8,1": { ... },
    "8,2": { ... }
  },
  "lights": {
    "11": { "1": { ... }, "2": { ... } },
    "12": { "1": { ... }, "2": { ... }, "3": { ... } }
  },
  "switches": {
    "3,1": { ... },
    "3,3": { ... }
  },
  "climate": {
    "truma_heater": { ... },
    "fridge": { ... },
    "selects": { "fridge_dometic_cooling_step": { ... }, "fridge_dometic_mode": { ... } },
    "numbers": { "ebl400_capacity": { ... } }
  }
}
```

### Field reference — all available fields per entity type

#### **sensors**

Key format: `"bus_id,sensor_id"` or `"bus_id,sensor_id#group_id"` (the `#group_id` suffix is optional, for readability only; it does not affect parsing).

| Field | Type | Mandatory? | Description | Example |
|-------|------|:---:|-----------|---------|
| **`name`** | string | ✅ Yes | Entity name prefix in HA (platform + name becomes entity ID) | `"battery_voltage"` → entity: `sensor.hymer_battery_voltage` |
| **`platform`** | string | ✅ Yes | HA platform: `sensor`, `binary_sensor`, `button`, `switch` (select/climate are inferred) | `"sensor"` |
| **`require_observed`** | boolean | ❌ No | **Gating.** Create the entity **only once the vehicle actually reports this bus** — no phantom entities for hardware you don't have. Set `true` on every new appliance mapping. | `true` |
| **`unit`** | string | ❌ No | Unit of measurement. Overrides any hardcoded default | `"V"`, `"%"`, `"°C"`, `"A"` |
| **`state_class`** | string | ❌ No | `"measurement"` (most sensors), `"total"` (cumulative), `"total_increasing"` (never resets) | `"measurement"` |
| **`device_class`** | string | ❌ No | HA device class (enables icons, UOM, automations) | `"temperature"`, `"voltage"`, `"pressure"`, `"battery"` |
| **`icon`** | string | ❌ No | MDI icon override | `"mdi:thermometer"`, `"mdi:battery"` |
| **`bus_name`** | string | ❌ No | **Multi-device discriminator** (required for multi-device buses only). Use: fixed pins (`"pin-6"`), hex IDs (`"hex:1a2b3c4d"`), or auto-slot template (`"auto:tyre:{n}"`) | `"auto:tyre:{n}"` |
| **`transform`** | string | ❌ No | Raw value transform: `"div100"`, `"div1000"`, `"mul10"`, etc. Applied before unit display | `"div1000"` for mV → V |
| **`restore`** | boolean | ❌ No | Restore the last known value after a Home Assistant restart until a fresh live value arrives. Useful for slowly-updating or standby-only sensors such as tank levels. **Currently implemented only for `platform: "sensor"`.** | `true` |
| **`friendly_name`** | string | ❌ No | Override HA entity friendly_name. Normally auto-generated from name + strings.json | `"Front Left Tyre"` |
| **`int_labels`** | dict | ❌ No | Map integer raw values → display strings. Priority over hardcoded `_INT_LABELS` | `{"0": "Off", "1": "On", "2": "Error"}` |
| **`value_labels`** | dict | ❌ No | Map string raw values → display strings (e.g. LTE quality "poor" → "⚠️ Poor") | `{"poor": "⚠️ Poor", "excellent": "✅ Excellent"}` |
| **`_doc`** | string | ❌ No | Developer comment explaining non-obvious mappings (not parsed, for contributors) | `"Fridge door sensor, not VehicleBrand"` |

#### **lights**

Key format: `"bus_id"` (top-level) → `"1"`, `"2"`, `"3"` (slots; by convention: 1=on/off, 2=brightness, 3=color_temp).

| Field | Type | Mandatory? | Description |
|-------|------|:---:|-----------|
| **`name`** | string | ✅ Yes | Entity name (without platform prefix) |
| **`icon`** | string | ❌ No | MDI icon |
| **`_doc`** | string | ❌ No | Developer comment |

Example:

```json
"12": {
  "1": { "name": "light_living_ambient", "icon": "mdi:lightbulb" },
  "2": { "name": "light_living_ambient_brightness" },
  "3": { "name": "light_living_ambient_color_temp" }
}
```

#### **switches**

Key format: `"bus_id,sensor_id"` — the slot the switch **writes to**. Boolean/string on-off controls (water pump, 12V main, fridge ECO).

| Field | Type | Mandatory? | Description |
|-------|------|:---:|-----------|
| **`name`** | string | ✅ Yes | Entity name |
| **`write_type`** | string | ✅ Yes | How the value is written: `"bool"`, `"str"`, or `"uint"` |
| **`on_value`** | any | ❌ No | Readback value that means **ON** (e.g. `true`, `"On"`) |
| **`write_on`** / **`write_off`** | string | ❌ No | For `write_type: "str"` — the exact strings written for on/off (e.g. `"On"`/`"Off"`) |
| **`read_path`** | string | ❌ No | Where the state is read back, e.g. `"signalr_sensors.water_pump_active"`. Defaults to `signalr_sensors.<name>` |
| **`requires_12v`** | boolean | ❌ No | Mark the switch unavailable when 12V main is off |
| **`holdoff_off`** | int | ❌ No | Seconds to hold an optimistic OFF (for SCU bounce-back) |
| **`require_observed`** | boolean | ❌ No | **Gating** — create only once the vehicle reports this bus |
| **`icon`** | string | ❌ No | MDI icon |
| **`_doc`** | string | ❌ No | Developer comment |

#### **climate.selects** (writable selects — stepped & string)

Writable selects live under `climate` → `selects`, keyed by a **select name** (not a `bus,slot`). The generic driver (`HymerSteppedSelect`) reads a backing sensor and writes to control slots. Two shapes:

| Field | Type | Mandatory? | Description |
|-------|------|:---:|-----------|
| **`name`** | string | ✅ Yes | Friendly name (HA entity id = `select.hymer_<slug(name)>`) |
| **`control_bus`** | int | ✅ Yes | Bus the writes target |
| **`options`** | list | ✅ Yes | Valid options — SCU **wire values**, do not relabel |
| **`require_observed`** | boolean | ✅ recommended | Create only once the vehicle reports `control_bus` |
| **`read`** | object | ✅ Yes | `{ "value_sensor": "..." }` (string select) or `{ "step_sensor", "power_sensor", "off_when_power_false", "off_value" }` (stepped) |
| **`writes`** | object | ✅ Yes | Write recipe (see below) |
| **`icon`** / **`_doc`** | | ❌ No | Icon / comment |

**Stepped cooling-step select** (Off/1–5, power-then-step dance):

```json
"climate": {
  "selects": {
    "fridge_dometic_cooling_step": {
      "name": "Dometic fridge cooling step",
      "control_bus": 60,
      "require_observed": true,
      "options": ["Off", "1", "2", "3", "4", "5"],
      "read": { "step_sensor": "dometic_fridge_level", "power_sensor": "dometic_fridge_power", "off_when_power_false": true, "off_value": 0 },
      "writes": {
        "off":  [ { "sid": 8, "bool": false } ],
        "step": [ { "sid": 8, "bool": true }, { "delay_ms": 500 }, { "sid": 2, "uint": "$option_int" } ]
      }
    }
  }
}
```

**String select** (writes the chosen option verbatim):

```json
"fridge_dometic_mode": {
  "name": "Dometic fridge mode",
  "control_bus": 60,
  "require_observed": true,
  "options": ["Performance Cooling", "Silent Mode", "Turbo Mode"],
  "read":  { "value_sensor": "dometic_fridge_mode" },
  "writes": { "option": [ { "sid": 1, "str": "$option" } ] }
}
```

`$option` = the selected string, `$option_int` = its integer. The `read` sensors must exist in `sensors` (they can be decode-only).

#### **climate.numbers** (writable numeric slots)

Under `climate` → `numbers`, keyed by a **number name**:

```json
"climate": {
  "numbers": {
    "ebl400_capacity": {
      "name": "Living battery capacity",
      "control_bus": 2, "sid": 10,
      "min": 1, "max": 4095, "step": 1,
      "unit": "Ah", "mode": "box", "write_type": "uint",
      "require_observed": true,
      "read": { "value_sensor": "ebl400_living_battery_capacity" }
    }
  }
}
```

#### **climate** profiles (Truma heater thermostat)

The Truma climate thermostat + boiler/energy selects are configured by a `truma_heater` (or `truma_heater_*` for additional variants) block under `climate`, keyed by **bus + sid** and pointing at backing readback sensors. Add another variant with a key starting `truma_heater_` — the loader picks up any `truma_heater_*` automatically (no code change).

```json
"climate": {
  "truma_heater": {
    "require_observed": true,
    "heater_bus": 58,
    "setpoint_sid": 8, "fuel_type_sid": 4, "fuel_type_2_sid": 6, "boiler_sid": 5, "electric_power_sid": 9,
    "temp_sensor": "outside_temperature",
    "setpoint_sensor": "heater_setpoint",
    "fuel_type_sensor": "heater_fuel_type",
    "boiler_sensor": "heater_fan_speed",
    "electric_power_sensor": "heater_electric_power"
  }
}
```

Set `"supports_energy_select": false` for a gas/diesel-only Combi with no electric slot 9. The absorber/compressor **fridge** cooling controls are `climate.selects` stepped selects (shown above), not a separate slot-map.

### Decision matrix — when do you need which fields?

| Scenario | Required Fields | Optional But Recommended |
|----------|---|---|
| **Simple read-only sensor** (e.g. voltage, temperature) | `name`, `platform`, (maybe `unit`, `device_class`) | `state_class`, `icon`, `int_labels`/`value_labels` |
| **Sensor that should survive HA restarts** (e.g. water tank level while SCU is in standby) | `name`, `platform: "sensor"`, `restore: true` | `unit`, `state_class`, `device_class`, `icon` |
| **Computed sensor with transform** (e.g. raw mV → display V) | `name`, `platform`, `unit`, `transform` | `state_class`, `device_class` |
| **Integer enum sensor** (e.g. fridge mode, error codes) | `name`, `platform`, `int_labels` | `icon`, `_doc` |
| **String enum sensor** (e.g. LTE quality) | `name`, `platform`, `value_labels` | — |
| **Light with brightness** | Use 3 entries (on/off + brightness + color_temp) in lights section | `icon` |
| **Multi-device bus** (e.g. 4 tyre sensors on bus 70) | `name`, `platform`, `bus_name: "auto:tyre:{n}"` | `device_class`, `unit`, `icon` |
| **Fixed discriminator** (e.g. only 2 specific hex IDs, never more) | `name`, `platform`, `bus_name: "pin-6"` or `"hex:1a2b3c4d"` | — |

### Step 1: Identify your vehicle's bus/slot pairs

Run the **Sensor Discovery Tool** (recommended, see above) or enable Dynamic Slot Discovery in the integration and let the HA logs show you unmapped sensors.

**Expected output:**

```text
(bus, slot) | raw_value | mapped_name
(3, 1)      | 1.0       | main_switch
(3, 5)      | 13.1      | battery_voltage
(8, 2)      | 19.9      | solar_voltage
(34, 1)     | False     | ??? (unmapped)
```

### Step 2: Determine the entity type and write the JSON entry

#### **Example 1: Simple temperature sensor (bus 3, slot 16)**

```json
"3,16": {
  "name": "ambient_temperature",
  "platform": "sensor",
  "unit": "°C",
  "state_class": "measurement",
  "device_class": "temperature",
  "icon": "mdi:thermometer"
}
```

This creates: `sensor.hymer_ambient_temperature` with value display like "23.5 °C" in HA.

If the sensor should keep showing its last known value across a Home Assistant
restart until the SCU pushes a new live reading, add `"restore": true`:

```json
"76,1#pin-6": {
  "name": "fresh_water_level",
  "bus_name": "pin-6",
  "platform": "sensor",
  "restore": true,
  "unit": "%",
  "state_class": "measurement",
  "icon": "mdi:water"
}
```

Current implementation note: this optional restore behavior is only wired for
plain `sensor` entities, not for `binary_sensor`, `switch`, `light`, `select`,
or `climate`.

#### **Example 2: Binary sensor with label map (bus 3, slot 1 — 12V switch)**

Raw values: `0` = Off, `1` = On, but the SCU sends string values `"On"` / `"Off"` or sometimes booleans.

```json
"3,1": {
  "name": "main_switch",
  "platform": "binary_sensor",
  "device_class": "switch",
  "value_labels": { "On": "On", "Off": "Off" },
  "int_labels": { "0": "Off", "1": "On" }
}
```

**Why both?** The decoder tries all three: (1) exact value match in `value_labels`, (2) fallback to `int_labels`, (3) raw value if neither matches.

#### **Example 3: Stepped cooling-step select (fridge, Off/1–5)**

A compressor fridge with power on slot 1 (bool) and a cooling level 1–5 on slot 3 (int). It becomes an `Off / 1…5` select under `climate.selects` — Off writes power off; a level writes power on, waits, then the level:

```json
"climate": {
  "selects": {
    "my_fridge_cooling_step": {
      "name": "My fridge cooling step",
      "control_bus": 34,
      "require_observed": true,
      "options": ["Off", "1", "2", "3", "4", "5"],
      "read": { "step_sensor": "my_fridge_level", "power_sensor": "my_fridge_power", "off_when_power_false": true, "off_value": 0 },
      "writes": {
        "off":  [ { "sid": 1, "bool": false } ],
        "step": [ { "sid": 1, "bool": true }, { "delay_ms": 500 }, { "sid": 3, "uint": "$option_int" } ]
      }
    }
  }
}
```

(The `my_fridge_level` / `my_fridge_power` readback sensors are mapped in `sensors` — they can be decode-only.)

Creates: `select.hymer_fridge_cooling_step` with options "Off"–"High". Selecting "High" sends raw int `5` to the SCU.

#### **Example 4: Multi-device auto-slot template (bus 70 — 4 tyre sensors)**

Vehicle has 4 identical HYMER Smart tyre sensors. Each has slots 1–4 (status, pressure, temperature, battery). Raw hex IDs are different but unknown at JSON-write time. Use auto-slot template:

```json
"70,1#t{n}": {
  "_doc": "HYMER Smart tyre sensor #1 (auto-numbered), status readback.",
  "name": "hss_tyre{n}_status",
  "bus_name": "auto:tyre:{n}",
  "platform": "sensor",
  "icon": "mdi:car-tire-alert"
},
"70,2#t{n}": {
  "name": "hss_tyre{n}_pressure",
  "bus_name": "auto:tyre:{n}",
  "unit": "bar",
  "platform": "sensor",
  "device_class": "pressure",
  "state_class": "measurement",
  "icon": "mdi:gauge"
},
"70,3#t{n}": {
  "name": "hss_tyre{n}_temperature",
  "bus_name": "auto:tyre:{n}",
  "unit": "°C",
  "platform": "sensor",
  "device_class": "temperature",
  "state_class": "measurement",
  "icon": "mdi:thermometer"
},
"70,4#t{n}": {
  "name": "hss_tyre{n}_battery",
  "bus_name": "auto:tyre:{n}",
  "unit": "%",
  "platform": "sensor",
  "device_class": "battery",
  "state_class": "measurement",
  "icon": "mdi:battery"
}
```

**Key points:**

- `"name"` contains `{n}` placeholder — replaced at runtime with slot number 1, 2, 3, 4
- All 4 entries share same `"bus_name": "auto:tyre:{n}"` group pattern → grouped together in auto-slot assignment
- `#t{n}` suffix in key is a comment (never parsed)
- Result: `sensor.hymer_hss_tyre1_pressure`, `sensor.hymer_hss_tyre2_pressure`, etc.
- **No `strings.json` / translations entry needed** — auto-slot sensors derive a
  readable display name from their key (`hss_tyre1_pressure` → "HSS Tyre1
  Pressure"). This is also why you *cannot* pre-list them: the device count is
  unbounded (`{n}` may become 5, 6, …). Only fixed-name sensors use Step 3.

### Step 3: Update strings.json and translations/en.json

> **Skip this step for auto-slot `{n}` sensors** (Example 4). They already get a
> friendly name from their key and are numbered at runtime, so there is nothing
> to pre-translate. This step applies only to sensors with a **fixed** name.

**Only for human-readable entity names** (most entity types). The integration looks up friendly names in:

1. JSON `friendly_name` field (if present)
2. `custom_components/hymer_connect/strings.json` (platform-scoped)
3. `custom_components/hymer_connect/translations/en.json` (platform + name keys)

**Example:** After adding `"hss_tyre1_pressure"` sensor, edit `strings.json`:

```json
{
  "entity": {
    "sensor": {
      "hss_tyre1_pressure": { "name": "HYMER Smart Tyre 1 Pressure" },
      "hss_tyre2_pressure": { "name": "HYMER Smart Tyre 2 Pressure" }
    }
  }
}
```

Same keys go into `translations/en.json` under each platform section. See [translations.md](translations.md) for the complete playbook — **do not skip this**, or HA will display ugly translation-key names.

### Step 4: Test and validate

1. **Syntax:** Paste your JSON into [JSONLint](https://jsonlint.com/) to verify no syntax errors
2. **Integration:** Copy your updated `sensor_maps/<brand>.json` to a test HA instance
3. **Reload:** Go to **Settings → Integrations → HYMER Connect → (⋮ menu) → Reload integration**
4. **Check:** **Settings → Devices & Services → HYMER Connect → Device** — enable newly discovered entities
5. **Verify:** Physical action (toggle light, open door, change heater temp) should update entity state in **Developer Tools → States**

### Common mistakes

| ❌ Mistake | ✅ Fix |
|-----------|--------|
| `"name"` not unique within platform (e.g. two sensors with `"battery_voltage"`) | Suffix with appliance or location: `"fridge_battery_voltage"`, `"bms_battery_voltage"` |
| Missing `"platform"` on sensor entry | Always include: `"platform": "sensor"` or `"binary_sensor"` etc. |
| Typo in unit (e.g. `"°C"` as `"C"`) | Copy-paste from reference examples or JSON field reference above |
| `"unit"` but no `"device_class"` | Many units auto-infer device_class (V→voltage, %, °C→temperature); explicit is safer |
| Int labels with string keys (e.g. `"1": "On"` instead of `1: "On"` or `"1": "On"` for JSON) | JSON int_labels keys **must be strings** (JSON doesn't have int keys): `{ "0": "Off", "1": "On" }` |
| Multi-device bus without `"bus_name"` | Every entry on a multi-device bus needs `"bus_name": "auto:group:{n}"` or `"bus_name": "pin-6"` or similar discriminator |
| `{n}` placeholder in `"name"` but no `"bus_name": "auto:…"` | Placeholders only work in auto-slot templates; fixed names ignore them |
| Forgot to update `strings.json` and `translations/en.json` | New entities show ugly translation-key names in HA (e.g. `entity.sensor.hymer_hss_tyre1_pressure` instead of friendly name) |
| JSON keys like `"70,1#t{n}"` with wrong format | Format is `"bus_id,slot_id"` or `"bus_id,slot_id#comment"` — the `#comment` part doesn't affect parsing, but typos in `bus_id,slot_id` will create unmapped slots |

### Example: Complete brand overlay for a fictional "BestVan S1"

```json
{
  "_doc": "BestVan S1 (2025, Fiat Ducato base) sensor mappings.",
  "_schema_version": "1.0",
  "sensors": {
    "1,1": { "name": "odometer", "unit": "km", "transform": "div1000", "platform": "sensor", "state_class": "total_increasing" },
    "1,2": { "name": "fuel_level", "unit": "%", "platform": "sensor", "state_class": "measurement", "device_class": "battery" },
    "3,5": { "name": "battery_voltage", "unit": "V", "platform": "sensor", "device_class": "voltage", "state_class": "measurement" },
    "3,6": { "name": "battery_current", "unit": "A", "platform": "sensor", "device_class": "current", "state_class": "measurement" },
    "8,1": { "name": "solar_active", "platform": "binary_sensor" },
    "8,2": { "name": "solar_voltage", "unit": "V", "platform": "sensor", "device_class": "voltage", "state_class": "measurement" },
    "11,1": { "name": "light_interior", "platform": "binary_sensor" },
    "34,1": { "name": "fridge_power", "platform": "switch", "icon": "mdi:fridge" },
    "70,2#t{n}": {
      "_doc": "Multi-device tyre sensor pressure (auto-slot).",
      "name": "hss_tyre{n}_pressure",
      "bus_name": "auto:tyre:{n}",
      "unit": "bar",
      "platform": "sensor",
      "device_class": "pressure",
      "state_class": "measurement"
    }
  },
  "lights": {
    "11": {
      "1": { "name": "light_interior" },
      "2": { "name": "light_interior_brightness" }
    },
    "12": {
      "1": { "name": "light_ambient" },
      "2": { "name": "light_ambient_brightness" },
      "3": { "name": "light_ambient_color_temp" }
    }
  },
  "switches": {
    "3,1": { "name": "main_switch_12v", "icon": "mdi:power" },
    "34,1": { "name": "fridge_power", "icon": "mdi:fridge" }
  },
  "climate": {
    "selects": {
      "fridge_cooling_step": {
        "name": "Fridge cooling step",
        "control_bus": 34,
        "require_observed": true,
        "options": ["Off", "1", "2", "3", "4", "5"],
        "read": { "step_sensor": "fridge_level", "power_sensor": "fridge_power", "off_when_power_false": true, "off_value": 0 },
        "writes": {
          "off":  [ { "sid": 1, "bool": false } ],
          "step": [ { "sid": 1, "bool": true }, { "delay_ms": 500 }, { "sid": 3, "uint": "$option_int" } ]
        }
      }
    }
  }
}
```

This covers: battery (sensor), lights (on/off + brightness), fridge (switch + a `climate.selects` cooling-step select), and multi-device tyre sensors (auto-slot).

### Translations

For each new entity added above, add matching entries in `custom_components/hymer_connect/strings.json`:

```json
{
  "entity": {
    "sensor": {
      "odometer": { "name": "Odometer" },
      "fuel_level": { "name": "Fuel Level" },
      "battery_voltage": { "name": "Battery Voltage" },
      "hss_tyre1_pressure": { "name": "Front Left Pressure" },
      "hss_tyre2_pressure": { "name": "Front Right Pressure" }
    },
    "switch": {
      "main_switch_12v": { "name": "12V Main Switch" },
      "fridge_power": { "name": "Fridge Power" }
    }
  }
}
```

When you add a new entity to a brand overlay, Home Assistant needs a friendly display name. For most entity types this requires the matching key in **both** `custom_components/hymer_connect/strings.json` and `custom_components/hymer_connect/translations/en.json` — the only exception is the v2.63.0+ stepped-switch select driver, which reads its name directly from the JSON. Full step-by-step playbook with copy-paste examples per entity type: [`translations.md`](translations.md).

### Ready to contribute?

Once you've created and tested your brand overlay:

1. Open a **GitHub issue** with your vehicle brand, model, and test results
2. Open a **PR** adding or updating `sensor_maps/<brand>.json`
3. Update `strings.json` + `translations/en.json` with friendly names
4. Include a **changelog entry** (CHANGELOG.md)

See [CONTRIBUTING](https://github.com/BetaHydri/hymer-connect-ha-ble/blob/master/CONTRIBUTING.md) for the full PR template.

---

## See also

- [`../README.md`](../README.md) — project overview, setup, dashboards, troubleshooting
- [`sensor-map.md`](sensor-map.md) — canonical bus/slot reference, pinned mappings, auto-slot `{n}` templates
- [`translations.md`](translations.md) — when to edit `strings.json` / `translations/en.json`
- [`ehg-app-metadata.md`](ehg-app-metadata.md) — full EHG component/bus catalog (bootstrap source)
- [`../tools/README.md`](../tools/README.md) — token capture, sensor discovery, converter tooling
