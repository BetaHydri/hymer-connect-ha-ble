"""Cover platform for HYMER Connect — moving components (e.g. the power awning).

Cover definitions live entirely in the brand overlay JSON under the top-level
``"covers"`` section, so adding a new moving component is a JSON-only change.
Each entry maps the open/close/stop/position write slots and the readback
sensors of one component (bus). See ``docs/sensor-map.md`` for the schema.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
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
    """Set up HYMER Connect cover entities from a config entry."""
    from .pia_decoder import COVER_DEFS

    coordinator: HymerConnectCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Observation-gated cover factories: created only once the vehicle reports
    # one of the component's read sensors, so an absent awning leaves no phantom
    # cover. Entries WITHOUT require_observed are created immediately.
    always: list[HymerCover] = []
    gated: list[tuple[str, dict[str, Any], tuple[str, ...]]] = []
    for key, defn in COVER_DEFS.items():
        if not isinstance(defn, dict):
            continue
        watch = _cover_read_sensors(defn)
        if defn.get("require_observed") and watch:
            gated.append((key, defn, watch))
        else:
            always.append(HymerCover(coordinator, entry, key, defn))

    if always:
        async_add_entities(always)

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
        new_entities: list[HymerCover] = []
        for key, defn, watch in gated:
            if key in created_keys:
                continue
            if not any(name in sensors for name in watch):
                continue
            created_keys.add(key)
            new_entities.append(HymerCover(coordinator, entry, key, defn))
            _LOGGER.info(
                "Observation-gated cover '%s' materialised (read sensor reported)",
                key,
            )
        if new_entities:
            async_add_entities(new_entities)

    _async_discover_gated()
    entry.async_on_unload(coordinator.async_add_listener(_async_discover_gated))


def _cover_read_sensors(defn: dict[str, Any]) -> tuple[str, ...]:
    """Return the sensor names a cover watches for gating/state."""
    read = defn.get("read", {}) or {}
    names = [
        read.get("position_sensor"),
        read.get("direction_sensor"),
        read.get("status_sensor"),
    ]
    return tuple(name for name in names if isinstance(name, str) and name)


class HymerCover(CoordinatorEntity[HymerConnectCoordinator], CoverEntity):
    """Generic JSON-driven cover (v2.73.0+).

    Drives a moving component that exposes momentary open/close write slots, an
    optional stop command and an optional 0-100 position slot. Read sources and
    write slots come entirely from the brand overlay JSON under ``covers.<key>``.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HymerConnectCoordinator,
        entry: ConfigEntry,
        key: str,
        defn: dict[str, Any],
    ) -> None:
        """Initialize a cover from a JSON definition."""
        super().__init__(coordinator)
        self._key = key
        self._defn = defn
        self._attr_unique_id = f"{entry.entry_id}_cover_{key}"
        self._attr_icon = defn.get("icon", "mdi:awning-outline")
        self._attr_name = defn.get("name") or key
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HYMER",
            "manufacturer": MANUFACTURER,
            "model": "Smart Interface Unit",
        }
        try:
            self._attr_device_class = CoverDeviceClass(str(defn.get("device_class", "awning")))
        except ValueError:
            self._attr_device_class = CoverDeviceClass.AWNING

        self._bus = int(defn.get("control_bus", 0))
        self._open_sid = defn.get("open_sid")
        self._close_sid = defn.get("close_sid")
        self._position_sid = defn.get("position_sid")
        self._stop = defn.get("stop") or {}
        read = defn.get("read", {}) or {}
        self._position_sensor: str | None = read.get("position_sensor")
        self._direction_sensor: str | None = read.get("direction_sensor")
        # Direction readback tokens (decompiled EHG DirectionMovement enum).
        self._extend_token = str(defn.get("extend_token", "Extend"))
        self._retract_token = str(defn.get("retract_token", "Retract"))

        features = CoverEntityFeature(0)
        if self._open_sid is not None:
            features |= CoverEntityFeature.OPEN
        if self._close_sid is not None:
            features |= CoverEntityFeature.CLOSE
        if self._stop.get("sid") is not None:
            features |= CoverEntityFeature.STOP
        if self._position_sid is not None:
            features |= CoverEntityFeature.SET_POSITION
        self._attr_supported_features = features

        self._optimistic_state: str | None = None

    def _read_int(self, sensor: str | None) -> int | None:
        if not sensor or self.coordinator.data is None:
            return None
        raw = _resolve_path(self.coordinator.data, f"signalr_sensors.{sensor}")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def current_cover_position(self) -> int | None:
        """Return the current position (0 = closed, 100 = fully open)."""
        pos = self._read_int(self._position_sensor)
        if pos is None:
            return None
        return max(0, min(100, pos))

    @property
    def is_closed(self) -> bool | None:
        """Return True when fully retracted; None when position is unknown."""
        pos = self.current_cover_position
        if pos is None:
            return None
        return pos == 0

    def _direction(self) -> str | None:
        if not self._direction_sensor or self.coordinator.data is None:
            return None
        raw = _resolve_path(
            self.coordinator.data, f"signalr_sensors.{self._direction_sensor}"
        )
        return str(raw) if raw is not None else None

    @property
    def is_opening(self) -> bool:
        """Return True while extending."""
        if self._optimistic_state == "opening":
            return True
        return self._direction() == self._extend_token

    @property
    def is_closing(self) -> bool:
        """Return True while retracting."""
        if self._optimistic_state == "closing":
            return True
        return self._direction() == self._retract_token

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Extend the cover (momentary write)."""
        if self._open_sid is None:
            return
        await self.coordinator.async_send_light_command(
            self._bus, int(self._open_sid), bool_value=True
        )
        self._optimistic_state = "opening"
        self.async_write_ha_state()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Retract the cover (momentary write)."""
        if self._close_sid is None:
            return
        await self.coordinator.async_send_light_command(
            self._bus, int(self._close_sid), bool_value=True
        )
        self._optimistic_state = "closing"
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        sid = self._stop.get("sid")
        if sid is None:
            return
        if "str" in self._stop:
            await self.coordinator.async_send_light_command(
                self._bus, int(sid), str_value=str(self._stop["str"])
            )
        elif "uint" in self._stop:
            await self.coordinator.async_send_light_command(
                self._bus, int(sid), uint_value=int(self._stop["uint"])
            )
        else:
            await self.coordinator.async_send_light_command(
                self._bus, int(sid), bool_value=True
            )
        self._optimistic_state = None
        self.async_write_ha_state()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a target position (0-100)."""
        if self._position_sid is None or "position" not in kwargs:
            return
        position = max(0, min(100, int(kwargs["position"])))
        await self.coordinator.async_send_light_command(
            self._bus, int(self._position_sid), uint_value=position
        )
        self.async_write_ha_state()

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic movement once the SCU reports a settled direction."""
        if self._optimistic_state is not None:
            direction = self._direction()
            if direction is not None and direction not in (
                self._extend_token,
                self._retract_token,
            ):
                self._optimistic_state = None
        super()._handle_coordinator_update()
