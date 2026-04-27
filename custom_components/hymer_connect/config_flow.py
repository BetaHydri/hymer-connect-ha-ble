"""Config flow for HYMER Connect integration."""

from __future__ import annotations

import asyncio
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
        self._ble_pairing_task: asyncio.Task | None = None
        self._ble_pairing_error: str = ""

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
          1. User presses VERBINDUNG (connection) button on SCU control panel
          2. User provides the SCU Bluetooth MAC address (or leaves empty to auto-scan)
          3. At runtime, BLE pairing happens: Pi connects via BLE/TLS,
             sends PairMobileRequest, SCU returns remoteAccessToken
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            qr_token = user_input.get(CONF_QR_TOKEN, "").strip()
            ble_address = user_input.get(CONF_BLE_ADDRESS, "").strip()
            ble_enabled = user_input.get(CONF_BLE_ENABLED, False)

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
                        self._data[CONF_BLE_ENABLED] = ble_enabled
                        self._data[CONF_QR_TOKEN] = qr_token
                except HymerConnectApiError:
                    errors["base"] = "invalid_qr_token"
            elif ble_address:
                # BLE address without QR token — can't pair without activation token
                errors[CONF_QR_TOKEN] = "qr_token_required"
            else:
                # No QR token, no BLE — cloud-only mode, auto-discover at runtime
                self._data[CONF_VEHICLE_URN] = ""
                self._data[CONF_SCU_URN] = ""
                self._data[CONF_BLE_ADDRESS] = ""
                self._data[CONF_BLE_ENABLED] = False

            if not errors:
                # If QR token provided, always attempt BLE pairing to get EHG token
                # (even if ble_enabled is False — user may want token-only, not BLE data path)
                if self._data.get(CONF_QR_TOKEN):
                    return await self.async_step_ble_pairing()
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
                    vol.Optional(CONF_BLE_ENABLED, default=True): bool,
                }
            ),
            errors=errors,
        )

    async def _async_do_ble_pairing(self) -> None:
        """Background task: connect BLE, establish TLS, pair with SCU.

        Stores the EHG refresh token in self._data on success, or sets
        self._ble_pairing_error on failure.
        """
        from .ble_client import ScuBleClient, BleTransportError

        ble_address = self._data.get(CONF_BLE_ADDRESS, "")
        qr_token = self._data.get(CONF_QR_TOKEN, "")

        # Tell the coordinator to skip BLE attempts while we're pairing
        # to prevent a concurrent connect/pair from racing and killing
        # our bonding negotiation.
        coordinator = None
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            coordinator = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if coordinator and hasattr(coordinator, "_ble_pairing_in_progress"):
                coordinator._ble_pairing_in_progress = True
                break

        try:
            # Auto-scan if no MAC address provided
            if not ble_address:
                scanner = ScuBleClient(scu_address="")
                devices = await scanner.scan_for_scu(timeout=10.0)
                if not devices:
                    self._ble_pairing_error = "ble_no_scu_found"
                    return
                ble_address = devices[0]["address"]
                self._data[CONF_BLE_ADDRESS] = ble_address
                _LOGGER.info("BLE scan found SCU: %s", ble_address)

            # Retry bonding up to 12 times over ~120 seconds.
            # The user needs time to press VERBINDUNG on the SCU after
            # submitting the config flow. Each attempt takes ~3-5 seconds
            # (connect + bond attempt + disconnect + sleep).
            max_bond_attempts = 12
            bond_retry_delay = 8  # seconds between attempts
            client = None

            for attempt in range(1, max_bond_attempts + 1):
                remaining_time = (max_bond_attempts - attempt + 1) * bond_retry_delay
                _LOGGER.warning(
                    "🔵 BLE pairing attempt %d/%d — waiting for VERBINDUNG button press "
                    "(%ds remaining). SCU bonding state: checking...",
                    attempt, max_bond_attempts, remaining_time,
                )
                try:
                    client = ScuBleClient(
                        scu_address=ble_address,
                        connect_timeout=15.0,
                        tls_timeout=30.0,
                    )
                    await client.connect()
                    # If connect() succeeded, bonding worked → VERBINDUNG was pressed!
                    _LOGGER.warning(
                        "🟢 BLE bonding SUCCESSFUL on attempt %d/%d — "
                        "VERBINDUNG was pressed! Proceeding to TLS + pairing...",
                        attempt, max_bond_attempts,
                    )
                    break
                except BleTransportError as bond_err:
                    if "VERBINDUNG" in str(bond_err) or "Authentication" in str(bond_err):
                        _LOGGER.warning(
                            "🔴 BLE bonding attempt %d/%d — VERBINDUNG NOT pressed yet "
                            "(SCU rejected bonding). Retrying in %ds... (%ds remaining)",
                            attempt, max_bond_attempts, bond_retry_delay, remaining_time,
                        )
                        if client:
                            try:
                                await client.disconnect()
                            except Exception:
                                pass
                            client = None
                        if attempt < max_bond_attempts:
                            await asyncio.sleep(bond_retry_delay)
                            continue
                        self._ble_pairing_error = "ble_pairing_failed"
                        return
                    raise  # Non-bonding error → don't retry
                except Exception as err:
                    _LOGGER.warning("BLE connect attempt %d failed: %s", attempt, err)
                    if client:
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
                        client = None
                    if attempt < max_bond_attempts:
                        await asyncio.sleep(bond_retry_delay)
                        continue
                    self._ble_pairing_error = "ble_pairing_failed"
                    return

            if not client or not client.connected:
                self._ble_pairing_error = "ble_pairing_failed"
                return

            try:
                # Step 2: TLS handshake
                await client.establish_tls()

                # Step 3: Get confirmation token from cloud API
                confirmation = await self._api.get_confirmation_token()
                confirmation_token = confirmation.get("token", "")
                if not confirmation_token:
                    self._ble_pairing_error = "ble_no_confirmation_token"
                    return

                # Step 4: PairMobileRequest → PairMobileResponse
                pair_result = await client.pair_mobile(
                    activation_token=qr_token,
                    confirmation_token=confirmation_token,
                    mobile_device_name="homeassistant",
                    timeout=120.0,
                )

                if pair_result.remote_access_refresh_token:
                    self._data[CONF_EHG_REFRESH_TOKEN] = (
                        pair_result.remote_access_refresh_token
                    )
                    _LOGGER.info("BLE pairing successful — EHG refresh token obtained")
                else:
                    self._ble_pairing_error = "ble_no_refresh_token"
            finally:
                await client.disconnect()

        except BleTransportError as err:
            _LOGGER.warning("BLE pairing failed: %s", err)
            self._ble_pairing_error = "ble_pairing_failed"
        except Exception as err:
            _LOGGER.warning("BLE pairing unexpected error: %s", err)
            self._ble_pairing_error = "ble_pairing_failed"
        finally:
            # Release the pairing lock so the coordinator can resume BLE attempts
            if coordinator and hasattr(coordinator, "_ble_pairing_in_progress"):
                coordinator._ble_pairing_in_progress = False

    async def async_step_ble_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show progress while BLE pairing runs in the background."""
        if not self._ble_pairing_task:
            self._ble_pairing_error = ""
            self._ble_pairing_task = self.hass.async_create_task(
                self._async_do_ble_pairing()
            )

        if not self._ble_pairing_task.done():
            return self.async_show_progress(
                step_id="ble_pairing",
                progress_action="ble_pairing",
                progress_task=self._ble_pairing_task,
            )

        # Task finished — advance to result step
        return self.async_show_progress_done(next_step_id="ble_result")

    async def async_step_ble_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show BLE pairing result and create/update the config entry."""
        from homeassistant.config_entries import SOURCE_RECONFIGURE

        is_reconfigure = self.source == SOURCE_RECONFIGURE

        if self._ble_pairing_error:
            _LOGGER.info(
                "BLE pairing did not complete (%s) — %s. "
                "Pairing will be retried automatically when near the vehicle.",
                self._ble_pairing_error,
                "updating entry" if is_reconfigure else "creating entry in cloud-only mode",
            )
        else:
            _LOGGER.info("BLE pairing succeeded — EHG refresh token obtained")

        if is_reconfigure:
            reconfigure_entry = self._get_reconfigure_entry()
            return self.async_update_reload_and_abort(
                reconfigure_entry,
                data_updates=self._data,
            )

        brand_name = BRANDS.get(
            self._data[CONF_BRAND], self._data[CONF_BRAND]
        )
        return self.async_create_entry(
            title=f"HYMER Connect ({brand_name})",
            data=self._data,
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

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration — add QR code / BLE address to an existing entry.

        Accessible via Settings → Integrations → HYMER Connect → ⋮ → Reconfigure.
        Allows users who initially set up cloud-only to add BLE pairing later
        (e.g. after installing the RPi in the vehicle).
        """
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            qr_token = user_input.get(CONF_QR_TOKEN, "").strip()
            ble_address = user_input.get(CONF_BLE_ADDRESS, "").strip()
            ehg_refresh_token = user_input.get(CONF_EHG_REFRESH_TOKEN, "").strip()

            data_updates: dict[str, Any] = {}

            if qr_token:
                # Re-authenticate to get a fresh API client
                session = async_create_clientsession(self.hass)
                api = HymerConnectApi(
                    session, brand=reconfigure_entry.data.get(CONF_BRAND, "hymer"),
                )
                try:
                    await api.authenticate(
                        reconfigure_entry.data[CONF_USERNAME],
                        reconfigure_entry.data[CONF_PASSWORD],
                    )
                    vehicle_info = await api.get_vehicle_by_token(qr_token)
                    vehicle_urn = vehicle_info.get("urn", "")
                    scu_urn = vehicle_info.get("smartUnitUrn", "")
                    if not scu_urn:
                        try:
                            scc_vehicles = await api.get_vehicles()
                            if scc_vehicles:
                                scu_urn = scc_vehicles[0].get("smartUnitUrn", "")
                        except HymerConnectApiError:
                            pass
                    if vehicle_urn:
                        data_updates[CONF_VEHICLE_URN] = vehicle_urn
                        data_updates[CONF_SCU_URN] = scu_urn
                        data_updates[CONF_QR_TOKEN] = qr_token
                        data_updates[CONF_BLE_ENABLED] = True
                    else:
                        errors["base"] = "invalid_qr_token"
                except HymerConnectApiError:
                    errors["base"] = "invalid_qr_token"

            if ble_address:
                data_updates[CONF_BLE_ADDRESS] = ble_address
                data_updates[CONF_BLE_ENABLED] = True

            if ehg_refresh_token:
                data_updates[CONF_EHG_REFRESH_TOKEN] = ehg_refresh_token

            if not errors and data_updates:
                # If BLE pairing data is present, trigger pairing step
                qr_for_pair = data_updates.get(CONF_QR_TOKEN) or reconfigure_entry.data.get(CONF_QR_TOKEN, "")
                ble_enabled = data_updates.get(CONF_BLE_ENABLED, reconfigure_entry.data.get(CONF_BLE_ENABLED, False))
                if qr_for_pair and ble_enabled and not data_updates.get(CONF_EHG_REFRESH_TOKEN):
                    # Merge updates into _data for the pairing task
                    self._data = {**reconfigure_entry.data, **data_updates}
                    self._data[CONF_QR_TOKEN] = qr_for_pair
                    # Re-authenticate API for the pairing task
                    session = async_create_clientsession(self.hass)
                    self._api = HymerConnectApi(
                        session, brand=self._data.get(CONF_BRAND, "hymer"),
                    )
                    try:
                        await self._api.authenticate(
                            self._data[CONF_USERNAME],
                            self._data[CONF_PASSWORD],
                        )
                    except Exception:
                        _LOGGER.warning("Re-auth failed for BLE pairing in reconfigure")
                    return await self.async_step_ble_pairing()

                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    data_updates=data_updates,
                )
            if not errors and not data_updates:
                # No new data entered — but if BLE is already enabled with
                # a stored QR token, allow re-triggering BLE pairing
                stored_qr = reconfigure_entry.data.get(CONF_QR_TOKEN, "")
                stored_ble = reconfigure_entry.data.get(CONF_BLE_ENABLED, False)
                stored_ehg = reconfigure_entry.data.get(CONF_EHG_REFRESH_TOKEN, "")
                if stored_qr and stored_ble and not stored_ehg:
                    self._data = {**reconfigure_entry.data}
                    session = async_create_clientsession(self.hass)
                    self._api = HymerConnectApi(
                        session, brand=self._data.get(CONF_BRAND, "hymer"),
                    )
                    try:
                        await self._api.authenticate(
                            self._data[CONF_USERNAME],
                            self._data[CONF_PASSWORD],
                        )
                    except Exception:
                        _LOGGER.warning("Re-auth failed for BLE pairing in reconfigure")
                    return await self.async_step_ble_pairing()
                errors["base"] = "no_changes"

        current_qr = ""
        current_ble = reconfigure_entry.data.get(CONF_BLE_ADDRESS, "")
        current_ehg = reconfigure_entry.data.get(CONF_EHG_REFRESH_TOKEN, "")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_QR_TOKEN, default=current_qr): str,
                    vol.Optional(CONF_BLE_ADDRESS, default=current_ble): str,
                    vol.Optional(CONF_EHG_REFRESH_TOKEN, default=current_ehg): str,
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
            CONF_BLE_ENABLED,
            self._config_entry.data.get(CONF_BLE_ENABLED, False),
        )
        current_ble_address = self._config_entry.options.get(
            CONF_BLE_ADDRESS,
            self._config_entry.data.get(CONF_BLE_ADDRESS, ""),
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
