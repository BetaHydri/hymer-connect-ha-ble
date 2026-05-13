"""Data update coordinator for HYMER Connect.

Uses the SCC REST API for vehicle metadata and SignalR for real-time sensor data.
The coordinator polls REST periodically and merges SignalR push data on arrival.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import HymerConnectApi, HymerConnectApiError, HymerConnectAuthError
from .const import (
    CONF_BLE_ADDRESS,
    CONF_BLE_ENABLED,
    CONF_EHG_REFRESH_TOKEN,
    CONF_QR_TOKEN,
    CONF_TANK_CAPACITY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TANK_CAPACITY_LITERS,
    DOMAIN,
)
from .signalr_client import HymerSignalRClient

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

_LOGGER = logging.getLogger(__name__)

# Reconnection backoff constants
_INITIAL_BACKOFF = 60  # 1 minute
_MAX_BACKOFF = 900  # 15 minutes
_MAX_CONSECUTIVE_FAILURES = 5  # force re-auth after this many failures
_REST_METADATA_INTERVAL = 600  # 10 minutes between full REST metadata refreshes
_RESUBSCRIBE_INTERVAL = 600  # 10 minutes — only resubscribe periodically, not every poll

# Fuel consumption tracking
_FUEL_REFUEL_THRESHOLD_PCT = 5  # fuel increase > 5% = refueling detected
_FUEL_MIN_DISTANCE_KM = 5  # minimum distance before computing consumption


class HymerConnectCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to manage fetching HYMER Connect data."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        api: HymerConnectApi,
        session: aiohttp.ClientSession,
        entry: ConfigEntry,
        vehicle_urn: str = "",
        scu_urn: str = "",
        ehg_refresh_token: str = "",
    ) -> None:
        """Initialize the coordinator."""
        self.api = api
        self._session = session
        self._vehicle_urn = vehicle_urn  # urn:ehg:vehicle:hy-XXXXXXXXXX
        self._scu_urn = scu_urn  # urn:ehg:scu:sXXX.XX.XX.XXX.XXX
        self._ehg_refresh_token = ehg_refresh_token  # BLE-derived refresh token
        self._signalr: HymerSignalRClient | None = None
        self._signalr_data: dict[str, Any] = {}
        self._reconnect_backoff: int = _INITIAL_BACKOFF
        self._last_reconnect_attempt: float = 0.0
        self._consecutive_failures: int = 0
        self._last_rest_metadata_refresh: float = 0.0
        self._last_resubscribe: float = 0.0
        self._cached_rest_data: dict[str, Any] = {}
        self._signalr_lock = asyncio.Lock()  # prevent concurrent reconnect attempts
        self._shutting_down = False  # suppress reconnects during HA shutdown/unload
        # BLE dual-path support (experimental)
        self._ble_client = None  # ScuBleClient instance when BLE is enabled
        self._ble_connected = False
        self._connection_mode = "cloud"  # "ble" or "cloud"
        self._ble_consecutive_failures = 0  # TLS timeout counter for backoff
        self._ble_next_attempt: float = 0.0  # monotonic time of next BLE attempt
        self._ble_pairing_in_progress = False  # set by config_flow during Step 3 BLE pairing
        self._ble_command_ack = asyncio.Event()  # set when BLE PIA response arrives after a command
        # Fuel consumption tracking — reference point for trip calculation
        self._fuel_ref_odo: float | None = None  # odometer at trip start (km)
        self._fuel_ref_level: float | None = None  # fuel level at trip start (%)
        self._fuel_consumption_l100: float | None = None  # current L/100km
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
            config_entry=entry,
        )

    @property
    def signalr_client(self) -> HymerSignalRClient | None:
        """Return the active SignalR client for sending commands."""
        return self._signalr

    @property
    def connection_mode(self) -> str:
        """Return the current connection mode: 'ble' or 'cloud'."""
        return self._connection_mode

    @property
    def ble_enabled(self) -> bool:
        """Return True if BLE direct path is enabled (options override data)."""
        return bool(
            self.config_entry.options.get(
                CONF_BLE_ENABLED,
                self.config_entry.data.get(CONF_BLE_ENABLED, False),
            )
        )

    @property
    def ble_address(self) -> str:
        """Return the configured SCU BLE address (options override data)."""
        return (
            self.config_entry.options.get(CONF_BLE_ADDRESS)
            or self.config_entry.data.get(CONF_BLE_ADDRESS, "")
        )

    def _ble_backoff_seconds(self) -> int:
        """Return backoff delay in seconds based on consecutive BLE failures.

        First 5 failures: no extra backoff (normal 60s poll interval suffices).
        This keeps the retry cadence fast enough to catch the SCU pairing window
        (~60-120s after pressing CONNECTION).
        After 5: escalate to 5min, 10min, max 15min.
        """
        n = self._ble_consecutive_failures
        if n <= 5:
            return 0  # rely on normal poll interval (60s)
        excess = n - 5
        return min(15 * 60, 5 * 60 * min(excess, 3))

    @property
    def tank_capacity(self) -> int:
        """Return the configured diesel tank capacity in liters."""
        return self.config_entry.options.get(
            CONF_TANK_CAPACITY, DEFAULT_TANK_CAPACITY_LITERS
        )

    def _on_signalr_update(self, sensor_data: dict[str, Any]) -> None:
        """Handle incoming SignalR sensor data."""
        self._signalr_data.update(sensor_data)
        self._compute_fuel_metrics()
        _LOGGER.debug(
            "SignalR push: %d total sensors", len(self._signalr_data)
        )
        # Trigger HA entity updates immediately
        self.async_set_updated_data({
            **(self.data or {}),
            "signalr_sensors": self._signalr_data,
        })

    def _compute_fuel_metrics(self) -> None:
        """Compute fuel consumption (L/100km) and range from odometer + fuel level.

        Uses a trip reference point (odo + fuel level at start).  When the fuel
        level increases by more than the refuel threshold, the reference resets
        (tank was filled up).  Consumption is only computed after at least
        _FUEL_MIN_DISTANCE_KM have been driven to avoid noisy readings.
        """
        odo = self._signalr_data.get("odometer")
        fuel_pct = self._signalr_data.get("fuel_level")

        if not isinstance(odo, (int, float)) or not isinstance(fuel_pct, (int, float)):
            return
        if odo <= 0 or fuel_pct < 0 or fuel_pct > 100:
            return

        tank_cap = self.tank_capacity
        fuel_liters = fuel_pct / 100.0 * tank_cap
        self._signalr_data["fuel_level_liters"] = round(fuel_liters, 1)

        # Initialize reference point on first valid reading
        if self._fuel_ref_odo is None or self._fuel_ref_level is None:
            self._fuel_ref_odo = odo
            self._fuel_ref_level = fuel_pct
            _LOGGER.debug("Fuel tracking initialized: odo=%.1f km, level=%.1f%%", odo, fuel_pct)
            return

        # Detect refueling: fuel level jumped up significantly
        if fuel_pct > self._fuel_ref_level + _FUEL_REFUEL_THRESHOLD_PCT:
            _LOGGER.info(
                "Refueling detected: %.1f%% → %.1f%% — resetting trip reference",
                self._fuel_ref_level, fuel_pct,
            )
            self._fuel_ref_odo = odo
            self._fuel_ref_level = fuel_pct
            return

        # Compute consumption when enough distance has been driven
        delta_km = odo - self._fuel_ref_odo
        delta_fuel_pct = self._fuel_ref_level - fuel_pct  # positive = fuel used

        if delta_km >= _FUEL_MIN_DISTANCE_KM and delta_fuel_pct > 0:
            fuel_used_liters = delta_fuel_pct / 100.0 * tank_cap
            consumption = fuel_used_liters / delta_km * 100.0
            # Sanity check: realistic diesel consumption is 5–40 L/100km for a Sprinter
            if 2.0 <= consumption <= 60.0:
                self._fuel_consumption_l100 = round(consumption, 1)
                self._signalr_data["fuel_consumption"] = self._fuel_consumption_l100
                _LOGGER.debug(
                    "Fuel consumption: %.1f L/100km (%.1f L over %.1f km)",
                    consumption, fuel_used_liters, delta_km,
                )

        # Compute estimated range
        if self._fuel_consumption_l100 and self._fuel_consumption_l100 > 0:
            est_range = fuel_liters / self._fuel_consumption_l100 * 100.0
            self._signalr_data["fuel_range_estimated"] = round(est_range, 0)


    def _on_signalr_connection_lost(self) -> None:
        """Handle SignalR connection loss — schedule immediate reconnect.

        Called from the listen loop's finally block when the WebSocket
        closes unexpectedly.  Resets backoff and triggers a coordinator
        refresh so `_async_update_data` reconnects within seconds instead
        of waiting for the next poll interval + exponential backoff.
        """
        if self._shutting_down:
            _LOGGER.debug("SignalR connection lost during shutdown — suppressing reconnect")
            return
        _LOGGER.info("SignalR connection lost — scheduling immediate reconnect")
        self._reconnect_backoff = _INITIAL_BACKOFF
        self._last_reconnect_attempt = 0.0
        # Schedule an async coordinator refresh — listen loop runs on the
        # HA event loop (asyncio.ensure_future), so async_create_task is safe.
        self.hass.async_create_task(self.async_request_refresh())

    async def start_signalr(self) -> None:
        """Start the SignalR WebSocket connection.

        Uses an asyncio.Lock to prevent the race condition where both
        the connection-lost callback and the coordinator poll trigger
        concurrent reconnects, creating duplicate WebSocket connections
        with double the traffic (which Azure then throttles/drops).
        """
        if not self._scu_urn:
            _LOGGER.warning("No SCU URN — skipping SignalR")
            return

        if self._signalr_lock.locked():
            _LOGGER.debug("SignalR reconnect already in progress — skipping")
            return

        async with self._signalr_lock:
            # Re-check after acquiring the lock — another task may have
            # reconnected while we were waiting.
            if self._signalr and self._signalr.connected and not self._signalr.needs_reconnect:
                _LOGGER.debug("SignalR already connected (checked under lock)")
                return

            # Stop any existing dead/stale connection first
            if self._signalr:
                _LOGGER.info("Stopping stale SignalR client before reconnect")
                await self.stop_signalr()

            self._signalr = HymerSignalRClient(
                api=self.api,
                session=self._session,
                vehicle_urn=self._vehicle_urn,
                scu_urn=self._scu_urn,
                ehg_refresh_token=self._ehg_refresh_token,
                on_sensor_update=self._on_signalr_update,
                on_connection_lost=self._on_signalr_connection_lost,
            )

            try:
                await self._signalr.start()
                _LOGGER.info("SignalR connected for %s", self._vehicle_urn)
                # Reset backoff, failure counter, and shutdown flag on success.
                # _shutting_down MUST be reset here — stop_signalr() sets it
                # to True, but after a successful reconnect the connection-lost
                # callback must be re-enabled for fast recovery.  See issue #47.
                self._reconnect_backoff = _INITIAL_BACKOFF
                self._consecutive_failures = 0
                self._shutting_down = False
            except HymerConnectApiError as err:
                self._consecutive_failures += 1
                _LOGGER.warning(
                    "SignalR connection failed (%d/%d): %s",
                    self._consecutive_failures, _MAX_CONSECUTIVE_FAILURES, err,
                )
                self._signalr = None

                # After too many consecutive failures, force OAuth2 token refresh
                if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    _LOGGER.warning(
                        "SignalR failed %d times in a row — forcing OAuth2 token refresh",
                        self._consecutive_failures,
                    )
                    try:
                        await self.api._refresh_access_token()
                        _LOGGER.info("OAuth2 token refreshed after consecutive failures")
                    except Exception:
                        _LOGGER.error("OAuth2 token refresh failed", exc_info=True)
                    self._consecutive_failures = 0
                    self._reconnect_backoff = _INITIAL_BACKOFF
                else:
                    # Increase backoff (exponential, capped)
                    self._reconnect_backoff = min(
                        self._reconnect_backoff * 2, _MAX_BACKOFF
                    )
                _LOGGER.info(
                    "Next SignalR reconnect attempt in %ds", self._reconnect_backoff
                )

    async def stop_signalr(self) -> None:
        """Stop the SignalR WebSocket connection."""
        self._shutting_down = True
        if self._signalr:
            await self._signalr.stop()
            self._signalr = None

    async def force_reauth_and_reconnect(self) -> None:
        """Force a full OAuth2 re-authentication and SignalR reconnect.

        This mirrors what happens during an integration reload: a fresh
        password-grant authentication creates a new server-side session
        with clean hub→SCU routing.  A simple SignalR negotiate reuses
        the existing OAuth2 session which may have stale routing.

        Called by switch._verify_send when a command fails after the
        normal reconnect+retry cycle.
        """
        _LOGGER.info(
            "Forcing full OAuth2 re-authentication before SignalR reconnect"
        )

        # Stop the existing dead connection first
        if self._signalr:
            await self.stop_signalr()

        try:
            username = self.config_entry.data.get(CONF_USERNAME, "")
            password = self.config_entry.data.get(CONF_PASSWORD, "")
            if username and password:
                await self.api.authenticate(username, password)
                _LOGGER.info("OAuth2 re-authentication successful")
            else:
                # Fallback: at least refresh the token
                await self.api._refresh_access_token()
                _LOGGER.info("OAuth2 token refresh successful (no stored credentials)")
        except Exception:
            _LOGGER.warning("OAuth2 re-authentication failed", exc_info=True)

        # Now reconnect SignalR with the fresh session
        self._shutting_down = False  # re-enable connection-lost callbacks
        await self.start_signalr()

    async def start_ble(self) -> bool:
        """Attempt to connect to SCU via BLE direct path.

        Returns True if BLE connection + TLS handshake succeeded.
        Falls back gracefully — caller should use cloud SignalR if this fails.
        """
        if not self.ble_enabled:
            return False

        ble_address = self.ble_address
        if not ble_address:
            _LOGGER.info("BLE enabled but no SCU address configured — attempting scan")
            try:
                from .ble_client import ScuBleClient
                scanner = ScuBleClient(scu_address="")
                devices = await scanner.scan_for_scu(timeout=10.0, hass=self.hass)
                if devices:
                    ble_address = devices[0]["address"]
                    scu_name = devices[0].get("name", "")
                    _LOGGER.info("BLE scan found SCU: %s (%s)", scu_name, ble_address)
                    # Persist discovered address so it's available for Reconfigure
                    # and survives restarts without re-scanning
                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        data={
                            **self.config_entry.data,
                            CONF_BLE_ADDRESS: ble_address,
                        },
                    )
                    _LOGGER.info(
                        "SCU BLE address %s stored in config entry", ble_address
                    )
                else:
                    _LOGGER.warning("BLE scan found no SCU devices")
                    return False
            except Exception:
                _LOGGER.warning("BLE scan failed", exc_info=True)
                return False

        try:
            from .ble_client import ScuBleClient, BleTransportError

            self._ble_client = ScuBleClient(
                scu_address=ble_address,
                on_pia_response=self._on_ble_pia_response,
            )
            await self._ble_client.connect()
            await self._ble_client.establish_tls()

            # If no EHG refresh token yet, attempt BLE pairing to obtain one
            if not self._ehg_refresh_token:
                qr_token = self.config_entry.data.get(CONF_QR_TOKEN, "")
                if qr_token:
                    self._ble_pairing_in_progress = True  # prevent concurrent BLE attempts
                    _LOGGER.warning(
                        "No EHG refresh token — attempting BLE pairing with SCU %s. "
                        "IMPORTANT: Press the CONNECTION button on the SCU "
                        "control panel in the vehicle now. "
                        "Waiting up to 60 seconds...",
                        ble_address,
                    )
                    try:
                        confirmation = await self.api.get_confirmation_token()
                        confirmation_token = confirmation.get("token", "")
                        if not confirmation_token:
                            _LOGGER.warning("Cloud did not return a confirmation token")
                        else:
                            pair_result = await self._ble_client.pair_mobile(
                                activation_token=qr_token,
                                confirmation_token=confirmation_token,
                                mobile_device_name=f"ha-{int(time.time()) % 100000}",
                            )
                            if pair_result.remote_access_refresh_token:
                                self._ehg_refresh_token = pair_result.remote_access_refresh_token
                                # Persist to config entry so it survives restarts
                                self.hass.config_entries.async_update_entry(
                                    self.config_entry,
                                    data={
                                        **self.config_entry.data,
                                        CONF_EHG_REFRESH_TOKEN: self._ehg_refresh_token,
                                    },
                                )
                                _LOGGER.info(
                                    "BLE pairing successful — EHG refresh token obtained and stored"
                                )
                            else:
                                _LOGGER.warning(
                                    "BLE pairing completed but no refresh token was returned"
                                )
                    except Exception as pair_err:
                        _LOGGER.warning("BLE pairing failed: %s", pair_err)
                    finally:
                        self._ble_pairing_in_progress = False

            self._ble_connected = True
            self._connection_mode = "dual" if (self._signalr and self._signalr.connected) else "ble"
            _LOGGER.info("BLE direct path established to SCU %s (mode=%s)", ble_address, self._connection_mode)

            # Send PIA subscription requests over BLE to unlock all sensor
            # groups — same subscriptions SignalR sends.  Without these, the
            # SCU only pushes ~28 sensors autonomously.  With subscriptions,
            # all ~130 sensors should stream over BLE.
            try:
                from .pia_decoder import build_subscription_requests, build_refresh_command
                requests = build_subscription_requests()
                _LOGGER.info(
                    "Sending %d PIA subscription requests over BLE", len(requests)
                )
                for payload in requests:
                    await self._ble_client.send_pia_command(payload)
                # Send refresh to force SCU to push current states
                refresh = build_refresh_command()
                await self._ble_client.send_pia_command(refresh)
                _LOGGER.info("BLE PIA subscriptions + refresh sent")
            except Exception:
                _LOGGER.warning(
                    "BLE PIA subscription failed — SCU will only push "
                    "autonomous sensors (~28). SignalR provides full coverage.",
                    exc_info=True,
                )

            # Start BLE listen loop in background
            self.hass.async_create_task(self._ble_listen_loop())
            return True
        except Exception as err:
            _LOGGER.warning("BLE connection failed, will use cloud: %s", err)
            # Disconnect the BLE client to release GATT resources (notifications, etc.)
            if self._ble_client:
                try:
                    await self._ble_client.disconnect()
                except Exception:
                    pass
            self._ble_client = None
            self._ble_connected = False
            self._connection_mode = "cloud"
            # Only clear the stored BLE address on connection-level failures
            # (timeout, device not found). Don't clear on bonding rejection —
            # the SCU address is still valid, bonding just needs CONNECTION.
            err_str = str(err)
            is_bonding_rejection = (
                "AuthenticationFailed" in err_str
                or "AuthenticationCanceled" in err_str
                or "AuthenticationRejected" in err_str
                or "CONNECTION" in err_str
                or "Stale bond" in err_str
                or "0x0e" in err_str
            )
            if ble_address and self.config_entry.data.get(CONF_BLE_ADDRESS) and not is_bonding_rejection:
                _LOGGER.info(
                    "Clearing stored BLE address %s — will re-scan on next attempt "
                    "(SCU may have changed its random BLE address after reboot)",
                    ble_address,
                )
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={
                        **self.config_entry.data,
                        CONF_BLE_ADDRESS: "",
                    },
                )
            return False

    def _on_ble_pia_response(self, b64_payload: str) -> None:
        """Handle PIA response received via BLE — same decoder as SignalR."""
        from .pia_decoder import decode_pia_payload
        sensor_data = decode_pia_payload(b64_payload)
        if sensor_data:
            self._on_signalr_update(sensor_data)
            # Signal that a BLE response arrived (for command ACK tracking)
            if not self._ble_command_ack.is_set():
                _LOGGER.debug(
                    "BLE ACK received (%d fields: %s)",
                    len(sensor_data), list(sensor_data.keys())[:5],
                )
            self._ble_command_ack.set()

    async def _ble_listen_loop(self) -> None:
        """Background loop receiving PIA data from SCU via BLE."""
        if not self._ble_client:
            return
        try:
            await self._ble_client.listen()
        except Exception:
            _LOGGER.warning("BLE listen loop ended", exc_info=True)
        finally:
            # Always clean up BLE client state so the next poll cycle
            # creates a fresh BleakClient with valid GATT services.
            # Without this, the stale client's empty services table
            # causes "Service Discovery has not been performed yet".
            if self._ble_client:
                try:
                    await self._ble_client.disconnect()
                except Exception:
                    pass
                self._ble_client = None
            self._ble_connected = False
            self._connection_mode = "cloud"
            _LOGGER.info(
                "BLE disconnected — SignalR continues providing sensor data. "
                "BLE will be retried on next poll cycle."
            )

    async def stop_ble(self) -> None:
        """Disconnect the BLE client."""
        if self._ble_client:
            await self._ble_client.disconnect()
            self._ble_client = None
        self._ble_connected = False
        if self._connection_mode == "ble":
            self._connection_mode = "cloud"

    async def async_ensure_signalr_healthy(self) -> None:
        """Ensure SignalR is connected and healthy, reconnecting if needed.

        Checks both the WebSocket connection state and whether the connection
        is stale (needs_reconnect).  If unhealthy, attempts to reconnect.
        Raises HomeAssistantError if reconnection fails.
        """
        client = self._signalr
        if client and client.connected and not client.needs_reconnect:
            return
        reason = "stale" if (client and client.connected) else "disconnected"
        _LOGGER.info("SignalR %s — reconnecting before command", reason)
        await self.start_signalr()
        if not self._signalr or not self._signalr.connected:
            raise HomeAssistantError(
                "Cannot send command — SignalR not connected. "
                "Try reloading the integration."
            )

    async def _send_via_ble(self, b64_payload: str) -> bool:
        """Send a base64-encoded PIA command over the BLE direct path.

        Returns True if sent successfully, False on failure.
        """
        if not self._ble_client or not self._ble_connected:
            return False
        try:
            await self._ble_client.send_pia_command(b64_payload)
            _LOGGER.info("BLE command sent (%d chars payload)", len(b64_payload))
            return True
        except Exception:
            _LOGGER.warning("BLE command GATT write failed", exc_info=True)
            return False

    async def _send_with_retry(
        self, method_name: str, *args: Any, **kwargs: Any
    ) -> None:
        """Send a command with BLE-first routing and cloud fallback.

        When BLE is connected, builds the PIA payload and sends it over
        the BLE direct path (~50ms latency).  Then waits up to 2 seconds
        for the SCU to echo back a PIA response (ACK).  If no response
        arrives within the timeout, re-sends the same command via the
        cloud/SignalR path as a safety net.  Commands are idempotent
        (set-value, not toggle), so a duplicate is harmless.

        If BLE is not connected, sends directly via cloud.

        Args:
            method_name: Name of the method on HymerSignalRClient
                         (e.g. 'send_light_command').
            *args, **kwargs: Forwarded to the client method.

        Raises HomeAssistantError if all attempts fail.
        """
        # --- BLE path: try first when connected ---
        if self._ble_connected and self._ble_client:
            from .pia_decoder import build_light_command, build_multi_sensor_command
            b64_payload: str | None = None
            cmd_detail = method_name  # human-readable command description
            if method_name == "send_light_command":
                b64_payload = build_light_command(*args, **kwargs)
                bus_id, sensor_id = args[0], args[1]
                cmd_detail = f"set_value bus={bus_id} sid={sensor_id} {kwargs}"
            elif method_name == "send_multi_sensor_command":
                b64_payload = build_multi_sensor_command(*args)
                slots = [(s.get('bus_id'), s.get('sensor_id')) for s in args[0]]
                cmd_detail = f"multi_sensor {slots}"
            elif method_name == "send_pia_request":
                b64_payload = args[0] if args else None
                cmd_detail = f"pia_request ({len(b64_payload) if b64_payload else 0} chars)"

            if b64_payload:
                _LOGGER.info("BLE command routing: %s", cmd_detail)
                # Clear the ACK event before sending so we only detect
                # responses that arrive AFTER our command.
                self._ble_command_ack.clear()
                ok = await self._send_via_ble(b64_payload)
                if ok:
                    # Wait up to 500ms for the SCU to echo back a PIA response.
                    # BLE round-trip is ~50-200ms, so 500ms is 2.5-10x margin.
                    try:
                        await asyncio.wait_for(
                            self._ble_command_ack.wait(), timeout=0.5
                        )
                        _LOGGER.info(
                            "BLE ACK confirmed: %s", cmd_detail
                        )
                        return
                    except asyncio.TimeoutError:
                        _LOGGER.warning(
                            "BLE ACK timeout (500ms): %s "
                            "— re-sending via cloud as safety net",
                            cmd_detail,
                        )
                else:
                    _LOGGER.warning(
                        "BLE send failed: %s — falling back to cloud",
                        cmd_detail,
                    )
        elif self._ble_connected:
            _LOGGER.debug(
                "BLE connected but no client — routing %s via cloud",
                method_name,
            )
        else:
            _LOGGER.debug(
                "BLE not connected — routing %s via cloud", method_name
            )

        # --- Cloud/SignalR path: fallback with reconnect + retry ---
        for attempt in range(2):
            await self.async_ensure_signalr_healthy()
            method = getattr(self._signalr, method_name)
            ok = await method(*args, **kwargs)
            if ok:
                _LOGGER.info(
                    "Cloud command sent (attempt %d/2, %s, ble_connected=%s)",
                    attempt + 1, method_name, self._ble_connected,
                )
                return
            if attempt == 0:
                _LOGGER.warning(
                    "Cloud send failed (attempt 1/2): %s — reconnecting for retry",
                    method_name,
                )
                # Force disconnected state so ensure_healthy reconnects
                if self._signalr:
                    self._signalr._connected = False
        raise HomeAssistantError(
            "Command failed after reconnect+retry. "
            "Try reloading the integration."
        )

    async def async_send_light_command(
        self,
        bus_id: int,
        sensor_id: int,
        **kwargs: Any,
    ) -> None:
        """Send a light/switch command via BLE (preferred) or cloud."""
        await self._send_with_retry(
            "send_light_command", bus_id, sensor_id, **kwargs
        )

    async def async_send_multi_sensor_command(
        self, sensors: list[dict]
    ) -> None:
        """Send a multi-sensor command via BLE (preferred) or cloud."""
        await self._send_with_retry("send_multi_sensor_command", sensors)

    async def async_send_pia_request(
        self, payload: str
    ) -> None:
        """Send a raw PIA request via BLE (preferred) or cloud."""
        await self._send_with_retry("send_pia_request", payload)

    async def async_send_restart_system_command(self) -> None:
        """Send an SCU restart command via PIA."""
        from .pia_decoder import build_restart_system_request
        payload = build_restart_system_request(cold=True)
        await self.async_send_pia_request(payload)
        _LOGGER.warning("SCU restart command sent — the SCU will reboot")

    async def async_ble_field_scan(self) -> None:
        """Connect via BLE/TLS and brute-force UserRequestTopic field numbers 1-15.

        Retries bonding up to 8 times (~64s) waiting for CONNECTION button press.
        Results are logged at WARNING level.
        """
        from .ble_client import ScuBleClient, BleTransportError

        ble_address = self.ble_address
        if not ble_address:
            _LOGGER.warning("BLE field scan: no BLE address configured")
            return

        _LOGGER.warning(
            "BLE field scan: starting — press CONNECTION on the SCU touch panel! "
            "Connecting to %s (will retry bonding for ~64s)",
            ble_address,
        )
        self._ble_pairing_in_progress = True
        try:
            client = None
            max_attempts = 8
            retry_delay = 8

            for attempt in range(1, max_attempts + 1):
                remaining = (max_attempts - attempt + 1) * retry_delay
                _LOGGER.warning(
                    "BLE field scan: bonding attempt %d/%d (%ds remaining) — "
                    "press CONNECTION if not already pressed",
                    attempt, max_attempts, remaining,
                )
                try:
                    client = ScuBleClient(
                        scu_address=ble_address,
                        connect_timeout=15.0,
                        tls_timeout=30.0,
                    )
                    await client.connect()
                    _LOGGER.warning(
                        "BLE field scan: bonding SUCCESSFUL on attempt %d/%d",
                        attempt, max_attempts,
                    )
                    break
                except BleTransportError as bond_err:
                    if "CONNECTION" in str(bond_err) or "Authentication" in str(bond_err):
                        _LOGGER.warning(
                            "BLE field scan: bonding attempt %d/%d failed — "
                            "CONNECTION not pressed yet. Retrying in %ds...",
                            attempt, max_attempts, retry_delay,
                        )
                        if client:
                            try:
                                await client.disconnect()
                            except Exception:
                                pass
                            client = None
                        if attempt < max_attempts:
                            await asyncio.sleep(retry_delay)
                            continue
                        _LOGGER.warning("BLE field scan: bonding failed after %d attempts", max_attempts)
                        return
                    raise
                except Exception as err:
                    _LOGGER.warning("BLE field scan: connect attempt %d failed: %s", attempt, err)
                    if client:
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
                        client = None
                    if attempt < max_attempts:
                        await asyncio.sleep(retry_delay)
                        continue
                    _LOGGER.warning("BLE field scan: connect failed after %d attempts", max_attempts)
                    return

            if not client or not client.ble_connected:
                _LOGGER.warning("BLE field scan: could not connect")
                return

            _LOGGER.info("BLE field scan: connected, establishing TLS")
            await client.establish_tls()
            _LOGGER.info("BLE field scan: TLS established, scanning fields 1-15")

            results = await client.brute_force_user_fields(
                field_range=range(1, 16),
                timeout_per_field=8.0,
            )

            _LOGGER.warning(
                "BLE field scan COMPLETE — %d fields responded: %s",
                len(results),
                {fn: r.get("status") for fn, r in results.items()},
            )
            for fn, r in sorted(results.items()):
                _LOGGER.warning(
                    "BLE field scan result: field=%d status=%s fields=%s",
                    fn, r.get("status"), r.get("fields"),
                )

            await client.disconnect()
        except BleTransportError as err:
            _LOGGER.warning("BLE field scan failed: %s", err)
        except Exception as err:
            _LOGGER.warning("BLE field scan unexpected error: %s", err)
        finally:
            self._ble_pairing_in_progress = False

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the REST API and merge with SignalR data."""
        now = time.monotonic()

        # Only refresh full REST metadata periodically (URNs, VIN, model are static)
        needs_metadata_refresh = (
            not self._cached_rest_data
            or (now - self._last_rest_metadata_refresh) > _REST_METADATA_INTERVAL
        )

        if needs_metadata_refresh:
            try:
                rest_data = await self.api.get_vehicle_status()
                self._cached_rest_data = rest_data
                self._last_rest_metadata_refresh = now
            except HymerConnectAuthError as err:
                raise ConfigEntryAuthFailed(
                    f"Authentication error: {err}"
                ) from err
            except HymerConnectApiError as err:
                raise UpdateFailed(
                    f"Error communicating with API: {err}"
                ) from err
        else:
            rest_data = dict(self._cached_rest_data)

        # Store URNs from REST data if not set yet
        if not self._scu_urn and rest_data.get("vehicle"):
            vehicle = rest_data["vehicle"]
            self._scu_urn = vehicle.get("smartUnitUrn", "")
            vin = vehicle.get("vin", "")
            _LOGGER.info("Discovered VIN=%s SCU=%s", vin, self._scu_urn)

        # Get vehicle URN (urn:ehg:vehicle:hy-...) from EHG API
        if not self._vehicle_urn and rest_data.get("vehicle_urn"):
            self._vehicle_urn = rest_data["vehicle_urn"]
            _LOGGER.info(
                "Discovered vehicle_urn=%s, scu_urn=%s",
                self._vehicle_urn,
                self._scu_urn,
            )

            # Persist URNs in config entry for next restart
            if self._vehicle_urn and self._scu_urn:
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={
                        **self.config_entry.data,
                        "vehicle_urn": self._vehicle_urn,
                        "scu_urn": self._scu_urn,
                    },
                )

        if not self._scu_urn and self._vehicle_urn:
            self._scu_urn = self._vehicle_urn

        # --- Connection management: try BLE first, fall back to SignalR ---

        # Attempt BLE direct path if enabled and not already connected.
        # After consecutive TLS failures (SCU ignores ClientHello without
        # bonding/CONNECTION), back off to avoid wasting ~20s per poll.
        # Keep first 5 attempts at normal poll interval (60s) so we don't
        # miss the SCU pairing window (~60-120s after pressing CONNECTION).
        # After 5 failures, escalate: 5min, 10min, max 15min.
        #
        # Skip BLE if the config flow pairing task is actively running —
        # a concurrent connect/pair would race and sabotage the pairing.
        if self.ble_enabled and not self._ble_connected:
            if self._ble_pairing_in_progress:
                _LOGGER.debug("BLE attempt skipped — config flow pairing in progress")
            elif time.monotonic() < self._ble_next_attempt:
                remaining = int(self._ble_next_attempt - time.monotonic())
                _LOGGER.debug(
                    "BLE attempt deferred — %d consecutive failures, "
                    "next attempt in %ds",
                    self._ble_consecutive_failures, remaining,
                )
            else:
                try:
                    ble_ok = await self.start_ble()
                    if ble_ok:
                        self._ble_consecutive_failures = 0
                        self._ble_next_attempt = 0.0
                        _LOGGER.info(
                            "BLE direct path active — running alongside SignalR "
                            "(BLE: ~28 sensors at ~50ms, SignalR: ~130 sensors)"
                        )
                    else:
                        self._ble_consecutive_failures += 1
                        backoff = self._ble_backoff_seconds()
                        self._ble_next_attempt = time.monotonic() + backoff
                        _LOGGER.info(
                            "BLE failed %d times — next attempt in %ds",
                            self._ble_consecutive_failures, backoff,
                        )
                except Exception:
                    self._ble_consecutive_failures += 1
                    backoff = self._ble_backoff_seconds()
                    self._ble_next_attempt = time.monotonic() + backoff
                    _LOGGER.debug(
                        "BLE connection attempt failed (attempt %d, next in %ds)",
                        self._ble_consecutive_failures, backoff,
                        exc_info=True,
                    )

        # --- SignalR connection management (always active) ---
        # SignalR runs alongside BLE — BLE provides ~28 sensors at ~50ms,
        # SignalR provides the full ~130 sensors. Both feed into the same
        # _signalr_data dict. Commands route BLE-first (no duplicates).
        signalr_connected = (
            self._signalr is not None and self._signalr.connected
        )
        needs_reconnect = (
            self._signalr is not None and self._signalr.needs_reconnect
        )

        if not signalr_connected or needs_reconnect:
            # Apply exponential backoff between reconnection attempts
            since_last_attempt = now - self._last_reconnect_attempt
            if since_last_attempt >= self._reconnect_backoff:
                _LOGGER.info(
                    "SignalR %s (obj=%s, connected=%s, needs_reconnect=%s), attempting start",
                    "needs reconnect" if needs_reconnect else "not connected",
                    self._signalr is not None,
                    self._signalr.connected if self._signalr else "N/A",
                    needs_reconnect,
                )
                self._last_reconnect_attempt = now
                try:
                    await self.start_signalr()
                except Exception:
                    _LOGGER.warning("SignalR connect attempt failed", exc_info=True)
            else:
                remaining = self._reconnect_backoff - since_last_attempt
                _LOGGER.warning(
                    "SignalR reconnect backoff: %.0fs remaining (attempt %d/%d)",
                    remaining, self._consecutive_failures, _MAX_CONSECUTIVE_FAILURES,
                )
        else:
            # Send lightweight refresh every poll to keep SCU data flowing.
            # The SCU stops pushing data after ~2-3 min of silence.
            # This sends 1 message per poll (vs 8 for full resubscribe).
            try:
                await self._signalr.send_refresh()
            except Exception:
                _LOGGER.debug("PIA refresh failed", exc_info=True)

            # Full resubscribe less frequently — reinitialises all sensor
            # groups to pick up any missed subscriptions after reconnects.
            since_last_resub = now - self._last_resubscribe
            if since_last_resub >= _RESUBSCRIBE_INTERVAL:
                try:
                    await self._signalr.resubscribe()
                    self._last_resubscribe = now
                    _LOGGER.debug(
                        "Full PIA re-subscription sent (interval=%.0fs)",
                        since_last_resub,
                    )
                except Exception:
                    _LOGGER.debug("PIA re-subscription failed", exc_info=True)

        # Merge REST + SignalR data
        signalr_ok = self._signalr.connected if self._signalr else False
        _LOGGER.debug(
            "Data update: signalr_sensors=%d, signalr_connected=%s",
            len(self._signalr_data),
            signalr_ok,
        )
        rest_data["signalr_sensors"] = self._signalr_data
        return rest_data
