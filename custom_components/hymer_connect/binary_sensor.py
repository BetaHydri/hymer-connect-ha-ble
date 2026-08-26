"""Binary sensor platform for HYMER Connect.

Most binary sensor entity descriptions are generated dynamically from
JSON-defined entity metadata in ``sensor_maps/*.json``.  Only entities
with computed values or cross-referenced source keys stay hardcoded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import HymerConnectCoordinator
from .sensor import _resolve_path

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HymerBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describe a HYMER Connect binary sensor."""

    value_path: str
    on_value: Any = True


# ---------------------------------------------------------------------------
# Static binary sensor descriptions — entities with computed values or
# cross-referenced source keys that cannot be expressed in JSON.
# ---------------------------------------------------------------------------
STATIC_BINARY_SENSORS: tuple[HymerBinarySensorEntityDescription, ...] = (
    # cruise_control reads from "cruise_control" which has no SENSOR_MAP entry
    HymerBinarySensorEntityDescription(
        key="cruise_control",
        translation_key="cruise_control",
        value_path="signalr_sensors.cruise_control",
        icon="mdi:car-cruise-control",
    ),
    # Solar active is computed from solar_current > 0
    HymerBinarySensorEntityDescription(
        key="solar_active",
        translation_key="solar_active",
        device_class=BinarySensorDeviceClass.POWER,
        value_path="computed.solar_active",
        icon="mdi:solar-power",
    ),
)

# Keys of static descriptions — the dynamic builder skips these.
_STATIC_BINARY_KEYS: set[str] = {d.key for d in STATIC_BINARY_SENSORS}

# Observation-gated static binary sensors: created only once the backing
# sensor name has actually been reported by the vehicle.  Each tuple is
# (description, observed_sensor_name).  This avoids phantom "unknown"
# entities on vehicles that lack the component (e.g. the Dometic fridge).
_OBSERVED_GATED_BINARY: tuple[tuple[HymerBinarySensorEntityDescription, str], ...] = (
    # Dometic compressor fridge (bus 60) has no dedicated door bool slot — the
    # door-open state is encoded as value 10 in the slot-16 warning enum
    # (dometic_fridge_warning). Gated on that slot being observed.
    (
        HymerBinarySensorEntityDescription(
            key="dometic_fridge_door",
            translation_key="dometic_fridge_door",
            device_class=BinarySensorDeviceClass.DOOR,
            value_path="signalr_sensors.dometic_fridge_warning",
            on_value=10,
            icon="mdi:fridge-outline",
        ),
        "dometic_fridge_warning",
    ),
)


