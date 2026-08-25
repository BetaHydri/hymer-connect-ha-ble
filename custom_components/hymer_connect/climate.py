"""Climate platform for HYMER Connect — Truma heater control.

Bus/slot IDs are loaded from the JSON ``"climate"."truma_heater"`` section
in the brand overlay file (e.g. ``hymer.json``).  If no climate definition
is found, the climate entity is not created.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import HymerConnectCoordinator
from .sensor import _resolve_path

_LOGGER = logging.getLogger(__name__)

# Heater temperature range (from Truma spec)
MIN_TEMP = 5.0
MAX_TEMP = 30.0
TEMP_STEP = 1.0

# Sentinel value the SCU sends when heater is off
HEATER_OFF_SETPOINT = -273.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HYMER Connect climate from a config entry."""
    from .pia_decoder import (
        get_air_conditioner_defs,
        get_airxcel_zone_defs,
        get_modern_heater_defs,
        get_truma_heater_defs,
    )

    coordinator: HymerConnectCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Single-zone air-conditioners (Teleco / Saphir …) — each observation-gated.
    _setup_air_conditioners(coordinator, entry, async_add_entities, get_air_conditioner_defs())
    # Airxcel dual-zone A/C (front/rear) + modern enum heaters (Timberline).
    _setup_gated_climate(
        coordinator, entry, async_add_entities,
        get_airxcel_zone_defs(), HymerAirxcelClimate, "Airxcel zone",
    )
    _setup_gated_climate(
        coordinator, entry, async_add_entities,
        get_modern_heater_defs(), HymerModernHeaterClimate, "Modern heater",
    )

    heater_defs = get_truma_heater_defs()
    if not heater_defs:
        _LOGGER.debug("Climate platform: no truma_heater definition in JSON — skipping")
        return

    profiles: list[tuple[str, dict[str, Any], tuple[str, ...]]] = []
    for profile_key, heater_def in heater_defs:
        _LOGGER.debug(
            "Climate platform: %s on bus %d",
            profile_key,
            heater_def.get("heater_bus", 58),
        )
        # Component-specific read sensors used to gate creation (exclude the
        # generic outside_temperature so the gate only fires on a Truma slot).
        watch = tuple(
            n for n in (
                heater_def.get("setpoint_sensor", "heater_setpoint"),
                heater_def.get("fuel_type_sensor", "heater_fuel_type"),
                heater_def.get("boiler_sensor", "heater_fan_speed"),
                heater_def.get("electric_power_sensor", "heater_electric_power"),
            )
            if isinstance(n, str) and n
        )
        if not (heater_def.get("require_observed") and watch):
            async_add_entities([HymerHeaterClimate(coordinator, entry, heater_def)])
            return
        profiles.append((profile_key, heater_def, watch))

    # Observation-gated: create the thermostat only once the vehicle reports a
    # slot for one Truma profile, so mutually exclusive hardware variants do not
    # create duplicate entities or route writes to the wrong bus.
    created = False

    @callback
    def _async_discover_gated() -> None:
        nonlocal created
        if created or not coordinator.data:
            return
        sensors = coordinator.data.get("signalr_sensors")
        if not isinstance(sensors, dict):
            return
        for profile_key, heater_def, watch in profiles:
            if not any(name in sensors for name in watch):
                continue
            created = True
            async_add_entities([HymerHeaterClimate(coordinator, entry, heater_def)])
            _LOGGER.info(
                "Observation-gated Truma climate materialised (%s slot reported)",
                profile_key,
            )
            return

    _async_discover_gated()
    entry.async_on_unload(coordinator.async_add_listener(_async_discover_gated))


