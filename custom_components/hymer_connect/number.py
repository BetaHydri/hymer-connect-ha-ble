"""Number platform for HYMER Connect — writable float slots (e.g. Alde setpoint).

Bus/slot IDs and ranges are loaded from the JSON ``"climate"."numbers"``
subsection in the brand overlay file.  Each entry drives one writable numeric
slot; the value is written to the SCU as a 32-bit float via the multi-sensor
command path (the same path the Truma climate setpoint uses).  See
``docs/sensor-map.md`` for the JSON contract.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import HymerConnectCoordinator
from .optimistic import OptimisticCommandMixin
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

    # Observation-gated number factories: created only once the vehicle reports
    # their backing read sensor, so absent components (e.g. the Alde setpoints
    # on a non-Alde vehicle) never leave phantom number sliders.  Entries
    # WITHOUT require_observed are created immediately, exactly as before.
    gated: list[tuple[str, Any, tuple[str, ...]]] = []

    for key, defn in NUMBER_DEFS.items():
        if defn.get("require_observed"):
            gated.append((
                key,
                lambda k=key, d=defn: HymerNumber(coordinator, entry, k, d),
                _number_read_sensors(defn),
            ))
            continue
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
        _LOGGER.debug("Number platform: no immediate number definitions — skipping")

    if not gated:
        return

    created_keys: set[str] = set()

    @callback
    def _async_discover_gated() -> None:
        if not coordinator.data:
            return
        sensors = coordinator.data.get("signalr_sensors")
        if not isinstance(sensors, dict):
            return
        new_entities: list[NumberEntity] = []
        for key, factory, watch in gated:
            if key in created_keys:
                continue
            if not any(name in sensors for name in watch):
                continue
            try:
                new_entities.append(factory())
            except Exception:  # noqa: BLE001 — never let one bad JSON entry kill the platform
                _LOGGER.exception("Failed to create gated number '%s' — skipping", key)
                created_keys.add(key)
                continue
            created_keys.add(key)
            _LOGGER.info(
                "Observation-gated number '%s' materialised (read sensor reported)",
                key,
            )
        if new_entities:
            async_add_entities(new_entities)

    _async_discover_gated()
    entry.async_on_unload(coordinator.async_add_listener(_async_discover_gated))


def _number_read_sensors(defn: dict[str, Any]) -> tuple[str, ...]:
    """Return the backing read sensor names a gated number watches."""
    read = defn.get("read") or {}
    names = [read.get("value_sensor")]
    return tuple(name for name in names if isinstance(name, str) and name)


class HymerNumber(CoordinatorEntity[HymerConnectCoordinator], NumberEntity, OptimisticCommandMixin):
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
        # Display mode: "slider" | "box" | "auto" (JSON-configurable, default
        # "auto" so HA renders a slider for a reasonable range and a box only
        # when the range is too large). Falls back to AUTO for unknown values.
        _mode_map = {
            "slider": NumberMode.SLIDER,
            "box": NumberMode.BOX,
            "auto": NumberMode.AUTO,
        }
        self._attr_mode = _mode_map.get(
            str(defn.get("mode", "auto")).lower(), NumberMode.AUTO
        )
        read = defn.get("read", {}) or {}
        self._value_sensor: str | None = read.get("value_sensor")
        # Wire datatype: "float" (default, 32-bit float) or "uint" for integer
        # slots (e.g. battery capacity Ah, Truma NEO target temperature).
        self._write_type: str = str(defn.get("write_type", "float")).lower()
        self._optimistic: float | None = None
        self._init_optimistic()

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
        """Write the new value to the SCU (float, or uint for integer slots)."""
        if not self._bus or not self._sid:
            _LOGGER.warning(
                "Number '%s': missing control_bus/sid — cannot write", self._key
            )
            return
        if self._write_type == "uint":
            payload = {"bus_id": self._bus, "sensor_id": self._sid, "uint_value": int(round(value))}
        else:
            payload = {"bus_id": self._bus, "sensor_id": self._sid, "float_value": float(value)}
        await self.coordinator.async_send_multi_sensor_command([payload])
        self._optimistic = float(value)
        self.async_write_ha_state()
        self._note_command(lambda p=payload: self.coordinator.async_send_multi_sensor_command([p]))

    def _has_pending_optimistic(self) -> bool:
        return self._optimistic is not None

    def _command_confirmed(self) -> bool:
        if self._optimistic is None or not self.coordinator.data or not self._value_sensor:
            return False
        raw = _resolve_path(
            self.coordinator.data, f"signalr_sensors.{self._value_sensor}"
        )
        try:
            return raw is not None and abs(float(raw) - self._optimistic) < 0.05
        except (TypeError, ValueError):
            return False

    def _clear_optimistic(self) -> None:
        self._optimistic = None

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state once confirmed or the TTL self-heals it."""
        if self._optimistic_ttl_expired():
            self._clear_optimistic()
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

    async def async_will_remove_from_hass(self) -> None:
        await self._cancel_verify()
        await super().async_will_remove_from_hass()