def _build_dynamic_binary_sensors() -> tuple[
    list[HymerBinarySensorEntityDescription],
    list[tuple[HymerBinarySensorEntityDescription, str]],
]:
    """Build binary sensor entity descriptions from JSON-loaded ENTITY_DEFS.

    Returns ``(always, gated)`` where ``gated`` entries carry
    ``require_observed`` and are created on demand once their slot is
    reported by the vehicle (keyed by the sensor name to watch).
    """
    from .pia_decoder import ENTITY_DEFS

    always: list[HymerBinarySensorEntityDescription] = []
    gated: list[tuple[HymerBinarySensorEntityDescription, str]] = []
    _gated_static_keys = {desc.key for desc, _ in _OBSERVED_GATED_BINARY}
    for name, meta in ENTITY_DEFS.items():
        if meta.get("platform") != "binary_sensor":
            continue
        if name in _STATIC_BINARY_KEYS or name in _gated_static_keys:
            continue
        kwargs: dict[str, Any] = {
            "key": name,
            "translation_key": name,
            "value_path": f"signalr_sensors.{name}",
        }
        if meta.get("device_class"):
            kwargs["device_class"] = BinarySensorDeviceClass(meta["device_class"])
        if meta.get("icon"):
            kwargs["icon"] = meta["icon"]
        if "on_value" in meta:
            kwargs["on_value"] = meta["on_value"]
        if meta.get("enabled") is False:
            kwargs["entity_registry_enabled_default"] = False
        desc = HymerBinarySensorEntityDescription(**kwargs)
        if meta.get("require_observed"):
            gated.append((desc, name))
        else:
            always.append(desc)
    return always, gated


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HYMER Connect binary sensors from a config entry."""
    coordinator: HymerConnectCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Build the full description list: static + JSON-driven dynamic
    dynamic, gated_dynamic = _build_dynamic_binary_sensors()
    all_descriptions = STATIC_BINARY_SENSORS + tuple(dynamic)
    _LOGGER.debug(
        "Binary sensor platform: %d static + %d dynamic (+%d gated) = %d created now",
        len(STATIC_BINARY_SENSORS),
        len(dynamic),
        len(gated_dynamic) + len(_OBSERVED_GATED_BINARY),
        len(all_descriptions),
    )

    async_add_entities(
        HymerConnectBinarySensor(coordinator, desc, entry)
        for desc in all_descriptions
    )

    # #24 diagnostic: expose "BLE degraded / writes failing" so a stale BlueZ
    # write channel (link up, MTU 23, every write fails) is visible without
    # reading the debug log. Only relevant when the BLE direct path is enabled.
    if coordinator.ble_enabled:
        async_add_entities([HymerBleDegradedBinarySensor(coordinator, entry)])

    # Observation-gated binary sensors: create each only once its backing
    # sensor name is actually reported by the vehicle, so absent components
    # (e.g. the Dometic fridge) never leave phantom entities behind.
    gated: list[tuple[HymerBinarySensorEntityDescription, str]] = [
        *_OBSERVED_GATED_BINARY,
        *gated_dynamic,
    ]
    created_keys: set[str] = {desc.key for desc in all_descriptions}

    @callback
    def _async_discover_gated() -> None:
        if not coordinator.data:
            return
        sensors = coordinator.data.get("signalr_sensors")
        if not isinstance(sensors, dict):
            return
        new_entities: list[HymerConnectBinarySensor] = []
        for desc, observed_key in gated:
            if desc.key in created_keys:
                continue
            if observed_key not in sensors:
                continue
            created_keys.add(desc.key)
            new_entities.append(HymerConnectBinarySensor(coordinator, desc, entry))
            _LOGGER.info(
                "Observation-gated binary sensor %s materialised (slot %s reported)",
                desc.key, observed_key,
            )
        if new_entities:
            async_add_entities(new_entities)

    if gated:
        _async_discover_gated()
        entry.async_on_unload(coordinator.async_add_listener(_async_discover_gated))


class HymerConnectBinarySensor(
    CoordinatorEntity[HymerConnectCoordinator], BinarySensorEntity
):
    """Representation of a HYMER Connect binary sensor."""

    entity_description: HymerBinarySensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HymerConnectCoordinator,
        description: HymerBinarySensorEntityDescription,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HYMER",
            "manufacturer": MANUFACTURER,
            "model": "Smart Interface Unit",
        }

    @property
    def is_on(self) -> bool | None:
        """Return True if the sensor is on."""
        if self.coordinator.data is None:
            return None

        path = self.entity_description.value_path

        # Computed binary sensors
        if path == "computed.solar_active":
            sensors = self.coordinator.data.get("signalr_sensors", {})
            current = sensors.get("solar_current")
            if isinstance(current, (int, float)):
                return current > 0
            return None

        value = _resolve_path(self.coordinator.data, path)
        if value is None:
            return None
        on_value = self.entity_description.on_value
        # Case-insensitive comparison for string values to handle
        # SCU casing variations (e.g. "ON"/"On"/"on", "Open"/"OPEN").
        if isinstance(value, str) and isinstance(on_value, str):
            return value.upper() == on_value.upper()
        return value == on_value


class HymerBleDegradedBinarySensor(
    CoordinatorEntity[HymerConnectCoordinator], BinarySensorEntity
):
    """Diagnostic sensor: on when the BLE write/notify channel is dead (#24).

    Reads the coordinator's degraded flag directly (not a vehicle slot), set when
    a stale BlueZ ``Write acquired`` acquisition survives a fresh GATT session.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "ble_degraded"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:bluetooth-off"

    def __init__(
        self,
        coordinator: HymerConnectCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the BLE-degraded diagnostic sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_ble_degraded"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HYMER",
            "manufacturer": MANUFACTURER,
            "model": "Smart Interface Unit",
        }

    @property
    def is_on(self) -> bool:
        """Return True while BLE is connected but its write channel is stale."""
        return self.coordinator.ble_write_degraded

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the reason so the failure is actionable from the UI."""
        reason = self.coordinator._ble_degraded_reason
        if reason:
            return {"reason": reason}
        return None