class HymerHeaterClimate(
    CoordinatorEntity[HymerConnectCoordinator], ClimateEntity
):
    """Truma heater climate entity."""

    _attr_has_entity_name = True
    _attr_translation_key = "truma_heater"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = TEMP_STEP
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    # NOTE: The Truma SCU bus exposes NO writable fan-speed slot. Slot 58:5
    # (water_heater_mode) was previously misused as fan_mode, which actually
    # toggled the boiler ECO/HOT mode. Use the dedicated boiler select for that
    # and the new heater air-mode select for OFF/Normal/Automatic (slot 58:11).
    # Vent mode and the 1-10 numeric vent steps are physical-panel only.
    _attr_icon = "mdi:radiator"

    def __init__(
        self,
        coordinator: HymerConnectCoordinator,
        entry: ConfigEntry,
        heater_def: dict[str, Any],
    ) -> None:
        """Initialize the heater climate entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_truma_heater"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HYMER",
            "manufacturer": MANUFACTURER,
            "model": "Smart Interface Unit",
        }
        self._optimistic_mode: HVACMode | None = None
        self._optimistic_temp: float | None = None
        # Bus/slot IDs from JSON
        self._bus = heater_def.get("heater_bus", 58)
        self._setpoint_sid = heater_def.get("setpoint_sid", 8)
        self._fuel_type_2_sid = heater_def.get("fuel_type_2_sid", 6)
        self._temp_sensor = heater_def.get("temp_sensor", "ambient_temp")
        self._setpoint_sensor = heater_def.get("setpoint_sensor", "heater_setpoint")
        self._fuel_type_sensor = heater_def.get("fuel_type_sensor", "heater_fuel_type")

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current HVAC mode."""
        if self._optimistic_mode is not None:
            return self._optimistic_mode
        setpoint = self._get_setpoint()
        if setpoint is not None and setpoint > HEATER_OFF_SETPOINT:
            return HVACMode.HEAT
        return HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return current HVAC action."""
        if self.hvac_mode == HVACMode.HEAT:
            return HVACAction.HEATING
        return HVACAction.OFF

    @property
    def current_temperature(self) -> float | None:
        """Return the current indoor temperature."""
        if self.coordinator.data is None:
            return None
        # Use ambient temp from CAN bus as current temp
        val = _resolve_path(self.coordinator.data, f"signalr_sensors.{self._temp_sensor}")
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
        return None

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature."""
        if self._optimistic_temp is not None:
            return self._optimistic_temp
        setpoint = self._get_setpoint()
        if setpoint is not None and setpoint > HEATER_OFF_SETPOINT:
            return setpoint
        return None

    def _get_setpoint(self) -> float | None:
        """Get heater setpoint from coordinator data."""
        if self.coordinator.data is None:
            return None
        val = _resolve_path(self.coordinator.data, f"signalr_sensors.{self._setpoint_sensor}")
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def _get_fuel_type(self) -> str:
        """Get current fuel type from coordinator data."""
        if self.coordinator.data:
            val = _resolve_path(self.coordinator.data, f"signalr_sensors.{self._fuel_type_sensor}")
            if val and isinstance(val, str) and val not in ("unknown", "unavailable"):
                return val
        return "Diesel"

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode (heat or off)."""
        if hvac_mode == HVACMode.HEAT:
            # Turn on with default 20°C
            temp = self._optimistic_temp or self._get_setpoint() or 20.0
            if temp <= HEATER_OFF_SETPOINT:
                temp = 20.0
            fuel = self._get_fuel_type()
            await self.coordinator.async_send_multi_sensor_command([
                {"bus_id": self._bus, "sensor_id": self._setpoint_sid, "float_value": temp},
                {"bus_id": self._bus, "sensor_id": self._fuel_type_2_sid, "str_value": fuel},
            ])
            self._optimistic_mode = HVACMode.HEAT
            self._optimistic_temp = temp
        else:
            fuel = self._get_fuel_type()
            _LOGGER.info(
                "Climate → OFF (bus=%d, fuel=%s)",
                self._bus, fuel,
            )
            await self.coordinator.async_send_multi_sensor_command([
                {"bus_id": self._bus, "sensor_id": self._setpoint_sid, "float_value": HEATER_OFF_SETPOINT},
                {"bus_id": self._bus, "sensor_id": self._fuel_type_2_sid, "str_value": fuel},
            ])
            self._optimistic_mode = HVACMode.OFF
            self._optimistic_temp = None

        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature."""
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return

        fuel = self._get_fuel_type()
        _LOGGER.info(
            "Climate setpoint → %.1f°C (bus=%d, fuel=%s)",
            float(temp), self._bus, fuel,
        )
        await self.coordinator.async_send_multi_sensor_command([
            {"bus_id": self._bus, "sensor_id": self._setpoint_sid, "float_value": float(temp)},
            {"bus_id": self._bus, "sensor_id": self._fuel_type_2_sid, "str_value": fuel},
        ])
        self._optimistic_mode = HVACMode.HEAT
        self._optimistic_temp = float(temp)
        self.async_write_ha_state()

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state when SCU confirms."""
        if self.coordinator.data and self._optimistic_mode is not None:
            setpoint = self._get_setpoint()
            if setpoint is not None:
                if self._optimistic_mode == HVACMode.OFF and setpoint <= HEATER_OFF_SETPOINT:
                    self._optimistic_mode = None
                    self._optimistic_temp = None
                elif self._optimistic_mode == HVACMode.HEAT and setpoint > HEATER_OFF_SETPOINT:
                    if self._optimistic_temp and abs(setpoint - self._optimistic_temp) < 0.5:
                        self._optimistic_mode = None
                        self._optimistic_temp = None
        super()._handle_coordinator_update()


# Wire values (from the decompiled EHG app) -> HA HVAC / fan modes.
_AC_MODE_TO_HVAC: dict[str, HVACMode] = {
    "OFF": HVACMode.OFF,
    "FAN": HVACMode.FAN_ONLY,
    "VENTILATION": HVACMode.FAN_ONLY,
    "COOL": HVACMode.COOL,
    "HEAT": HVACMode.HEAT,
    "DEHUMIDIFY": HVACMode.DRY,
    "AUTO": HVACMode.AUTO,
}
_AC_HVAC_TO_MODE: dict[HVACMode, str] = {
    HVACMode.OFF: "OFF",
    HVACMode.FAN_ONLY: "FAN",
    HVACMode.COOL: "COOL",
    HVACMode.HEAT: "HEAT",
    HVACMode.DRY: "DEHUMIDIFY",
    HVACMode.AUTO: "AUTO",
}
_AC_FAN_MODES = ["OFF", "LOW", "MID", "HIGH", "NIGHT", "AUTO"]


def _setup_air_conditioners(
    coordinator: HymerConnectCoordinator,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    ac_defs: tuple[tuple[str, dict[str, Any]], ...],
) -> None:
    """Create one observation-gated AC climate entity per def."""
    if not ac_defs:
        return
    created: set[str] = set()

    @callback
    def _discover() -> None:
        if not coordinator.data:
            return
        sensors = coordinator.data.get("signalr_sensors")
        if not isinstance(sensors, dict):
            return
        for key, ac_def in ac_defs:
            if key in created:
                continue
            gate = ac_def.get("mode_sensor") or ac_def.get("current_sensor")
            # Ungated defs (require_observed false) are created immediately.
            if ac_def.get("require_observed") and gate and gate not in sensors:
                continue
            created.add(key)
            async_add_entities([HymerACClimate(coordinator, entry, key, ac_def)])
            _LOGGER.info("Air-conditioner climate materialised: %s (bus %s)",
                         key, ac_def.get("control_bus"))

    _discover()
    entry.async_on_unload(coordinator.async_add_listener(_discover))


class HymerACClimate(CoordinatorEntity[HymerConnectCoordinator], ClimateEntity):
    """Single-zone air-conditioner climate (target/current/mode/fan).

    JSON-driven from ``"climate"."air_conditioners"``.  All writes are
    observation-gated test controls until confirmed on-vehicle.
    """

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1.0
    _attr_icon = "mdi:air-conditioner"
    _attr_fan_modes = _AC_FAN_MODES
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.FAN_MODE
    )

    def __init__(
        self,
        coordinator: HymerConnectCoordinator,
        entry: ConfigEntry,
        key: str,
        ac_def: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._bus = int(ac_def["control_bus"])
        self._target_sid = int(ac_def.get("target_sid", 1))
        self._mode_sid = int(ac_def.get("mode_sid", 3))
        self._fan_sid = int(ac_def.get("fan_sid", 4))
        self._target_sensor = ac_def.get("target_sensor", "")
        self._current_sensor = ac_def.get("current_sensor", "")
        self._mode_sensor = ac_def.get("mode_sensor", "")
        self._fan_sensor = ac_def.get("fan_sensor", "")
        self._attr_min_temp = float(ac_def.get("min_temp", 16))
        self._attr_max_temp = float(ac_def.get("max_temp", 32))
        self._attr_name = ac_def.get("name", "Air conditioner")
        self._attr_unique_id = f"{entry.entry_id}_ac_{key}_b{self._bus}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HYMER",
            "manufacturer": MANUFACTURER,
            "model": "Smart Interface Unit",
        }
        self._attr_hvac_modes = [
            HVACMode.OFF, HVACMode.FAN_ONLY, HVACMode.COOL,
            HVACMode.HEAT, HVACMode.DRY, HVACMode.AUTO,
        ]
        self._optimistic_mode: HVACMode | None = None
        self._optimistic_temp: float | None = None
        self._optimistic_fan: str | None = None

    def _sensor(self, name: str) -> Any:
        if not name or self.coordinator.data is None:
            return None
        return _resolve_path(self.coordinator.data, f"signalr_sensors.{name}")

    @property
    def hvac_mode(self) -> HVACMode | None:
        if self._optimistic_mode is not None:
            return self._optimistic_mode
        raw = self._sensor(self._mode_sensor)
        if isinstance(raw, str):
            return _AC_MODE_TO_HVAC.get(raw.upper())
        return None

    @property
    def hvac_action(self) -> HVACAction | None:
        mode = self.hvac_mode
        if mode == HVACMode.COOL:
            return HVACAction.COOLING
        if mode == HVACMode.HEAT:
            return HVACAction.HEATING
        if mode == HVACMode.DRY:
            return HVACAction.DRYING
        if mode == HVACMode.FAN_ONLY:
            return HVACAction.FAN
        if mode == HVACMode.OFF:
            return HVACAction.OFF
        return HVACAction.IDLE

    @property
    def current_temperature(self) -> float | None:
        val = self._sensor(self._current_sensor)
        try:
            return float(val) if val is not None else None
        except (ValueError, TypeError):
            return None

    @property
    def target_temperature(self) -> float | None:
        if self._optimistic_temp is not None:
            return self._optimistic_temp
        val = self._sensor(self._target_sensor)
        try:
            return float(val) if val is not None else None
        except (ValueError, TypeError):
            return None

    @property
    def fan_mode(self) -> str | None:
        if self._optimistic_fan is not None:
            return self._optimistic_fan
        raw = self._sensor(self._fan_sensor)
        return raw if isinstance(raw, str) and raw in _AC_FAN_MODES else None

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        wire = _AC_HVAC_TO_MODE.get(hvac_mode)
        if wire is None:
            return
        await self.coordinator.async_send_multi_sensor_command([
            {"bus_id": self._bus, "sensor_id": self._mode_sid, "str_value": wire},
        ])
        self._optimistic_mode = hvac_mode
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        await self.coordinator.async_send_multi_sensor_command([
            {"bus_id": self._bus, "sensor_id": self._target_sid, "float_value": float(temp)},
        ])
        self._optimistic_temp = float(temp)
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if fan_mode not in _AC_FAN_MODES:
            return
        await self.coordinator.async_send_multi_sensor_command([
            {"bus_id": self._bus, "sensor_id": self._fan_sid, "str_value": fan_mode},
        ])
        self._optimistic_fan = fan_mode
        self.async_write_ha_state()

    def _handle_coordinator_update(self) -> None:
        # Clear optimistic flags once the SCU readback matches.
        if self.coordinator.data:
            raw_mode = self._sensor(self._mode_sensor)
            if (
                self._optimistic_mode is not None
                and isinstance(raw_mode, str)
                and _AC_MODE_TO_HVAC.get(raw_mode.upper()) == self._optimistic_mode
            ):
                self._optimistic_mode = None
            raw_fan = self._sensor(self._fan_sensor)
            if self._optimistic_fan is not None and raw_fan == self._optimistic_fan:
                self._optimistic_fan = None
            raw_temp = self._sensor(self._target_sensor)
            if self._optimistic_temp is not None and raw_temp is not None:
                try:
                    if abs(float(raw_temp) - self._optimistic_temp) < 0.5:
                        self._optimistic_temp = None
                except (ValueError, TypeError):
                    pass
        super()._handle_coordinator_update()


try:  # ATTR_TARGET_TEMP_LOW/HIGH moved across HA cores
    from homeassistant.components.climate.const import (
        ATTR_TARGET_TEMP_HIGH,
        ATTR_TARGET_TEMP_LOW,
    )
except ImportError:  # pragma: no cover
    from homeassistant.const import ATTR_TARGET_TEMP_HIGH, ATTR_TARGET_TEMP_LOW

_HEAT_COOL_MODE = getattr(HVACMode, "HEAT_COOL", HVACMode.AUTO)
_TARGET_RANGE_FEATURE = getattr(ClimateEntityFeature, "TARGET_TEMPERATURE_RANGE", 0)

_AIRXCEL_MODE_TO_HVAC: dict[str, HVACMode] = {
    "OFF": HVACMode.OFF,
    "COOL": HVACMode.COOL,
    "HEAT": HVACMode.HEAT,
    "AUTO_HEAT_COOL": _HEAT_COOL_MODE,
    "FAN_ONLY": HVACMode.FAN_ONLY,
    "AUX_HEAT": HVACMode.HEAT,
}
_AIRXCEL_HVAC_TO_MODE: dict[HVACMode, str] = {
    HVACMode.OFF: "OFF",
    HVACMode.COOL: "COOL",
    HVACMode.HEAT: "HEAT",
    _HEAT_COOL_MODE: "AUTO_HEAT_COOL",
    HVACMode.FAN_ONLY: "FAN_ONLY",
}
_AIRXCEL_FAN_MODES = ["AUTO", "LOW", "MED", "HIGH"]

# Modern enum-heater option -> HA HVAC (Timberline air heater).
_MODERN_MODE_TO_HVAC: dict[str, HVACMode] = {
    "OFF": HVACMode.OFF,
    "HEAT": HVACMode.HEAT,
    "HEATING": HVACMode.HEAT,
    "FAN_ONLY": HVACMode.FAN_ONLY,
    "VENTILATING": HVACMode.FAN_ONLY,
}


def _setup_gated_climate(
    coordinator: HymerConnectCoordinator,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    defs: tuple[tuple[str, dict[str, Any]], ...],
    cls: type,
    label: str,
) -> None:
    """Create one observation-gated climate entity per def, on the mode sensor."""
    if not defs:
        return
    created: set[str] = set()

    @callback
    def _discover() -> None:
        if not coordinator.data:
            return
        sensors = coordinator.data.get("signalr_sensors")
        if not isinstance(sensors, dict):
            return
        for key, cdef in defs:
            if key in created:
                continue
            gate = cdef.get("mode_sensor") or cdef.get("current_sensor")
            if cdef.get("require_observed") and gate and gate not in sensors:
                continue
            created.add(key)
            async_add_entities([cls(coordinator, entry, key, cdef)])
            _LOGGER.info("%s climate materialised: %s (bus %s)",
                         label, key, cdef.get("control_bus"))

    _discover()
    entry.async_on_unload(coordinator.async_add_listener(_discover))


class HymerAirxcelClimate(CoordinatorEntity[HymerConnectCoordinator], ClimateEntity):
    """Airxcel dual-zone A/C climate with separate heat/cool targets."""

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1.0
    _attr_icon = "mdi:air-conditioner"
    _attr_fan_modes = _AIRXCEL_FAN_MODES
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | _TARGET_RANGE_FEATURE
    )

    def __init__(self, coordinator, entry, key, cdef) -> None:
        super().__init__(coordinator)
        self._bus = int(cdef["control_bus"])
        self._mode_sid = int(cdef["mode_sid"])
        self._fan_mode_sid = int(cdef["fan_mode_sid"])
        self._fan_speed_sid = int(cdef["fan_speed_sid"])
        self._heat_sid = int(cdef["heat_sid"])
        self._cool_sid = int(cdef["cool_sid"])
        self._mode_sensor = cdef.get("mode_sensor", "")
        self._fan_mode_sensor = cdef.get("fan_mode_sensor", "")
        self._fan_speed_sensor = cdef.get("fan_speed_sensor", "")
        self._heat_sensor = cdef.get("heat_sensor", "")
        self._cool_sensor = cdef.get("cool_sensor", "")
        self._current_sensor = cdef.get("current_sensor", "")
        self._attr_min_temp = float(cdef.get("min_temp", 10))
        self._attr_max_temp = float(cdef.get("max_temp", 35))
        self._attr_name = cdef.get("name", "Airxcel A/C")
        self._attr_unique_id = f"{entry.entry_id}_airxcel_{key}_b{self._bus}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HYMER",
            "manufacturer": MANUFACTURER,
            "model": "Smart Interface Unit",
        }
        self._attr_hvac_modes = [
            HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT,
            _HEAT_COOL_MODE, HVACMode.FAN_ONLY,
        ]

    def _sensor(self, name: str) -> Any:
        if not name or self.coordinator.data is None:
            return None
        return _resolve_path(self.coordinator.data, f"signalr_sensors.{name}")

    def _temp(self, name: str) -> float | None:
        val = self._sensor(name)
        try:
            f = float(val) if val is not None else None
        except (ValueError, TypeError):
            return None
        return f if f is not None and f > HEATER_OFF_SETPOINT else None

    @property
    def hvac_mode(self) -> HVACMode | None:
        raw = self._sensor(self._mode_sensor)
        return _AIRXCEL_MODE_TO_HVAC.get(raw.upper()) if isinstance(raw, str) else None

    @property
    def hvac_action(self) -> HVACAction | None:
        mode = self.hvac_mode
        if mode == HVACMode.COOL:
            return HVACAction.COOLING
        if mode == HVACMode.HEAT:
            return HVACAction.HEATING
        if mode == HVACMode.FAN_ONLY:
            return HVACAction.FAN
        if mode == HVACMode.OFF:
            return HVACAction.OFF
        return HVACAction.IDLE

    @property
    def current_temperature(self) -> float | None:
        return self._temp(self._current_sensor)

    @property
    def target_temperature(self) -> float | None:
        if self.hvac_mode == _HEAT_COOL_MODE:
            return None
        if self.hvac_mode in (HVACMode.COOL, HVACMode.FAN_ONLY):
            return self._temp(self._cool_sensor)
        return self._temp(self._heat_sensor)

    @property
    def target_temperature_low(self) -> float | None:
        return self._temp(self._heat_sensor) if self.hvac_mode == _HEAT_COOL_MODE else None

    @property
    def target_temperature_high(self) -> float | None:
        return self._temp(self._cool_sensor) if self.hvac_mode == _HEAT_COOL_MODE else None

    @property
    def fan_mode(self) -> str | None:
        fm = self._sensor(self._fan_mode_sensor)
        if isinstance(fm, str) and fm.upper() == "AUTO":
            return "AUTO"
        fs = self._sensor(self._fan_speed_sensor)
        return fs if isinstance(fs, str) and fs in _AIRXCEL_FAN_MODES else None

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        wire = _AIRXCEL_HVAC_TO_MODE.get(hvac_mode)
        if wire is None:
            return
        await self.coordinator.async_send_multi_sensor_command([
            {"bus_id": self._bus, "sensor_id": self._mode_sid, "str_value": wire},
        ])
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        low = kwargs.get(ATTR_TARGET_TEMP_LOW)
        high = kwargs.get(ATTR_TARGET_TEMP_HIGH)
        cmds: list[dict[str, Any]] = []
        if low is not None and high is not None:
            cmds.append({"bus_id": self._bus, "sensor_id": self._heat_sid, "float_value": float(low)})
            cmds.append({"bus_id": self._bus, "sensor_id": self._cool_sid, "float_value": float(high)})
        else:
            temp = kwargs.get(ATTR_TEMPERATURE)
            if temp is None:
                return
            sid = self._cool_sid if self.hvac_mode in (HVACMode.COOL, HVACMode.FAN_ONLY) else self._heat_sid
            cmds.append({"bus_id": self._bus, "sensor_id": sid, "float_value": float(temp)})
        await self.coordinator.async_send_multi_sensor_command(cmds)
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if fan_mode not in _AIRXCEL_FAN_MODES:
            return
        if fan_mode == "AUTO":
            cmds = [{"bus_id": self._bus, "sensor_id": self._fan_mode_sid, "str_value": "AUTO"}]
        else:
            cmds = [
                {"bus_id": self._bus, "sensor_id": self._fan_mode_sid, "str_value": "ON"},
                {"bus_id": self._bus, "sensor_id": self._fan_speed_sid, "str_value": fan_mode},
            ]
        await self.coordinator.async_send_multi_sensor_command(cmds)
        self.async_write_ha_state()


class HymerModernHeaterClimate(CoordinatorEntity[HymerConnectCoordinator], ClimateEntity):
    """Modern enum-based heater climate (int mode slot + float target)."""

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1.0
    _attr_icon = "mdi:radiator"
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

    def __init__(self, coordinator, entry, key, cdef) -> None:
        super().__init__(coordinator)
        self._bus = int(cdef["control_bus"])
        self._mode_sid = int(cdef["mode_sid"])
        self._target_sid = int(cdef["target_sid"])
        self._mode_sensor = cdef.get("mode_sensor", "")
        self._target_sensor = cdef.get("target_sensor", "")
        self._current_sensor = cdef.get("current_sensor", "")
        self._options: list[str] = [str(o).upper() for o in cdef.get("mode_options", [])]
        self._attr_min_temp = float(cdef.get("min_temp", 5))
        self._attr_max_temp = float(cdef.get("max_temp", 35))
        self._attr_name = cdef.get("name", "Heater")
        self._attr_unique_id = f"{entry.entry_id}_modern_heater_{key}_b{self._bus}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HYMER",
            "manufacturer": MANUFACTURER,
            "model": "Smart Interface Unit",
        }
        modes: list[HVACMode] = []
        for opt in self._options:
            hv = _MODERN_MODE_TO_HVAC.get(opt)
            if hv is not None and hv not in modes:
                modes.append(hv)
        if HVACMode.OFF not in modes:
            modes.insert(0, HVACMode.OFF)
        self._attr_hvac_modes = modes or [HVACMode.OFF, HVACMode.HEAT]

    def _sensor(self, name: str) -> Any:
        if not name or self.coordinator.data is None:
            return None
        return _resolve_path(self.coordinator.data, f"signalr_sensors.{name}")

    def _mode_index(self) -> int | None:
        raw = self._sensor(self._mode_sensor)
        try:
            return int(raw) if raw is not None else None
        except (ValueError, TypeError):
            return None

    @property
    def hvac_mode(self) -> HVACMode | None:
        idx = self._mode_index()
        if idx is None or idx < 0 or idx >= len(self._options):
            return None
        return _MODERN_MODE_TO_HVAC.get(self._options[idx])

    @property
    def hvac_action(self) -> HVACAction | None:
        mode = self.hvac_mode
        if mode == HVACMode.HEAT:
            return HVACAction.HEATING
        if mode == HVACMode.FAN_ONLY:
            return HVACAction.FAN
        if mode == HVACMode.OFF:
            return HVACAction.OFF
        return HVACAction.IDLE

    @property
    def current_temperature(self) -> float | None:
        val = self._sensor(self._current_sensor)
        try:
            return float(val) if val is not None else None
        except (ValueError, TypeError):
            return None

    @property
    def target_temperature(self) -> float | None:
        val = self._sensor(self._target_sensor)
        try:
            f = float(val) if val is not None else None
        except (ValueError, TypeError):
            return None
        return f if f is not None and f > HEATER_OFF_SETPOINT else None

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        # Map the requested HVAC mode back to the first matching enum index.
        target_opt = None
        for opt in self._options:
            if _MODERN_MODE_TO_HVAC.get(opt) == hvac_mode:
                target_opt = opt
                break
        if target_opt is None:
            return
        idx = self._options.index(target_opt)
        await self.coordinator.async_send_multi_sensor_command([
            {"bus_id": self._bus, "sensor_id": self._mode_sid, "uint_value": idx},
        ])
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        await self.coordinator.async_send_multi_sensor_command([
            {"bus_id": self._bus, "sensor_id": self._target_sid, "float_value": float(temp)},
        ])
        self.async_write_ha_state()
