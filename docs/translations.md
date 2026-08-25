# Translations — when (and when not) to edit `strings.json` and `translations/en.json`

> **Audience:** Contributors and maintainers. Normal users do not need this file
> unless they are editing sensor maps or creating pull requests.

> **TL;DR**
>
> Whenever you add a new entity to a sensor map (the shared `custom_components/hymer_connect/sensor_maps/base.json` or `lights.json`, or — rarely — a brand-specific `sensor_maps/<brand>.json`), Home Assistant needs to know how to display its name. There are **two ways** the integration provides that name:
>
> 1. **Translation-key style** (the default) — the JSON `name` is just an identifier, and the real display name lives in **both** `custom_components/hymer_connect/strings.json` **and** `custom_components/hymer_connect/translations/en.json`. **You must edit both files for the entity to show a nice label in HA.**
> 2. **Direct-name style** (only the v2.63.0+ stepped-switch select driver) — the JSON `name` is shown verbatim, and the translation files are **skipped entirely**.
>
> Every entity type uses style 1 *except* the stepped-switch select. The cheat sheet at the bottom of this file is the quick lookup.

---

## Why two files? (`strings.json` vs `translations/en.json`)

- `strings.json` is the **source of truth** Home Assistant uses for the integration's UI strings during development and for any locale that does not have an override.
- `translations/en.json` is the **shipped English translation**. HA reads this at runtime for English users.

For the integration to display the right name in HA, **both files must contain the same key in the same section**. If `en.json` is missing a key, HA falls back to the raw key string (you will see something ugly like `light_bedroom_ceiling` instead of `"Bedroom ceiling light"`). If `strings.json` is missing the key, the linting / Hassfest checks will eventually flag it.

There is no other supported language file in this repo today, so editing the two English files is enough.

---

## Section layout (where to put new keys)

Both files have the same `entity.<platform>` structure. The platforms used by this integration are:

| Section in `strings.json` / `en.json` | Used by |
|---|---|
| `entity.sensor` | All `platform: sensor` entries — *plus* sub-sensors of lights (brightness, color temp) and selects (readback step), because each underlying value is also a sensor entity. |
| `entity.binary_sensor` | All `platform: binary_sensor` entries. |
| `entity.light` | The `lights` block in `lights.json` (one entry per light entity). |
| `entity.switch` | All `platform: switch` entries — plus controller entities ending in `_ctrl` for sensor-mirror switches. |
| `entity.climate` | The `climate.truma_heater` / `climate.fridge` blocks. |
| `entity.select` | The select entities created by `HymerFridgeSelect`, `HymerBoilerSelect`, `HymerHeaterEnergySelect`. **Not** used by the v2.63.0+ stepped-switch driver. |
| `entity.button` | All button entities (currently only `restart_system`). |

The key inside each section is the **same identifier you used in the brand overlay** (the `name` field, or the `_ctrl` suffix for the writable controller).

---

## Step-by-step playbook (per entity type)

The sensor-map edits below use `sensor_maps/base.json` as the example (a light entity goes in `sensor_maps/lights.json` instead). The steps are identical if you ever edit a brand-specific overlay such as `sensor_maps/hymer.json`.

### 1. Adding a `sensor` (read-only)

**Example**: a new tank pressure sensor on bus 50, slot 9.

1. **Sensor map** — `sensor_maps/base.json`:

   ```jsonc
   "sensors": {
     "50,9": {
       "platform": "sensor",
       "name": "fresh_water_pressure",
       "unit": "bar",
       "device_class": "pressure",
       "state_class": "measurement",
       "icon": "mdi:gauge"
     }
   }
   ```

2. **`strings.json`** — add inside `entity.sensor` (around the other tank entries):

   ```jsonc
   "fresh_water_pressure": {
     "name": "Fresh water pressure"
   }
   ```

3. **`translations/en.json`** — add the **same block** inside `entity.sensor`.

4. Reload the integration. HA will show *Fresh water pressure* in the UI.

### 2. Adding a `binary_sensor`

**Example**: a window-open sensor on bus 50, slot 12.

1. **Sensor map** (`base.json`):

   ```jsonc
   "50,12": {
     "platform": "binary_sensor",
     "name": "window_kitchen",
     "device_class": "window",
     "icon": "mdi:window-open"
   }
   ```

2. **`strings.json`** and **`translations/en.json`** — add to `entity.binary_sensor`:

   ```jsonc
   "window_kitchen": {
     "name": "Kitchen window"
   }
   ```

### 3. Adding a `light` (and its brightness / color-temp sub-sensors)

This is the **only entity type that needs entries in two sections** of the translation files: the `light` entry itself **and** the sub-sensors.

**Example**: a dimmable ceiling light on bus 70 (on/off at slot 1, brightness at slot 2).

1. **Sensor map** (`lights.json`) — add the *sub-sensors* in `sensors` and the *light entity* in `lights`:

   ```jsonc
   "sensors": {
     "70,1": { "platform": "sensor", "name": "light_kitchen_ceiling", "icon": "mdi:ceiling-light" },
     "70,2": { "platform": "sensor", "name": "light_kitchen_ceiling_brightness", "unit": "%", "state_class": "measurement", "icon": "mdi:brightness-percent" }
   },
   "lights": {
     "70": {
       "name": "light_kitchen_ceiling",
       "icon": "mdi:ceiling-light",
       "supports": { "brightness": true, "color_temp": false }
     }
   }
   ```

