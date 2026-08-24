# Changing the climate "current temperature" source (JSON)

> **Audience:** Users and contributors who want the Truma heater card in Home
> Assistant to *display* a different room-temperature reading — for example a
> HYMER Smart Sensor (HSS) paired to the SCU instead of the default probe.

## TL;DR — read this first

You **can** change which temperature the Truma climate entity **shows** as its
*current temperature*, purely by editing one field in the shared sensor map
(`sensor_maps/base.json` → `climate.truma_heater.temp_sensor`).

> [!IMPORTANT]
> **This does NOT change how the Truma heater behaves.** It only changes the
> number Home Assistant *displays* on the climate card. The heater keeps
> regulating from its **own built-in ambient sensor** inside the vehicle — the
> one wired to the Truma/SCU. Home Assistant never runs a thermostat loop; when
> you set a target temperature it just forwards that **setpoint** to the SCU, and
> the Truma firmware decides when to fire based on *its* sensor, not on whatever
> value HA shows.
>
> In other words: pointing `temp_sensor` at a HYMER BLE sensor is a **cosmetic /
> informational** change to the dashboard reading. The heating comfort
> temperature, hysteresis, and on/off timing are unchanged.

If what you actually want is for the heater to regulate from a *different* sensor
(true "remote thermostat" behaviour), that is **not possible** from this
integration — the regulation happens inside the Truma/SCU firmware and is not
exposed. See [Why it can't change heater behaviour](#why-it-cant-change-heater-behaviour)
below.

## What the `temp_sensor` field controls

The Truma climate entity is defined in the shared `base.json` under
`climate.truma_heater` (see [`sensor-map.md`](sensor-map.md)):

```jsonc
"climate": {
  "truma_heater": {
    "heater_bus": 58,
    "setpoint_sid": 8,
    "temp_sensor": "outside_temperature",   // ← the DISPLAYED "current temperature"
    "setpoint_sensor": "heater_setpoint",
    // ...
  }
}
```

At runtime the climate entity's `current_temperature` is resolved as:

```text
signalr_sensors.<temp_sensor>
```

So `temp_sensor` must be the **name of a sensor that the integration already
produces** (i.e. a `(bus, slot)` slot mapped in `base.json` or a brand overlay).
The value is looked up in the SCU sensor dictionary that is fed by **both** the
cloud/SignalR push **and** the BLE PIA mirror — so a HYMER Smart Sensor works
here exactly like any built-in SCU sensor.

## Example: use a HYMER Smart Sensor (HSS) as the shown temperature

HYMER Smart Sensors (the wireless BLE temperature/humidity pucks that pair to the
SCU) are mapped on **bus 74** and auto-numbered. The first paired temperature
sensor materialises at runtime as the sensor name:

```text
hss_temp1_temperature
```

(The second is `hss_temp2_temperature`, and so on. See the `74,1#tp{n}` auto-slot
template in `base.json` and the auto-slot rules in
[`sensor-map.md`](sensor-map.md).)

To make the Truma climate card display that HSS reading instead of the default,
change **one line** in `sensor_maps/base.json`:

```jsonc
"climate": {
  "truma_heater": {
    "heater_bus": 58,
    "setpoint_sid": 8,
    "temp_sensor": "hss_temp1_temperature",   // ← was "outside_temperature"
    "setpoint_sensor": "heater_setpoint",
    // ... leave the rest unchanged
  }
}
```

Restart Home Assistant (or reload the integration). The Truma climate card now
shows the HSS reading as its *current temperature*. The dedicated
`sensor.hymer_hss_temp1_temperature` entity keeps working too — nothing is
removed, the climate card simply *mirrors* that value for display.

## Requirements & limits

- **The replacement must be an SCU-sourced sensor.** `temp_sensor` is resolved
  under `signalr_sensors.*`, which only contains sensors the SCU delivers
  (cloud push or BLE PIA). Good choices: `hss_temp1_temperature` (bus 74),
  another mapped `*_temp` slot, `alde_inside_temp` (bus 5), etc.
- **You cannot point it at an arbitrary Home Assistant entity** (e.g. a Xiaomi,
  Ruuvi, or Mopeka thermometer integrated directly into HA). Those never enter
  the SCU sensor dictionary, so the JSON lookup would return nothing. To display
  a truly third-party sensor on the climate card you would need a
  [Template](https://www.home-assistant.io/integrations/template/) helper or a
  custom card — that is outside this integration's JSON.
- **Name it, don't renumber it.** Use the runtime sensor **name**
  (`hss_temp1_temperature`), not the raw `(bus, slot)` key.
- **No translation change needed.** `temp_sensor` references an existing sensor
  name; you are not adding a new entity, so `strings.json` / `translations/en.json`
  are untouched.

## Why it can't change heater behaviour

The control path and the display path are completely separate:

| Path | Source in code | Effect |
| --- | --- | --- |
| **Display** — climate card "current temperature" | `climate.truma_heater.temp_sensor` → `signalr_sensors.<name>` | Cosmetic. What HA shows. |
| **Control** — turning the heater on/off & target | `async_set_temperature` / `async_set_hvac_mode` write the **setpoint** to bus 58 slot 8 | HA forwards the target to the SCU. |

When you drag the target temperature slider, Home Assistant sends that value to
the SCU as a **setpoint** and stops there. The **Truma/SCU firmware** then
compares its **own internal ambient probe** against the setpoint and switches the
burner. Home Assistant is not in that loop and has no access to the probe the
Truma regulates from. Changing `temp_sensor` therefore cannot move the actual
comfort point — it only changes the informational reading on the card.

## See also

- [`sensor-map.md`](sensor-map.md) — the `climate.truma_heater` block and the
  bus 74 HSS auto-slot template.
- [`contributing-overlays.md`](contributing-overlays.md) — how the overlay JSON
  is structured and loaded.
- [`external-sensors.md`](external-sensors.md) — how HYMER/EHG BLE sensors (HSS)
  reach the SCU, and why generic third-party BLE sensors are best integrated
  directly into Home Assistant instead.
