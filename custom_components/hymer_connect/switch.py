"""Switch platform for HYMER Connect — controllable switches.

Switch definitions are loaded from JSON ``"switches"`` sections in
``sensor_maps/base.json`` + ``{brand}.json``.  Each entry defines the
bus/slot, write type (str/bool/uint), on/off values, and optional
holdoff for SCU bounce-back protection.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import HymerConnectCoordinator
from .sensor import _resolve_path
from .signalr_client import EXTENDED_STANDBY_THRESHOLD, STALE_DATA_TIMEOUT

_LOGGER = logging.getLogger(__name__)

# Grace window (s) for a trailing echo frame after a 12V-OFF command before
# treating the SCU stream as silent (= 12V confirmed off).
_OFF_CONFIRM_GRACE = 5.0


@dataclass(frozen=True, kw_only=True)
class HymerSwitchEntityDescription(SwitchEntityDescription):
    """Describe a HYMER Connect switch."""

    bus_id: int
    sensor_id: int
    value_path: str
    on_value: Any = True
    write_type: str = "bool"       # "str", "bool", or "uint"
    write_on: Any = None           # value sent for ON (str switches)
    write_off: Any = None          # value sent for OFF (str switches)
    holdoff_off: int = 15          # seconds to hold optimistic OFF
    requires_12v: bool = False     # entity unavailable when 12V off
    is_main_switch: bool = False   # special main_switch optimistic hack
    # Command-pair ("momentary") switches write ON and OFF to *different*
    # slots — e.g. a satellite dish where ON=Start(10,1) and OFF=Park(10,2),
    # each triggered by sending bool true to its own slot.  When these are
    # None the write target falls back to (bus_id, sensor_id).  bool_on/bool_off
    # let both directions send the same trigger value (true/true) instead of
    # the default toggle (true/false).
    write_on_bus: int | None = None
    write_on_sid: int | None = None
    write_off_bus: int | None = None
    write_off_sid: int | None = None
    bool_on: bool = True           # bool value sent for ON
    bool_off: bool = False         # bool value sent for OFF


def _build_switch_descriptions() -> tuple[
    list[HymerSwitchEntityDescription],
    list[tuple[HymerSwitchEntityDescription, str]],
]:
    """Build switch entity descriptions from JSON-loaded SWITCH_DEFS.

    Returns ``(always, gated)`` where ``gated`` entries carry
    ``require_observed`` and are created on demand once their read sensor is
    reported by the vehicle (keyed by the sensor name to watch).
    """
    from .pia_decoder import SWITCH_DEFS

    always: list[HymerSwitchEntityDescription] = []
    gated: list[tuple[HymerSwitchEntityDescription, str]] = []
    for key_str, meta in SWITCH_DEFS.items():
        if not isinstance(meta, dict) or "name" not in meta:
            continue
        parts = key_str.split(",")
        if len(parts) != 2:
            continue
        bus_id, sensor_id = int(parts[0].strip()), int(parts[1].strip())
        name = meta["name"]
        read_path = meta.get("read_path", f"signalr_sensors.{name}")
        kwargs: dict[str, Any] = {
            "key": name,
            "translation_key": name,
            "device_class": SwitchDeviceClass.SWITCH,
            "bus_id": bus_id,
            "sensor_id": sensor_id,
            "value_path": read_path,
        }
        if "on_value" in meta:
            kwargs["on_value"] = meta["on_value"]
        if meta.get("icon"):
            kwargs["icon"] = meta["icon"]
        if meta.get("write_type"):
            kwargs["write_type"] = meta["write_type"]
        if "write_on" in meta:
            kwargs["write_on"] = meta["write_on"]
        if "write_off" in meta:
            kwargs["write_off"] = meta["write_off"]
        if "holdoff_off" in meta:
            kwargs["holdoff_off"] = meta["holdoff_off"]
        for _split_key in (
            "write_on_bus", "write_on_sid",
            "write_off_bus", "write_off_sid",
            "bool_on", "bool_off",
        ):
            if _split_key in meta:
                kwargs[_split_key] = meta[_split_key]
        if meta.get("requires_12v"):
            kwargs["requires_12v"] = True
        if meta.get("enabled") is False:
            kwargs["entity_registry_enabled_default"] = False
        # Detect main switch for optimistic sensor_data hack
        if name == "main_switch_ctrl":
            kwargs["is_main_switch"] = True
        desc = HymerSwitchEntityDescription(**kwargs)
        if meta.get("require_observed"):
            watch = read_path.split(".", 1)[-1] if "." in read_path else name
            gated.append((desc, watch))
        else:
            always.append(desc)
    return always, gated


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HYMER Connect switches from a config entry."""
    coordinator: HymerConnectCoordinator = hass.data[DOMAIN][entry.entry_id]
    descriptions, gated = _build_switch_descriptions()
    _LOGGER.debug(
        "Switch platform: %d switch entities from JSON (+%d gated)",
        len(descriptions), len(gated),
    )
    async_add_entities(
        HymerConnectSwitch(coordinator, desc, entry)
        for desc in descriptions
    )

    if not gated:
        return

    # Observation-gated switches: create each only once its read sensor is
    # actually reported, so absent components leave no phantom switch.
    created_keys: set[str] = set()

    @callback
    def _async_discover_gated() -> None:
        if not coordinator.data:
            return
        sensors = coordinator.data.get("signalr_sensors")
        if not isinstance(sensors, dict):
            return
        new_entities: list[HymerConnectSwitch] = []
        for desc, watch in gated:
            if desc.key in created_keys:
                continue
            if watch not in sensors:
                continue
            created_keys.add(desc.key)
            new_entities.append(HymerConnectSwitch(coordinator, desc, entry))
            _LOGGER.info(
                "Observation-gated switch %s materialised (%s reported)",
                desc.key, watch,
            )
        if new_entities:
            async_add_entities(new_entities)

    _async_discover_gated()
    entry.async_on_unload(coordinator.async_add_listener(_async_discover_gated))


