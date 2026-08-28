"""Data update coordinator for HYMER Connect.

Uses the SCC REST API for vehicle metadata and SignalR for real-time sensor data.
The coordinator polls REST periodically and merges SignalR push data on arrival.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
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
    CONF_BLE_WRITE_ENABLED,
    CONF_EHG_REFRESH_TOKEN,
    CONF_QR_TOKEN,
    CONF_TANK_CAPACITY,
    DEFAULT_BLE_WRITE_ACK_TIMEOUT,
    DEFAULT_BLE_WRITE_ENABLED,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TANK_CAPACITY_LITERS,
    DOMAIN,
    UNAVAILABLE_SILENCE_BLE,
    UNAVAILABLE_SILENCE_CLOUD,
)
from .signalr_client import HymerSignalRClient

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

_LOGGER = logging.getLogger(__name__)

# Reconnection backoff constants
_INITIAL_BACKOFF = 60  # 1 minute
_MAX_BACKOFF = 900  # 15 minutes
_MAX_CONSECUTIVE_FAILURES = 5  # force re-auth after this many failures
_RAPID_DROP_THRESHOLD = 30  # seconds — connection shorter than this = rapid drop
_RAPID_DROP_COOLDOWN = 5  # seconds to wait before reconnecting after a rapid drop
_REST_METADATA_INTERVAL = 600  # 10 minutes between full REST metadata refreshes
_RESUBSCRIBE_INTERVAL = 600  # 10 minutes — only resubscribe periodically, not every poll
_BLE_STARTUP_TIMEOUT = 90  # hard cap so BLE/BlueZ cannot block HA startup
_BLE_CLEANUP_TIMEOUT = 5  # best-effort disconnect cap after failed startup
# #23: cold-start cloud-first gate. On some SCUs (Schaudt EBL 400 / PIA rc.2) the
# habitation control slots arrive ONLY over the cloud, and an active BLE session makes
# the SCU withhold them from the cloud channel. Defer the FIRST BLE attempt at cold
# start until the cloud snapshot has landed (or a grace cap), so those slots reach the
# monotonic union before BLE claims the link. Non-retrofit fleet SCUs deliver the full
# set over BLE anyway, so this only shifts their BLE connect a few seconds later with
# no data lost. Latches open after the first pass; reconnects/toggles are never gated.
# Two decoupled timeouts: when the cloud IS connecting we wait for its snapshot to
# settle; when the cloud is absent (off-grid / no LTE) we release BLE quickly so the
# fleet's BLE path is not delayed at an off-grid restart. In the cloud-connected case
# we open on PLATEAU (the merged set has stopped growing) rather than a fixed delay,
# so slow-delivering boots still get the one-shot habitation slots into the union
# before BLE claims the link (#23 run-1 race).
_BLE_COLD_START_SETTLE_MIN = 8  # cloud connected: minimum wait after connect before considering the set settled
_BLE_COLD_START_PLATEAU = 8  # cloud connected: open once the merged set hasn't grown for this long
_BLE_COLD_START_SETTLE_MAX = 45  # cloud connected: hard cap since connect if the set never plateaus
_BLE_COLD_START_NO_CLOUD = 20  # cloud absent: release BLE this soon rather than wait for a cloud that isn't coming
_SCU_FROZEN_TIMEOUT = 900  # SCU clock (scu_internal_time) unchanged this long while still connected + data flowing = hung SCU (physical power-cycle required)
_SCU_FROZEN_DATA_WINDOW = 180  # only judge frozen while frames still arrive; beyond this it's standby/12V-off, not a hung-but-connected SCU
_SCU_FROZEN_WALLCLOCK_LAG = 900  # SCU's own UTC clock this far behind real time = hung (instant, survives restarts)
_BLE_RX_LIVENESS_TIMEOUT = 60  # #24: BLE claims connected but no BLE frame for this long (while other data flows) = silently dead link
_BLE_REBOND_BURST_SECONDS = 120  # active re-bond burst after a manual bond reset — matches the SCU pairing window
_BLE_REBOND_BURST_INTERVAL = 8  # seconds between attempts during the active re-bond burst

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
        # #23: persist the merged sensor set at entry scope so it survives a
        # coordinator rebuild (config-entry re-setup) mid-startup. A fresh
        # coordinator re-seeds this accumulated union instead of starting empty,
        # so BLE-only one-shot habitation slots are never lost when the cloud
        # connects. Only ever grows via .update(); cleared on entry removal.
        self._signalr_data: dict[str, Any] = hass.data.setdefault(DOMAIN, {}).setdefault(
            f"{entry.entry_id}_sensor_union", {}
        )
        self._reconnect_backoff: int = _INITIAL_BACKOFF
        self._last_reconnect_attempt: float = 0.0
        self._consecutive_failures: int = 0
        self._last_rest_metadata_refresh: float = 0.0
        self._last_resubscribe: float = 0.0
        self._cached_rest_data: dict[str, Any] = {}
        self._signalr_lock = asyncio.Lock()  # prevent concurrent reconnect attempts
        self._shutting_down = False  # suppress reconnects during HA shutdown/unload
        self._signalr_connected_at: float = 0.0  # monotonic time of last successful connect
        self._last_data_monotonic: float = 0.0  # last data merged from ANY transport (SignalR OR BLE)
        # BLE dual-path support (experimental)
        self._ble_client = None  # ScuBleClient instance when BLE is enabled
        self._ble_connected = False
        self._connection_mode = "cloud"  # "ble" or "cloud"
        self._ble_consecutive_failures = 0  # TLS timeout counter for backoff
        self._ble_next_attempt: float = 0.0  # monotonic time of next BLE attempt
        self._ble_pairing_in_progress = False  # set by config_flow during Step 3 BLE pairing
        self._ble_command_ack = asyncio.Event()  # set when BLE PIA response arrives after a command
        self._ble_pending_cmd_key: str | None = None  # expected sensor name for ACK matching
        self._ble_listen_task: asyncio.Task | None = None  # background NUS listen loop; cancelled on teardown
        self._ble_connecting = False  # re-entrancy guard: poll + watchdog + option-toggle may all call connect
        self._ble_rebond_task: asyncio.Task | None = None  # active re-bond burst after a manual bond reset
        self._ble_rebond_pending = False  # set by the options flow when the user resets the BLE bond
        self._ble_reset_pending = False  # #19: clear the bond inside the guarded connect (never racing the watchdog)
        self._startup_monotonic: float = time.monotonic()  # #23 cold-start cloud-first gate reference
        self._ble_cold_start_gate_open = False  # #23 latches True once cloud seeded or grace elapsed
        self._signalr_last_growth_monotonic: float = 0.0  # #23 last time the merged set gained a new key
        self._ble_write_degraded = False  # #24 True when BLE is up but the write/notify channel is a stale BlueZ acquisition
        self._ble_degraded_reason: str | None = None  # #24 human-readable reason for the degraded state
        self._ble_last_rx_monotonic: float = 0.0  # #24 last time a BLE frame arrived (liveness; is_connected is not trustworthy)
        self._scu_clock_value: Any = None  # last observed scu_internal_time value (frozen-SCU detection)
        self._scu_clock_last_change_monotonic: float = 0.0  # when the SCU clock last advanced
        # Fuel consumption tracking — reference point for trip calculation
        self._fuel_ref_odo: float | None = None  # odometer at trip start (km)
        self._fuel_ref_level: float | None = None  # fuel level at trip start (%)
        self._fuel_consumption_l100: float | None = None  # current L/100km
        # Paired BLE devices (getPairedMobileDevices) — populated on demand.
        self._paired_ble_devices: list[dict] = []
        self._paired_ble_devices_updated: str | None = None
        self._paired_ble_user_uuid: str | None = None
        self._unpair_selected_mac: str | None = None  # target chosen in the select
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
    def paired_ble_devices(self) -> list[dict]:
        """Last-fetched paired mobile devices [{name, mac, uuid}]. Empty until fetched."""
        return self._paired_ble_devices

    @property
    def paired_ble_devices_updated(self) -> str | None:
        """ISO timestamp of the last successful paired-devices fetch, or None."""
        return self._paired_ble_devices_updated

    @property
    def unpair_selected_mac(self) -> str | None:
        """MAC currently selected for unpairing (set by the select entity)."""
        return self._unpair_selected_mac

    def set_unpair_selected_mac(self, mac: str | None) -> None:
        """Record the device MAC chosen in the unpair select entity."""
        self._unpair_selected_mac = mac.strip().lower() if mac else None

    @property
    def unavailable_silence_threshold(self) -> int:
        """Data-silence seconds before 12V-gated entities go unavailable.

        BLE-only mode streams every ~200-300ms, so 15s of silence conclusively
        means 12V off. When the cloud is involved (cloud/dual) its ~30-40s push
        cadence sets the floor for ``data_silence_seconds``, so use 60s to avoid
        false flicker while the link is healthy.
        """
        if self._connection_mode == "ble":
            return UNAVAILABLE_SILENCE_BLE
        return UNAVAILABLE_SILENCE_CLOUD

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
        address = (
            self.config_entry.options.get(CONF_BLE_ADDRESS)
            or self.config_entry.data.get(CONF_BLE_ADDRESS, "")
        )
        return address.upper() if address else ""

    @property
    def ble_write_enabled(self) -> bool:
        """Return True if the BLE write path is active (default on).

        When on, writes are attempted over BLE first (field-1
        BleProtocol.request + write-with-response) and fall back to cloud on any
        failure/non-ACK. BLE writes only fire when BLE is actually connected, so
        with BLE down this is a no-op and everything goes cloud-only. Untick the
        option to force cloud-only even when BLE is connected.
        """
        return bool(
            self.config_entry.options.get(
                CONF_BLE_WRITE_ENABLED,
                self.config_entry.data.get(
                    CONF_BLE_WRITE_ENABLED, DEFAULT_BLE_WRITE_ENABLED
                ),
            )
        )

    @property
    def ble_write_degraded(self) -> bool:
        """Return True when BLE is up but its write/notify channel is dead (#24).

        Set when a stale BlueZ ``Write acquired`` acquisition survives a fresh
        GATT session (MTU pinned at 23, writes/TLS fail). The cloud path keeps
        working; recovery needs a host-side ``systemctl restart bluetooth`` or a
        reboot. Exposed as a diagnostic binary_sensor.
        """
        return self._ble_write_degraded

    def _ble_backoff_seconds(
        self, *, bonding_rejected: bool = False, stale_channel: bool = False
    ) -> int:
        """Return backoff delay in seconds based on consecutive BLE failures.

        Bonding rejections (AuthenticationFailed) mean CONNECTION hasn't been
        pressed — no point retrying every 60s.  Use 2-minute minimum, escalating
        to 5 min max.  This cuts wasted GATT+Pair churn from 5 to ~2 attempts
        while still catching the button press within a reasonable window.

        Other failures: first 5 at normal poll interval (60s), then 5/10/15 min.
        """
        n = self._ble_consecutive_failures
        if stale_channel:
            # #24: a daemon-leaked BlueZ acquisition never clears by reconnecting
            # — an identical retry every 30s is pointless. Escalate 2—15 min so we
            # still catch a host-side recovery (systemctl restart bluetooth /
            # reboot) without hammering the wedged adapter.
            return min(15 * 60, 120 + 60 * min(max(n - 1, 0), 13))
        if bonding_rejected:
            # 2 min, 3 min, 5 min max
            return min(5 * 60, 120 + 60 * min(n - 1, 3))
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
        self._last_data_monotonic = time.monotonic()
        # BLE also flows through here; mark it so a telemetry-quiet cloud socket
        # in dual mode is treated as a hot standby, not a dead link (no churn).
        if self._signalr is not None:
            self._signalr.note_alt_transport_data()
        _before = len(self._signalr_data)
        self._signalr_data.update(sensor_data)
        if len(self._signalr_data) > _before:
            self._signalr_last_growth_monotonic = time.monotonic()
        # Frozen-SCU detection: track the SCU's own clock. A hung SCU keeps the
        # link (scu_connected stays on) and may keep re-pushing frames, but its
        # internal clock stops advancing — the one unambiguous "firmware wedged"
        # signal (only a physical power-cycle recovers it).
        clock = self._signalr_data.get("scu_internal_time")
        if clock is not None and clock != self._scu_clock_value:
            self._scu_clock_value = clock
            self._scu_clock_last_change_monotonic = time.monotonic()
        self._compute_fuel_metrics()
        _LOGGER.debug(
            "Sensor data merged (SignalR/BLE): %d total sensors", len(self._signalr_data)
        )
        # Trigger HA entity updates immediately
        self.async_set_updated_data({
            **(self.data or {}),
            "signalr_sensors": self._signalr_data,
        })

    @property
    def data_silence_seconds(self) -> float:
        """Seconds since the last data frame from ANY transport (SignalR OR BLE).

        Transport-agnostic: BLE frames also refresh this because they flow
        through _on_signalr_update. The 12V-off availability guard uses this so
        BLE mode does not falsely conclude data-silence while the SignalR socket
        is quiet. Returns 0.0 before the first frame.
        """
        if self._last_data_monotonic <= 0:
            return 0.0
        return time.monotonic() - self._last_data_monotonic

    @property
    def scu_frozen(self) -> bool:
        """True when the SCU firmware appears hung.

        A hung SCU keeps the connection (``scu_connected`` stays on) and may keep
        re-pushing frames, but its internal clock (``scu_internal_time``) stops
        advancing and it ignores every command over BOTH BLE and cloud — only a
        physical Aufbaubatterie power-cycle recovers it. Two OR-ed detectors:
        (a) the SCU's own UTC clock lags real time (instant, survives restarts);
        (b) the clock value has not advanced for a while (fallback for a clock
        that is not real-time-synced). Gated on live data flow so a genuine
        standby/12V-off (frames go silent) is not misreported.
        """
        if not self._signalr_data.get("scu_connected"):
            return False
        if self.data_silence_seconds >= _SCU_FROZEN_DATA_WINDOW:
            return False
        lag = self._scu_clock_wallclock_lag()
        if lag is not None and lag >= _SCU_FROZEN_WALLCLOCK_LAG:
            return True
        return (
            self._scu_clock_last_change_monotonic > 0
            and (time.monotonic() - self._scu_clock_last_change_monotonic)
            >= _SCU_FROZEN_TIMEOUT
        )

    @property
    def scu_frozen_since_seconds(self) -> float:
        """Seconds the SCU has been frozen (0.0 if not frozen).

        Prefers the wall-clock lag (the true frozen duration, survives restarts);
        falls back to time since the clock value was last seen to advance.
        """
        if not self.scu_frozen:
            return 0.0
        lag = self._scu_clock_wallclock_lag()
        if lag is not None:
            return lag
        if self._scu_clock_last_change_monotonic > 0:
            return time.monotonic() - self._scu_clock_last_change_monotonic
        return 0.0

    def _scu_clock_wallclock_lag(self) -> float | None:
        """Seconds the SCU's internal UTC clock lags real time, or None.

        ``scu_internal_time`` is the SCU's own wall clock (e.g. "2026-08-24 11:07
        UTC"). Healthy it tracks real time; when the firmware hangs it stops and
        falls behind. Returns None if the value is absent or unparseable.
        """
        raw = self._signalr_data.get("scu_internal_time")
        if raw is None or raw == "":
            return None
        parsed: datetime | None = None
        text = str(raw).strip()
        for fmt in (
            "%Y-%m-%d %H:%M UTC", "%Y-%m-%d %H:%M:%S UTC",
            "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
        ):
            try:
                parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = datetime.fromtimestamp(float(raw), tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                return None
        lag = (datetime.now(timezone.utc) - parsed).total_seconds()
        return lag if lag > 0 else 0.0

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
        """Handle SignalR connection loss — schedule reconnect.

        Called from the listen loop's finally block when the WebSocket
        closes unexpectedly.  If the connection was long-lived (>30s),
        reconnects immediately.  If the connection dropped quickly (<30s),
        applies a short cooldown to avoid hammering the Azure SignalR
        service — rapid reconnects cause the server to drop the new
        connection within seconds (seen as 8-message drops in logs).
        """
        if self._shutting_down:
            _LOGGER.debug("SignalR connection lost during shutdown — suppressing reconnect")
            return

        session_duration = time.monotonic() - self._signalr_connected_at if self._signalr_connected_at else 0
        self._reconnect_backoff = _INITIAL_BACKOFF

        if session_duration < _RAPID_DROP_THRESHOLD:
            # Short-lived session — server likely hasn't cleaned up yet.
            # Apply a cooldown so the next reconnect waits a few seconds.
            _LOGGER.info(
                "SignalR connection dropped after %.1fs — applying %ds cooldown before reconnect",
                session_duration, _RAPID_DROP_COOLDOWN,
            )
            self._last_reconnect_attempt = time.monotonic() - _INITIAL_BACKOFF + _RAPID_DROP_COOLDOWN
        else:
            # Long-lived session — normal disconnect, reconnect immediately.
            _LOGGER.info("SignalR connection lost after %.0fs — scheduling immediate reconnect", session_duration)
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
                
                # v2.64.1: After successful handshake, send explicit re-subscription
                # to ensure the SCU has received and processed all bus subscriptions.
                # Without this, commands sent immediately after reconnect may be silently
                # dropped if the SCU hasn't finished processing the initial subscriptions.
                # See issue #XX (50min reconnect timeout causing command failures).
                try:
                    await self._signalr.resubscribe()
                    _LOGGER.debug("Explicit re-subscription sent after reconnect")
                except Exception:
                    _LOGGER.warning("Re-subscription after reconnect failed", exc_info=True)
                
                # Reset backoff, failure counter, and shutdown flag on success.
                # _shutting_down MUST be reset here — stop_signalr() sets it
                # to True, but after a successful reconnect the connection-lost
                # callback must be re-enabled for fast recovery.  See issue #47.
                self._reconnect_backoff = _INITIAL_BACKOFF
                self._consecutive_failures = 0
                self._shutting_down = False
                self._signalr_connected_at = time.monotonic()
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

        # Imported before the try so the except clauses below can reference
        # BleStaleChannelError even if construction/connect raises.
        from .ble_client import (
            ScuBleClient,
            BleTransportError,
            BleStaleChannelError,
        )

        try:
            self._ble_client = ScuBleClient(
                scu_address=ble_address,
                on_pia_response=self._on_ble_pia_response,
                hass=self.hass,
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
            self._ble_write_degraded = False  # #24: a healthy connect clears the degraded flag
            self._ble_degraded_reason = None
            self._ble_last_rx_monotonic = time.monotonic()  # #24: reset liveness clock on a fresh connect
            self._connection_mode = "dual" if (self._signalr and self._signalr.connected) else "ble"
            _LOGGER.info("BLE direct path established to SCU %s (mode=%s)", ble_address, self._connection_mode)

            # Send PIA subscription requests over BLE to unlock all sensor
            # groups — same subscriptions SignalR sends.  Without these, the
            # SCU only pushes ~28 sensors autonomously.  With subscriptions,
            # all ~130 sensors should stream over BLE.
            await self._send_ble_subscriptions()

            # Start BLE listen loop as a background task — NOT a tracked task.
            # async_create_task() tasks are awaited during HA bootstrap/shutdown,
            # which causes "Setup timed out" if the BLE connection dies (e.g.
            # 12V off) and the listen loop's 30s uart_queue.get() keeps cycling.
            # hass.async_create_background_task() is truly fire-and-forget:
            # not awaited during bootstrap/shutdown, not tied to config entry.
            # Keep the handle so unload/reload can cancel it (avoids orphaning
            # the BleakClient, which wedges BlueZ on USB-passthrough hosts).
            self._ble_listen_task = self.hass.async_create_background_task(
                self._ble_listen_loop(),
                name=f"hymer_connect_ble_listen_{self.ble_address or 'scu'}",
            )
            # Best-effort: populate the paired-devices list once so the
            # diagnostic sensor/select have data without a manual refresh.
            if not self._paired_ble_devices:
                self.hass.async_create_background_task(
                    self._async_autofetch_paired_devices(),
                    name="hymer_connect_ble_paired_autofetch",
                )
            return True
        except BleStaleChannelError as err:
            # #24: link came up but the write/notify channel is a stale BlueZ
            # acquisition a fresh GATT session did not clear. Surface it (diagnostic
            # binary_sensor + honest log) and let the caller back off hard instead
            # of hammering an identical reconnect every 30s. Cloud path unaffected.
            self._ble_write_degraded = True
            self._ble_degraded_reason = str(err)
            _LOGGER.warning(
                "BLE degraded — the write/notify channel is a stale BlueZ "
                "acquisition that a fresh GATT session did not clear; writes and "
                "TLS will fail on this link. Recover by rebooting the host "
                "(required on Home Assistant OS), or on Supervised/Proxmox/"
                "container installs by restarting the bluetooth service "
                "('systemctl restart bluetooth'). BLE will retry slowly; the "
                "cloud path keeps working. Detail: %s",
                err,
            )
            if self._ble_client:
                try:
                    await self._ble_client.disconnect()
                except Exception:
                    pass
            self._ble_client = None
            self._ble_connected = False
            self._connection_mode = "cloud"
            return False
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
            # Return sentinel so caller can apply bonding-specific backoff
            return "bonding_rejected" if is_bonding_rejection else False

    async def _send_ble_subscriptions(self) -> None:
        """Send the PIA subscription burst + refresh over the BLE link.

        This is what makes the SCU push the full ~130-sensor set (including the
        one-shot habitation control slots) instead of only the ~28 autonomous
        sensors. Safe no-op when BLE is not connected.
        """
        client = self._ble_client
        if client is None or not self._ble_connected:
            return
        try:
            from .pia_decoder import build_subscription_requests, build_refresh_command
            requests = build_subscription_requests()
            _LOGGER.info(
                "Sending %d PIA subscription requests over BLE", len(requests)
            )
            for payload in requests:
                await client.send_pia_command(payload)
            await client.send_pia_command(build_refresh_command())
            _LOGGER.info("BLE PIA subscriptions + refresh sent")
        except Exception:
            _LOGGER.warning(
                "BLE PIA subscription failed — SCU will only push "
                "autonomous sensors (~28). SignalR provides full coverage.",
                exc_info=True,
            )

    async def async_warmup_reobserve(self, _now: Any = None) -> None:
        """Re-deliver the full snapshot during startup so gated entities materialise.

        Fixes #23: at a BLE-first cold start the one-shot habitation control
        slots (pump/main/shoreline/fresh-water, Dometic S10 selects) can arrive
        before the entity platforms have attached their discovery listeners, so
        those gated entities are never created — and the SCU never re-pushes the
        static slots. Re-issuing the BLE subscription+refresh (the only source
        of those slots) and forcing a coordinator refresh makes discovery re-run
        against the full accumulated set, materialising anything that missed the
        initial window.
        """
        await self._send_ble_subscriptions()
        if self._signalr is not None and self._signalr.connected:
            try:
                await self._signalr.resubscribe()
            except Exception:
                _LOGGER.debug("Warm-up cloud resubscribe failed", exc_info=True)
        await self.async_request_refresh()

    def _on_ble_pia_response(self, b64_payload: str) -> None:
        """Handle PIA response received via BLE — same decoder as SignalR."""
        self._ble_last_rx_monotonic = time.monotonic()  # #24 BLE liveness: a frame arrived over BLE
        from .pia_decoder import decode_pia_payload
        sensor_data = decode_pia_payload(b64_payload)
        if sensor_data:
            self._on_signalr_update(sensor_data)
            # Signal ACK only if the response contains the sensor we commanded.
            # Previous logic fired on ANY PIA response, causing false ACKs
            # from unrelated periodic sensor pushes (e.g. battery_current).
            # When _ble_pending_cmd_key is None (e.g. pia_request), accept
            # any response as ACK.
            if not self._ble_command_ack.is_set():
                cmd_key = self._ble_pending_cmd_key
                if cmd_key is None or cmd_key in sensor_data:
                    _LOGGER.debug(
                        "BLE ACK received (%d fields, matched %s)",
                        len(sensor_data), cmd_key or "any",
                    )
                    self._ble_command_ack.set()
                else:
                    _LOGGER.debug(
                        "BLE response ignored for ACK (waiting for %s, got %s)",
                        cmd_key,
                        list(sensor_data.keys())[:5],
                    )

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
                "BLE will be retried automatically."
            )

    async def stop_ble(self) -> None:
        """Disconnect the BLE client and cancel its background listen loop.

        Cancelling the fire-and-forget listen task first prevents an orphaned
        BleakClient from keeping the BlueZ GATT connection open after a reload.
        """
        task = self._ble_listen_task
        self._ble_listen_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                _LOGGER.debug("BLE listen task raised on cancel", exc_info=True)
        if self._ble_client:
            await self._ble_client.disconnect()
            self._ble_client = None
        self._ble_connected = False
        if self._connection_mode == "ble":
            self._connection_mode = "cloud"

    async def async_shutdown(self) -> None:
        """Full teardown for config-entry unload/reload: stop SignalR AND BLE.

        Critical on an integration UPDATE/reload: without releasing the BLE
        client and its background listen loop, the orphaned BleakClient keeps
        the BlueZ GATT connection open. The next setup then cannot reconnect,
        and on USB-passthrough hosts (e.g. a Bluetooth dongle passed into a
        Proxmox VM) the adapter stays wedged until a full host reboot.
        """
        self._shutting_down = True
        if self._ble_rebond_task is not None and not self._ble_rebond_task.done():
            self._ble_rebond_task.cancel()
            self._ble_rebond_task = None
        await self.stop_signalr()
        try:
            await asyncio.wait_for(self.stop_ble(), timeout=_BLE_CLEANUP_TIMEOUT)
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "BLE cleanup did not finish within %ds during unload — "
                "releasing references anyway",
                _BLE_CLEANUP_TIMEOUT,
            )
            self._ble_listen_task = None
            self._ble_client = None
            self._ble_connected = False
        except Exception:
            _LOGGER.debug("BLE cleanup during unload failed", exc_info=True)
            self._ble_listen_task = None
            self._ble_client = None
            self._ble_connected = False

    async def _cleanup_ble_after_start_failure(self) -> None:
        """Best-effort BLE cleanup that must not block HA setup.

        BlueZ/GATT cleanup can be the very thing that is stuck on unusual
        host setups (for example HA Container with host D-Bus passed through).
        Bound this cleanup separately so a failed BLE attempt always falls back
        to SignalR/cloud instead of holding Home Assistant's config-entry setup.
        """
        try:
            await asyncio.wait_for(self.stop_ble(), timeout=_BLE_CLEANUP_TIMEOUT)
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "BLE cleanup did not finish within %ds — continuing with cloud fallback",
                _BLE_CLEANUP_TIMEOUT,
            )
            self._ble_client = None
            self._ble_connected = False
            self._connection_mode = "cloud"
        except Exception:
            _LOGGER.debug("BLE cleanup after failed startup failed", exc_info=True)
            self._ble_client = None
            self._ble_connected = False
            self._connection_mode = "cloud"

    async def async_ensure_signalr_healthy(self) -> None:
        """Ensure SignalR is connected and healthy, reconnecting if needed.

        Checks both the WebSocket connection state and whether the connection
        is stale (needs_reconnect).  If unhealthy, attempts to reconnect.

        Also detects extended SCU standby (>10 min) and proactively forces
        a full re-auth + reconnect BEFORE sending the command.  After
        extended standby the server-side hub→SCU routing is stale —
        commands sent through the existing WebSocket are silently dropped.
        Waiting for _verify_send (60s) to detect the failure is too slow;
        the user sees an unresponsive switch and has to reload.  See #48.

        v2.64.1: After reconnect, wait for first sensor update to confirm
        subscriptions are active. Without this, commands arrive before the
        SCU processes subscriptions, causing silent drops. See issue #XX.

        Raises HomeAssistantError if reconnection fails.
        """
        from .signalr_client import EXTENDED_STANDBY_THRESHOLD

        client = self._signalr

        # Proactive extended-standby recovery: if the SCU has been in
        # standby longer than EXTENDED_STANDBY_THRESHOLD, the existing
        # WebSocket's send channel is stale.  Force a full re-auth +
        # reconnect to establish clean hub→SCU routing BEFORE sending.
        if client and client.connected and not client.needs_reconnect:
            standby = client.scu_standby_seconds
            if standby > EXTENDED_STANDBY_THRESHOLD:
                _LOGGER.info(
                    "SCU in extended standby (%.0fs > %ds) — forcing "
                    "full re-auth + reconnect before sending command",
                    standby, EXTENDED_STANDBY_THRESHOLD,
                )
                await self.force_reauth_and_reconnect()
                if not self._signalr or not self._signalr.connected:
                    raise HomeAssistantError(
                        "Cannot send command — SignalR reconnect after "
                        "extended standby failed. Try reloading the integration."
                    )
                # Fall through to subscription confirmation wait below
            else:
                # v2.64.1: After reconnect, confirm subscriptions are active
                # by waiting for first sensor update. Subscriptions may have
                # been sent but not yet processed by the SCU.
                now = time.monotonic()
                reconnect_age = now - self._signalr_connected_at if self._signalr_connected_at else 999
                if reconnect_age < 10 and len(self._signalr_data) == 0:
                    _LOGGER.info(
                        "SignalR just reconnected (%.1fs ago) — waiting for "
                        "subscription confirmation (first sensor update)",
                        reconnect_age,
                    )
                    # Wait up to 5 seconds for first sensor data
                    for i in range(50):
                        await asyncio.sleep(0.1)
                        if len(self._signalr_data) > 0:
                            _LOGGER.debug(
                                "Subscriptions confirmed active (received after %.1fs)",
                                time.monotonic() - now,
                            )
                            return
                    _LOGGER.warning(
                        "No sensor data received after reconnect — subscriptions may not be active"
                    )
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
        """Deprecated stub — retained only for API compatibility.

        v2.62.24: BLE write path was permanently removed after the
        v2.62.17 → v2.62.23 investigation conclusively proved the SCU
        (firmware 1.12.0.0) silently drops every BLE `setValues` frame
        regardless of `connectedComponentInstance` (CCValue field 10),
        ACK timeout, or bus type. All writes now go via the cloud /
        SignalR path. BLE remains a read-only mirror for low-latency
        sensor pushes. See CHANGELOG v2.62.24 and README "BLE write path"
        for details. This method is kept as a no-op so any external
        plug-ins that may have called it continue to import cleanly.
        """
        _LOGGER.debug(
            "_send_via_ble() called but BLE write path is disabled "
            "since v2.62.24 — caller should route via cloud/SignalR"
        )
        return False

    async def _send_with_retry(
        self, method_name: str, *args: Any, **kwargs: Any
    ) -> None:
        """Send a command via the cloud / SignalR path with one retry.

        v2.62.24: BLE write path removed. Vehicle testing on SCU firmware
        1.12.0.0 showed every BLE `setValues` write was silently dropped
        regardless of timeout, instance field, or bus. The only working
        write transport is cloud / SignalR, so we route directly there.

        BLE is still used aggressively for **reads** (sensor pushes
        decode through the same PIA pipeline as cloud) — only the write
        leg is gone.

        Args:
            method_name: Name of the method on HymerSignalRClient
                         (e.g. ``send_light_command``).
            *args, **kwargs: Forwarded to the client method.

        Raises:
            HomeAssistantError: All send attempts failed.
        """
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
        """Send a light/switch command via BLE (opt-in) or cloud."""
        from .pia_decoder import build_light_command

        payload = build_light_command(bus_id, sensor_id, **kwargs)
        if await self._try_ble_write(
            payload, label=f"send_light_command bus={bus_id} sid={sensor_id}"
        ):
            return
        await self._send_with_retry(
            "send_light_command", bus_id, sensor_id, **kwargs
        )

    async def async_send_multi_sensor_command(
        self, sensors: list[dict]
    ) -> None:
        """Send a multi-sensor command via BLE (opt-in) or cloud."""
        from .pia_decoder import build_multi_sensor_command

        payload = build_multi_sensor_command(sensors)
        if await self._try_ble_write(
            payload, label=f"send_multi_sensor_command ({len(sensors)} sensors)"
        ):
            return
        await self._send_with_retry("send_multi_sensor_command", sensors)

    async def async_send_pia_request(
        self, payload: str
    ) -> None:
        """Send a raw PIA request via BLE (opt-in) or cloud."""
        if await self._try_ble_write(payload, label="send_pia_request"):
            return
        await self._send_with_retry("send_pia_request", payload)

    async def _try_ble_write(self, b64_payload: str, *, label: str = "command") -> bool:
        """Attempt a write over the BLE path when the user has opted in.

        Returns True only when BLE delivered the command AND the SCU returned a
        SUCCESS ACK (matching request_id). Any other outcome — opt-in disabled,
        BLE not connected, non-success status, timeout, or error — returns False
        so the caller falls back to the cloud/SignalR path. Worst case is
        therefore identical to the cloud-only behaviour.
        """
        if not self.ble_write_enabled:
            return False
        client = self._ble_client
        if client is None or not self._ble_connected or not client.connected:
            return False
        try:
            status = await client.send_setvalue_with_ack(
                b64_payload, timeout=DEFAULT_BLE_WRITE_ACK_TIMEOUT
            )
        except Exception:
            _LOGGER.warning(
                "BLE write attempt raised — falling back to cloud", exc_info=True
            )
            return False
        # status 1 = SUCCESS; 0 = NO_STATUS (treated as success per PIA decoder).
        if status in (0, 1):
            _LOGGER.info("Command sent over BLE (%s, status=%s)", label, status)
            return True
        _LOGGER.info(
            "BLE write not accepted (status=%s) — falling back to cloud", status
        )
        return False

    async def async_send_restart_system_command(self) -> None:
        """Send an SCU restart command via PIA."""
        from .pia_decoder import build_restart_system_request
        payload = build_restart_system_request(cold=True)
        await self.async_send_pia_request(payload)
        _LOGGER.warning("SCU restart command sent — the SCU will reboot")

    async def _async_autofetch_paired_devices(self) -> None:
        """One-shot best-effort paired-devices fetch shortly after BLE connects."""
        await asyncio.sleep(5)  # let the listen loop + SCU settle
        if self._ble_connected and not self._paired_ble_devices:
            try:
                await self.async_log_paired_ble_devices()
            except Exception:
                _LOGGER.debug("Paired-devices autofetch failed", exc_info=True)

    async def async_log_paired_ble_devices(self) -> list:
        """Read the SCU's paired mobile devices over BLE, store and log them.

        Read-only diagnostic (getPairedMobileDevices, field 5). Requires an
        active bonded BLE session; over the cloud path the SCU replies
        ACCESS_DENIED. Stores the result for the sensor/select entities and
        returns the device list (empty on any failure).
        """
        client = self._ble_client
        if client is None or not self._ble_connected or not client.connected:
            _LOGGER.warning(
                "Paired-BLE-devices: BLE not connected — cannot query the SCU "
                "(this command is bond-gated and cloud-rejected)"
            )
            return []
        try:
            devices = await client.get_paired_mobile_devices()
        except Exception:
            _LOGGER.warning("Paired-BLE-devices: query raised", exc_info=True)
            return []

        stored = [
            {"name": (d.name or "").strip(), "mac": d.mac, "uuid": d.user_uuid}
            for d in devices
        ]
        self._paired_ble_devices = stored
        self._paired_ble_devices_updated = datetime.now(timezone.utc).isoformat()
        if devices:
            self._paired_ble_user_uuid = devices[0].user_uuid or self._paired_ble_user_uuid
        # Drop a stale selection if the chosen device is no longer paired.
        if self._unpair_selected_mac and not any(
            d["mac"].lower() == self._unpair_selected_mac for d in stored
        ):
            self._unpair_selected_mac = None
        self.async_update_listeners()

        if not stored:
            _LOGGER.info(
                "Paired-BLE-devices: SCU returned no devices (empty list, "
                "ACCESS_DENIED, or no reply — SCU may be asleep)"
            )
            return []
        _LOGGER.info("Paired-BLE-devices: SCU reports %d paired device(s):", len(stored))
        for i, d in enumerate(stored, 1):
            _LOGGER.info(
                "  [%d] name=%r mac=%s userUuid=%s", i, d["name"], d["mac"], d["uuid"],
            )
        return stored

    async def async_unpair_ble_device(self, mac: str) -> bool:
        """Unpair one paired mobile device over BLE by MAC (DESTRUCTIVE).

        Frees a pairing slot on the SCU. BLE-only and bond-gated. Looks the
        device up in the last-fetched paired list (for its name + userUuid),
        sends deleteMobileDevices, and on SUCCESS re-reads the list. Returns
        True only on a SUCCESS ACK.
        """
        target = (mac or "").strip().lower()
        if not target:
            _LOGGER.warning("Unpair BLE device: no MAC given")
            return False
        client = self._ble_client
        if client is None or not self._ble_connected or not client.connected:
            _LOGGER.warning(
                "Unpair BLE device %s: BLE not connected — refusing (bond-gated, "
                "cloud-rejected)", target,
            )
            return False

        from .ble_client import MobileDevice
        match = next(
            (d for d in self._paired_ble_devices if d["mac"].lower() == target), None,
        )
        if match is None:
            _LOGGER.warning(
                "Unpair BLE device %s: not in the last-fetched paired list — press "
                "'Log paired BLE devices' first to refresh", target,
            )
            return False

        device = MobileDevice(
            mac=match["mac"], name=match["name"], user_uuid=match.get("uuid", ""),
        )
        user_uuid = match.get("uuid") or self._paired_ble_user_uuid or ""
        _LOGGER.warning(
            "Unpair BLE device: removing %r (%s) from SCU pairing slots",
            device.name, device.mac,
        )
        try:
            status = await client.delete_mobile_device(device, user_uuid=user_uuid)
        except Exception:
            _LOGGER.warning("Unpair BLE device %s: raised", target, exc_info=True)
            return False

        if status not in (0, 1):  # anything but SUCCESS/NO_STATUS is an outright reject
            _LOGGER.warning(
                "Unpair BLE device %s: SCU rejected (status=%s) — slot NOT freed",
                device.mac, status,
            )
            return False
        # #26 (stbcgn): a SUCCESS ACK is NOT proof of removal. Some SCU firmware
        # replies status=1 but silently keeps the device (same ack-then-discard as
        # the bus-9 slot-2 Dometic write). Never claim "slot freed" on the ACK alone —
        # re-read the table and confirm the entry is actually gone.
        refreshed = await self.async_log_paired_ble_devices()  # refresh list + push update
        if not any(d["mac"].lower() == target for d in refreshed):
            _LOGGER.warning(
                "Unpair BLE device: %s removed — slot freed (SCU status=%s, confirmed "
                "by re-read)", device.mac, status,
            )
            return True
        _LOGGER.warning(
            "Unpair BLE device: SCU ACKed removal of %s (status=%s) but the device is "
            "STILL paired after a re-read — the SCU accepted the request and silently "
            "discarded it (known ack-then-discard behaviour on some firmware, #26). "
            "Slot NOT freed.", device.mac, status,
        )
        return False

    async def _async_try_ble_connect(self) -> None:
        """Attempt the BLE direct path if enabled and not already connected.

        Driven by three callers: the coordinator poll (often starved because
        every SignalR push reschedules it), an independent watchdog timer, and
        an immediate kick when the BLE option is toggled on. Re-entrant-safe via
        ``_ble_connecting`` so overlapping callers never create two clients.

        Backoff: the first few failures retry at the normal interval (so we do
        not miss the ~60-120s SCU pairing window after pressing CONNECTION);
        after that it escalates. Skipped while the config-flow pairing task runs.
        """
        if not self.ble_enabled:
            return
        if self._ble_pairing_in_progress:
            _LOGGER.debug("BLE attempt skipped — config flow pairing in progress")
            return
        # #24: detect a silently-dead BLE link. BlueZ can drop the channel with no
        # disconnect callback, so bleak still reports is_connected=True and the
        # listen loop just spins on an empty queue — the integration never notices
        # and _ble_connected stays True forever. If BLE claims connected but no BLE
        # frame has arrived for a while WHILE data is still flowing (SCU is awake,
        # witnessed by the cloud/other transport), the link is dead: tear it down so
        # this same tick reconnects. Gated on "data still arriving" so a genuine
        # 12V-off standby (both transports silent) does not churn reconnects.
        if self._ble_connected and self._ble_last_rx_monotonic > 0:
            now = time.monotonic()
            rx_age = now - self._ble_last_rx_monotonic
            data_age = now - self._last_data_monotonic
            if rx_age >= _BLE_RX_LIVENESS_TIMEOUT and data_age < _BLE_RX_LIVENESS_TIMEOUT:
                _LOGGER.warning(
                    "BLE link appears silently dead — no BLE frame for %.0fs while "
                    "data still arrives over another transport (%.0fs ago); forcing "
                    "teardown and reconnect", rx_age, data_age,
                )
                try:
                    await asyncio.wait_for(
                        self.stop_ble(), timeout=_BLE_CLEANUP_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    self._ble_listen_task = None
                    self._ble_client = None
                    self._ble_connected = False
                    self._connection_mode = "cloud"
                except Exception:
                    _LOGGER.debug("BLE liveness teardown failed", exc_info=True)
        if self._ble_connected:
            return
        # #23: at cold start, let the cloud snapshot land first so cloud-only
        # habitation slots reach the union before BLE claims the SCU link (an
        # active BLE session makes some SCUs withhold those slots from the cloud).
        if not self._ble_cold_start_gate_open:
            now = time.monotonic()
            sr_connected = (
                self._signalr is not None
                and self._signalr.connected
                and self._signalr_connected_at > 0
            )
            if sr_connected:
                # Cloud is here — open once its snapshot has PLATEAUED (the merged
                # set stopped growing), so slow-delivering boots still get the
                # one-shot habitation slots into the union before BLE claims the
                # link. Capped so a continuously-trickling cloud can't hold BLE off.
                since_connect = now - self._signalr_connected_at
                plateaued = (
                    since_connect >= _BLE_COLD_START_SETTLE_MIN
                    and self._signalr_last_growth_monotonic > 0
                    and (now - self._signalr_last_growth_monotonic)
                    >= _BLE_COLD_START_PLATEAU
                )
                capped = since_connect >= _BLE_COLD_START_SETTLE_MAX
                open_gate = plateaued or capped
                reason = (
                    "cloud snapshot plateaued" if plateaued else "settle cap"
                )
            else:
                # No cloud (yet). Don't hold BLE waiting for a cloud that may be
                # absent (off-grid / no LTE) — release after a short window so the
                # fleet's BLE path is not delayed at an off-grid restart.
                open_gate = (
                    now - self._startup_monotonic
                ) >= _BLE_COLD_START_NO_CLOUD
                reason = "no cloud — releasing BLE"
            if open_gate:
                self._ble_cold_start_gate_open = True
                _LOGGER.info(
                    "BLE cold-start gate open (%s) — proceeding with BLE connect",
                    reason,
                )
            else:
                _LOGGER.debug(
                    "BLE deferred at cold start — letting cloud seed first "
                    "(signalr_connected=%s, %.0fs since startup)",
                    sr_connected,
                    now - self._startup_monotonic,
                )
                return
        if self._ble_connecting:
            return
        if time.monotonic() < self._ble_next_attempt:
            return
        self._ble_connecting = True
        try:
            # #19: apply a requested bond reset HERE, under the same guard as the
            # connect, so it can never run concurrently with an in-flight bond and
            # wipe one that just succeeded. Order is always disconnect -> clear ->
            # fresh bond, never bond -> clear.
            if self._ble_reset_pending:
                self._ble_reset_pending = False
                await self._async_reset_ble_bond()
            try:
                ble_result = await asyncio.wait_for(
                    self.start_ble(), timeout=_BLE_STARTUP_TIMEOUT
                )
                if ble_result is True:
                    self._ble_consecutive_failures = 0
                    self._ble_next_attempt = 0.0
                    _LOGGER.info(
                        "BLE direct path active — running alongside SignalR "
                        "(both paths: ~130 sensors, BLE ~50ms / SignalR ~500ms–2s)"
                    )
                else:
                    is_bonding = ble_result == "bonding_rejected"
                    self._ble_consecutive_failures += 1
                    backoff = self._ble_backoff_seconds(
                        bonding_rejected=is_bonding,
                        stale_channel=self._ble_write_degraded,
                    )
                    self._ble_next_attempt = time.monotonic() + backoff
                    _LOGGER.info(
                        "BLE failed %d times — next attempt in %ds%s",
                        self._ble_consecutive_failures, backoff,
                        " (bonding rejected)" if is_bonding
                        else " (write channel degraded)" if self._ble_write_degraded
                        else "",
                    )
            except asyncio.TimeoutError:
                self._ble_consecutive_failures += 1
                backoff = self._ble_backoff_seconds()
                self._ble_next_attempt = time.monotonic() + backoff
                _LOGGER.warning(
                    "BLE startup exceeded %ds — continuing with cloud fallback "
                    "and retrying BLE in %ds",
                    _BLE_STARTUP_TIMEOUT, backoff,
                )
                await self._cleanup_ble_after_start_failure()
            except Exception:
                self._ble_consecutive_failures += 1
                backoff = self._ble_backoff_seconds()
                self._ble_next_attempt = time.monotonic() + backoff
                _LOGGER.debug(
                    "BLE connection attempt failed (attempt %d, next in %ds)",
                    self._ble_consecutive_failures, backoff,
                    exc_info=True,
                )
        finally:
            self._ble_connecting = False

    async def async_ble_watchdog(self, _now: Any = None) -> None:
        """Watchdog tick: (re)connect BLE independent of the push-starved poll."""
        await self._async_try_ble_connect()

    def request_ble_rebond(self) -> None:
        """Flag an active re-bond, set by the options flow after the user resets the
        BLE bond. Consumed by the options-updated handler once the new options are
        applied (so ``ble_enabled`` already reflects the reset)."""
        self._ble_rebond_pending = True

    def request_ble_reset_and_rebond(self) -> None:
        """Flag a BLE bond reset + active re-bond (options flow, #19).

        The clear itself is deferred to the guarded connect path so it cannot race
        the watchdog and delete a bond that just succeeded. Only the flags are set
        here; ``_async_options_updated`` then kicks the re-bond burst."""
        self._ble_reset_pending = True
        self._ble_rebond_pending = True

    async def _async_reset_ble_bond(self) -> None:
        """Clear the BlueZ bond from inside the connect guard (#19).

        Drops any half-open link + acquired write/notify FDs, then removes the bond.
        The EHG token and SCU address are kept, so the cloud path is untouched; the
        very next connect re-bonds after the CONNECTION press."""
        address = (self.ble_address or "").upper()
        if not address:
            _LOGGER.warning("BLE bond reset requested but no SCU address is stored")
            return
        from .ble_client import async_clear_bluez_bond, async_dbus_disconnect
        if self._ble_client is not None:
            try:
                await self._ble_client.disconnect()
            except Exception:
                pass
            self._ble_client = None
            self._ble_connected = False
        try:
            await async_dbus_disconnect(address)  # release any acquired write/notify FDs first
        except Exception:
            pass
        removed = await async_clear_bluez_bond(address)
        _LOGGER.info(
            "Reset BLE bond for %s under connect guard: %s (EHG token + address kept — cloud unaffected)",
            address, "cleared" if removed else "no existing bond",
        )

    async def async_kick_ble_rebond(self) -> None:
        """Run an active BLE re-bond burst after a manual bond reset.

        Tight retries that line up with the user's CONNECTION press at the vehicle,
        reusing the existing EHG token (no re-mint — ``start_ble`` skips pairing when
        a token is stored). No-op for cloud-only setups (BLE disabled), so it only
        does anything for a HA host with BLE in the vehicle (Path A).
        """
        self._ble_rebond_pending = False
        if not self.ble_enabled:
            return
        # Clear backoff + cold-start gate so attempts fire immediately and tightly.
        self._ble_consecutive_failures = 0
        self._ble_next_attempt = 0.0
        self._ble_cold_start_gate_open = True
        if self._ble_rebond_task is not None and not self._ble_rebond_task.done():
            return  # a burst is already running
        self._ble_rebond_task = self.hass.async_create_background_task(
            self._ble_rebond_burst(), name="hymer_ble_rebond_burst"
        )

    async def _ble_rebond_burst(self) -> None:
        """~2 minutes of tight BLE (re)connect attempts to catch the SCU pairing
        window right after the user presses CONNECTION. Stops once BLE is up."""
        deadline = time.monotonic() + _BLE_REBOND_BURST_SECONDS
        _LOGGER.info(
            "BLE re-bond burst started — press CONNECTION on the SCU now "
            "(retrying ~%ds, reusing the existing token, no new pairing)",
            _BLE_REBOND_BURST_SECONDS,
        )
        try:
            while (
                time.monotonic() < deadline
                and self.ble_enabled
                and not self._ble_connected
            ):
                self._ble_consecutive_failures = 0
                self._ble_next_attempt = 0.0
                await self._async_try_ble_connect()
                if self._ble_connected:
                    _LOGGER.info("BLE re-bond burst: bonded and connected")
                    return
                await asyncio.sleep(_BLE_REBOND_BURST_INTERVAL)
            if not self._ble_connected:
                _LOGGER.info(
                    "BLE re-bond burst ended without a bond — the background watchdog "
                    "will keep retrying; press CONNECTION near the vehicle"
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("BLE re-bond burst error", exc_info=True)

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
        #
        # BLE (re)connect lives in _async_try_ble_connect and is driven from
        # here AND from an independent watchdog timer (the poll alone is starved
        # whenever SignalR pushes keep rescheduling it). The method is
        # re-entrant-safe, so calling it from both is harmless.
        await self._async_try_ble_connect()

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
