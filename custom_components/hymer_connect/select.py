"""Select platform for HYMER Connect — fridge mode, boiler mode, heater energy.

Bus/slot IDs are loaded from the JSON ``"climate"`` section in the brand
overlay file.  If a ``"fridge"`` or ``"truma_heater"`` definition is missing,
the corresponding select entity is not created.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import HymerConnectCoordinator
from .optimistic import OptimisticCommandMixin
from .sensor import _resolve_path

_LOGGER = logging.getLogger(__name__)

# Fridge cooling steps (ECO is a separate switch, not a mode)
FRIDGE_OPTIONS = ["Off", "1", "2", "3", "4", "5"]

# Boiler modes: Off, ECO, Turbo (HOT)
BOILER_OPTIONS = ["Off", "ECO", "Turbo"]

# Heater energy source modes matching Truma panel display
HEATER_ENERGY_OPTIONS = ["Diesel", "Mix 900W", "Mix 1800W", "Electric 900W", "Electric 1800W"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HYMER Connect select entities from a config entry."""
    from .pia_decoder import CLIMATE_DEFS, STEPPED_SELECT_DEFS, get_truma_heater_defs

    coordinator: HymerConnectCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SelectEntity] = []

    # Observation-gated select factories: created only once the vehicle reports
    # one of their read sensors, so absent components (e.g. a Dometic fridge on
    # a non-Dometic vehicle, or the Thetford fridge on a compressor-fridge
    # vehicle) never leave phantom selects.  Entries WITHOUT require_observed
    # are created immediately, exactly as before.
    gated: list[
        tuple[str, Callable[[], SelectEntity], tuple[str, ...], str | None]
    ] = []

    fridge_def = CLIMATE_DEFS.get("fridge")
    heater_defs = get_truma_heater_defs()
    selected_heater_profile: str | None = None

    if fridge_def:
        watch = _climate_read_sensors("fridge", fridge_def)
        if fridge_def.get("require_observed") and watch:
            gated.append((
                "fridge",
                lambda d=fridge_def: HymerFridgeSelect(coordinator, entry, d),
                watch,
                None,
            ))
        else:
            entities.append(HymerFridgeSelect(coordinator, entry, fridge_def))
        _LOGGER.debug("Select platform: fridge on bus %d", fridge_def.get("control_bus", 34))
    for profile_key, heater_def in heater_defs:
        watch = _climate_read_sensors("truma_heater", heater_def)
        if heater_def.get("require_observed") and watch:
            gated.append((
                f"{profile_key}_boiler",
                lambda d=heater_def: HymerBoilerSelect(coordinator, entry, d),
                watch,
                profile_key,
            ))
            if heater_def.get("supports_energy_select", True):
                gated.append((
                    f"{profile_key}_energy",
                    lambda d=heater_def: HymerHeaterEnergySelect(coordinator, entry, d),
                    watch,
                    profile_key,
                ))
        else:
            entities.append(HymerBoilerSelect(coordinator, entry, heater_def))
            if heater_def.get("supports_energy_select", True):
                entities.append(HymerHeaterEnergySelect(coordinator, entry, heater_def))
            selected_heater_profile = profile_key
        _LOGGER.debug(
            "Select platform: %s controls on bus %d",
            profile_key,
            heater_def.get("heater_bus", 58),
        )
        if selected_heater_profile is not None:
            break

    # Generic JSON-driven stepped-switch selects (v2.63.0+).
    for key, defn in STEPPED_SELECT_DEFS.items():
        if defn.get("require_observed"):
            gated.append((
                key,
                lambda k=key, d=defn: HymerSteppedSelect(coordinator, entry, k, d),
                _stepped_read_sensors(defn),
                None,
            ))
            continue
        try:
            entities.append(HymerSteppedSelect(coordinator, entry, key, defn))
            _LOGGER.debug(
                "Select platform: stepped select '%s' on bus %s",
                key, defn.get("control_bus"),
            )
        except Exception:  # noqa: BLE001 — never let one bad JSON entry kill the platform
            _LOGGER.exception("Failed to create stepped select '%s' — skipping", key)

    if entities:
        async_add_entities(entities)
    else:
        _LOGGER.debug("Select platform: no immediate fridge, heater, or stepped definitions")

    # Unpair-target picker (disabled by default). Selecting only RECORDS the
    # choice — the destructive delete happens on the separate button press.
    async_add_entities([HymerUnpairBleDeviceSelect(coordinator, entry)])

    if not gated:
        return

    created_keys: set[str] = set()

    @callback
    def _async_discover_gated() -> None:
        nonlocal selected_heater_profile
        if not coordinator.data:
            return
        sensors = coordinator.data.get("signalr_sensors")
        if not isinstance(sensors, dict):
            return
        new_entities: list[SelectEntity] = []
        for key, factory, watch, heater_profile in gated:
            if key in created_keys:
                continue
            if (
                heater_profile is not None
                and selected_heater_profile is not None
                and heater_profile != selected_heater_profile
            ):
                continue
            if not any(name in sensors for name in watch):
                continue
            try:
                new_entities.append(factory())
            except Exception:  # noqa: BLE001 — never let one bad JSON entry kill the platform
                _LOGGER.exception("Failed to create gated select '%s' — skipping", key)
                created_keys.add(key)
                continue
            created_keys.add(key)
            if heater_profile is not None:
                selected_heater_profile = heater_profile
            _LOGGER.info(
                "Observation-gated select '%s' materialised (read sensor reported)",
                key,
            )
        if new_entities:
            async_add_entities(new_entities)

    _async_discover_gated()
    entry.async_on_unload(coordinator.async_add_listener(_async_discover_gated))


