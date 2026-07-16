# PIA sensor bus map

> **Audience:** Users who want to look up current bus/slot meanings and contributors who need the current canonical names.
> **Primary baseline vehicle:** HYMER Grand Canyon S 600 CrossOver (2025), Mercedes Sprinter 419 CDI.

This file is the **current reference map** of known `(bus_id, sensor_id)` slots.
It intentionally focuses on the **current meaning** of each bus and slot, not
the full discovery history.

## How to use this document

- Use this file when you want to know **what a bus/slot currently means**.
- Use the main [`README.md`](../README.md) for installation, first setup, and
  user-facing troubleshooting.
- Use [`translations.md`](translations.md) when you add or rename entities and
  need to know when `strings.json` / `translations/en.json` must be updated.

## Scope and conventions

- `base.json` contains universal EHG buses shared across vehicles.
- Brand overlays such as `hymer.json` and `eriba.json` add or override
  vehicle-specific mappings.
- Some buses below are confirmed on S600/S700, some were added from other EHG
  vehicles such as the ML-T 570 or Eriba Car 602.
- If your vehicle exposes additional unmapped slots, enable debug logging and
  use the dynamic discovery helpers described in the main README.

## What is intentionally not repeated here

The following topics are documented elsewhere to keep this file shorter and
easier to use as a reference:

- JSON overlay architecture and loading behavior → `README.md`
- translation-file update rules → [`translations.md`](translations.md)
- deep BLE / SignalR / reverse-engineering notes → the other files in `docs/`

## Practical notes for contributors

- Keep entries here focused on the **current bus meaning**.
- Prefer short notes over issue-history narratives.
- Only keep backward-compatibility or legacy-name notes when they still help
  users understand an existing entity name.

## Pinned sensor mappings and auto-slot templates (v2.64.0+)

### The Problem: Shared Slots on Multi-Device Buses

Some buses host **multiple identical physical devices** that share the same `(bus_id, sensor_id)` slots.
The devices are distinguished **only** by their factory-assigned binary ID (PIA field 10, the `connectedComponentInstance`).

**Example**: HYMER Smart tyre-pressure sensors on **bus 70**:
- 4 sensors, each with slots 1=status, 2=pressure, 3=temperature, 4=battery
- Each sensor has a different hex factory ID (e.g., `0x1a2b3c4d`, `0x1a2b3c4e`, …)
- Without discrimination, all 4 would map to the same sensor name (`"tyre_pressure"`) — HA would create only 1 entity instead of 4

### Solution 1: fixed `bus_name` discriminator (pin-6, pin-7, …)

When a bus hosts a **small, fixed, known set** of devices that each report a
**stable, human-readable** instance string (PIA field 10), give each one its own
entry with an explicit `"bus_name"`. The canonical example is the two water-tank
levels on bus 76, told apart by `pin-6` / `pin-7`:

```json
"76,1#pin-6": { "name": "fresh_water_level", "bus_name": "pin-6", "unit": "%", "platform": "sensor" },
"76,1#pin-7": { "name": "gray_water_level",  "bus_name": "pin-7", "unit": "%", "platform": "sensor" }
```

- The `#pin-6` / `#pin-7` suffix on the JSON **key** is only there so the same
  `76,1` pair can appear twice in one JSON object (JSON keys must be unique).
  Everything after `#` is stripped before parsing — the real discriminator is
  the `"bus_name"` field.
- When decoding a bus 76 slot 1 frame, the decoder reads field 10, and looks up
  `(76, 1, "pin-6")` in **`SENSOR_MAP_PINNED`**; if not found it falls back to
  the plain `SENSOR_MAP[(76, 1)]`.

Use this when you know the exact instance strings up front and there are only a
few of them. For **many identical devices with factory-random binary IDs**, use
Solution 2 instead.

### Solution 2: auto-slot templates (`auto:<group>:{n}`)

For a bus with **an unknown number of identical devices** whose instance IDs are
opaque binary bytes (surfaced as `hex:…`), write **one template entry per slot**
using the `{n}` placeholder. The decoder auto-numbers each physical device and
expands `{n}` at runtime — you never list device #2, #3, #4 by hand:

```json
"70,1#t{n}": { "name": "hss_tyre{n}_status",      "bus_name": "auto:tyre:{n}", ... },
"70,2#t{n}": { "name": "hss_tyre{n}_pressure",    "bus_name": "auto:tyre:{n}", ... },
"70,3#t{n}": { "name": "hss_tyre{n}_temperature", "bus_name": "auto:tyre:{n}", ... },
"70,4#t{n}": { "name": "hss_tyre{n}_battery",     "bus_name": "auto:tyre:{n}", ... }
```

**How it works:**
- `"bus_name": "auto:<group>:{n}"` marks the entry as an **auto-slot template**.
  `<group>` (e.g. `tyre`) ties all slots of one physical device to the **same**
  number, so device #1's status/pressure/temperature/battery all share `n = 1`.
- The `{n}` in both `"name"` and `"bus_name"` is a placeholder. It **must** be
  written literally as `{n}` — the loader recognises it and captures the entry
  as a template (native support since **v2.64.3**; before that only the legacy
  `auto:<group>:1` anchor form below worked).
- The `#t{n}` suffix on the JSON **key** is only there to keep the four `70,x`
  keys unique and readable; it is stripped before parsing.
- At **decode time** the decoder sees each distinct `hex:…` instance on bus 70,
  assigns it the next free number in **stable first-seen order** (1, 2, 3, …),
  and **persists** the `hex → number` map to `sensor_maps/_auto_slots.json` so
  the numbering survives restarts — "tyre 1" stays the same wheel every time.
- Devices are **unbounded**: a 5th tyre sensor simply becomes `n = 5`,
  materialised on the fly with no JSON edit and no restart.

> **Numbering is per-install, not global.** Each Home Assistant installation
> numbers its own devices starting at 1. There is no shared numbering across
> users — a second owner's four tyre sensors are *their* 1–4, not 5–8.

**Result**: owners never touch hex IDs. They rename the HA entities once in the
UI ("Tyre sensor 1" → "Front left tyre") and the persisted map keeps them
stable. To reset the numbering, delete `sensor_maps/_auto_slots.json` and
restart.

#### Legacy anchor form (`auto:<group>:1`)

The older form spells device #1 out concretely and lets the decoder derive the
`{n}` template from it. It is still accepted for backward compatibility:

```json
"70,2#t1": { "name": "hss_tyre1_pressure", "bus_name": "auto:tyre:1", ... }
```

Prefer the `{n}` form for new overlays — it is self-documenting and cannot be
mistaken for a concrete device #1 entry.

#### Recipe: add your own auto-slot family

1. Find the bus + slots via **Dynamic Slot Discovery** (see below). Enable the
   `discovered_bus_X_slot_Y` sensors and watch which slots your devices report.
2. Pick a short `<group>` name (e.g. `awning`, `battery2`).
3. Add one template entry per slot to your brand overlay's `"sensors"` section:
   ```json
   "80,1#a{n}": { "name": "hss_awning{n}_state",    "bus_name": "auto:awning:{n}", "platform": "sensor", "icon": "mdi:awning-outline" },
   "80,2#a{n}": { "name": "hss_awning{n}_position", "bus_name": "auto:awning:{n}", "unit": "%", "platform": "sensor", "state_class": "measurement" }
   ```
   Keep the `{n}` identical across the key suffix, `"name"`, and `"bus_name"`.