class HymerConnectSwitch(
    CoordinatorEntity[HymerConnectCoordinator], SwitchEntity
):
    """Representation of a HYMER Connect switch."""

    entity_description: HymerSwitchEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HymerConnectCoordinator,
        description: HymerSwitchEntityDescription,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "HYMER",
            "manufacturer": MANUFACTURER,
            "model": "Smart Interface Unit",
        }
        self._optimistic_on: bool | None = None
        self._optimistic_set_at: float = 0.0  # monotonic timestamp of last command
        self._verify_task: asyncio.Task | None = None
        self._retry_task: asyncio.Task | None = None
        self._retry_count: int = 0  # cap reconnect+retry loops
        self._is_retry: bool = False  # True when called from _retry_after_reconnect

    async def _verify_send(self, expected_on: bool) -> None:
        """Verify the SCU acknowledged the command after a delay.

        When the SCU is offline (scu_connected=false), commands are queued
        server-side but not delivered until the SCU wakes up.  In that case
        we clear optimistic state so the UI shows the real readback, but
        do NOT kill the SignalR connection (it's healthy — the SCU is just
        asleep).
        """
        # Main switch ON needs extra time — SCU may be waking from standby
        if expected_on and self.entity_description.is_main_switch:
            delay = 60
        elif not expected_on:
            delay = self.entity_description.holdoff_off
        else:
            delay = 15
        await asyncio.sleep(delay)
        if self.coordinator.data is None:
            return
        value = _resolve_path(
            self.coordinator.data, self.entity_description.value_path
        )
        if value is None:
            return
        if isinstance(value, str) and isinstance(self.entity_description.on_value, str):
            actual_on = value.upper() == self.entity_description.on_value.upper()
        else:
            actual_on = value == self.entity_description.on_value
        if actual_on != expected_on:
            # Check if SCU is offline — if so, the command wasn't delivered
            # yet but SignalR itself is fine.
            client = self.coordinator.signalr_client
            scu_online = False
            if client:
                scu_online = client._sensor_data.get("scu_connected") is True

            # 12V main switch OFF: on some vehicles (e.g. CBE EBL402 on bus 3)
            # the 12V main relay does NOT power down the SCU, so it keeps
            # reporting scu_connected=True — but it STOPS streaming sensor data
            # the instant 12V goes off.  A readback still at "On" with no fresh
            # frames received since the command IS the confirmation that 12V
            # switched off, not a dead command channel.  Only treat it as a
            # failed command when the SCU is genuinely still streaming.
            if self.entity_description.is_main_switch and not expected_on:
                last_data = client._last_data_received if client else 0.0
                no_fresh_data = (
                    last_data <= self._optimistic_set_at + _OFF_CONFIRM_GRACE
                )
                if not scu_online or no_fresh_data:
                    _LOGGER.info(
                        "Switch %s: 12V OFF confirmed — SCU stopped streaming "
                        "(scu_connected=%s, no frames since command) as "
                        "expected after 12V off",
                        self.entity_description.key, scu_online,
                    )
                    # Keep optimistic OFF; don't revert to the stale "On" and
                    # don't trigger a pointless re-auth/reconnect.
                    self._retry_count = 0
                    return

            if not scu_online:
                # Check how long SCU has been in standby.  After extended
                # standby (hours/days) the OAuth2 session routing is stale
                # — commands sent through this session are silently dropped
                # by the server, not actually "queued".  Force a full
                # re-auth + reconnect + retry instead of giving up.
                standby_secs = 0.0
                if client and client._scu_disconnected_at > 0:
                    standby_secs = time.monotonic() - client._scu_disconnected_at

                if standby_secs > EXTENDED_STANDBY_THRESHOLD:
                    self._retry_count += 1
                    if self._retry_count > 1:
                        _LOGGER.warning(
                            "Switch %s: command (%s) still not confirmed "
                            "after re-auth + retry during extended standby "
                            "(%.0fs) — giving up",
                            self.entity_description.key, expected_on,
                            standby_secs,
                        )
                        self._optimistic_on = None
                        self._retry_count = 0
                        self.async_write_ha_state()
                        return
                    _LOGGER.warning(
                        "Switch %s: command (%s) not confirmed after %ds "
                        "— SCU offline for %.0fs (extended standby), "
                        "session likely stale — forcing full re-auth + "
                        "reconnect (attempt %d)",
                        self.entity_description.key, expected_on, delay,
                        standby_secs, self._retry_count,
                    )
                    coord = self.coordinator
                    coord._reconnect_backoff = 60
                    coord._last_reconnect_attempt = 0.0
                    self.hass.async_create_task(
                        coord.force_reauth_and_reconnect()
                    )
                    self._retry_task = asyncio.ensure_future(
                        self._retry_after_reconnect(expected_on)
                    )
                else:
                    _LOGGER.warning(
                        "Switch %s: command (%s) not confirmed after %ds "
                        "— SCU is offline (standby %.0fs), command queued "
                        "until SCU wakes up",
                        self.entity_description.key, expected_on, delay,
                        standby_secs,
                    )
                    # Short standby — server queue is likely valid
                    self._optimistic_on = None
                    self.async_write_ha_state()
            else:
                # A hung SCU keeps scu_connected=on and the SignalR link healthy,
                # but ignores every command — a full re-auth won't help and just
                # churns. The "SCU frozen" diagnostic sensor is already on; stop
                # here instead of re-auth-storming (only a power-cycle recovers).
                if self.coordinator.scu_frozen:
                    _LOGGER.warning(
                        "Switch %s: command (%s) not confirmed — SCU appears "
                        "frozen (clock stuck; ignores BLE + cloud). A physical "
                        "power-cycle is required; not re-authenticating.",
                        self.entity_description.key, expected_on,
                    )
                    self._optimistic_on = None
                    self._retry_count = 0
                    self.async_write_ha_state()
                    return
                self._retry_count += 1
                if self._retry_count > 1:
                    _LOGGER.warning(
                        "Switch %s: readback still mismatched after %d "
                        "reconnect+retry attempts — giving up (reload "
                        "integration to recover)",
                        self.entity_description.key,
                        self._retry_count,
                    )
                    self._optimistic_on = None
                    self._retry_count = 0
                    self.async_write_ha_state()
                    return
                _LOGGER.warning(
                    "Switch %s: SCU readback (%s) doesn't match commanded (%s) "
                    "after %ds — command channel dead, forcing full re-auth + "
                    "reconnect (attempt %d)",
                    self.entity_description.key, value, expected_on, delay,
                    self._retry_count,
                )
                # Force full OAuth2 re-authentication + SignalR reconnect.
                # A simple SignalR reconnect (negotiate only) reuses the
                # stale OAuth2 session and creates a connection that looks
                # healthy but can't deliver commands.  Full re-auth creates
                # a new server-side session with clean hub→SCU routing —
                # matching what integration reload does.
                coord = self.coordinator
                coord._reconnect_backoff = 60  # _INITIAL_BACKOFF
                coord._last_reconnect_attempt = 0.0
                _LOGGER.info(
                    "Triggering full re-auth + reconnect for clean command channel"
                )
                self.hass.async_create_task(
                    coord.force_reauth_and_reconnect()
                )
                # Retry the command after reconnect settles
                self._retry_task = asyncio.ensure_future(
                    self._retry_after_reconnect(expected_on)
                )

    async def _retry_after_reconnect(self, expected_on: bool) -> None:
        """Wait for SignalR to reconnect, then re-send the failed command.

        After _verify_send detects a dead send channel and marks SignalR as
        disconnected, the coordinator's next poll cycle will reconnect.  We
        wait up to 90s for the connection to come back, then re-issue the
        original command exactly once.  No further verification loop — if the
        retry also fails, the user will see the mismatch in the UI.
        """
        # Wait for reconnect (coordinator polls every ~60s)
        for _ in range(18):  # 18 × 5s = 90s
            await asyncio.sleep(5)
            client = self.coordinator.signalr_client
            if client and client._connected:
                break
        else:
            _LOGGER.warning(
                "Switch %s: reconnect did not complete within 90s — "
                "skipping command retry",
                self.entity_description.key,
            )
            self._optimistic_on = None
            self.async_write_ha_state()
            return

        _LOGGER.info(
            "Switch %s: SignalR reconnected — retrying %s command",
            self.entity_description.key,
            "ON" if expected_on else "OFF",
        )
        self._is_retry = True
        try:
            if expected_on:
                await self.async_turn_on()
            else:
                await self.async_turn_off()
        finally:
            self._is_retry = False

    @property
    def available(self) -> bool:
        """Switches with requires_12v need the 12V main switch to be on."""
        if self.entity_description.requires_12v:
            if self.coordinator.data is None:
                return False
            main = _resolve_path(self.coordinator.data, "signalr_sensors.main_switch")
            # App parity: water pump + lights sit on the bus-3 habitation
            # controller and are the ONLY entities the EHG app greys out at
            # 12V-off; gate purely on the 12V main state. Only a mapped
            # main_switch reports the string "On"/"Off"/"Changing"; a phantom raw
            # bus-3 int (vehicles without a bus-3 controller, #20) must NOT be
            # misread as "Off".
            if isinstance(main, str) and main == "Off":
                return False
            # SCU standby (scu_connected=False) is NOT 12V-off: the SCU can flap
            # into cloud standby while 12V stays on, which previously greyed the
            # running pump (v2.95.5 regression). Do NOT gate on scu_connected.
            # Fallback for vehicles whose main_switch freezes at "On" when 12V is
            # cut (#20/#24): prolonged data silence from ANY transport = 12V off.
            # Standby push frames keep this clock alive, so it never false-fires
            # during the reconnect flapping above.
            if self.coordinator.data_silence_seconds > self.coordinator.unavailable_silence_threshold:
                return False
        return super().available

    @property
    def is_on(self) -> bool | None:
        """Return True if the switch is on."""
        if self._optimistic_on is not None:
            return self._optimistic_on
        if self.coordinator.data is None:
            return None
        value = _resolve_path(
            self.coordinator.data, self.entity_description.value_path
        )
        if value is None:
            return None
        return value == self.entity_description.on_value

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        desc = self.entity_description
        on_bus = desc.write_on_bus if desc.write_on_bus is not None else desc.bus_id
        on_sid = desc.write_on_sid if desc.write_on_sid is not None else desc.sensor_id
        _LOGGER.info(
            "Switch %s → ON (bus=%d, sid=%d, type=%s)",
            desc.key, on_bus, on_sid, desc.write_type,
        )
        if desc.write_type == "str":
            on_str = desc.write_on if desc.write_on else str(desc.on_value)
            await self.coordinator.async_send_light_command(
                on_bus, on_sid, str_value=on_str,
            )
        elif desc.write_type == "uint":
            await self.coordinator.async_send_light_command(
                on_bus, on_sid, uint_value=1,
            )
        else:
            await self.coordinator.async_send_light_command(
                on_bus, on_sid, bool_value=desc.bool_on,
            )
        self._optimistic_on = True
        self._optimistic_set_at = time.monotonic()
        if not self._is_retry:
            self._retry_count = 0  # reset on new explicit command only
        # NOTE: Do NOT write back into client._sensor_data here.  Doing so
        # poisons _verify_send (which reads the same path back) and the
        # is_standby check in needs_reconnect (main_switch=='Off' bypasses
        # the 3-min stale-data reconnect).  _optimistic_on already drives
        # is_on for the UI; the SCU will push the real value within ~3s.
        self.async_write_ha_state()
        if self._verify_task and not self._verify_task.done():
            self._verify_task.cancel()
        self._verify_task = asyncio.ensure_future(self._verify_send(True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        desc = self.entity_description
        off_bus = desc.write_off_bus if desc.write_off_bus is not None else desc.bus_id
        off_sid = desc.write_off_sid if desc.write_off_sid is not None else desc.sensor_id
        _LOGGER.info(
            "Switch %s → OFF (bus=%d, sid=%d, type=%s)",
            desc.key, off_bus, off_sid, desc.write_type,
        )
        if desc.write_type == "str":
            off_str = desc.write_off if desc.write_off else "Off"
            await self.coordinator.async_send_light_command(
                off_bus, off_sid, str_value=off_str,
            )
        elif desc.write_type == "uint":
            await self.coordinator.async_send_light_command(
                off_bus, off_sid, uint_value=0,
            )
        else:
            await self.coordinator.async_send_light_command(
                off_bus, off_sid, bool_value=desc.bool_off,
            )
        self._optimistic_on = False
        self._optimistic_set_at = time.monotonic()
        # NOTE: Do NOT write back into client._sensor_data here.  See the
        # matching comment in async_turn_on — poisoning _sensor_data breaks
        # _verify_send and the standby reconnect heuristic.
        self.async_write_ha_state()
        if self._verify_task and not self._verify_task.done():
            self._verify_task.cancel()
        self._verify_task = asyncio.ensure_future(self._verify_send(False))

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state when SCU confirms the commanded value."""
        if self._optimistic_on is not None and self.coordinator.data:
            value = _resolve_path(
                self.coordinator.data, self.entity_description.value_path
            )
            if value is not None:
                if isinstance(value, str) and isinstance(self.entity_description.on_value, str):
                    actual = value.upper() == self.entity_description.on_value.upper()
                else:
                    actual = value == self.entity_description.on_value

                # Hold optimistic OFF through bounce-back window
                if (
                    self._optimistic_on is False
                    and actual is True
                    and self.entity_description.holdoff_off > 15
                ):
                    elapsed = time.monotonic() - self._optimistic_set_at
                    if elapsed < self.entity_description.holdoff_off:
                        _LOGGER.debug(
                            "Switch %s: ignoring stale readback "
                            "%.1fs after OFF command (holdoff %ds)",
                            self.entity_description.key,
                            elapsed,
                            self.entity_description.holdoff_off,
                        )
                        super()._handle_coordinator_update()
                        return

                if actual == self._optimistic_on:
                    self._optimistic_on = None
        super()._handle_coordinator_update()