def _climate_read_sensors(kind: str, defn: dict[str, Any]) -> tuple[str, ...]:
    """Return the component-specific sensor names a climate select reads.

    Excludes generic chassis sensors (e.g. ``outside_temperature``) so the
    observation gate only fires on a sensor unique to the component.
    """
    if kind == "fridge":
        names = [
            defn.get("power_sensor", "fridge_power"),
            defn.get("cooling_step_sensor", "fridge_cooling_step"),
            defn.get("mode_sensor", "fridge_mode"),
        ]
    elif kind == "truma_heater":
        names = [
            defn.get("setpoint_sensor", "heater_setpoint"),
            defn.get("fuel_type_sensor", "heater_fuel_type"),
            defn.get("boiler_sensor", "heater_fan_speed"),
            defn.get("electric_power_sensor", "heater_electric_power"),
        ]
    else:
        names = []
    return tuple(n for n in names if isinstance(n, str) and n)


def _stepped_read_sensors(defn: dict[str, Any]) -> tuple[str, ...]:
    """Return the sensor names a stepped select reads its state from."""
    read = defn.get("read") or {}
    names = [
        read.get("step_sensor"),
        read.get("value_sensor"),
        read.get("power_sensor"),
    ]
    return tuple(name for name in names if isinstance(name, str) and name)


