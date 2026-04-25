"""Config flow for HYMER Connect integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import HymerConnectApi, HymerConnectApiError, HymerConnectAuthError
from .const import (
    BRANDS,
    CONF_ACCESS_TOKEN,
    CONF_BLE_ADDRESS,
    CONF_BLE_ENABLED,
    CONF_BRAND,
    CONF_EHG_REFRESH_TOKEN,
    CONF_QR_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_SCU_URN,
    CONF_TANK_CAPACITY,
    CONF_VEHICLE_URN,
    DEFAULT_TANK_CAPACITY_LITERS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BRAND, default="hymer"): vol.In(BRANDS),
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_EHG_REFRESH_TOKEN, default=""): str,
    }
)


class HymerConnectConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HYMER Connect."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict[str, Any] = {}
        self._api: HymerConnectApi | None = None

    @staticmethod
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> HymerConnectOptionsFlow:
        """Get the options flow for this handler."""
        return HymerConnectOptionsFlow(config_entry)

    async def _async_try_authenticate(
        self, brand: str, username: str, password: str
    ) -> dict[str, str]:
        """Try to authenticate and return tokens."""
        session = async_create_clientsession(self.hass)
        self._api = HymerConnectApi(session, brand=brand)
        return await self._api.authenticate(username, password)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — login credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                tokens = await self._async_try_authenticate(
                    user_input[CONF_BRAND],
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
            except HymerConnectAuthError:
                errors["base"] = "invalid_auth"
            except HymerConnectApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during authentication")
                errors["base"] = "unknown"
            else:
                unique_id = user_input[CONF_USERNAME].lower()
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                self._data = {
                    CONF_BRAND: user_input[CONF_BRAND],
                    CONF_USERNAME: user_input[CONF_USERNAME],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_ACCESS_TOKEN: tokens["access_token"],
                    CONF_REFRESH_TOKEN: tokens["refresh_token"],
                    CONF_EHG_REFRESH_TOKEN: user_input.get(CONF_EHG_REFRESH_TOKEN, ""),
                }
                return await self.async_step_vehicle()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def _async_resolve_scu_urn(self) -> str:
        """Fetch SCU URN from the SCC vehicle list."""
        if self._api is None:
            return ""
        try:
            scc_vehicles = await self._api.get_vehicles()
            if scc_vehicles:
                return scc_vehicles[0].get("smartUnitUrn", "")
        except HymerConnectApiError:
            _LOGGER.debug("Could not fetch SCC vehicles for SCU URN")
        return ""

    async def async_step_vehicle(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle vehicle activation — QR code token + SCU BLE address.

        This step is optional. Users who already have an EHG refresh token
        (from mitmproxy) can skip it by leaving both fields empty — the
        integration falls back to cloud-only mode and auto-discovers the
        vehicle URN at runtime.

        For BLE pairing, both the QR activation token and the SCU BLE
        address are needed:
          1. User scans/enters the QR code text from the vehicle sticker
          2. User provides the SCU Bluetooth MAC address (or leaves empty to auto-scan)
          3. At runtime, BLE pairing happens: SCU prompts "Allow?" on touchscreen,
             user presses ALLOW, SCU issues a remoteAccessToken bound to the
             RPi's BLE MAC — stored as the EHG refresh token.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            qr_token = user_input.get(CONF_QR_TOKEN, "").strip()
            ble_address = user_input.get(CONF_BLE_ADDRESS, "").strip()

            if qr_token:
                # QR token provided — resolve vehicle via API
                try:
                    vehicle_info = await self._api.get_vehicle_by_token(qr_token)
                    vehicle_urn = vehicle_info.get("urn", "")
                    scu_urn = vehicle_info.get("smartUnitUrn", "")
                    if not scu_urn:
                        scu_urn = await self._async_resolve_scu_urn()
                    if not vehicle_urn:
                        errors["base"] = "invalid_qr_token"
                    else:
                        self._data[CONF_VEHICLE_URN] = vehicle_urn
                        self._data[CONF_SCU_URN] = scu_urn
                        self._data[CONF_BLE_ADDRESS] = ble_address
                        self._data[CONF_BLE_ENABLED] = bool(ble_address)
                except HymerConnectApiError:
                    errors["base"] = "invalid_qr_token"
            else:
                # No QR token — cloud-only mode, auto-discover at runtime
                self._data[CONF_VEHICLE_URN] = ""
                self._data[CONF_SCU_URN] = ""
                self._data[CONF_BLE_ADDRESS] = ""
                self._data[CONF_BLE_ENABLED] = False

            if not errors:
                brand_name = BRANDS.get(
                    self._data[CONF_BRAND], self._data[CONF_BRAND]
                )
                return self.async_create_entry(
                    title=f"HYMER Connect ({brand_name})",
                    data=self._data,
                )

        return self.async_show_form(
            step_id="vehicle",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_QR_TOKEN, default=""): str,
                    vol.Optional(CONF_BLE_ADDRESS, default=""): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauth when credentials expire."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth confirmation."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            try:
                tokens = await self._async_try_authenticate(
                    reauth_entry.data[CONF_BRAND],
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
            except HymerConnectAuthError:
                errors["base"] = "invalid_auth"
            except HymerConnectApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during reauthentication")
                errors["base"] = "unknown"
            else:
                unique_id = user_input[CONF_USERNAME].lower()
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_ACCESS_TOKEN: tokens["access_token"],
                        CONF_REFRESH_TOKEN: tokens["refresh_token"],
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=reauth_entry.data.get(CONF_USERNAME, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )


class HymerConnectOptionsFlow(OptionsFlow):
    """Handle options for HYMER Connect."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options — tank capacity + BLE settings."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_capacity = self._config_entry.options.get(
            CONF_TANK_CAPACITY, DEFAULT_TANK_CAPACITY_LITERS
        )
        current_ble_enabled = self._config_entry.options.get(
            CONF_BLE_ENABLED, False
        )
        current_ble_address = self._config_entry.options.get(
            CONF_BLE_ADDRESS, ""
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TANK_CAPACITY,
                        default=current_capacity,
                    ): vol.All(vol.Coerce(int), vol.Range(min=30, max=200)),
                    vol.Optional(
                        CONF_BLE_ENABLED,
                        default=current_ble_enabled,
                    ): bool,
                    vol.Optional(
                        CONF_BLE_ADDRESS,
                        default=current_ble_address,
                    ): str,
                }
            ),
            description_placeholders={
                "ble_help": "Enable BLE direct path for local SCU control (experimental). "
                "Requires BLE hardware and physical SCU pairing. "
                "Leave address empty to auto-scan.",
            },
        )
