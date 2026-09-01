"""Light platform for HYMER Connect — controllable interior lights.

Light definitions are loaded from JSON ``"lights"`` sections in
``sensor_maps/base.json`` + ``{brand}.json``.  Each entry is keyed by
bus_id and declares capabilities (brightness, color_temp).  The SCU
convention is: sid 1 = on/off (bool), sid 2 = brightness (uint 0-100%),
sid 3 = color_temp (uint 0-100%).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
    LightEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import HymerConnectCoordinator

# An optimistic value is normally cleared the moment the SCU confirms the
# commanded state. If the command never takes effect (e.g. a BLE write failed,
# fell back to cloud, and that command was dropped), the SCU keeps reporting the
# OLD state, so a "confirm-only" clear would leave the entity stuck on the wrong
# optimistic value forever. After this TTL we drop the unconfirmed optimistic
# value and let the real SCU readback win.
OPTIMISTIC_STATE_TTL = 20.0

# After sending a light command, wait this long for a confirming SCU readback
# before re-sending it once. A dropped command (failed BLE write that fell back
# to cloud but was not applied) is otherwise only corrected visually by the TTL.
LIGHT_VERIFY_DELAY = 8.0
from .sensor import _resolve_path
from .signalr_client import STALE_DATA_TIMEOUT  # noqa: F401  (kept for compat)

_LOGGER = logging.getLogger(__name__)

# Color temperature range (Kelvin) for lights with color_temp support
MIN_COLOR_TEMP_KELVIN = 2700  # warm white
MAX_COLOR_TEMP_KELVIN = 6500  # daylight


@dataclass(frozen=True, kw_only=True)
class HymerLightEntityDescription(LightEntityDescription):
    bus_id: int
    on_off_path: str
    brightness_path: str | None = None
    color_temp_path: str | None = None


def _build_light_descriptions() -> tuple[
    list[HymerLightEntityDescription],
    list[tuple[HymerLightEntityDescription, str]],
]:
    """Build light entity descriptions from JSON-loaded LIGHT_DEFS.

    Returns ``(always, gated)`` where ``gated`` entries carry
    ``require_observed`` and are created on demand once their on/off read
    sensor is reported by the vehicle (keyed by the sensor name to watch), so
    a shared lights.json leaves no phantom lights on brands without a circuit.
    """
    from .pia_decoder import LIGHT_DEFS, SENSOR_MAP

    always: list[HymerLightEntityDescription] = []
    gated: list[tuple[HymerLightEntityDescription, str]] = []
    for bus_id, meta in LIGHT_DEFS.items():
        if not isinstance(meta, dict) or "name" not in meta:
            continue
        name = meta["name"]
        # Resolve the on/off sensor name from SENSOR_MAP (bus, sid=1)
        sm_entry = SENSOR_MAP.get((bus_id, 1))
        on_off_name = sm_entry[0] if sm_entry else name
        kwargs: dict[str, Any] = {
            "key": name,
            "translation_key": name,
            "bus_id": bus_id,
            "on_off_path": f"signalr_sensors.{on_off_name}",
        }
        if meta.get("icon"):
            kwargs["icon"] = meta["icon"]
        if meta.get("enabled") is False:
            kwargs["entity_registry_enabled_default"] = False
        # Brightness: look for (bus, 2) in SENSOR_MAP
        if meta.get("brightness"):
            br_entry = SENSOR_MAP.get((bus_id, 2))
            if br_entry:
                kwargs["brightness_path"] = f"signalr_sensors.{br_entry[0]}"
        # Color temp: look for (bus, 3) in SENSOR_MAP
        if meta.get("color_temp"):
            ct_entry = SENSOR_MAP.get((bus_id, 3))
            if ct_entry:
                kwargs["color_temp_path"] = f"signalr_sensors.{ct_entry[0]}"
        desc = HymerLightEntityDescription(**kwargs)
        if meta.get("require_observed"):
            gated.append((desc, on_off_name))
        else:
            always.append(desc)
    return always, gated


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HymerConnectCoordinator = hass.data[DOMAIN][entry.entry_id]
    descriptions, gated = _build_light_descriptions()
    _LOGGER.debug(
        "Light platform: %d light entities from JSON (+%d gated)",
        len(descriptions), len(gated),
    )
    async_add_entities(
        HymerConnectLight(coordinator, desc, entry)
        for desc in descriptions
    )

    if not gated:
        return

    # Observation-gated lights: create each only once its on/off read sensor is
    # actually reported, so absent circuits leave no phantom light.
    created_keys: set[str] = set()

    @callback
    def _async_discover_gated() -> None:
        if not coordinator.data:
            return
        sensors = coordinator.data.get("signalr_sensors")
        if not isinstance(sensors, dict):
            return
        new_entities: list[HymerConnectLight] = []
        for desc, watch in gated:
            if desc.key in created_keys:
                continue
            if watch not in sensors:
                continue
            created_keys.add(desc.key)
            new_entities.append(HymerConnectLight(coordinator, desc, entry))
            _LOGGER.info(
                "Observation-gated light %s materialised (%s reported)",
                desc.key, watch,
            )
        if new_entities:
            async_add_entities(new_entities)

    _async_discover_gated()
    entry.async_on_unload(coordinator.async_add_listener(_async_discover_gated))


class HymerConnectLight(
    CoordinatorEntity[HymerConnectCoordinator], LightEntity
):
    entity_description: HymerLightEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HymerConnectCoordinator,
        description: HymerLightEntityDescription,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HYMER",
            "manufacturer": MANUFACTURER,
            "model": "Smart Interface Unit",
        }
        modes = set()
        if description.color_temp_path:
            modes.add(ColorMode.COLOR_TEMP)
        elif description.brightness_path:
            modes.add(ColorMode.BRIGHTNESS)
        else:
            modes.add(ColorMode.ONOFF)
        self._attr_supported_color_modes = modes
        self._attr_color_mode = next(iter(modes))
        if description.color_temp_path:
            self._attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN
            self._attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN
        self._optimistic_on: bool | None = None
        self._optimistic_brightness: int | None = None
        self._optimistic_color_temp: int | None = None
        self._optimistic_set_at: float = 0.0
        self._verify_task: asyncio.Task | None = None

    @property
    def available(self) -> bool:
        """Lights require the 12V main switch to be on (mirrors the EHG app)."""
        if self.coordinator.data is None:
            return False
        main = _resolve_path(self.coordinator.data, "signalr_sensors.main_switch")
        # App parity: lights + water pump sit on the bus-3 habitation controller
        # and are the ONLY entities the EHG app greys out at 12V-off; gate purely
        # on the 12V main state. Only a mapped main_switch reports the string
        # "On"/"Off"/"Changing"; a phantom raw bus-3 int (vehicles without a bus-3
        # controller, #20) must NOT be misread as "Off".
        if isinstance(main, str) and main == "Off":
            return False
        # SCU standby (scu_connected=False) is NOT 12V-off: the SCU can flap into
        # cloud standby while 12V stays on, which previously greyed the running
        # pump (v2.95.5 regression). Do NOT gate on scu_connected.
        # Fallback for vehicles whose main_switch freezes at "On" when 12V is cut
        # (#20/#24): prolonged data silence from ANY transport = 12V off. Standby
        # push frames keep this clock alive, so it never false-fires during the
        # reconnect flapping above.
        if self.coordinator.data_silence_seconds > self.coordinator.unavailable_silence_threshold:
            return False
        return super().available

    @property
    def is_on(self) -> bool | None:
        if self._optimistic_on is not None:
            return self._optimistic_on
        if self.coordinator.data is None:
            return None
        val = _resolve_path(self.coordinator.data, self.entity_description.on_off_path)
        if val is None:
            return None
        return bool(val)

    @property
    def brightness(self) -> int | None:
        if self._optimistic_brightness is not None:
            return self._optimistic_brightness
        if not self.entity_description.brightness_path:
            return None
        if self.coordinator.data is None:
            return None
        val = _resolve_path(
            self.coordinator.data, self.entity_description.brightness_path
        )
        if val is None or not isinstance(val, (int, float)):
            return None
        return min(255, max(0, int(val * 255 / 100)))

    @property
    def color_temp_kelvin(self) -> int | None:
        if self._optimistic_color_temp is not None:
            return self._optimistic_color_temp
        if not self.entity_description.color_temp_path:
            return None
        if self.coordinator.data is None:
            return None
        val = _resolve_path(
            self.coordinator.data, self.entity_description.color_temp_path
        )
        if val is None or not isinstance(val, (int, float)):
            return None
        return int(
            MIN_COLOR_TEMP_KELVIN
            + val * (MAX_COLOR_TEMP_KELVIN - MIN_COLOR_TEMP_KELVIN) / 100
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        bus = self.entity_description.bus_id
        await self.coordinator.async_send_light_command(bus, 1, bool_value=True)
        self._optimistic_on = True
        self._optimistic_set_at = time.monotonic()
        if ATTR_BRIGHTNESS in kwargs and self.entity_description.brightness_path:
            pct = min(100, max(0, int(kwargs[ATTR_BRIGHTNESS] * 100 / 255)))
            await self.coordinator.async_send_light_command(bus, 2, uint_value=pct)
            self._optimistic_brightness = kwargs[ATTR_BRIGHTNESS]
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            pct = min(100, max(0, int((kelvin - MIN_COLOR_TEMP_KELVIN) * 100 / (MAX_COLOR_TEMP_KELVIN - MIN_COLOR_TEMP_KELVIN))))
            await self.coordinator.async_send_light_command(bus, 3, uint_value=pct)
            self._optimistic_color_temp = kelvin
        self.async_write_ha_state()
        self._schedule_verify(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        bus = self.entity_description.bus_id
        await self.coordinator.async_send_light_command(bus, 1, bool_value=False)
        self._optimistic_on = False
        self._optimistic_set_at = time.monotonic()
        self.async_write_ha_state()
        self._schedule_verify(False)

    def _schedule_verify(self, expected_on: bool) -> None:
        """(Re)start the verify-and-retry watchdog for the last command."""
        if self._verify_task and not self._verify_task.done():
            self._verify_task.cancel()
        self._verify_task = asyncio.ensure_future(
            self._verify_and_retry_light(expected_on)
        )

    def _real_on(self) -> bool | None:
        """Return the SCU on/off readback, bypassing the optimistic value."""
        if self.coordinator.data is None:
            return None
        val = _resolve_path(
            self.coordinator.data, self.entity_description.on_off_path
        )
        return None if val is None else bool(val)

    async def _verify_and_retry_light(self, expected_on: bool) -> None:
        """Re-send the command once if the SCU never confirms the new state.

        A dropped command (e.g. a BLE write that failed, fell back to cloud, and
        was not applied) would otherwise leave the light on the wrong state until
        the optimistic TTL expires. Skip retrying when the SCU cannot confirm
        anyway (12V off / data silent) or is frozen.
        """
        try:
            await asyncio.sleep(LIGHT_VERIFY_DELAY)
            if self._optimistic_on != expected_on:
                return  # superseded by a newer command
            if self._real_on() == expected_on:
                return  # confirmed by a real readback
            if (
                self.coordinator.data_silence_seconds
                > self.coordinator.unavailable_silence_threshold
                or self.coordinator.scu_frozen
            ):
                return  # 12V off or hung SCU — a retry could not be confirmed
            _LOGGER.info(
                "Light %s: %s not confirmed after %.0fs — re-sending once",
                self.entity_description.key,
                "ON" if expected_on else "OFF",
                LIGHT_VERIFY_DELAY,
            )
            await self.coordinator.async_send_light_command(
                self.entity_description.bus_id, 1, bool_value=expected_on
            )
            self._optimistic_set_at = time.monotonic()  # restart self-heal TTL
            await asyncio.sleep(LIGHT_VERIFY_DELAY)
            if self._optimistic_on == expected_on and self._real_on() != expected_on:
                _LOGGER.info(
                    "Light %s: still unconfirmed after retry — leaving the real "
                    "SCU state to win",
                    self.entity_description.key,
                )
        except asyncio.CancelledError:
            pass

    async def async_will_remove_from_hass(self) -> None:
        if self._verify_task and not self._verify_task.done():
            self._verify_task.cancel()
        await super().async_will_remove_from_hass()

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state once the SCU confirms it or the TTL expires."""
        # Self-heal: drop an unconfirmed optimistic value after the TTL so a
        # command that never took effect can't leave the entity stuck on a wrong
        # state; the real SCU readback then wins.
        if (
            self._optimistic_set_at > 0
            and time.monotonic() - self._optimistic_set_at > OPTIMISTIC_STATE_TTL
        ):
            self._optimistic_on = None
            self._optimistic_brightness = None
            self._optimistic_color_temp = None
        if self._optimistic_on is not None and self.coordinator.data:
            val = _resolve_path(
                self.coordinator.data,
                self.entity_description.on_off_path,
            )
            if val is not None and bool(val) == self._optimistic_on:
                self._optimistic_on = None
        if self._optimistic_brightness is not None and self.coordinator.data:
            val = _resolve_path(
                self.coordinator.data,
                self.entity_description.brightness_path or "",
            )
            if val is not None and isinstance(val, (int, float)):
                scu_brightness = min(255, max(0, int(val * 255 / 100)))
                if abs(scu_brightness - self._optimistic_brightness) <= 5:
                    self._optimistic_brightness = None
        if self._optimistic_color_temp is not None and self.coordinator.data:
            val = _resolve_path(
                self.coordinator.data,
                self.entity_description.color_temp_path or "",
            )
            if val is not None and isinstance(val, (int, float)):
                scu_ct = int(MIN_COLOR_TEMP_KELVIN + val * (MAX_COLOR_TEMP_KELVIN - MIN_COLOR_TEMP_KELVIN) / 100)
                if abs(scu_ct - self._optimistic_color_temp) <= 100:
                    self._optimistic_color_temp = None
        super()._handle_coordinator_update()