4. Reload the integration. No `strings.json` / translations edit is needed —
   auto-slot sensors derive a readable display name from their key
   (`hss_awning1_position` → "HSS Awning1 Position").

### Example: Other Multi-Device Buses

- **Bus 74** — HYMER Smart temperature sensors (2-4 devices per vehicle)
  - `"auto:temp:{n}"` with `{n}` resolved per discovered device
  - 3 slots per device: temperature, humidity, battery

- **Bus 73** — HYMER Smart contact sensors (door/window sensors; 1-N per vehicle)
  - `"auto:contact:{n}"` with `{n}` resolved per discovered device
  - 2 slots per device: status, battery

- **Bus 71** — HYMER Smart gas-bottle sensors (1-N per vehicle)
  - `"auto:gas:{n}"` with `{n}` resolved per discovered device
  - 2 slots per device: gas level percentage, height

### JSON label maps: value_labels and int_labels (v2.64.0+)

To eliminate hardcoded value mappings in Python, the JSON can declare **per-sensor label maps**:

#### `value_labels` — String-to-string mapping

```json
"30,3": {
  "name": "lte_connection_quality",
  "value_labels": {
    "poor": "⚠️ Poor",
    "fair": "Fair",
    "good": "Good",
    "excellent": "Excellent"
  }
}
```

When the PIA decoder receives a raw value `"good"` for this sensor, it looks up the label and displays "Good".
Without this, raw values are passed through as-is.

#### `int_labels` — Integer-to-string mapping

```json
"34,6": {
  "name": "fridge_warning",
  "int_labels": {
    "0": "Error 0",
    "1": "Error 1",
    "2": "Temperature Warning",
    "3": "Door Open",
    ...
    "13": "Error 13"
  }
}
```

When the PIA decoder receives a raw integer value `2` for this sensor, it looks up the label and displays "Temperature Warning".
JSON int_labels take precedence over hardcoded `_INT_LABELS` in `pia_decoder.py`, so new mappings can be added without modifying Python code.

**Benefit**: Contributors can extend or add label mappings to any sensor by editing JSON only — no Python coding required.

## Bus 1 — VehicleSignal (Mercedes Sprinter chassis CAN)

> **⚠️ Ignition dependency:** Bus 1 data comes from the Mercedes chassis CAN, which is
> only active when the ignition is in ACC or ON position. With ignition OFF, the SCU
> has no new CAN frames to relay — sensors on this bus (outside temp, fuel, odometer,
> doors, etc.) will show stale/cached values even if the 12V habitation main is on.
>
> **Theory (2026-05-07, unverified):** The SCU appears to cache the last-known CAN
> values and re-sends them on SignalR reconnect / subscription refresh. HA records
> this as a state change (`last-changed` updates), but the value itself may be days
> old. Example: `outside_temperature` showed 25.5 °C with `last-changed` = 15 h ago,
> but actual weather in Munich at that time was far below 25 °C. The 25.5 °C
> was likely cached from a warm afternoon days earlier.
> **Planned verification:** Weekend camping trip — drive with ignition on,
> confirm live temperature, fuel consumption, and estimated range updates.

