"""Number platform for HYMER Connect — writable float slots (e.g. Alde setpoint).

Bus/slot IDs and ranges are loaded from the JSON ``"climate"."numbers"``
subsection in the brand overlay file.  Each entry drives one writable numeric
slot; the value is written to the SCU as a 32-bit float via the multi-sensor
command path (the same path the Truma climate setpoint uses).  See
``docs/sensor-map.md`` for the JSON contract.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import HymerConnectCoordinator
from .sensor import _resolve_path

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HYMER Connect number entities from a config entry."""
    from .pia_decoder import NUMBER_DEFS

    coordinator: HymerConnectCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[NumberEntity] = []

    for key, defn in NUMBER_DEFS.items():
        try:
            entities.append(HymerNumber(coordinator, entry, key, defn))
            _LOGGER.debug(
                "Number platform: '%s' on bus %s slot %s",
                key, defn.get("control_bus"), defn.get("sid"),
            )
        except Exception:  # noqa: BLE001 — never let one bad JSON entry kill the platform
            _LOGGER.exception("Failed to create number '%s' — skipping", key)

    if entities:
        async_add_entities(entities)
    else:
        _LOGGER.debug("Number platform: no number definitions — skipping")


class HymerNumber(CoordinatorEntity[HymerConnectCoordinator], NumberEntity):
    """Generic JSON-driven writable float slot (v2.65.5+).

    Reads its current value from a backing sensor and writes changes to the
    SCU as a 32-bit float using ``async_send_multi_sensor_command``.  Read
    source, target bus/slot and the value range live entirely in the brand
    overlay JSON under ``climate.numbers.<key>``.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HymerConnectCoordinator,
        entry: ConfigEntry,
        key: str,
        defn: dict[str, Any],
    ) -> None:
        """Initialize a writable number from a JSON definition."""
        super().__init__(coordinator)
        self._key = key
        self._defn = defn
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_icon = defn.get("icon", "mdi:knob")
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HYMER",
            "manufacturer": MANUFACTURER,
            "model": "Smart Interface Unit",
        }
        # Translation key takes precedence; otherwise use the JSON-provided
        # display name directly so adding a new device needs zero changes
        # to strings.json / translations/en.json.
        if "translation_key" in defn:
            self._attr_translation_key = defn["translation_key"]
        else:
            self._attr_name = defn.get("name") or key

        self._bus = int(defn.get("control_bus", 0))
        self._sid = int(defn.get("sid", 0))
        try:
            self._attr_native_min_value = float(defn.get("min", 0))
            self._attr_native_max_value = float(defn.get("max", 100))
            self._attr_native_step = float(defn.get("step", 1))
        except (TypeError, ValueError):
            self._attr_native_min_value = 0.0
            self._attr_native_max_value = 100.0
            self._attr_native_step = 1.0
        if "unit" in defn:
            self._attr_native_unit_of_measurement = defn["unit"]
        if "device_class" in defn:
            self._attr_device_class = defn["device_class"]
        self._attr_mode = NumberMode.BOX
        read = defn.get("read", {}) or {}
        self._value_sensor: str | None = read.get("value_sensor")
        self._optimistic: float | None = None

    @property
    def native_value(self) -> float | None:
        """Resolve the current value from coordinator data."""
        if self._optimistic is not None:
            return self._optimistic
        if self.coordinator.data is None or not self._value_sensor:
            return None
        raw = _resolve_path(
            self.coordinator.data, f"signalr_sensors.{self._value_sensor}"
        )
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Write the new float value to the SCU."""
        if not self._bus or not self._sid:
            _LOGGER.warning(
                "Number '%s': missing control_bus/sid — cannot write", self._key
            )
            return
        await self.coordinator.async_send_multi_sensor_command(
            [{"bus_id": self._bus, "sensor_id": self._sid, "float_value": float(value)}]
        )
        self._optimistic = float(value)
        self.async_write_ha_state()

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state once the coordinator confirms it."""
        if self._optimistic is not None and self.coordinator.data:
            raw = _resolve_path(
                self.coordinator.data, f"signalr_sensors.{self._value_sensor}"
            ) if self._value_sensor else None
            try:
                if raw is not None and abs(float(raw) - self._optimistic) < 0.05:
                    self._optimistic = None
            except (TypeError, ValueError):
                pass
        super()._handle_coordinator_update()
