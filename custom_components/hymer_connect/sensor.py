"""Sensor platform for HYMER Connect.

GUIDELINE: All new entities MUST be defined in sensor_maps/*.json
(base.json for universal buses, <brand>.json for brand-specific).
Only computed/calculated sensors (cross-referenced sources, formulas,
override logic) may remain as static Python descriptions.
The static descriptions below are legacy exceptions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfLength,
    UnitOfPower,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MANUFACTURER
from .coordinator import HymerConnectCoordinator

_LOGGER = logging.getLogger(__name__)

_DISCOVERED_KEY_RE = re.compile(r"^bus(\d+)_s(\d+)$")


@dataclass(frozen=True, kw_only=True)
class HymerSensorEntityDescription(SensorEntityDescription):
    """Describe a HYMER Connect sensor."""

    value_path: str
    restore_last: bool = False
    friendly_name: str | None = None


REST_SENSORS: tuple[HymerSensorEntityDescription, ...] = (
    HymerSensorEntityDescription(
        key="vehicle_model",
        translation_key="vehicle_model",
        value_path="model",
        icon="mdi:rv-truck",
    ),
    HymerSensorEntityDescription(
        key="vehicle_model_year",
        translation_key="vehicle_model_year",
        value_path="model_year",
        icon="mdi:calendar",
    ),
    HymerSensorEntityDescription(
        key="vehicle_vin",
        translation_key="vehicle_vin",
        value_path="vin",
        icon="mdi:identifier",
    ),
)

STATIC_SIGNALR_SENSORS: tuple[HymerSensorEntityDescription, ...] = (
    HymerSensorEntityDescription(
        key="fuel_level_liters",
        translation_key="fuel_level_liters",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        device_class=SensorDeviceClass.VOLUME_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_path="signalr_sensors.fuel_level_liters",
        icon="mdi:fuel",
        restore_last=True,
    ),
    HymerSensorEntityDescription(
        key="fuel_consumption",
        translation_key="fuel_consumption",
        native_unit_of_measurement="L/100km",
        state_class=SensorStateClass.MEASUREMENT,
        value_path="signalr_sensors.fuel_consumption",
        icon="mdi:gas-station-outline",
        restore_last=True,
    ),
    HymerSensorEntityDescription(
        key="fuel_range_estimated",
        translation_key="fuel_range_estimated",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        value_path="signalr_sensors.fuel_range_estimated",
        icon="mdi:map-marker-distance",
        restore_last=True,
    ),
    HymerSensorEntityDescription(
        key="solar_power",
        translation_key="solar_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_path="computed.solar_power",
        icon="mdi:solar-power",
    ),
    HymerSensorEntityDescription(
        key="battery_soc",
        translation_key="battery_soc",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        value_path="signalr_sensors.lithium_soc",
        icon="mdi:battery",
        restore_last=True,
    ),
    HymerSensorEntityDescription(
        key="charge_phase",
        translation_key="charge_phase",
        value_path="signalr_sensors.charge_phase",
        icon="mdi:battery-charging",
    ),
)

_STATIC_SENSOR_KEYS: set[str] = {
    d.key for d in REST_SENSORS + STATIC_SIGNALR_SENSORS
}

_DISPLAY_ACRONYMS = {"hss": "HSS"}


def _make_friendly_name(key: str) -> str:
    words = key.replace("_", " ").title().split()
    return " ".join(_DISPLAY_ACRONYMS.get(w.lower(), w) for w in words)


def _make_sensor_description(
    name: str, meta: dict[str, Any]
) -> HymerSensorEntityDescription | None:
    if meta.get("platform") != "sensor":
        return None
    if name in _STATIC_SENSOR_KEYS:
        return None
    kwargs: dict[str, Any] = {
        "key": name,
        "translation_key": name,
        "value_path": f"signalr_sensors.{name}",
        "friendly_name": _make_friendly_name(name),
    }
    if meta.get("unit"):
        kwargs["native_unit_of_measurement"] = meta["unit"]
    if meta.get("device_class"):
        kwargs["device_class"] = SensorDeviceClass(meta["device_class"])
    if meta.get("state_class"):
        kwargs["state_class"] = SensorStateClass(meta["state_class"])
    if meta.get("icon"):
        kwargs["icon"] = meta["icon"]
    if meta.get("enabled") is False:
        kwargs["entity_registry_enabled_default"] = False
    if meta.get("restore") is True:
        kwargs["restore_last"] = True
    return HymerSensorEntityDescription(**kwargs)


def _build_dynamic_sensors() -> list[HymerSensorEntityDescription]:
    from .pia_decoder import ENTITY_DEFS

    descriptions: list[HymerSensorEntityDescription] = []
    for name, meta in ENTITY_DEFS.items():
        # Observation-gated entries are created on demand by
        # _async_discover_slots() once the vehicle actually reports the slot.
        if meta.get("require_observed"):
            continue
        desc = _make_sensor_description(name, meta)
        if desc is not None:
            descriptions.append(desc)
    return descriptions


def _resolve_path(data: dict[str, Any], path: str) -> Any | None:
    current: Any = data
    for key in path.split("."):
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
        if current is None:
            return None
    return current


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HymerConnectCoordinator = hass.data[DOMAIN][entry.entry_id]

    dynamic = _build_dynamic_sensors()
    all_descriptions = REST_SENSORS + STATIC_SIGNALR_SENSORS + tuple(dynamic)
    _LOGGER.debug(
        "Sensor platform: %d static + %d dynamic = %d total descriptions",
        len(REST_SENSORS) + len(STATIC_SIGNALR_SENSORS),
        len(dynamic),
        len(all_descriptions),
    )

    async_add_entities(
        HymerConnectSensor(coordinator, desc, entry)
        for desc in all_descriptions
    )

    # Connection-mode status tile (BLE / cloud / dual) — reads coordinator
    # state directly, so it is a shipped entity rather than a JSON slot.
    async_add_entities([
        HymerConnectionModeSensor(coordinator, entry),
        HymerPairedBleDevicesSensor(coordinator, entry),
    ])

    created_named: set[str] = {desc.key for desc in all_descriptions}
    discovered: set[str] = set()

    @callback
    def _async_discover_slots() -> None:
        if not coordinator.data:
            return
        sensors = coordinator.data.get("signalr_sensors")
        if not isinstance(sensors, dict):
            return
        from .pia_decoder import ENTITY_DEFS

        new_entities: list[Any] = []
        for key in sensors:
            if key not in created_named and key in ENTITY_DEFS:
                desc = _make_sensor_description(key, ENTITY_DEFS[key])
                if desc is not None:
                    created_named.add(key)
                    new_entities.append(
                        HymerConnectSensor(coordinator, desc, entry)
                    )
                    _LOGGER.info(
                        "Auto-slot: adding runtime sensor entity %s", key
                    )
                    continue
            if key in discovered:
                continue
            match = _DISCOVERED_KEY_RE.match(key)
            if not match:
                continue
            discovered.add(key)
            bus_id = int(match.group(1))
            sensor_id = int(match.group(2))
            new_entities.append(
                HymerDiscoveredSensor(coordinator, entry, bus_id, sensor_id)
            )
            _LOGGER.info(
                "Discovered unmapped slot (%d,%d) — creating diagnostic entity %s",
                bus_id, sensor_id, key,
            )
        if new_entities:
            async_add_entities(new_entities)

    _async_discover_slots()
    entry.async_on_unload(coordinator.async_add_listener(_async_discover_slots))


class HymerConnectSensor(
    CoordinatorEntity[HymerConnectCoordinator], RestoreEntity, SensorEntity
):
    entity_description: HymerSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HymerConnectCoordinator,
        description: HymerSensorEntityDescription,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        if description.friendly_name:
            self._attr_name = description.friendly_name
            self._attr_translation_key = None
            self._attr_suggested_object_id = description.key
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HYMER",
            "manufacturer": MANUFACTURER,
            "model": "Smart Interface Unit",
        }
        self._last_real_value: Any | None = None
        self._last_real_update: str | None = None
        self._value_is_restored: bool = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if not self.entity_description.restore_last:
            return
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        if last_state.state in (None, "unknown", "unavailable", ""):
            return
        self._last_real_value = last_state.state
        self._last_real_update = last_state.attributes.get("last_real_update")
        self._value_is_restored = True

    def _live_value(self) -> Any | None:
        if self.coordinator.data is None:
            return None

        path = self.entity_description.value_path

        if path == "computed.solar_power":
            sensors = self.coordinator.data.get("signalr_sensors", {})
            voltage = sensors.get("solar_voltage")
            current = sensors.get("solar_current")
            if isinstance(voltage, (int, float)) and isinstance(current, (int, float)):
                return round(voltage * current, 1)
            return None

        if path == "signalr_sensors.charge_phase":
            sensors = self.coordinator.data.get("signalr_sensors", {})
            solar_current = sensors.get("solar_current")
            charger_active = sensors.get("charger_active")
            solar_charging = isinstance(solar_current, (int, float)) and solar_current > 0
            mains_charging = charger_active is True or charger_active == 1
            if not solar_charging and not mains_charging:
                return "Idle"

        value = _resolve_path(self.coordinator.data, path)
        if value is not None and isinstance(value, (int, float)):
            if value <= -273:
                return None
            if value in (3276.8, 32768.0, 65535.0, 6553.5):
                return None
        return value

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.entity_description.restore_last:
            live = self._live_value()
            if live is not None:
                if live != self._last_real_value or self._value_is_restored:
                    self._last_real_update = dt_util.utcnow().isoformat()
                self._last_real_value = live
                self._value_is_restored = False
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> Any | None:
        if not self.entity_description.restore_last:
            return self._live_value()

        live = self._live_value()
        if live is not None:
            return live
        return self._last_real_value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self.entity_description.restore_last:
            return None
        return {
            "last_real_update": self._last_real_update,
            "restored": self._value_is_restored,
        }


class HymerDiscoveredSensor(
    CoordinatorEntity[HymerConnectCoordinator], SensorEntity
):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:help-circle-outline"

    def __init__(
        self,
        coordinator: HymerConnectCoordinator,
        entry: ConfigEntry,
        bus_id: int,
        sensor_id: int,
    ) -> None:
        super().__init__(coordinator)
        self._key = f"bus{bus_id}_s{sensor_id}"
        self._attr_unique_id = f"{entry.entry_id}_discovered_{self._key}"
        self._attr_name = f"Discovered bus {bus_id} slot {sensor_id}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HYMER",
            "manufacturer": MANUFACTURER,
            "model": "Smart Interface Unit",
        }

    @property
    def native_value(self) -> Any | None:
        if self.coordinator.data is None:
            return None
        sensors = self.coordinator.data.get("signalr_sensors")
        if not isinstance(sensors, dict):
            return None
        value = sensors.get(self._key)
        if isinstance(value, (bytes, bytearray)):
            return value.hex()
        return value


class HymerConnectionModeSensor(
    CoordinatorEntity[HymerConnectCoordinator], SensorEntity
):
    """Status tile showing which transport currently carries SCU data.

    Reads ``coordinator.connection_mode`` (``ble`` / ``cloud`` / ``dual``) so a
    dashboard tile shows at a glance whether the live local BLE path is up, the
    cloud is carrying data, or both. Shipped as a first-class entity so it
    survives HACS updates (a locally patched sensor would be overwritten).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "connection_mode"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["ble", "cloud", "dual"]

    _MODE_ICONS = {
        "ble": "mdi:bluetooth",
        "cloud": "mdi:cloud-outline",
        "dual": "mdi:transit-connection-variant",
    }

    def __init__(
        self,
        coordinator: HymerConnectCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_connection_mode"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HYMER",
            "manufacturer": MANUFACTURER,
            "model": "Smart Interface Unit",
        }

    @property
    def native_value(self) -> str | None:
        mode = self.coordinator.connection_mode
        return mode if mode in self._attr_options else None

    @property
    def icon(self) -> str:
        return self._MODE_ICONS.get(
            self.coordinator.connection_mode, "mdi:help-network-outline"
        )


class HymerPairedBleDevicesSensor(
    CoordinatorEntity[HymerConnectCoordinator], SensorEntity
):
    """Diagnostic sensor listing the SCU's paired mobile devices.

    State is the device count; the full list (name / MAC / userUuid) is in
    ``extra_state_attributes.devices``. Populated over BLE by getPairedMobileDevices
    (read-only, BLE-only). Disabled by default; refresh via the 'Log paired BLE
    devices' button or automatically shortly after a BLE connect.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:cellphone-link"

    def __init__(
        self,
        coordinator: HymerConnectCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_paired_ble_devices"
        self._attr_name = "Paired BLE devices"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HYMER",
            "manufacturer": MANUFACTURER,
            "model": "Smart Interface Unit",
        }

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.paired_ble_devices_updated:
            return None
        return len(self.coordinator.paired_ble_devices)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "devices": self.coordinator.paired_ble_devices,
            "last_updated": self.coordinator.paired_ble_devices_updated,
        }