| Slot | Sensor Name | Unit | Transform | Notes |
|------|------------|------|-----------|-------|
| (1, 1) | `odometer` | km | div1000 | Lifetime odometer |
| (1, 2) | `fuel_level` | % | — | Diesel fuel level (confirmed: EHG app 73% = sensor 72.72) |
| (1, 3) | `lock_status` | — | — | Vehicle locked/unlocked string |
| (1, 4) | `handbrake` | — | — | Handbrake state |
| (1, 5) | `distance_to_service` | km | div100 | km to next service interval (was: rpm) |
| (1, 6) | `adblue_level` | % | — | AdBlue tank level |
| (1, 7) | `engine_hours` | h | div3600 | Engine runtime |
| (1, 8) | `vin_text` | — | — | VIN string |
| (1, 9) | `outside_temperature` | °C | — | Mercedes outside temperature sensor (bumper-mounted). Confirmed 2026-04-20: read 13°C → 16°C tracking real ambient weather in Unterföhring. Same value as Mercedes cockpit "Außentemperatur" display. |
| (1, 10) | `engine_running` | — | — | Engine on/off |
| (1, 11) | `wiping_water_empty` | — | — | Washer fluid low warning (per S700 PR #44; was door_sliding — never updated on S600) |
| (1, 12) | `door_driver` | — | — | Driver door (confirmed at vehicle 2026-04-20, re-confirmed 2026-04-23). MB API: `doorstatusfrontleft` |
| (1, 13) | `door_passenger` | — | — | Passenger door (confirmed at vehicle 2026-04-20, re-confirmed 2026-04-23). MB API: `doorstatusfrontright` |
| (1, 14) | `motor_oil_warning` | — | — | Shows "SNA" (Sensor Not Available) on S600. Per S700 PR #44: engine oil warning. Not a door — confirmed 2026-04-23. Sliding/rear doors only via MB API (`doorstatusrearright`, `decklidstatus`). |
| (1, 15) | `ignition_state` | — | — | IGN_LOCK/OFF/ACC/ON/START |
| (1, 16) | `seatbelt_warning` | — | — | Seatbelt warning |
| (1, 17) | `coolant_warning` | — | — | Coolant low warning (was: turn_signal) |
| (1, 18) | `parking_brake` | — | — | Parking brake engaged (was: headlamp — confirmed by "ON" while parked) |
| (1, 19) | `standheizung_available` | — | — | Auxiliary heater fitted (was: parking_light) |
| (1, 20) | `standheizung_state` | — | — | Auxiliary heater on/off (was: fog_front) |
| (1, 21) | `cruise_control_can` | — | — | Cruise control active (was: fog_rear) |
| (1, 22) | `downhill_assist` | — | — | Downhill assist active (was: high_beam) |
| (1, 23) | `language` | — | — | Dashboard language code |

## Bus 3 — CBE EBL402 (habitation electrics)

| Slot | Sensor Name | Unit | Transform | Notes |
|------|------------|------|-----------|-------|
| (3, 1) | `main_switch` | — | — | 12V main switch state. Write: str "On"/"Off" |
| (3, 2) | `power_source` | — | — | "Battery / solar operated", "Mains" etc. |
| (3, 3) | `charger_active` | — | — | Shore charger active. Also water pump write target (bool) |
| (3, 4) | `charge_phase` | — | — | Bulk/Absorption/Float/Idle |
| (3, 5) | `battery_voltage` | V | — | Living battery voltage |
| (3, 6) | `battery_current` | A | — | Living battery current (negative = discharging) |
| (3, 7) | `chassis_battery_voltage` | V | — | Starter battery voltage |
| (3, 8) | `fresh_water_level_ebl` | % | — | **Fresh water level** from EBL402 tank input (per S700 PR #44). Discovery: `0` with empty tank. |
| (3, 9) | `grey_water_level_ebl` | % | — | **Grey water level** from EBL402 tank input (per S700 PR #44). Discovery: `0` with empty tank. |
| (3, 10) | `battery_soc` | % | — | Battery state of charge. Discovery: 95% |
| (3, 11) | `battery_type` | — | — | "AGM/Lithium" |
| (3, 12–18) | `switch_12v_1..7` | — | — | 12V switch channels |
| (3, 19) | `solar_voltage_sentinel` | V | — | Always 3276.8 (sentinel). Real solar on bus 8 |
| (3, 20) | `solar_connected` | — | — | Solar panel connected. Discovery: `1` (int, not bool) |
| (3, 21) | `solar_charger_status` | — | — | MPPT charger status. Discovery: `1` (int) |
| (3, 22) | `shoreline_connected` | — | — | Shore power connected. Discovery: `False` (bool) |

## Bus 8 — Voltronic MPP260CI (MPPT solar charger)

All 7 slots are solar charger data — same layout on both S600 (MPP260CI) and
S700 (MPP250Duo). Some code labels in `pia_decoder.py` still carry legacy names
from an earlier incorrect sensor map where bus 8 was wrongly labeled as grey
water / ventilation. Solar power is computed as `voltage × current` instead of
reading the raw slot (8, 7) directly.

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (8, 1) | `solar_active` | — | MPPT "charging active" flag. Computed from `solar_current > 0`. Legacy code label: `gray_water_sensor` |
| (8, 2) | `solar_voltage` | V | Panel voltage — confirmed 19.9V live |
| (8, 3) | `solar_current` | A | Charge current — confirmed 2.1A live |
| (8, 4) | `solar_error` | — | MPPT error flag (bool). `binary_sensor` with `device_class: problem`. Promoted from decode-only in v2.63.8. Legacy code label: `vent_1` |
| (8, 5) | `solar_reduced_power` | — | MPPT reduced power flag (bool). `binary_sensor`. Promoted in v2.63.8. Legacy code label: `vent_2` |
| (8, 6) | `solar_aes_active` | — | MPPT AES (Automatic Energy Selector) mode flag (bool). `binary_sensor`. Promoted in v2.63.8. Legacy code label: `vent_3` |
| (8, 7) | `solar_power_raw` | W | Raw MPPT power output. Decode-only — superseded by computed `solar_power` (V×A). Renamed from `tire_pressure` in v2.63.8. |

## Bus 11 — Living ceiling light

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (11, 1) | `light_living_ceiling` | — | On/off |
| (11, 2) | `light_living_ceiling_brightness` | % | Brightness |

## Bus 12 — Living ambient light

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (12, 1) | `light_living_ambient` | — | On/off |
| (12, 2) | `light_living_ambient_brightness` | % | Brightness |
| (12, 3) | `light_living_ambient_color_temp` | — | Color temperature |

## Bus 14 — Bedroom ceiling light (ML-T 570 CrossOver, confirmed 2026-06-01)

Not present on Grand Canyon S 600/S 700. Discovered and confirmed on a HYMER ML-T 570 CrossOver by user @mcfly1969 in [#7](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/7) via the dynamic-discovery diagnostic sensors. Member of the bus 27 *Privat* group — toggling the group also drives this light.

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (14, 1) | `light_bedroom_ceiling` | — | On/off |
| (14, 2) | `light_bedroom_ceiling_brightness` | % | Brightness (0–100, dimmable; no color temp) |

## Bus 15 — Bedroom ambient light

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (15, 1) | `light_bedroom_ambient` | — | On/off |
| (15, 2) | `light_bedroom_ambient_brightness` | % | Brightness |
| (15, 3) | `light_bedroom_ambient_color_temp` | — | Color temperature |

## Bus 16 — Night light

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (16, 1) | `light_nightlight` | — | On/off |
| (16, 2) | `light_nightlight_brightness` | % | Brightness |

## Bus 19 — Bathroom ceiling light

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (19, 1) | `light_bathroom_ceiling` | — | On/off |
| (19, 2) | `light_bathroom_ceiling_brightness` | % | Brightness |

## Bus 21 — Kitchen light

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (21, 1) | `light_kitchen` | — | On/off |
| (21, 2) | `light_kitchen_brightness` | % | Brightness |

> **Note:** EHG app metadata defines only 2 slots for `LightCircuit11` (On + Brightness).
> No color temperature control — confirmed in the EHG app UI. Previously mapped slot 3
> as `light_kitchen_color_temp` was removed in v2.63.7.

## Bus 22 — Outside LED bar (confirmed at vehicle 2026-04-23)

Previously labelled as fresh water tank. Confirmed at vehicle 2026-04-23: both water tanks were empty but bus 22 showed 88%, matching LED bar brightness on bus 25. Bus 22 is the outside LED bar — same physical light as bus 25 (separate SCU component registration). Sensor entities disabled by default (bus 25 is the primary control channel).

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (22, 1) | `light_led_bar_2` | — | On/off (duplicate of bus 25) |
| (22, 2) | `light_led_bar_2_brightness` | % | Brightness (tracks bus 25 LED bar) |

## Bus 24 — All Wohnen light group

Sending (24,1)=true toggles all living area lights (ceiling, ambient, kitchen, seating). **NOT an individual outside light** — verified 2026-04-22: toggling activates all Wohnen lights.

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (24, 1) | `light_wohnen_group` | — | On/off. Toggles all Wohnen lights |
| (24, 2) | `light_wohnen_group_brightness` | % | Group brightness (sentinel: 10000 when off) |
| (24, 3) | `light_wohnen_group_night_mode` | — | EHG: `NightMode`. SCU readback = `100` (brightness percentage, not bool). Sending `bool=True` is ignored by SCU. **Not writable as a simple toggle** — may require a different write type or may not be supported on S600 LIM modules. Decode-only. Under observation. |

## Bus 25 — Outside LED bar (confirmed via mitmproxy 2026-04-22)

Previously mislabelled as grey water. Mitmproxy capture confirmed the EHG app sends on/off + brightness commands to bus 25 when toggling the LED bar. Issue #46 resolved.

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (25, 1) | `light_led_bar` | — | On/off (bool) |
| (25, 2) | `light_led_bar_brightness` | % | Brightness (0-100) |
| (25, 3) | `light_led_bar_night_mode` | — | EHG: `NightMode`. Same as bus 24 slot 3 — not writable as bool. Decode-only. Under observation. |

## Bus 27 — All Privat light group (discovered 2026-04-22)

Discovered by `tools/discover_sensors.py`. Same structure as Bus 24 (All Wohnen group). Sending (27,1)=true toggles all bedroom/bath lights. **NOT the outside LED bar** — verified by user: toggling (27,1) activates all private area lights.

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (27, 1) | `light_privat_group` | — | On/off (bool). Toggles all Privat lights |
| (27, 2) | `light_privat_group_brightness` | % | Group brightness (sentinel: 10000 when off) |
| (27, 3) | `light_privat_group_night_mode` | — | EHG: `NightMode`. Same as bus 24 slot 3 — not writable as bool. Decode-only. Under observation. |

## Bus 30 — ScuSignals (SCU telemetry, LTE, BT, GPS)

> **Prerequisite:** GPS coordinates (slot 1) require the **"Find-My-RV"** service to be
> enabled in the EHG app under **Mehr → Services und Abonnements** (or **More → Services
> and Subscriptions** in English). Without it, the SCU does not include position data in
> its PIA stream (affects both BLE and cloud paths).

Slot labels verified against EHG app Hermes bundle (APK 2.10.14, decompiled
2026-05-05). Previous mapping incorrectly labelled slots 3-7 as GPS data —
they are actually LTE quality, SCU voltage, and Bluetooth device counts.
GPS position comes only from slot 1; the actual GPS fix/altitude/satellites/
heading are delivered via the REST API, not PIA.

| Slot | EHG Name | Sensor Name | Unit | Datatype | Mode | Notes |
|------|----------|-------------|------|----------|------|-------|
| (30, 1) | `GpsLocation` | `gps_coordinates` | — | string | r | Lat,Lng string |
| (30, 2) | `ScuInternalTime` | `scu_internal_time` | — | string | r | SCU internal clock (YYYY-MM-DD hh:mm Z) |
| (30, 3) | `LteConnectionQuality` | `lte_connection_quality` | — | string | r | LTE signal quality (e.g. "excellent"). Previously mislabelled as gps_signal_quality |
| (30, 4) | `LteConnectionState` | `lte_connection_state` | — | bool | r | LTE modem connected. Previously mislabelled as gps_fix |
| (30, 5) | `ScuVoltage` | `scu_voltage` | V | float | r | SCU supply voltage (e.g. 13.1V). Previously mislabelled as gps_altitude (13.1m) |
| (30, 6) | `PairedBTDevices` | `paired_bt_devices` | — | int | r | Number of paired Bluetooth devices. Previously mislabelled as gps_satellites |
| (30, 7) | `ConnectedBTDevices` | `connected_bt_devices` | — | int | r | Currently connected BT devices. Previously mislabelled as gps_heading |
| (30, 8) | `BatteryCutoffSwitch` | `battery_cutoff_switch` | — | bool | r | Battery disconnect/cutoff switch state |
| (30, 9) | `UserActive` | `user_active` | — | bool | rw | User activity flag. Previously mislabelled as lte_connected |
| (30, 10) | `DPlus` | `d_plus_signal` | — | bool | r | D+ alternator charge signal from chassis |
| (30, 11) | `WakeUpChassis` | `wake_up_chassis` | — | bool | w | Chassis wake-up trigger (write-only — may not produce readable data) |
| (30, 12) | `BatterySwitchActive` | `battery_switch_active` | — | bool | r | 12V battery switch active. True = 12V ON |
| (30, 13) | `ShoreLineConnected` | `shoreline_connected_scu` | — | bool | r | Shore power (deprecated in EHG app; primary source is bus 3 slot 22) |
| (30, 14) | `VehicleMovement` | `vehicle_movement` | — | bool | r | Vehicle in motion detection |

## Bus 34 — Thetford N4112A fridge (shared S600/S700)

Slot labels verified against EHG app Hermes bundle (APK 2.10.14, decompiled
2026-05-05).

| Slot | EHG Name | Sensor Name | Notes |
|------|----------|------------|-------|
| (34, 1) | `FridgeOn` | `fridge_power` | Power on/off (bool write). Discovery: `False` |
| (34, 2) | `NightMode` | `fridge_eco` | ECO/quiet mode (bool write). Discovery: `False` |
| (34, 3) | `FridgeLevel` | `fridge_cooling_step` | Cooling step 1–5 (uint write). Discovery: `2` |
| (34, 4) | `FreezerLevel` | `fridge_freezer_level` | Freezer level (deprecated in EHG app). Discovery: `0` (int) |
| (34, 5) | **`DoorOpen`** | `fridge_door` | **Fridge door open/closed** (bool, read-only). Binary sensor. Previously unmapped — fridge_door entity was incorrectly reading from bus 37 slot 2 (VehicleBrand). Fixed in v2.53.0. |
| (34, 6) | `WarningErrorInformation` | `fridge_warning` | Fridge warning/error code (int). EHG app shows generic "check manual, error code: N" for Thetford N4000 series (codes 0–13). Displayed as "Error N" by the integration. Discovery: `0` |
| (34, 7) | `DCVoltage` | `fridge_dc_voltage` | Fridge DC supply voltage. Raw value in mV, displayed as V via `div1000` (e.g. 13000 → 13.0 V). `device_class: voltage`. |

## Bus 37 — VehicleInformation (EHG) / Fridge status readback (PIA)

> **Note:** The EHG app metadata labels this bus as `VehicleInformation` with
> slots `VehicleType` and `VehicleBrand`. However, on the S600, the PIA protobuf
> data on bus 37 carries **fridge mode and status readback**, not vehicle
> identification. The fridge select entity reads `fridge_mode` from here and it
> works correctly. This discrepancy may be a SCU firmware routing difference vs
> the EHG app's component registry.

| Slot | EHG Name | Sensor Name | Notes |
|------|----------|------------|-------|
| (37, 1) | `VehicleType` | `fridge_mode` | Fridge operating mode on S600 PIA. Discovery: `Off` (string) |
| (37, 2) | `VehicleBrand` | `fridge_status` | Fridge status on S600 PIA. **Not** the fridge door — door is on bus 34 slot 5. |

## Bus 43 — Seating overhead light

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (43, 1) | `light_seating_overhead` | — | On/off |
| (43, 2) | `light_seating_overhead_brightness` | % | Brightness |

## Bus 44 — Bedroom overhead light

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (44, 1) | `light_bedroom_overhead` | — | On/off |
| (44, 2) | `light_bedroom_overhead_brightness` | % | Brightness |

## Bus 45 — SCU / LIM module

| Slot | Sensor Name | Notes |
|------|------------|-------|
| (45, 8) | `scu_connected` | SCU connectivity flag. Discovery: `True` |
| (45, 9) | `scu_sensor_9` | Discovery: `False` (bool) |
| (45, 10) | `scu_sensor_10` | Discovery: `False` (bool) |
| (45, 11) | `scu_firmware` | SCU firmware version string. Discovery: `1.12.0.0` |

## Bus 49 — Truma / LIM module

| Slot | Sensor Name | Notes |
|------|------------|-------|
| (49, 8) | `truma_connected` | Truma connectivity flag |
| (49, 10) | `truma_status` | Truma status code |
| (49, 11) | `truma_firmware` | Truma firmware version string |

## Bus 58 — Truma Combi D6E heater (shared S600/S700)

EHG canonical name in parentheses where the local sensor key is a legacy
misnomer kept for backwards-compatibility with existing dashboards/history.

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (58, 4) | `heater_fuel_type` (EHG: `heater_air_energy_source`) | — | Heater air energy source string (Gas/Electric/Mix). `rw` |
| (58, 5) | `heater_fan_speed` (EHG: `water_heater_mode`) | — | **Boiler** mode (Off/ECO/HOT). The legacy `heater_fan_speed` name is misleading — this is the water-heater (boiler) mode, not the heater fan. The Truma fan power (Eco/High) is **not** exposed on the SCU bus and remains panel-only. `rw` |
| (58, 6) | `heater_fuel_type_2` (EHG: `heater_water_energy_source`) | — | Water heater energy source. `rw` |
| (58, 7) | `heater_state` (EHG: `panel_busy`) | — | Panel-busy flag (bool). `r` |
| (58, 8) | `heater_setpoint` (EHG: `target_air_temperature`) | °C | Target air temperature, float write, range -273…30. `rw` |
| (58, 9) | `heater_electric_power` (EHG: `power_limit`) | W | Electric heating element power (0/900/1800). `rw` |
| (58, 10) | `heater_combi_error` (EHG: `combi_error`) | — | Combi error flag (bool). `r` |
| (58, 11) | `heater_operating_mode` (EHG: `heater_air_mode`) | — | Heater air mode string (`OFF` / `Normal` / `Automatic`). Read-only in practice — SCU silently rolls back writes. `rw` per metadata |
| (58, 12) | `heater_response_error` (EHG: `response_error`) | — | Response error flag (bool). `r` |
| (58, 13) | `heater_shoreline_connected` (EHG: `shoreline_connected`) | — | Shoreline connected flag (bool). `r` |
| (58, 14) | `heater_diesel_safety` (EHG: `window_switch_closed`) | — | Diesel safety interlock flag (bool). `True` = safety OK / heater can run, `False` = interlock inactive. Not a physical window contact. `r` |

## Bus 60 — Dometic Compressor Fridge (DometicCompressorFridge)

> **Vehicles:** Eriba Car 602 (2025, VW Crafter). Not present on S600/S700 (which use Thetford on buses 34/37).
> **Contributed by:** @mvondemhagen ([#54](https://github.com/BetaHydri/hymer-connect-ha/issues/54))

| Slot | Sensor Name | Unit | Transform | Notes |
|------|------------|------|-----------|-------|
| (60, 1) | `dometic_fridge_mode` | — | — | User mode: "Silent Mode" / "Performance Cooling" / "Turbo Mode" (rw) |
| (60, 2) | `dometic_fridge_level` | — | — | Cooling level 1–5 (rw) |
| (60, 8) | `dometic_fridge_power` | — | — | Power on/off (rw, bool) |
| (60, 9) | `dometic_fridge_power_source` | — | — | Power source: "DC12V power" (r) |
| (60, 10) | `dometic_cibus_on` | — | — | CiBus communication active (r, bool) |
| (60, 11) | `dometic_compressor_on` | — | — | Compressor running (r, bool) |
| (60, 12) | `dometic_condenser_fan` | — | — | Condenser fan running (r, bool) |
| (60, 13) | `dometic_fridge_type` | — | — | Compressor type: "Compressor" (r) |
| (60, 16) | `dometic_fridge_warning` | — | — | Warning/error code 0–127 (r) |
| (60, 17) | `dometic_fridge_ai_type` | — | — | AI type: "Refrigeration" (r) |

> **EHG app metadata** defines 21 slots for bus 60 (`DometicCompressorFridge`, kind: `fridge`). Slots 3–7, 14–15, 18–21 are unmapped (not yet observed in live data). See `docs/ehg-app-metadata.md` for the full slot definitions.

## Bus 66 — Dinette pendant lamp (ML-T 570 CrossOver, confirmed 2026-06-01)

Not present on Grand Canyon S 600/S 700. Discovered and confirmed on a HYMER ML-T 570 CrossOver by user @mcfly1969 in [#7](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/7) via the dynamic-discovery diagnostic sensors. Member of the bus 24 *Wohnen* group — toggling the group also drives this light.

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (66, 1) | `light_dinette_pendant` | — | On/off |
| (66, 2) | `light_dinette_pendant_brightness` | % | Brightness (0–100, dimmable; no color temp) |

## Bus 99 — BOS LUX LiFePO4 BMS (4×80Ah)

Bus 99 is the BOS LUX LiFePO4 BMS on **both** S600 and S700 — same slot layout.
The old labels (AdBlue, engine torque, fuel range, gear) were incorrect; they
were remnants of an earlier wrong sensor map. Corrected by @dan-simms1 in
[#37](https://github.com/BetaHydri/hymer-connect-ha/issues/37). Legacy code
labels are noted below for historical reference only.

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (99, 1) | `bms_voltage` | V | BMS pack voltage. Legacy code label: `adblue_temp` |
| (99, 2) | `bms_current` | A | BMS current, negative = discharging. Legacy code label: `engine_torque` |
| (99, 3) | `bms_temperature` | °C | Pack cell temperature. Legacy code label: `ambient_temp` |
| (99, 4) | `lithium_soc` | % | Battery SOC |
| (99, 5) | `bms_time_remaining` | min | Estimated runtime. Legacy code label: `fuel_range` |
| (99, 6) | `bms_state_of_health` | % | Battery SoH. Legacy code label: `current_gear` |
| (99, 7) | `bms_capacity_remaining` | Ah | Remaining capacity. Legacy code label: `total_fuel_used` |
| (99, 8) | `lithium_soc_2` | % | Relative capacity |
| (99, 9) | `bms_charge_detected` | — | Charge active flag. Legacy code label: `cruise_control` |
| (99, 10) | `bms_device_failure` | — | BMS error flag. Legacy code label: `dpf_status` |

## Value Transforms

| Transform | Description |
|-----------|-------------|
| `div10` | Divide raw value by 10 |
| `div100` | Divide raw value by 100 |
| `div1000` | Divide raw value by 1000 |
| `div3600` | Convert seconds to hours |
| `invert100` | `100 - value` (inverted percentage) |

## Power Flow — Understanding the Three Current Sensors

The S600 reports current from three independent measurement points:

```
Solar Panel (19V)
      │
      ▼
┌─────────────┐
│  Voltronic  │  solar_current (bus 8)  → raw panel output
│  MPPT 260CI │  e.g. 2.1 A @ 19.4V = 40.7W
└──────┬──────┘
       │  MPPT converts to battery voltage
       ▼
┌─────────────┐
│  BOS LUX    │  bms_current (bus 99)   → net flow at battery
│  LiFePO4 BMS│  positive = charging, negative = discharging
│  4×80Ah     │  e.g. +1.54 A (net charge into cells)
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  CBE EBL402 │  battery_current (bus 3) → habitation load draw
│  Habitation │  negative = consuming power
│  Controller │  e.g. -0.37 A (SCU, fridge ECU, standby loads)
└─────────────┘
```

**Key relationships:**

- **Solar current** is measured at the panel side (higher voltage, lower
  current). After MPPT conversion the power stays the same but current
  increases at the lower battery voltage.
- **BMS current** is the net result: solar input minus habitation load.
  Positive means the battery is charging.
- **Battery current** (EBL) shows what the habitation system draws.
  This is always negative when loads are connected, even while solar is
  charging — it measures downstream of the battery, not the net flow.

A negative habitation current while BMS current is positive is normal —
the solar more than compensates for the load.

## Computed Fuel Sensors (v2.29.0)

These sensors are **not** read from a CAN bus slot. They are computed by the
coordinator from `(1,1) odometer` and `(1,2) fuel_level` plus the configured
diesel tank capacity (default: 93 L for Sprinter 419/519 CDI).

| Sensor | Unit | Description |
|--------|------|-------------|
| `fuel_level_liters` | L | `fuel_level_% × tank_capacity / 100` |
| `fuel_consumption` | L/100km | Trip-based: `(fuel_used_L / distance_km) × 100` |
| `fuel_range_estimated` | km | `fuel_liters / consumption_L100 × 100` |

**Trip tracking logic:**
- A reference point (odometer + fuel %) is stored on first reading
- Consumption is only computed after ≥ 5 km driven (noise filter)
- Refueling auto-detected when fuel level increases > 5% → trip resets
- Sanity bounds: only values between 2–60 L/100km are accepted

**Tank capacity configuration:**
Settings → Integrations → HYMER Connect → Configure → "Diesel tank capacity"
Range: 30–200 L. Common Sprinter values: 71 L (314/316 CDI), 93 L (419/519 CDI standard).

## Bus 114 — Thetford Compressor T2120C fridge (ML-T 570 CrossOver, confirmed 2026-06-07)

Not present on Grand Canyon S 600/S 700 — those use a Thetford absorber fridge on bus 34/37 instead.
Discovered and confirmed on a HYMER ML-T 570 CrossOver by user @mcfly1969 in
[#7](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/7) via the dynamic-discovery
diagnostic sensors. Initially mapped as "Dometic" in v2.62.29; corrected to **Thetford Compressor
T2120C-N306D310R25CI** (Item-No: 693465, 101.6 L + 17 L freezer) in v2.63.1 based on user
feedback in [#8](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/8).

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (114, 1) | `fridge_compressor_power` | bool | On/off (writable via `switch.fridge_compressor_power_ctrl`) |
| (114, 2) | `fridge_compressor_silent` | bool | Silent / night mode (writable via `switch.fridge_compressor_silent_ctrl`) |
| (114, 3) | `fridge_compressor_cooling_step` | step | Main compartment cooling step 1–5. **Writable via `select.fridge_compressor_cooling_step_ctrl`** (stepped-switch driver). |
| (114, 4) | `fridge_compressor_freezer` | step | Freezer compartment level: 0 = Off, 1–3 = step. **Writable via `select.fridge_compressor_freezer_ctrl`** (stepped-switch driver). |
| (114, 5) | `fridge_compressor_door` | bool | Door open/closed (FALSE = closed, TRUE = open). `binary_sensor` with `device_class: door`. |
| (114, 6) | `fridge_compressor_slot6` | int | Purpose unknown — user reports constant value 15. Not temperature (unchanged by cooling/weather). The EHG app defines DellCool error codes 0–11 for the `WarningErrorInformation` capability, but the PIA slot carrying those codes has not been confirmed (15 is outside 0–11 range). Under observation. |
| (114, 7) | `fridge_compressor_supply_voltage` | V | Fridge supply voltage in millivolts (raw), displayed as V via `div1000`. Oscillates 12.8–12.9 V under compressor load. Renamed from `fridge_compressor_warning` in v2.63.2. |

### DellCool error codes (Thetford Compressor T2120C / ThetfordT2152 / DellCoolFridge)

Extracted from the decompiled EHG app (APK 2.10.14). These codes apply to
DellCool-based compressor fridges on bus 114 (`ThetfordT2152`) and bus 116
(`DellCoolFridge`). The PIA slot carrying the error code on bus 114 has **not
been confirmed yet** — slot 6 reports a constant value of 15 (outside the 0–11
range), so the error code may arrive on an as-yet-undiscovered slot or only
appear transiently during fault conditions.

See [#8 (comment)](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/8#issuecomment-4653225087).

| Code | Meaning |
|------|---------|
| 0 | No error |
| 1 | Voltage error — input voltage out of range |
| 2 | Too many start attempts or fan overcurrent |
| 3 | Compressor start failure |
| 4 | Minimum speed error |
| 5 | Thermal shutdown of electronics |
| 6 | Compressor thermostat defective |
| 7 | Abnormal tilt angle |
| 8 | Hardware error — compressor controller |
| 9 | Fridge temperature sensor defective |
| 10 | Ambient temperature sensor defective |
| 11 | Ambient temperature shutdown |

## Bus 116 — DellCool Compressor Fridge (unmapped — no vehicle confirmed yet)

EHG component `DellCoolFridge` (kind: `fridge`). **Not yet mapped** — no user has
reported data on bus 116. Documented here so users with this fridge can create
their own brand overlay JSON. The DellCool error codes (0–11) above also apply
to this bus.

Compared to bus 114 (`ThetfordT2152`): slot 2 = `PowerMode` (not `NightMode`),
slot 4 = `CompressorOn` (not `FreezerLevel`), no `DCVoltage` slot (6 slots vs 7).

| Slot | EHG Capability | Suggested `name` | Notes |
|------|---------------|-----------------|-------|
| (116, 1) | `FridgeOn` | `dellcool_fridge_power` | Power on/off (bool, rw) |
| (116, 2) | `PowerMode` | `dellcool_fridge_power_mode` | Operating mode string (rw) — e.g. "Normal", "ECO" |
| (116, 3) | `FridgeLevel` | `dellcool_fridge_level` | Cooling step 1–5 (int, rw) |
| (116, 4) | `CompressorOn` | `dellcool_compressor_on` | Compressor running (bool, r) |
| (116, 5) | `DoorOpen` | `dellcool_fridge_door` | Door open/closed (bool, r) |
| (116, 6) | `WarningErrorInformation` | `dellcool_fridge_warning` | Error code 0–11 (int, r) — see DellCool error codes above |

<details>
<summary>Ready-to-use JSON overlay template for bus 116</summary>

Add this to your `{brand}.json` overlay file once a user confirms data on bus 116:

```json
{
  "sensors": {
    "116,1": {
      "_doc": "DellCool compressor fridge power on/off.",
      "name": "dellcool_fridge_power",
      "platform": "binary_sensor",
      "device_class": "power",
      "icon": "mdi:fridge"
    },
    "116,2": {
      "_doc": "DellCool operating mode (e.g. Normal, ECO).",
      "name": "dellcool_fridge_power_mode",
      "platform": "sensor",
      "icon": "mdi:fridge-variant"
    },
    "116,3": {
      "_doc": "DellCool cooling step 1-5. Decode-only — used by select entity.",
      "name": "dellcool_fridge_level",
      "icon": "mdi:fridge-industrial-outline"
    },
    "116,4": {
      "_doc": "DellCool compressor running state.",
      "name": "dellcool_compressor_on",
      "platform": "binary_sensor",
      "device_class": "running",
      "icon": "mdi:fridge-outline"
    },
    "116,5": {
      "_doc": "DellCool fridge door open/closed.",
      "name": "dellcool_fridge_door",
      "platform": "binary_sensor",
      "device_class": "door",
      "icon": "mdi:fridge-outline"
    },
    "116,6": {
      "_doc": "DellCool error code 0-11. See DellCool error codes in docs/sensor-map.md.",
      "name": "dellcool_fridge_warning",
      "platform": "sensor",
      "icon": "mdi:fridge-alert"
    }
  },
  "switches": {
    "116,1": {
      "name": "dellcool_fridge_power_ctrl",
      "icon": "mdi:fridge",
      "write_type": "bool",
      "on_value": true,
      "read_path": "signalr_sensors.dellcool_fridge_power",
      "requires_12v": true
    }
  }
}
```

</details>

## Bus 74 — SIU Smart Temperature Sensor (ML-T 570 CrossOver, confirmed 2026-06-08)

First **SIU (Smart Interface Unit)** sensor bus ever mapped. The SIU is an EHG BLE gateway that
connects external wireless sensors (temperature, humidity, tyre pressure, gas level, etc.) to the
SCU. Sensors pair to the SIU via QR code in the EHG app.

The ML-T 570 has 3 SIU temperature/humidity sensors in the EHG app (Kühlschrank / Schlafbereich /
Wohnbereich). Bus 74 is the first one confirmed — the other two likely use different bus IDs
(discovered buses 71, 73, 76, etc. are candidates). See `docs/external-sensors.md` for the full
SIU sensor ecosystem documentation.

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (74, 1) | `smart_temperature_1` | °C | Temperature reading. User reports 37.0 °C matching EHG app. |
| (74, 2) | `smart_humidity_1` | % | Humidity reading. User reports 32–33 % matching EHG app. |

## Bus 76 — Water tank levels (ML-T 570 CrossOver, confirmed 2026-06-08)

Not present on Grand Canyon S 600/S 700 — those use bus 3 slots 8/9 (`fresh_water_level_ebl` /
`grey_water_level_ebl`) from the CBE EBL402 controller. The ML-T 570 uses a separate bus 76
for water levels. Confirmed by @mcfly1969 by running water and watching discovered sensor
changes in real time ([#8](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/8)).

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (76, 1) | `fresh_water_level` | % | Fresh water tank level. Value decreases when water flows out. |
| (76, 2) | `gray_water_level` | % | Grey water tank level. Value increases when drain water flows in. |

## Bus 121 — Victron MultiPlus 12/1600/70 (inverter/charger) — NON-FUNCTIONAL

From EHG app metadata extraction (Dan, April 2026). SCU component 121 = VictronMultiplus.
**No data received** on S600 — even with Victron physically switched ON and entities
entities enabled. The Victron MultiPlus communicates via **VE.Bus** (RS-485), which
is incompatible with the vehicle CAN bus. A Victron Cerbo GX cannot bridge this
either — VE.Bus → VE.Can (250 kbps Victron-proprietary CAN) and VE.Bus → MQTT/Modbus
are supported, but neither VE.Bus nor VE.Can can bridge to the vehicle CAN bus
(different baud rates, protocols, and message structures).

The slot definitions exist in the EHG app metadata, suggesting EHG may have a
proprietary SCU-to-Victron interface (dedicated serial/LIN connection) on certain
vehicle configurations, or these are placeholder definitions that were never
implemented. No data has been observed on this bus on the S600.

All entities disabled by default.

| Slot | Sensor Name | R/W | Type | Notes |
|------|------------|-----|------|-------|
| (121, 1) | `victron_inverter_on` | rw | bool | Inverter power on/off |
| (121, 2) | `victron_inverter_state` | r | int | Inverter state code |
| (121, 3) | `victron_inverter_l1_voltage` | r | V | Inverter L1 output voltage |
| (121, 4) | `victron_inverter_l1_current` | r | A | Inverter L1 output current |
| (121, 5) | `victron_inverter_l1_frequency` | r | Hz | Inverter L1 output frequency |
| (121, 6) | `victron_inverter_l2_voltage` | r | V | Inverter L2 output voltage |
| (121, 7) | `victron_inverter_l2_current` | r | A | Inverter L2 output current |
| (121, 8) | `victron_inverter_l2_frequency` | r | Hz | Inverter L2 output frequency |
| (121, 9) | `victron_charger_on` | rw | bool | Charger power on/off |
| (121, 10) | `victron_charger_state` | r | — | Charger state |
| (121, 11) | `victron_charge_voltage` | r | V | Charge output voltage |
| (121, 12) | `victron_charge_current` | r | A | Charge output current |
| (121, 13) | `victron_max_charge_current` | rw | A | Maximum charge current limit |
| (121, 14) | `victron_input_current_limit` | rw | A | Shore power input current limit |
| (121, 15) | `victron_input_voltage` | r | V | Shore power input voltage |
| (121, 16) | `victron_input_current` | r | A | Shore power input current |
| (121, 17) | `victron_input_frequency` | r | Hz | Shore power input frequency |
| (121, 18) | `victron_device_failure` | r | — | Device failure status |
| (121, 19) | `victron_firmware` | r | — | Firmware version string |

## Bus 5 — Alde 3030 hydronic heater (HYMER BMC I 680 MY2024, confirmed 2026-07-11)

First **Alde** heater mapped in this repository. Not present on Grand Canyon S 600/S 700 or
ML-T 570 — those use a Truma diesel heater on bus 58/49 instead, so bus 5 is free on those
layouts. Discovered and confirmed on a HYMER BMC I 680 (MY2024) by user @FrankHae in
[#9](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/9) via RAW PIA toggle logs.
The full slot model (labels, datatypes, read/write flags, option lists) is confirmed against
the decompiled EHG app (`Alde3020` component, APK 2.10.14) — see below.

> **`Alde3020` vs. Alde 3030:** The vehicle has an **Alde Compact 3030** (current-generation
> hydronic heater). EHG models the whole Alde hydronic line under a single SCU component named
> `Alde3020` (the older-generation name) — the 3030 uses the same SCU interface and exposes the
> same parameters, which is why one slot map covers both. The 3030's extra fan stages (0/1/2)
> have no obvious slot in this component and may not be exposed via the SCU.

Read-only sensors (mapped in v2.64.7):

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (5, 1) | `alde_inside_temp` | °C | `Zone1ActualTemperature` (float). Matches the Hymer app value. |
| (5, 3) | `alde_setpoint` | °C | `Zone1TargetTemperature` (float, **rw**). Verified by changing it in the app (8/10/12/30 °C). Writable from HA via `number.hymer_alde_setpoint` (v2.65.5).
| (5, 5) | `alde_energy_priority` | — | `PrioElectricityGas` (string, **rw**): `Prio Gas` / `Prio EL`. |
| (5, 6) | `alde_hot_water_mode` | — | `HotWaterSetting` (string, **rw**): `Off` / `Normal` / `Boost`. Read sensor backing `select.hymer_alde_hot_water`. Added v2.65.2. |
| (5, 7) | `alde_electric_setting` | kW | `ElectricitySetting` (int, **rw**, 0–3 kW). Read sensor backing `select.hymer_alde_electric_booster`. Added v2.65.2. |
| (5, 9) | `alde_heating_on` | bool | `PanelOn` (bool, **rw**) — heater master on/off. `binary_sensor` `device_class: power`. |
| (5, 8) | `alde_warning` | bool | `PanelBusy` — a warning/attention is pending at the Alde panel (e.g. the antibacterial/Legionella boiler-service reminder). `binary_sensor` `device_class: problem`. **Renamed `alde_error` → `alde_warning` in v2.65.6** at @FrankHae's request: this slot is a panel-attention/warning flag, not a hard fault. **Remapped from slot 12 → slot 8 in v2.65.1** on @FrankHae's on-vehicle evidence: during a real Alde panel reminder (13–14 Jul 2026) slot 8 was `True` for the whole window and `False` otherwise, while slot 12 stayed `False`. **Only the boolean is transmitted — the message TEXT is panel-only.** A pending warning can block remote on/off from HA and the EHG app until acknowledged with **OK on the panel** (Alde firmware behaviour, not overridable). |
| (5, 14) | `alde_heating_active` | bool | `pump_running` — circulation pump active (= actively heating). `binary_sensor` `device_class: running`. |
| (5, 10) | `alde_gas_active` | bool | `GasSetting` (bool, **rw**) — gas enable. Read sensor backing `switch.hymer_alde_gas`. Added v2.65.2. |
| (5, 11) | `alde_acc_setting` | bool | `AccSetting` (bool, **rw** per decompiled model) — function unknown (likely an auxiliary output). Exposed **read-only** for now until @FrankHae observes what toggles it. Added v2.65.2. |
| (5, 12) | `alde_error` | bool | `Error` (bool, r) — dedicated hard-fault flag, distinct from the slot-8 panel-attention `alde_warning`. **Renamed `alde_fault` → `alde_error` and enabled in v2.65.6** at @FrankHae's request: this is the true fault flag. Stayed `False` during his antibacterial-reminder lockout (13–14 Jul 2026), confirming it only trips on a real fault. Added v2.65.2. |
| (5, 15) | `alde_outside_temp` | °C | `outdoor_actual_temperature` (float). Under-vehicle probe. |

Writable controls (added v2.64.8 — **bus-5 write path CONFIRMED on-vehicle by @FrankHae, issue [#9]**; only the Alde setpoint 5,3 float write is still deferred):

| Entity | Slot | Notes |
|--------|------|-------|
| `select.hymer_alde_energy_priority` | (5, 5) | Options `Prio Gas` / `Prio EL` (writes the literal string). Confirmed working. |
| `select.hymer_alde_hot_water` | (5, 6) | Options `Off` / `Normal` / `Boost` (`HotWaterSetting`, string). Confirmed working (arrives correctly in the EHG app). Added v2.65.2. |
| `select.hymer_alde_electric_booster` | (5, 7) | Options `Off` / `1 kW` / `2 kW` / `3 kW` (`ElectricitySetting`, int 0–3; display labels show the kW unit, the integer is written). Confirmed working. kW labels added v2.65.4. |
| `switch.hymer_alde_heating` | (5, 9) | Master on/off (`PanelOn`, bool). Confirmed working. |
| `switch.hymer_alde_gas` | (5, 10) | Gas enable (`GasSetting`, bool). Confirmed working. Added v2.65.2. |
| `number.hymer_alde_setpoint` | (5, 3) | Zone-1 target temperature (`Zone1TargetTemperature`, **float** °C, range 5–30, step 0.5). Written as a 32-bit float via the multi-sensor command path. **Write CONFIRMED on-vehicle by @FrankHae 2026-07-16** (issue #9): RAW PIA `bus=5 sid=3 f6/wt5=8.0` → `alde_setpoint 7.5 → 8.0`. Displayed as a **slider** since v2.65.6 (JSON-configurable `mode`). |

Confirmed but not yet exposed (from the decompiled `Alde3020` model — candidates for a future release once bus-5 writes are proven):

- (5, 2) `Zone2ActualTemperature` / (5, 4) `Zone2TargetTemperature` (rw) — second heating zone; on Frank's single-zone BMC they read constant placeholders (85.0 / 36.0).
- (5, 11) `AccSetting` (rw bool) — exposed read-only as `alde_acc_setting` in v2.65.2; a writable switch is deferred until its function is confirmed on-vehicle.
- (5, 13) `ac_installed` (r bool) — constant False on Frank's vehicle (no AC).

Unmapped slots remain available as disabled `Discovered bus 5 slot N` diagnostic sensors.

## Bus 10 — TenHaaft satellite dish (HYMER BMC I 680 MY2024, confirmed 2026-07-11)

EHG component `TenhaaftSatAntenna` (kind: `sat_antenna`). Confirmed on a HYMER BMC I 680 by
@FrankHae in [#9](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/9). Bus 10 is unused
on S600/S700/ML-T. The full slot model is confirmed against the decompiled EHG app.

Read-only sensors (mapped in v2.64.7):

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (10, 5) | `sat_satellite` | — | `SatellitePosition` (string, **rw**) — currently selected satellite, e.g. `Astra 1`, `Eutelsat 9`, `Hotbird`. |
| (10, 6) | `sat_status` | — | `state` (string) — dish status, e.g. `Searching clockwise`, `Satellite found`, `Is retracting`, `Is closed`. |
| (10, 8) | `sat_signal_strength` | % | `unified_signal_quality` (int) — 0–100. Reads `255` while idle/retracted. |

Writable control (added v2.64.9 — **write path UNVERIFIED on bus 10**, test build, revert if dropped):

| Entity | Slot | Notes |
|--------|------|-------|
| `select.hymer_satellite` | (10, 5) | Pick the target satellite from the app's 19-entry list (`Amos 2/3`, `Astra 1–5`, `Eutelsat 5W/7/8W/9/10/16`, `Hellas Sat 2`, `Hispasat`, `Hotbird`, `Intelsat 907`, `Telstar 12`, `Thor/Intelsat10`, `Türksat`). |

Confirmed in the decompiled model, not yet exposed:

- (10, 1) `start`, (10, 2) `park`, (10, 3) `stop_movement`, (10, 4) `open_sleep_mode` — bool **write-only** commands (candidates for future `button` entities).
- (10, 9) `dish_moving_state` (bool, r — **not** "satellite found"), (10, 10) `safe_position_state`, (10, 11) `unsupported_function_state`, (10, 12) `alarm_beeper_state`, (10, 13) `standby_mode_state`, (10, 14) `k15_state` — all bool r. (Slot 7 does not exist.)

### Recommended Home Assistant cards (satellite)

The satellite entities map cleanly onto stock HA cards (no custom/HACS frontend needed):

- **Satellite selection** (`select.hymer_satellite`) → an **Entities card** row or a **Tile card** — both render a `select` as a dropdown. On a Tile card, add the *"select"* feature to get the dropdown inline.
- **Signal strength** (`sensor.hymer_sat_signal_strength`, %) → a **Gauge card** (`min: 0`, `max: 100`, green severity above ~60) — the most intuitive "am I locked on?" indicator.
- **Dish status** (`sensor.hymer_sat_status`) → an **Entity card** or a small **Markdown card**; it shows the live search state (`Searching …`, `Satellite found`, `Is closed`).
- Group all three in a single **Entities card** titled "Satellite", or an **Entity Filter / Conditional card** that only shows signal strength while the dish is deployed.
- When the write-only commands (start/park/stop/sleep) are exposed later as buttons, a **Horizontal-stack of Button cards** is the natural layout.

## Bus 29 — Habitation battery (HYMER BMC I 680 MY2024, confirmed 2026-07-16)

Bus 29 is present on the HYMER BMC I 680 and unused on S600/S700/ML-T. Confirmed on-vehicle by
@FrankHae in [#9](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/9): slot 1 tracks the
**habitation / body battery (Aufbaubatterie) state of charge in %** and matches both the EHG app's
battery percentage and his Home Assistant history timing.

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (29, 1) | `body_battery_soc` | % | Habitation/body battery state of charge (`f3` int, 0–100, e.g. 99/100). `sensor` `device_class: battery`, `state_class: measurement`. Added v2.65.6. |

Unmapped slots remain available as disabled `Discovered bus 29 slot N` diagnostic sensors.

## Bus 32 — Thetford N4142E+ absorber fridge (HYMER BMC I 680 MY2024, confirmed 2026-07-11)

EHG component `ThetfordN4000` family. Not present on Grand Canyon S 600/S 700 (bus 34/37) or
ML-T 570 (bus 114), so bus 32 is free on those layouts. Confirmed on a HYMER BMC I 680 by
@FrankHae in [#9](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/9). Entity names use an
`_absorber_` prefix to stay distinct from the S600 N4000 (`fridge_*`) on bus 34.

| Slot | Sensor Name | Unit | Notes |
|------|------------|------|-------|
| (32, 1) | `fridge_absorber_power` | bool | `fridge_power` (bool, rw). `binary_sensor` with `device_class: power`. |
| (32, 2) | `fridge_absorber_power_mode` | — | `SetPowerMode` (string, rw) — power source. Read sensor backing the writable select below. |
| (32, 3) | `fridge_absorber_cooling_step` | step | `fridge_level` (int 1–5, rw). |
| (32, 5) | `fridge_absorber_door` | bool | `door_open` (bool, r — TRUE = open, FALSE = closed). `binary_sensor` `device_class: door`. |

Writable controls (⚠️ **write paths UNVERIFIED on-vehicle** — test builds, revert if the SCU drops the write):

| Entity | Slot | Added | Notes |
|--------|------|-------|-------|
| `select.hymer_absorber_fridge_cooling_step` | (32, 3) | v2.64.7 | Off / 1–5 (stepped-switch driver, mirrors bus 34/114: writes power sid 1 then step sid 3). |
| `select.hymer_absorber_fridge_power_source` | (32, 2) | v2.64.9 | `Automatic mode` / `Gas mode` / `12V mode` / `AC mode` (string select). The Auto/Gas/12V/230V control. |

Confirmed in the decompiled model, not yet exposed: (32, 8) `error_warning_information` (string, r), (32, 10) `automatic_mode_active` (bool, r).