2. **`strings.json`** and **`translations/en.json`** — add **two** keys to `entity.sensor` (for the underlying values) and **one** key to `entity.light` (for the light entity itself):

   ```jsonc
   // entity.sensor
   "light_kitchen_ceiling": { "name": "Kitchen ceiling light (state)" },
   "light_kitchen_ceiling_brightness": { "name": "Kitchen ceiling light brightness" },

   // entity.light
   "light_kitchen_ceiling": { "name": "Kitchen ceiling light" }
   ```

> **Why three keys?** HA shows the *light* entity in the UI for switching, but you still get raw `sensor.light_kitchen_ceiling*` entities for the on/off and brightness values. Each entity gets its own display name.

For lights with color temperature, add a third sub-sensor (suffix `_color_temp`) and a third `entity.sensor` translation key, same pattern.

### 4. Adding a `switch`

**Example**: a writable mirror of an existing boolean sensor on bus 50, slot 1.

1. **Brand overlay**:

   ```jsonc
   "switches": {
     "50,1": {
       "name": "exterior_light_ctrl",
       "value_from": "exterior_light",
       "icon": "mdi:lightbulb-outline"
     }
   }
   ```

   (Note: the *underlying* sensor `exterior_light` must already exist in `sensors` and already have its own translation key in `entity.sensor`.)

2. **`strings.json`** and **`translations/en.json`** — add to `entity.switch`:

   ```jsonc
   "exterior_light_ctrl": {
     "name": "Exterior light"
   }
   ```

### 5. Adding a `stepped-switch select` (v2.63.0+)

**This is the only case where you do *not* edit the translation files.** The driver reads its display name directly from the `name` field in the JSON.

**Example**: a freezer compartment select on bus 114, slot 4 (the real-world v2.63.0 ML-T 570 case).

1. **Sensor map** — `base.json` only (no translation files):

   ```jsonc
   "climate": {
     "selects": {
       "fridge_compressor_freezer": {
         "control_bus": 114,
         "options": ["Off", "1", "2", "3"],
         "read":  { "step_sensor": "fridge_compressor_freezer", "off_value": 0 },
         "writes": {
           "off":  [{ "sid": 4, "uint": 0 }],
           "step": [{ "sid": 4, "uint": "$option_int" }]
         },
         "name": "Compressor fridge freezer",
         "icon": "mdi:snowflake"
       }
     }
   }
   ```

2. **`strings.json`** — **no edit needed**.
3. **`translations/en.json`** — **no edit needed**.
4. Reload the integration. The entity `select.fridge_compressor_freezer_ctrl` shows up with the display name *"Compressor fridge freezer"* read from the JSON.

> **What about the existing fridge / heater selects** (`fridge_mode_ctrl`, `boiler_mode_ctrl`, `heater_energy_ctrl`)? Those use the **classic** code-driven driver (`HymerFridgeSelect` etc.) and **do** need entries in `entity.select` of both translation files. The stepped-switch driver is the new generic one — only it is translation-free.

### 6. Adding a climate entity

A new climate entity (heater / fridge with setpoint) requires Python work in `climate.py` — see [`README.md`](../README.md) → *Step-by-step: From converted JSON to a working brand overlay*.

If you only add or rename a climate **device** (`climate.truma_heater`, `climate.fridge`), the display name lives in `entity.climate` of both translation files.

### 7. Adding a button

Buttons are hardcoded in `button.py`. If you add a new one, also add a key to `entity.button` of both translation files. Otherwise the button has no nice label.

---

## Cheat sheet

| Adding a … | Sensor map file | Section in `strings.json` + `translations/en.json` (BOTH FILES) |
|---|---|---|
| Sensor | `sensor_maps/base.json` | `entity.sensor` |
| Binary sensor | `sensor_maps/base.json` | `entity.binary_sensor` |
| Light | `sensor_maps/lights.json` | `entity.sensor` (for each sub-sensor) **and** `entity.light` (for the light) |
| Switch | `sensor_maps/base.json` | `entity.switch` |
| Classic fridge / heater select (`*_ctrl` from `HymerFridgeSelect` / `HymerBoilerSelect` / `HymerHeaterEnergySelect`) | `sensor_maps/base.json` | `entity.select` |
| **Stepped-switch select** (`climate.selects.<key>`, v2.63.0+) | `sensor_maps/base.json` | **None — name comes from JSON** |
| Climate device rename | `sensor_maps/base.json` | `entity.climate` |
| Button | Code (`button.py`) | `entity.button` |

**Rule of thumb**: any entity whose Python code sets `_attr_translation_key` needs the dual-file translation entry. The stepped-switch driver intentionally uses `_attr_name` from JSON instead, which is what makes it translation-free.

---

## Common mistakes (release checklist)

- ❌ Forgetting to update `translations/en.json` after `strings.json`. **Always edit both at the same time.**
- ❌ Adding a light without translation keys for its sub-sensors (only for the light itself). You will see ugly `sensor.light_xxx_brightness` names in the UI even though the light itself is named correctly.
- ❌ Adding a translation key to the wrong section (e.g. putting a `switch` entry under `entity.sensor`). HA silently ignores it.
- ❌ Writing the matching sensor-map key with a different spelling than the translation key. Compare them character-by-character.
- ❌ Forgetting to **reload the integration** (or restart HA) after editing — translation files are only read on startup / reload.

---

## Where this fits in the larger picture

- The brand-overlay JSON schema and the stepped-switch driver are documented in [`docs/sensor-map.md`](sensor-map.md).
- Existing Python-driven climate / select entities are described in [`README.md`](../README.md) → *Brand overlay architecture*.
- For changes that need Python (new fridge type with setpoint, new heater driver, …) see the *Brand overlay architecture* section in `README.md` — those changes additionally require `entity.climate` or `entity.select` translation entries.
