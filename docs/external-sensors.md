# External SIU Sensors — EHG App Analysis

> **Audience:** Maintainers and advanced contributors. Normal users can ignore
> this file unless they are reverse-engineering SIU-based external sensors on a
> new vehicle.

> **Source:** HYMER Connect APK v2.10.14 — Hermes bytecode bundle string extraction
> **Date:** 2026-04-29
> **Method:** Regex extraction of readable strings (≥6 chars) from `index.android.bundle`

## Overview

The Erwin Hymer Group (EHG) ecosystem uses a **Smart Interface Unit (SIU)** to connect
external BLE sensors to the SCU (Smart Control Unit). The SIU acts as a BLE gateway —
sensors pair to the SIU via QR code, and the SIU relays data to the SCU.

## Wireless Protocol

**BLE (Bluetooth Low Energy) — exclusively.** No Zigbee, Thread, Z-Wave, or Matter support.

Evidence from the app code:

- `"Start BLE session"`, `"connected via BLE"`, `"BLE Request"`, `"BLE Response"`
- `"[SCU] Software update BSP and BLE for sensors"` — firmware updates target BLE radio
- `"This GATT service doesn't support by SIU"` — SIU validates BLE GATT service UUIDs
- `"scanBle"`, `"startPeripherals"`, `"stopPeripherals"` — BLE scanning functions
- Zero occurrences of `zigbee`, `z-wave`, `matter` (as protocol), or `thread` (as mesh protocol)

## Pairing Mechanism

Every vehicle section in the EHG app (Wasser, Licht, Energie, Klima, Komponenten) has an
**"Sensor hinzufügen" (Add sensor)** button at the bottom. Tapping it triggers the same
pairing flow regardless of section:

1. **BLE Connection** — The app requires an active BLE connection to the SCU/SIU first. A dialog `"Bluetooth-Verbindung erforderlich"` is shown if not connected. Sensor pairing is **not** possible via cloud/SignalR alone.
2. **QR Code Scan** — Each sensor package includes a QR code scanned by the app
3. **Whitelist Check** — `"Sensor is not whitelisted"` / `passesWhitelistCache` — the sensor identity is validated against a whitelist (likely server-side or SCU firmware)
4. **SCU Firmware Check** — `"De softwareversie van je voertuig is niet geschikt om een sensor toe te voegen"` (Your vehicle's software version is not suitable to add a sensor)
5. **Sensor Type Validation** — `"Mauvais type de capteur!"` (Wrong sensor type!) — the sensor type is checked during pairing

### App sections and their sensor types

| App Section | German Label | SIU Sensor Types |
|---|---|---|
| Wasser | Wasser | SIU.WATER, SIU.WWL (wired water level) |
| Licht | Licht | SIU.SWITCH (smart switches, Hegotec/Pegotec/Toptron modules) |
| Energie | Energie | SIU.BOS_BATTERY, SIU.LOAD (E-Load weight sensor) |
| Klima | Klima | SIU.TEMPERATURE (temperature/humidity sensors) |
| Komponenten | Komponenten | SIU.PRESSURE (TPMS), SIU.GAS, SIU.LEVELING, Contact sensors |

### Pairing Flow Strings

| String | Meaning |
|--------|---------|
| `SIU_PAIRING.BLUETOOTH_ON` | Enable Bluetooth prompt |
| `SIU_PAIRING.DEVICE_SCANNING` | Scanning for SIU devices |
| `SIU_PAIRING.PAIRING_BUTTON_ON` | Press pairing button on SIU |
| `PRESSURE_PAIRING.QR_SCANNED_STATUS` | QR code scan result |
| `PRESSURE_PAIRING.SUMMARY` | Pairing summary screen |
| `Sensor already paired with some Smart Unit` | Sensor bound to another SIU |
| `Pairing attempt succeed` | Successful pairing |
| `Pairing error:` | Pairing failure with details |

## Supported External Sensor Categories

### 1. SIU.PRESSURE — Tyre Pressure Monitoring (TPMS)

| Feature | Details |
|---------|---------|
| Type | BLE TPMS sensors |
| Axles | Front, Rear, Middle (configurable) |
| Positions | Left, Right per axle |
| Data | Pressure (bar/psi), Temperature (°C/°F) |
| Thresholds | Min/Max pressure, temperature upper limit |
| Pairing | Two QR code scans (one per sensor pair) |
| UI strings | `SIU.PRESSURE.AXLE.TITLE`, `SIU.PRESSURE.MOUNTING.FRONT_AXLE_LABEL`, `SIU.PRESSURE.MOUNTING.BACK_AXLE_LABEL`, `SIU.PRESSURE.MOUNTING.MIDDLE_AXLE_LABEL` |

### 2. SIU.GAS — Gas Level Sensors

| Feature | Details |
|---------|---------|
| Type | Ultrasonic/capacitive gas bottle level sensor |
| Variants | `"Senzor s 5 sondami"` (sensor with 5 probes) |
| Setup | Height measurement of gas bottle, rubber placement at cylinder center, sticker reminder |
| Data | Fill level (%) |
| Thresholds | Configurable low-level alert |
| UI strings | `SIU.GAS.HEIGHT.INPUT_LABEL`, `SIU.GAS.RUBBER.STATUS_LINE_LABEL`, `SIU.GAS.STICKER.TITLE` |

### 3. SIU.TEMPERATURE — Temperature & Humidity Sensors

| Feature | Details |
|---------|---------|
| Type | BLE temperature sensor |
| Data | Temperature (°C/°F) |
| Thresholds | Upper and lower temperature limits |
| Position | Selectable (`select_siu-temperature-position`) |
| UI strings | `SIU.TEMPERATURE.OVERVIEW.TEMPERATURE_LABEL`, `SIU.TEMPERATURE.OVERVIEW.UPPER_THRESHOLD_LABEL`, `SIU.TEMPERATURE.OVERVIEW.LOWER_THRESHOLD_LABEL` |

### 4. SIU.WATER — Water Level Sensors

| Feature | Details |
|---------|---------|
| Type | Capacitive/ultrasonic water level sensor |
| Tanks | Fresh water, Grey water, Black water (separate pairing flows) |
| Setup | Mounting position, tank location, tank capacity |
| Sensor Type | Selectable type (`SIU.WATER.SENSOR_TYPE_SELECTOR.TITLE`) — multiple sensor types supported |
| Data | Water level (%) |
| Thresholds | Upper and lower level alerts |
| UI strings | `SIU.WATER.LENGTH.CAPACITY_LABEL`, `SIU.WATER.LOCATION.TITLE`, `SIU.WATER.MOUNTING.TITLE` |

### 5. SIU.WWL — Wireless Water Level (Wired Variant)

| Feature | Details |
|---------|---------|
| Type | Wired water level sensor connected to SIU |
| Requirement | BLE connection to SIU required |
| Sensor selection | `SIU.WWL.SENSOR_SELECTOR.TITLE` |
| UI strings | `SIU.WWL.BLE_CONNECTION_REQUIRED`, `SIU.WWL.START.TITLE` |

### 6. SIU.LOAD — E-Load Weight/Towbar Sensors

| Feature | Details |
|---------|---------|
| Type | BLE load cell / weight sensor |
| Measurements | Nose wheel weight, towbar distance, wheel distance, angle |
| Behavior | Activates on button press, stays online 5 minutes to save battery |
| Data | Weight (kg), distance (cm), angle (°) |
| Thresholds | Weight limits |
| UI strings | `SIU.LOAD.NOSE_WHEEL.TITLE`, `SIU.LOAD.TOWBAR_DISTANCE.TITLE`, `SIU.LOAD.PULL_TAB.TITLE`, `SIU.LOAD.ANGLE.TITLE` |

Note: `"De trekgewichtsensor wordt ingeschakeld zodra de knop op het neuswiel wordt ingedrukt. Hij blijft 5 minuten online en gaat dan weer uit."` (The towbar sensor activates when the nose wheel button is pressed. It stays online for 5 minutes then turns off.)

### 7. SIU.LEVELING — Leveling Sensors

| Feature | Details |
|---------|---------|
| Type | BLE inclinometer / leveling sensor |
| Calibration | Vehicle length, width, start calibration |
| Data | Tilt angle (°) |
| Thresholds | Angle limits |
| UI strings | `SIU.LEVELING.CALIBRATION.LENGTH.TITLE`, `SIU.LEVELING.CALIBRATION.WIDTH.TITLE`, `SIU.LEVELING.CALIBRATION.CALIBRATE.TITLE` |

### 8. SIU.BOS_BATTERY — BOS LUX LiFePO4 Battery Sensors

| Feature | Details |
|---------|---------|
| Type | BLE battery management sensor |
| Manufacturer | **BOS LUX** (confirmed: `BosBatteryPairingVm`) |
| Data | Voltage (V), Temperature (°C), State of Charge (%) |
| Thresholds | Battery voltage, temperature, SOC tolerance |
| Battery types | Lead-acid (`PB_ACID`), Lithium LiFePO4 (`LITHIUM_LI_FE_PO_4`) |
| UI strings | `SIU.BOS_BATTERY.OVERVIEW.VOLTAGE_LABEL`, `SIU.BOS_BATTERY.OVERVIEW.TEMPERATURE_LABEL`, `BOS_BATTERY_PAIRING.THRESHOLDS_BATTERY_LITHIUM_POWER_ON` |

### 9. SIU.SWITCH — Smart Switches

| Feature | Details |
|---------|---------|
| Type | BLE smart switch module |
| Categories | Furniture (`SIU.SWITCH.SWITCH_CATEGORIES.FURNITURE`) |
| Verification | Pairing verification step |
| UI strings | `SIU.SWITCH.DETAILS.TITLE`, `SIU.SWITCH.VERIFICATION.TITLE` |

### 10. Contact Sensors (Door/Window)

| Feature | Details |
|---------|---------|
| Type | BLE contact sensor (open/close) |
| Function | Door and window state monitoring |
| Push notifications | `"Receive vehicle-triggered push notifications when your contact sensor is open"` |
| Sensor types | Default open, default closed (`-sensor-default-opened`, `-sensor-default-closed`) |

## Identified Hardware Manufacturers & Modules

These manufacturer references were found in the app code, indicating the OEM components that the EHG SIU ecosystem supports:

### Sensor Manufacturers

| Manufacturer | Product | Code Reference | Sensor Category |
|---|---|---|---|
| **BOS LUX** | LiFePO4 BMS | `BosBatteryPairingVm`, `BOS_BATTERY_PAIRING.*` | Battery monitoring |
| **Garnet Technologies** | SeeLevel Tank Monitoring | `See Level Tank Monitoring` | Water level |

### Integrated Vehicle Component Manufacturers

These are not SIU sensors but built-in vehicle components whose data appears in the EHG ecosystem:

| Manufacturer | Product | Code Reference | Function |
|---|---|---|---|
| **Truma** | Combi D6E Heater | `TrumaCombiNeoWaterHeater`, `TrumaCombiNeo_E` | Heating, hot water |
| **Truma** | Aventa 2G / Compact AC | `TrumaAventa2GACInstalled`, `TrumaAventaComfortAirConModel` | Air conditioning |
| **Thetford** | N4112A Fridge (Indus) | `getThetfordIndusToiletWaterError`, `thetfordT2095CompressorSettingFreezeMode` | Fridge, toilet |
| **Alde** | 3020 Water Heater | `Alde3020HotWaterSetting`, `AldeBoilerAdapter`, `AldeGasSettings` | Hydronic heating |
| **Airxcel** | AC Gateway | `AirxcelACGatewayRainSensorOnOff`, `AirxcelACGatewayRoofFanOnOff` | AC, rain sensor |
| **Victron** | MultiPlus Inverter | `CONTROLS.VICTRON_MULTIPLUS.*` | Inverter/charger |
| **CBE** | EBL402 / PL50 | `CBE_PL50_DIS`, `I7850_2_EBL40212VSupply` | Electrical panel |
| **Toptron** | EL711 Dimmer | `ToptronDimmerEL711` | LED dimming |
| **Hegotec / Pegotec** | Light Module | `HegotecLightModule`, `PegotecLightModuleNoGlass` | Interior lighting |
| **Votronic** | MPPT Solar | `SolarPanelVoltage` (via Bus 8) | Solar charge controller |
| **Dometic** | Series 10 Fridge | `DometicSeries10Integrator` | Compressor fridge |

## Third-Party Sensor Compatibility

### Can I use non-EHG sensors?

**Unlikely.** The app implements a strict pairing pipeline:

1. **QR Code identification** — Sensors are identified by a QR code containing a manufacturer-specific identifier. Generic BLE sensors won't have a recognized QR code.
2. **Whitelist enforcement** — `"Sensor ist nicht auf der Whitelist"` / `passesWhitelistCache` — the sensor ID is checked against a whitelist. Unknown sensor IDs are rejected.
3. **SCU firmware validation** — The SCU firmware must support the specific sensor type.
4. **GATT service validation** — `"This GATT service doesn't support by SIU"` — the BLE GATT service UUID must match expected values.

### What brands does the whitelist accept?

Based on code analysis, the EHG SIU ecosystem is **closed**. The supported sensor hardware comes from a small number of OEM suppliers (BOS LUX, Garnet/SeeLevel) that EHG has integrated. These sensors are sold through EHG dealers, often under the HYMER brand.

There is no evidence of a public API or SDK for third-party sensor integration.

### Recommendation for Home Assistant users

Since the SIU sensor ecosystem is closed, the best approach for monitoring additional parameters in Home Assistant is:

- **Use dedicated BLE integrations** (e.g., ESPHome BLE proxies, Mopeka, Ruuvi, Xiaomi) directly in Home Assistant
- **Use the existing SCU cloud/BLE integration** for built-in vehicle sensors
- **Do not attempt to pair generic BLE sensors** to the SIU — they will be rejected by the whitelist

## Whitelist & UUID Analysis — Can Sensors Be Spoofed?

### Identification System

The EHG sensor ecosystem uses a **URN-based identification system**, not raw BLE UUIDs:

| URN Pattern | Purpose | Example |
|---|---|---|
| `urn:ehg:scu:` | SCU identifier | SCU serial/model |
| `urn:ehg:sensor:` | Sensor identifier | Sensor serial from QR code |
| `urn:ehg:vehicle:` | Vehicle identifier | `urn:ehg:vehicle:hymer-b2-new-lp35` |

The QR code on each sensor package encodes a `urn:ehg:sensor:...` string containing the
sensor's serial number and type. The app parses this URN and forwards it to the SCU for
validation.

### QR Code Validation

| String | Meaning |
|--------|---------|
| `"came from qr-code is invalid"` | QR code content failed validation |
| `"De QR-code is ongeldig"` | Invalid QR code (Dutch) |
| `"Scan de QR-code op uw sensorpakket"` | Scan the QR on your sensor package |
| `"QR code gescand"` | QR code scanned successfully |

### BLE UUIDs Found in App Bundle

Only 4 UUIDs were found in the Hermes JS bundle:

| UUID | Purpose |
|---|---|
| `00000000-0000-0000-0000-000000000000` | Null UUID |
| `6a4c637e-cd09-4fe7-b5d7-3b7326990e5f` | App-internal identifier |
| `6ba7b810-9dad-11d1-80b4-00c04fd430c8` | RFC 4122 UUID namespace (DNS) |
| `6ba7b811-9dad-11d1-80b4-00c04fd430c8` | RFC 4122 UUID namespace (URL) |

**No sensor-specific BLE GATT service or characteristic UUIDs are present in the JS
bundle.** The actual GATT UUIDs for SIU sensors are implemented in the **native Android/iOS
layer** (Java/Kotlin/Swift), which is compiled to machine code and not accessible via
string extraction from the Hermes bundle.

### Whitelist Mechanism

| Code Reference | Function |
|---|---|
| `passesWhitelistCache` | Cached whitelist check — data originates from server or SCU firmware |
| `_blacklistConfig` | Blacklist configuration (likely for rejected sensor types) |
| `getScuAdvertisementNameByBrand` | SCU uses brand-specific BLE advertisement names |
| `isSiuStrategy` | Dynamic BLE strategy selection |
| `setBluetoothDeviceStrategy` | Device strategy is set per sensor type |
| `SensorPairingType` | Enum for sensor pairing type classification |
| `minimumSensorsSupportingScuBleVersion` | SCU BLE firmware version check for sensor support |

### Multi-Layer Validation

The sensor pairing pipeline has **5 independent validation layers**:

```
┌─────────────────────────────────────────────────────┐
│ Layer 1: QR Code Parse                              │
│ App parses urn:ehg:sensor:... from QR code.         │
│ Invalid format → "QR-code is ongeldig"              │
├─────────────────────────────────────────────────────┤
│ Layer 2: Whitelist Check (Server/Cache)             │
│ Sensor URN checked against passesWhitelistCache.    │
│ Unknown URN → "Sensor ist nicht auf der Whitelist"  │
├─────────────────────────────────────────────────────┤
│ Layer 3: SCU Firmware Version Check                 │
│ minimumSensorsSupportingScuBleVersion compared.     │
│ Old firmware → "Softwareversie niet geschikt"       │
├─────────────────────────────────────────────────────┤
│ Layer 4: BLE GATT Service Validation (Native/SIU)   │
│ SIU firmware checks GATT service UUIDs.             │
│ Wrong GATT → "This GATT service doesn't support"   │
├─────────────────────────────────────────────────────┤
│ Layer 5: Sensor Type Matching                       │
│ Sensor type from URN must match expected category.  │
│ Mismatch → "Mauvais type de capteur!"               │
└─────────────────────────────────────────────────────┘
```

### Can Third-Party Sensors Be Flashed to Spoof EHG Sensors?

**Not practical.** To successfully pair a third-party BLE sensor, you would need to
bypass all 5 validation layers simultaneously:

1. **Forge a valid QR code** — Must contain a valid `urn:ehg:sensor:` URN with an accepted
   serial number format. The exact format is not documented publicly.

2. **Pass the whitelist** — The URN must be recognized by `passesWhitelistCache`. Since
   this appears to be server-cached, the serial number likely needs to exist in the EHG
   backend database. You cannot simply invent a serial number.

3. **Match SCU firmware expectations** — The SCU firmware version must support the sensor
   type. This is a firmware-level check that cannot be bypassed from the app.

4. **Implement correct GATT services** — The SIU firmware validates the BLE GATT service
   and characteristic UUIDs. These UUIDs are **not in the JS bundle** — they are compiled
   into the native SIU firmware. You would need to reverse-engineer the SIU firmware or
   sniff the BLE traffic from an actual EHG sensor to discover the expected GATT profile.

5. **Match sensor type** — The sensor behavior must match the expected type (e.g., a
   pressure sensor must advertise pressure/temperature characteristics, not generic data).

### Theoretical Attack Vectors (For Research Only)

Even if attempted, the following challenges remain:

- **No public GATT UUIDs** — The sensor GATT profiles are proprietary to EHG/SIU and not
  found in the app's JS layer. BLE sniffing of an actual EHG sensor during pairing would
  be required.
- **Server-side validation** — `passesWhitelistCache` suggests the whitelist is fetched
  from the EHG cloud API, not stored locally. A forged serial number would fail server
  validation.
- **SIU firmware lock** — The SIU itself performs GATT validation at the firmware level.
  Even if the app accepted the sensor, the SIU firmware would reject it.
- **TLS-encrypted PIA protocol** — All SCU↔App communication uses TLS-encrypted PIA
  protobuf, making MITM manipulation of the pairing flow non-trivial.

### Verdict

The EHG SIU sensor ecosystem is a **closed, multi-layered authenticated system**. There is
no realistic path to pairing arbitrary third-party BLE sensors without access to:
- The EHG sensor URN database (or valid physical sensor QR codes)
- The SIU firmware's GATT service UUID expectations
- A way to bypass server-side whitelist validation

For Home Assistant users, the recommended approach remains using **independent BLE sensor
integrations** (Mopeka, Ruuvi, Xiaomi, ESPHome) directly in HA, bypassing the SCU/SIU
entirely.

## FOTA (Firmware Over The Air)

The SIU supports firmware updates via BLE:

- `SIU.FOTA.SCREEN_TITLE` — Firmware update screen
- `"[SCU] Software update BSP and BLE for sensors"` — Updates target both board support package and BLE radio
- `"This will take approximately 15 minutes. Please make sure to stay in Bluetooth range during the update."` — Update duration and requirements

## App Navigation Screens (SIU-related)

| Screen Key | Purpose |
|---|---|
| `siu_pressure_details_editing` | Edit TPMS sensor settings |
| `siu_gas_details_editing` | Edit gas level sensor settings |
| `siu_temperature_details_overview` | Temperature sensor overview |
| `siu_switch_details_overview` | Smart switch overview |
| `siu_load_details_overview` | E-Load weight sensor overview |
| `siu_leveling_details_overview` | Leveling sensor overview |
| `siu_leveling_calibration_length` | Leveling calibration (vehicle length) |
| `siu_dashboard_tab` | SIU sensor dashboard tab |
| `select_siu-temperature-position` | Temperature sensor position picker |
| `wired_water_level_pairing_sensor_selector` | Wired water level sensor selector |
