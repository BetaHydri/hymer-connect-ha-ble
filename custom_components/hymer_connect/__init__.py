"""HYMER Connect integration for Home Assistant."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .api import HymerConnectApi, HymerConnectApiError, HymerConnectAuthError
from .const import (
    BRANDS,
    CONF_ACCESS_TOKEN,
    CONF_BRAND,
    CONF_EHG_REFRESH_TOKEN,
    CONF_OAUTH_BASIC_AUTH,
    CONF_REFRESH_TOKEN,
    CONF_SCU_URN,
    CONF_VEHICLE_URN,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import HymerConnectCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORM_LIST = [Platform(p) for p in PLATFORMS]

type HymerConnectConfigEntry = ConfigEntry


async def async_setup_entry(
    hass: HomeAssistant, entry: HymerConnectConfigEntry
) -> bool:
    """Set up HYMER Connect from a config entry."""
    session = async_get_clientsession(hass)
    brand = entry.data.get(CONF_BRAND, "hymer")
    # Show the selected brand in the entry title, but only upgrade the old
    # auto-generated title - never override a title the user renamed themselves.
    brand_name = BRANDS.get(brand, brand)
    if entry.title == f"HYMER Connect ({brand_name})":
        hass.config_entries.async_update_entry(
            entry, title=f"HYMER Connect BLE (for {brand_name})"
        )
    oauth_basic_auth = entry.data.get(CONF_OAUTH_BASIC_AUTH, "") or None
    api = HymerConnectApi(session, brand=brand, oauth_basic_auth=oauth_basic_auth)

    # Always re-authenticate with stored credentials to get fresh tokens
    if CONF_USERNAME in entry.data:
        try:
            tokens = await api.authenticate(
                entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
            )
            # Update stored tokens
            hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_ACCESS_TOKEN: tokens["access_token"],
                    CONF_REFRESH_TOKEN: tokens["refresh_token"],
                },
            )
        except HymerConnectAuthError as err:
            raise ConfigEntryAuthFailed(
                f"Authentication failed: {err}"
            ) from err
        except HymerConnectApiError as err:
            raise ConfigEntryNotReady(
                f"Cannot connect to HYMER API: {err}"
            ) from err
    else:
        raise ConfigEntryAuthFailed("No credentials available")

    vehicle_urn = entry.data.get(CONF_VEHICLE_URN, "")
    scu_urn = entry.data.get(CONF_SCU_URN, "")
    ehg_refresh_token = entry.data.get(CONF_EHG_REFRESH_TOKEN, "")

    # Load sensor map: base (shared) + brand-specific overlay
    from .pia_decoder import load_sensor_map

    await hass.async_add_executor_job(load_sensor_map, brand)

    coordinator = HymerConnectCoordinator(
        hass, api, session, entry,
        vehicle_urn=vehicle_urn,
        scu_urn=scu_urn,
        ehg_refresh_token=ehg_refresh_token,
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Reload integration when options change (e.g. tank capacity)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # BLE (re)connect is otherwise carried by the coordinator poll, which is
    # starved whenever SignalR pushes keep rescheduling it. An independent
    # watchdog ensures BLE connects/recovers even while the cloud is healthy.
    entry.async_on_unload(
        async_track_time_interval(
            hass, coordinator.async_ble_watchdog, timedelta(seconds=30)
        )
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORM_LIST)

    # #23: at a BLE-first cold start the one-shot habitation slots can arrive
    # before the platform discovery listeners are attached, so those gated
    # entities never materialise. Re-deliver the full snapshot a few times over
    # the warm-up window so discovery runs against the complete set.
    for _delay in (15, 45, 90):
        entry.async_on_unload(
            async_call_later(hass, _delay, coordinator.async_warmup_reobserve)
        )

    # Auto-enable entities that were disabled during integration upgrades.
    # HA core may disable new entities added after initial setup.  All entities
    # should be enabled except Victron (bus 121) which has "enabled": false
    # in hymer.json.  Only re-enables entities disabled by the integration,
    # not ones the user deliberately disabled.
    _async_enable_new_entities(hass, entry)

    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: HymerConnectConfigEntry
) -> None:
    """Handle options update — apply new settings without full reload.

    Options (tank capacity, BLE address, etc.) are read dynamically from
    config_entry.options on every coordinator poll cycle, so no reload is
    needed.  A full async_reload() caused the 'Initialisieren' cycling bug
    where SignalR was torn down and reconnected every time options were
    evaluated.  Fixed in v2.63.10.
    """
    _LOGGER.info("Options updated for %s — applied without reload", entry.title)

    # Mirror the option both ways: enabling kicks an immediate connect instead
    # of waiting for the watchdog/poll; disabling tears the live link down now
    # instead of leaving it up until a reload (#20, @stbcgn). Both callees
    # self-guard, so they are safe no-ops in the wrong state.
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        if coordinator.ble_enabled:
            hass.async_create_task(coordinator._async_try_ble_connect())
        else:
            hass.async_create_task(coordinator.stop_ble())


def _async_enable_new_entities(
    hass: HomeAssistant, entry: HymerConnectConfigEntry
) -> None:
    """Re-enable entities that were disabled during integration upgrades.

    HA core disables new entities that appear after the initial platform
    setup (e.g. when new sensors are added in a version update).  This
    migration re-enables them so the user doesn't have to hunt for
    disabled entities after every upgrade.

    Skips entities whose sensor-map definition has ``"enabled": false``
    (Victron bus 121).  Only touches entities disabled by the integration
    (``disabled_by == RegistryEntryDisabler.INTEGRATION``), leaving
    user-disabled entities untouched.
    """
    from .pia_decoder import ENTITY_DEFS

    # Build set of entity keys that should stay disabled
    keep_disabled: set[str] = {
        name for name, meta in ENTITY_DEFS.items()
        if meta.get("enabled") is False
    }

    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    re_enabled = 0

    for entity_entry in entries:
        if entity_entry.disabled_by != er.RegistryEntryDisabler.INTEGRATION:
            continue

        # Extract the sensor key from the unique_id: "{entry_id}_{key}"
        unique_id = entity_entry.unique_id or ""
        prefix = f"{entry.entry_id}_"
        if unique_id.startswith(prefix):
            key = unique_id[len(prefix):]
        else:
            key = unique_id

        # Skip entities that should remain disabled (Victron)
        if key in keep_disabled:
            continue

        # Also skip discovered diagnostic entities (bus*_s*)
        if key.startswith("discovered_"):
            continue

        registry.async_update_entity(
            entity_entry.entity_id, disabled_by=None
        )
        re_enabled += 1

    if re_enabled:
        _LOGGER.info(
            "Auto-enabled %d entities that were disabled after upgrade",
            re_enabled,
        )


async def async_unload_entry(
    hass: HomeAssistant, entry: HymerConnectConfigEntry
) -> bool:
    """Unload a config entry."""
    coordinator: HymerConnectCoordinator = hass.data[DOMAIN].get(entry.entry_id)
    if coordinator:
        # Full teardown: stop SignalR AND release the BLE client + its background
        # listen loop. Skipping the BLE side orphans the BleakClient on reload,
        # which wedges BlueZ on USB-passthrough hosts (e.g. Proxmox) until reboot.
        await coordinator.async_shutdown()
    if unload_ok := await hass.config_entries.async_unload_platforms(
        entry, PLATFORM_LIST
    ):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def async_remove_entry(
    hass: HomeAssistant, entry: HymerConnectConfigEntry
) -> None:
    """Clean up when integration is removed — clear BlueZ bond.

    The SCU BLE bond must be removed from BlueZ so that re-adding the
    integration triggers a fresh pairing and the SCU issues a new
    EHG refresh token. Without this, the stale bond causes
    'Response does not contain mobilePair field' on re-pairing.
    """
    from .ble_client import async_clear_bluez_bond
    from .const import CONF_BLE_ADDRESS

    ble_address = entry.data.get(CONF_BLE_ADDRESS, "")
    if ble_address:
        removed = await async_clear_bluez_bond(ble_address)
        if removed:
            _LOGGER.info(
                "Cleared BlueZ bond for %s on integration removal", ble_address
            )
        else:
            _LOGGER.debug(
                "No BlueZ bond to clear for %s", ble_address
            )