class HymerUnpairBleDeviceSelect(
    CoordinatorEntity[HymerConnectCoordinator], SelectEntity
):
    """Picker for which paired device to unpair — RECORDS the choice only.

    Selecting an option never touches the SCU; it just stores the target MAC in
    the coordinator. The destructive unpair happens on the separate
    'Unpair selected BLE device' button. Disabled by default. Options come from
    the last paired-devices fetch (getPairedMobileDevices).
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:cellphone-remove"

    _NONE = "— none —"

    def __init__(
        self,
        coordinator: HymerConnectCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_unpair_ble_device"
        self._attr_name = "BLE device to unpair"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HYMER",
            "manufacturer": MANUFACTURER,
            "model": "Smart Interface Unit",
        }

    def _label(self, device: dict) -> str:
        name = device.get("name") or "(unnamed)"
        return f"{name} ({device['mac']})"

    @property
    def options(self) -> list[str]:
        return [self._NONE] + [
            self._label(d) for d in self.coordinator.paired_ble_devices
        ]

    @property
    def current_option(self) -> str:
        mac = self.coordinator.unpair_selected_mac
        if mac:
            for d in self.coordinator.paired_ble_devices:
                if d["mac"].lower() == mac:
                    return self._label(d)
        return self._NONE

    async def async_select_option(self, option: str) -> None:
        """Record the chosen device — no SCU action here (button does that)."""
        if option == self._NONE:
            self.coordinator.set_unpair_selected_mac(None)
        else:
            mac = option.rsplit("(", 1)[-1].rstrip(")").strip()
            self.coordinator.set_unpair_selected_mac(mac)
        self.async_write_ha_state()


class HymerFridgeSelect(
    CoordinatorEntity[HymerConnectCoordinator], SelectEntity, OptimisticCommandMixin
):
    """Fridge mode select entity — Off / 1-5 / ECO."""

    _attr_has_entity_name = True
    _attr_translation_key = "fridge_mode_ctrl"
    _attr_options = FRIDGE_OPTIONS
    _attr_icon = "mdi:fridge"

    def __init__(
        self,
        coordinator: HymerConnectCoordinator,
        entry: ConfigEntry,
        fridge_def: dict[str, Any],
    ) -> None:
        """Initialize the fridge select entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_fridge_mode_ctrl"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HYMER",
            "manufacturer": MANUFACTURER,
            "model": "Smart Interface Unit",
        }
        self._optimistic: str | None = None
        self._init_optimistic()
        # Bus/slot IDs from JSON
        self._bus = fridge_def.get("control_bus", 34)
        self._power_sid = fridge_def.get("power_sid", 1)
        self._step_sid = fridge_def.get("cooling_step_sid", 3)
        self._power_sensor = fridge_def.get("power_sensor", "fridge_power")
        self._step_sensor = fridge_def.get("cooling_step_sensor", "fridge_cooling_step")
        self._mode_sensor = fridge_def.get("mode_sensor", "fridge_mode")

    @property
    def current_option(self) -> str | None:
        """Return the current fridge mode."""
        if self._optimistic is not None:
            return self._optimistic
        return self._read_option()

    def _read_option(self) -> str | None:
        """Resolve the fridge mode from the SCU readback (no optimistic)."""
        if self.coordinator.data is None:
            return None

        # Check fridge power — if False → Off
        power = _resolve_path(self.coordinator.data, f"signalr_sensors.{self._power_sensor}")
        if power is False:
            return "Off"

        # Check cooling step
        step = _resolve_path(self.coordinator.data, f"signalr_sensors.{self._step_sensor}")
        if step is not None:
            try:
                s = int(step)
                if 1 <= s <= 5:
                    return str(s)
            except (ValueError, TypeError):
                pass

        # Fallback: check old fridge_mode sensor
        mode = _resolve_path(self.coordinator.data, f"signalr_sensors.{self._mode_sensor}")
        if mode is not None:
            mode_str = str(mode)
            if mode_str in ("Off", "0"):
                return "Off"

        return None

    async def _send_option(self, option: str) -> None:
        """Issue the SCU writes for ``option`` (no optimistic/verify)."""
        if option == "Off":
            await self.coordinator.async_send_light_command(self._bus, self._power_sid, bool_value=False)
        else:  # "1"-"5"
            # Power on first, wait, then set cooling step
            await self.coordinator.async_send_light_command(self._bus, self._power_sid, bool_value=True)
            await asyncio.sleep(0.5)
            await self.coordinator.async_send_light_command(self._bus, self._step_sid, uint_value=int(option))

    async def async_select_option(self, option: str) -> None:
        """Set the fridge mode."""
        if option not in ("Off", "1", "2", "3", "4", "5"):
            _LOGGER.warning("Unknown fridge option: %s", option)
            return
        await self._send_option(option)
        self._optimistic = option
        self.async_write_ha_state()
        self._note_command(lambda o=option: self._send_option(o))

    def _has_pending_optimistic(self) -> bool:
        return self._optimistic is not None

    def _command_confirmed(self) -> bool:
        return self._optimistic is not None and self._read_option() == self._optimistic

    def _clear_optimistic(self) -> None:
        self._optimistic = None

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state when confirmed or the TTL self-heals it."""
        if self._optimistic_ttl_expired():
            self._clear_optimistic()
        if self._optimistic is not None and self.coordinator.data:
            if self._read_option() == self._optimistic:
                self._optimistic = None
        super()._handle_coordinator_update()

    async def async_will_remove_from_hass(self) -> None:
        await self._cancel_verify()
        await super().async_will_remove_from_hass()


class HymerBoilerSelect(
    CoordinatorEntity[HymerConnectCoordinator], SelectEntity, OptimisticCommandMixin
):
    """Boiler mode select entity — Off / ECO / Turbo."""

    _attr_has_entity_name = True
    _attr_translation_key = "boiler_mode_ctrl"
    _attr_options = BOILER_OPTIONS
    _attr_icon = "mdi:water-boiler"

    def __init__(
        self,
        coordinator: HymerConnectCoordinator,
        entry: ConfigEntry,
        heater_def: dict[str, Any],
    ) -> None:
        """Initialize the boiler select entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_boiler_mode_ctrl"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HYMER",
            "manufacturer": MANUFACTURER,
            "model": "Smart Interface Unit",
        }
        self._optimistic: str | None = None
        self._init_optimistic()
        self._bus = heater_def.get("heater_bus", 58)
        self._boiler_sid = heater_def.get("boiler_sid", 5)
        self._fuel_type_sid = heater_def.get("fuel_type_sid", 4)
        self._boiler_sensor = heater_def.get("boiler_sensor", "heater_fan_speed")
        self._fuel_type_sensor = heater_def.get("fuel_type_sensor", "heater_fuel_type")

    def _get_fuel_type(self) -> str:
        """Get current fuel type from coordinator data."""
        if self.coordinator.data:
            val = _resolve_path(self.coordinator.data, f"signalr_sensors.{self._fuel_type_sensor}")
            if val and isinstance(val, str) and val not in ("unknown", "unavailable"):
                return val
        return "Diesel"

    @property
    def current_option(self) -> str | None:
        """Return the current boiler mode."""
        if self._optimistic is not None:
            return self._optimistic
        return self._read_option()

    def _read_option(self) -> str | None:
        """Resolve the boiler mode from the SCU readback (no optimistic)."""
        if self.coordinator.data is None:
            return None

        # Boiler mode is heater_fan_speed (bus N, sid M)
        fan = _resolve_path(self.coordinator.data, f"signalr_sensors.{self._boiler_sensor}")
        if fan is None:
            return None

        fan_str = str(fan).upper()
        if fan_str in ("OFF", "Off"):
            return "Off"
        if fan_str == "ECO":
            return "ECO"
        if fan_str == "HOT":
            return "Turbo"
        return "Off"

    async def _send_option(self, option: str) -> None:
        """Issue the SCU writes for ``option`` (no optimistic/verify)."""
        mode_map = {"Off": "OFF", "ECO": "ECO", "Turbo": "HOT"}
        mode_str = mode_map[option]
        fuel = self._get_fuel_type()
        await self.coordinator.async_send_multi_sensor_command([
            {"bus_id": self._bus, "sensor_id": self._boiler_sid, "str_value": mode_str},
            {"bus_id": self._bus, "sensor_id": self._fuel_type_sid, "str_value": fuel},
        ])

    async def async_select_option(self, option: str) -> None:
        """Set the boiler mode."""
        if option not in ("Off", "ECO", "Turbo"):
            _LOGGER.warning("Unknown boiler option: %s", option)
            return
        await self._send_option(option)
        self._optimistic = option
        self.async_write_ha_state()
        self._note_command(lambda o=option: self._send_option(o))

    def _has_pending_optimistic(self) -> bool:
        return self._optimistic is not None

    def _command_confirmed(self) -> bool:
        return self._optimistic is not None and self._read_option() == self._optimistic

    def _clear_optimistic(self) -> None:
        self._optimistic = None

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state when confirmed or the TTL self-heals it."""
        if self._optimistic_ttl_expired():
            self._clear_optimistic()
        if self._optimistic is not None and self.coordinator.data:
            if self._read_option() == self._optimistic:
                self._optimistic = None
        super()._handle_coordinator_update()

    async def async_will_remove_from_hass(self) -> None:
        await self._cancel_verify()
        await super().async_will_remove_from_hass()


class HymerHeaterEnergySelect(
    CoordinatorEntity[HymerConnectCoordinator], SelectEntity, OptimisticCommandMixin
):
    """Heater energy source select matching Truma Combi panel modes.

    Panel labels → SCU wire values:
      FUEL  → (58,4)="Diesel", (58,6)="Diesel"
      MIX 1 → (58,4)="Both",  (58,6)="Both",  (58,9)=uint 900
      MIX 2 → (58,4)="Both",  (58,6)="Both",  (58,9)=uint 1800
      EL 1  → (58,4)="Electric", (58,6)="Electric", (58,9)=uint 900
      EL 2  → (58,4)="Electric", (58,6)="Electric", (58,9)=uint 1800

    Protocol (captured 2026-04-19 via mitmproxy):
      - (58,4) heater_fuel_type and (58,6) heater_fuel_type_2 are always sent
        as a pair with the same string value: "Diesel", "Both", or "Electric".
      - (58,9) heater_electric_power is sent as uint 900 or 1800 when mode
        involves electric power (Both or Electric).
      - Electric modes require shore power — SCU rejects otherwise.
      - NOTE: "Both" and "Diesel" were captured via mitmproxy.  "Electric"
        with wattage and VENT fan mode are extrapolated from the same
        protocol pattern + Truma panel labels.  Not yet verified on wire.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "heater_energy_ctrl"
    _attr_options = HEATER_ENERGY_OPTIONS
    _attr_icon = "mdi:gas-station"

    def __init__(
        self,
        coordinator: HymerConnectCoordinator,
        entry: ConfigEntry,
        heater_def: dict[str, Any],
    ) -> None:
        """Initialize the heater energy source select entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_heater_energy_ctrl"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HYMER",
            "manufacturer": MANUFACTURER,
            "model": "Smart Interface Unit",
        }
        self._optimistic: str | None = None
        self._init_optimistic()
        self._bus = heater_def.get("heater_bus", 58)
        self._fuel_type_sid = heater_def.get("fuel_type_sid", 4)
        self._fuel_type_2_sid = heater_def.get("fuel_type_2_sid", 6)
        self._electric_power_sid = heater_def.get("electric_power_sid", 9)
        self._fuel_type_sensor = heater_def.get("fuel_type_sensor", "heater_fuel_type")
        self._electric_power_sensor = heater_def.get("electric_power_sensor", "heater_electric_power")

    @property
    def current_option(self) -> str | None:
        """Return the current heater energy source."""
        if self._optimistic is not None:
            return self._optimistic
        return self._read_option()

    def _read_option(self) -> str | None:
        """Resolve the energy source from the SCU readback (no optimistic)."""
        if self.coordinator.data is None:
            return None

        fuel = _resolve_path(
            self.coordinator.data, f"signalr_sensors.{self._fuel_type_sensor}"
        )
        if fuel is None:
            return None

        fuel_str = str(fuel)
        if fuel_str == "Diesel":
            return "Diesel"
        if fuel_str == "Electric":
            watt = _resolve_path(
                self.coordinator.data, f"signalr_sensors.{self._electric_power_sensor}"
            )
            try:
                w = int(watt) if watt is not None else 900
            except (ValueError, TypeError):
                w = 900
            return f"Electric {w}W"
        if fuel_str == "Both":
            watt = _resolve_path(
                self.coordinator.data, f"signalr_sensors.{self._electric_power_sensor}"
            )
            try:
                w = int(watt) if watt is not None else 900
            except (ValueError, TypeError):
                w = 900
            return f"Mix {w}W"
        return "Diesel"

    async def _send_option(self, option: str) -> None:
        """Issue the SCU writes for ``option`` (no optimistic/verify)."""
        b = self._bus
        ft = self._fuel_type_sid
        ft2 = self._fuel_type_2_sid
        ep = self._electric_power_sid

        if option == "Diesel":
            await self.coordinator.async_send_multi_sensor_command([
                {"bus_id": b, "sensor_id": ft, "str_value": "Diesel"},
                {"bus_id": b, "sensor_id": ft2, "str_value": "Diesel"},
            ])
        elif option == "Electric 900W":
            await self.coordinator.async_send_multi_sensor_command([
                {"bus_id": b, "sensor_id": ft, "str_value": "Electric"},
                {"bus_id": b, "sensor_id": ft2, "str_value": "Electric"},
                {"bus_id": b, "sensor_id": ep, "uint_value": 900},
            ])
        elif option == "Electric 1800W":
            await self.coordinator.async_send_multi_sensor_command([
                {"bus_id": b, "sensor_id": ft, "str_value": "Electric"},
                {"bus_id": b, "sensor_id": ft2, "str_value": "Electric"},
                {"bus_id": b, "sensor_id": ep, "uint_value": 1800},
            ])
        elif option == "Mix 900W":
            await self.coordinator.async_send_multi_sensor_command([
                {"bus_id": b, "sensor_id": ft, "str_value": "Both"},
                {"bus_id": b, "sensor_id": ft2, "str_value": "Both"},
                {"bus_id": b, "sensor_id": ep, "uint_value": 900},
            ])
        elif option == "Mix 1800W":
            await self.coordinator.async_send_multi_sensor_command([
                {"bus_id": b, "sensor_id": ft, "str_value": "Both"},
                {"bus_id": b, "sensor_id": ft2, "str_value": "Both"},
                {"bus_id": b, "sensor_id": ep, "uint_value": 1800},
            ])

    async def async_select_option(self, option: str) -> None:
        """Set the heater energy source."""
        if option not in HEATER_ENERGY_OPTIONS:
            _LOGGER.warning("Unknown heater energy option: %s", option)
            return
        await self._send_option(option)
        self._optimistic = option
        self.async_write_ha_state()
        self._note_command(lambda o=option: self._send_option(o))

    def _has_pending_optimistic(self) -> bool:
        return self._optimistic is not None

    def _command_confirmed(self) -> bool:
        return self._optimistic is not None and self._read_option() == self._optimistic

    def _clear_optimistic(self) -> None:
        self._optimistic = None

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state when confirmed or the TTL self-heals it."""
        if self._optimistic_ttl_expired():
            self._clear_optimistic()
        if self._optimistic is not None and self.coordinator.data:
            if self._read_option() == self._optimistic:
                self._optimistic = None
        super()._handle_coordinator_update()

    async def async_will_remove_from_hass(self) -> None:
        await self._cancel_verify()
        await super().async_will_remove_from_hass()


# Note: a HymerHeaterAirModeSelect for slot 58:11 (heater_air_mode) was
# removed in v2.36.3. The EHG metadata flag claimed the slot was writable
# with values OFF/Normal/Automatic, but inspection of the official EHG app
# (Klima tab) shows no such control exists, and captured SignalR traffic
# never contained a write to 58:11. The Truma SCU silently reverts any
# write back to "Normal", so the slot is effectively read-only for this
# Combi configuration. The reading is still available via
# sensor.hymer_heater_operating_mode.


class HymerSteppedSelect(
    CoordinatorEntity[HymerConnectCoordinator], SelectEntity, OptimisticCommandMixin
):
    """Generic JSON-driven stepped-switch select (v2.63.0+).

    Drives any appliance that exposes a small set of integer steps —
    fridge cooling 1-5, freezer 1-3, fan speed 1-3, etc. — optionally
    gated by a separate boolean "power" slot. Read sources and write
    recipes live entirely in the brand overlay JSON under
    ``climate.selects.<key>`` so adding a new device is a JSON-only
    change.

    See ``docs/sensor-map.md`` ("Stepped switch / select driver") for the
    full schema and worked examples.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HymerConnectCoordinator,
        entry: ConfigEntry,
        key: str,
        defn: dict[str, Any],
    ) -> None:
        """Initialize a stepped-switch select from a JSON definition."""
        super().__init__(coordinator)
        self._key = key
        self._defn = defn
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_icon = defn.get("icon", "mdi:tune-vertical")
        self._attr_options = list(defn.get("options", []))
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
        read = defn.get("read", {}) or {}
        self._step_sensor: str | None = read.get("step_sensor")
        self._power_sensor: str | None = read.get("power_sensor")
        self._off_when_power_false: bool = bool(read.get("off_when_power_false", False))
        try:
            self._off_value: int = int(read.get("off_value", 0))
        except (TypeError, ValueError):
            self._off_value = 0
        writes = defn.get("writes", {}) or {}
        self._writes_off: list[dict[str, Any]] = list(writes.get("off", []))
        self._writes_step: list[dict[str, Any]] = list(writes.get("step", []))
        # Optional parallel int list letting the display labels differ from the
        # integer written/read (e.g. options ``["Off","1 kW","2 kW","3 kW"]`` with
        # ``option_values`` ``[0,1,2,3]``). When absent the label itself must be a
        # bare int (legacy behaviour), so existing fridge selects are unaffected.
        self._option_values: list[int] | None = None
        ov = defn.get("option_values")
        if isinstance(ov, list) and len(ov) == len(self._attr_options):
            try:
                self._option_values = [int(x) for x in ov]
            except (TypeError, ValueError):
                self._option_values = None
        # String-valued select mode (e.g. Alde energy priority "Prio Gas"/"Prio EL"):
        # ``read.value_sensor`` holds the string state; ``writes.option`` is a single
        # recipe run with ``$option`` substituted by the selected option string.
        self._value_sensor: str | None = read.get("value_sensor")
        self._writes_option: list[dict[str, Any]] = list(writes.get("option", []))
        self._optimistic: str | None = None
        self._init_optimistic()

    @property
    def current_option(self) -> str | None:
        """Resolve the active option from coordinator data."""
        if self._optimistic is not None:
            return self._optimistic
        return self._read_option()

    def _read_option(self) -> str | None:
        """Resolve the active option from the SCU readback (no optimistic)."""
        if self.coordinator.data is None:
            return None

        # String-valued select: reflect the string state sensor directly.
        if self._value_sensor:
            raw = _resolve_path(
                self.coordinator.data, f"signalr_sensors.{self._value_sensor}"
            )
            if raw is None:
                return None
            s = str(raw)
            return s if s in self._attr_options else None

        if self._off_when_power_false and self._power_sensor:
            power = _resolve_path(
                self.coordinator.data, f"signalr_sensors.{self._power_sensor}"
            )
            if power is False:
                return "Off"

        if not self._step_sensor:
            return None
        raw = _resolve_path(
            self.coordinator.data, f"signalr_sensors.{self._step_sensor}"
        )
        if raw is None:
            return None
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return None
        if self._option_values is not None:
            for lbl, val in zip(self._attr_options, self._option_values):
                if val == n:
                    return lbl
            return None
        if n == self._off_value:
            return "Off" if "Off" in self._attr_options else None
        label = str(n)
        return label if label in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        """Execute the JSON-defined write recipe for ``option``."""
        if option not in self._attr_options:
            _LOGGER.warning("Unknown option '%s' for stepped select '%s'", option, self._key)
            return
        if not await self._send_option(option):
            return
        self._optimistic = option
        self.async_write_ha_state()
        self._note_command(lambda o=option: self._send_option(o))

    async def _send_option(self, option: str) -> bool:
        """Run the SCU write recipe for ``option`` (no optimistic/verify).

        Returns True when a command was issued, False when there was no recipe.
        """
        # String-valued select: run the single "option" recipe with "$option".
        if self._writes_option:
            for step in self._writes_option:
                if not isinstance(step, dict):
                    continue
                if "delay_ms" in step:
                    try:
                        delay = max(0, int(step["delay_ms"])) / 1000.0
                    except (TypeError, ValueError):
                        continue
                    if delay:
                        await asyncio.sleep(delay)
                    continue
                sid = int(step.get("sid", 0))
                if not sid:
                    _LOGGER.warning(
                        "String select '%s': write step missing 'sid' — skipping (%s)",
                        self._key, step,
                    )
                    continue
                if "str" in step:
                    val = step["str"]
                    if val == "$option":
                        val = option
                    await self.coordinator.async_send_light_command(
                        self._bus, sid, str_value=str(val)
                    )
                elif "bool" in step:
                    await self.coordinator.async_send_light_command(
                        self._bus, sid, bool_value=bool(step["bool"])
                    )
                elif "uint" in step:
                    try:
                        await self.coordinator.async_send_light_command(
                            self._bus, sid, uint_value=int(step["uint"])
                        )
                    except (TypeError, ValueError):
                        _LOGGER.warning(
                            "String select '%s': cannot coerce uint value %r",
                            self._key, step.get("uint"),
                        )
            return True

        is_off = option == "Off"
        recipe = self._writes_off if is_off else self._writes_step
        if not recipe:
            _LOGGER.warning(
                "Stepped select '%s' has no '%s' write recipe — ignoring",
                self._key, "off" if is_off else "step",
            )
            return False

        if self._option_values is not None:
            try:
                option_int = self._option_values[self._attr_options.index(option)]
            except (ValueError, IndexError):
                option_int = self._off_value
        else:
            try:
                option_int = self._off_value if is_off else int(option)
            except ValueError:
                option_int = self._off_value

        for step in recipe:
            if not isinstance(step, dict):
                continue
            if "delay_ms" in step:
                try:
                    delay = max(0, int(step["delay_ms"])) / 1000.0
                except (TypeError, ValueError):
                    continue
                if delay:
                    await asyncio.sleep(delay)
                continue

            sid = int(step.get("sid", 0))
            if not sid:
                _LOGGER.warning(
                    "Stepped select '%s': write step missing 'sid' — skipping (%s)",
                    self._key, step,
                )
                continue

            if "bool" in step:
                await self.coordinator.async_send_light_command(
                    self._bus, sid, bool_value=bool(step["bool"])
                )
            elif "uint" in step:
                val = step["uint"]
                if val == "$option_int":
                    val = option_int
                try:
                    await self.coordinator.async_send_light_command(
                        self._bus, sid, uint_value=int(val)
                    )
                except (TypeError, ValueError):
                    _LOGGER.warning(
                        "Stepped select '%s': cannot coerce uint value %r",
                        self._key, val,
                    )
            elif "str" in step:
                await self.coordinator.async_send_light_command(
                    self._bus, sid, str_value=str(step["str"])
                )
            else:
                _LOGGER.warning(
                    "Stepped select '%s': unrecognized write step %s",
                    self._key, step,
                )

        return True

    def _has_pending_optimistic(self) -> bool:
        return self._optimistic is not None

    def _command_confirmed(self) -> bool:
        return self._optimistic is not None and self._read_option() == self._optimistic

    def _clear_optimistic(self) -> None:
        self._optimistic = None

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state once confirmed or the TTL self-heals it."""
        if self._optimistic_ttl_expired():
            self._clear_optimistic()
        if self._optimistic is not None and self.coordinator.data:
            if self._read_option() == self._optimistic:
                self._optimistic = None
        super()._handle_coordinator_update()

    async def async_will_remove_from_hass(self) -> None:
        await self._cancel_verify()
        await super().async_will_remove_from_hass()
