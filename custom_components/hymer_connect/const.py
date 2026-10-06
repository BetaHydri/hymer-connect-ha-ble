"""Constants for HYMER Connect integration."""

DOMAIN = "hymer_connect"
MANUFACTURER = "Erwin Hymer Group"

# --- Base URLs ---
API_BASE_URL = "https://smartrv.erwinhymergroup.com"
API_BASE_URL_SCC = "https://scc-api.smartrv.erwinhymergroup.com"
API_BASE_URL_RVTWIN = "https://scc-rvtwin.smartrv.erwinhymergroup.com"
API_BASE_URL_APPCOMM = "https://scc-appcomm.smartrv.erwinhymergroup.com"

# --- OAuth2 Authentication ---
ENDPOINT_AUTH = "/api/v2/oauth/token"
OAUTH2_CLIENT_ID = "ehg-prod-mobile-app-technical-user"
# DEPRECATED: legacy fallback Basic-auth header. New installs should paste
# their own value (extracted from the EHG mobile app via mitmproxy) into the
# config flow; that value is stored per-entry under CONF_OAUTH_BASIC_AUTH and
# takes precedence over this constant. This constant will be removed in a
# future release after a deprecation period; existing users without a
# per-entry value continue to work in the meantime.
OAUTH2_BASIC_AUTH_LEGACY_DEFAULT = "Basic ZWhnLXByb2QtbW9iaWxlLWFwcC10ZWNobmljYWwtdXNlcjpaez96Ois3bVFhNXZAb2VlNV0lZEVeUSpxeDh9WXIoYWw1eFNUaC05LERdYm48OzhWbzh1PGclc8OcLShOMyV5"
AUTH_GRANT_TYPE_PASSWORD = "password"
AUTH_GRANT_TYPE_REFRESH = "refresh_token"

# --- Main API Endpoints ---
ENDPOINT_ACCOUNTS_ME = "/api/ehg/v1/accounts/me"
ENDPOINT_VEHICLES_BY_TOKEN = "/api/ehg/v1/vehicles/byToken"
ENDPOINT_CONFIRMATION_TOKEN = "/api/ehg/v1/accounts/confirmationToken"

# --- SCC API Endpoints ---
ENDPOINT_RV_TWIN_VEHICLES = "/api/rv-twin/vehicles"
ENDPOINT_CONFIG_MENU = "/api/config/menu"
ENDPOINT_CONFIG_BRANDS = "/api/config/brands/details"
ENDPOINT_SERVICE_CATALOGUE = "/api/service-catalogue/services"
ENDPOINT_PUSH_NOTIFICATIONS = "/api/push-notifications/subscriptions/scu"
ENDPOINT_PUSH_DEVICE_REG = "/api/push-notifications/devices"

# --- SignalR ---
SIGNALR_NEGOTIATE_PATH = "/datahub/negotiate"
SIGNALR_HUB_NAME = "datahub"

# --- Headers ---
HEADER_ACCESS_TOKEN = "scc-csngaccesstoken"
HEADER_BRAND = "scc-brand"
HEADER_LOCALE = "scc-locale"
HEADER_APP_VERSION = "scc-appversion"
HEADER_EHG_BRAND = "ehg-smart-caravan-brand"

# --- App Version ---
APP_VERSION = "2.10.14"
USER_AGENT = "okhttp/4.10.0"

# --- Brands ---
BRANDS = {
    "hymer": "HYMER",
    "buerstner": "Bürstner",
    "dethleffs": "Dethleffs",
    "eriba": "Eriba",
    "lmc": "LMC",
    "niesmann-bischoff": "Niesmann+Bischoff",
    "sunlight": "Sunlight",
    "carado": "Carado",
    "laika": "Laika",
    "freeontour": "FreeOnTour",
}

# Default scan interval (seconds)
DEFAULT_SCAN_INTERVAL = 60

# Seconds of data silence (any transport) after which 12V-dependent entities
# (lights, water pump) are marked unavailable. Cutting habitation 12V does NOT
# power the SCU down: main_switch freezes at "On" and the SCU just stops
# streaming, so data-silence is the reliable signal.
# Transport-aware: BLE streams sub-second, so 15s of silence is conclusive.
# On cloud, some (retrofit) SCUs push standby frames on a cadence that gaps just
# over 60s, which false-greyed the pump/lights ~200x/day (#30). The threshold is
# aligned with STALE_DATA_TIMEOUT (3 min): the v2.97.0 stale-routing detector
# forces a reconnect at that point, so real long stalls are restored BEFORE the
# grey-out fires, while a genuine 12V-off (frames never resume) still greys.
UNAVAILABLE_SILENCE_BLE = 15
UNAVAILABLE_SILENCE_CLOUD = 180

# Config keys
CONF_BRAND = "brand"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_VEHICLE_URN = "vehicle_urn"
CONF_SCU_URN = "scu_urn"
CONF_VEHICLE_ID = "vehicle_id"
CONF_EHG_TOKEN = "ehg_access_token"
CONF_EHG_REFRESH_TOKEN = "ehg_refresh_token"
CONF_OAUTH_BASIC_AUTH = "oauth_basic_auth"
CONF_TANK_CAPACITY = "tank_capacity_liters"

# Vehicle activation
CONF_QR_TOKEN = "qr_activation_token"

# BLE dual-path config keys
CONF_BLE_ADDRESS = "ble_scu_address"
CONF_BLE_ENABLED = "ble_enabled"
CONF_BLE_REFRESH_TOKEN = "ble_refresh_token"
# Stable mobile-device name presented to the SCU during BLE pairing. Generated
# once on the first successful pair and reused on every subsequent re-pair, so
# the SCU always sees the same (MAC, name) pairing slot instead of a fresh
# random "ha-<time>" name each time (which the SCU may reject / duplicate).
CONF_BLE_PAIR_NAME = "ble_pair_name"

# Route WRITE commands over BLE first (field-1 BleProtocol.request +
# write-with-response), falling back to cloud/SignalR on any BLE failure or
# non-success ACK. Default ON: when BLE is connected the local path is used
# (faster, works offline); if BLE is down or a write is not ACKed it
# transparently falls back to the cloud, so worst case == cloud-only. Untick to
# force cloud-only.
CONF_BLE_WRITE_ENABLED = "ble_write_enabled"
DEFAULT_BLE_WRITE_ENABLED = True
# Seconds to wait for a matching BleProtocol.response ACK before cloud fallback.
DEFAULT_BLE_WRITE_ACK_TIMEOUT = 3.0

# Opt-in automatic recovery for the #24 stale BlueZ write/notify wedge. When BLE
# comes up but its write/notify channel is a daemon-leaked acquisition (MTU
# pinned at 23, "Write acquired"), a fresh GATT session cannot clear it — only a
# host-side bluetooth restart does. With this ON, the integration restarts the
# host bluetooth service via systemd D-Bus (RestartUnit bluetooth.service),
# falling back to power-cycling the owning BlueZ adapter (Adapter1.Powered
# off→on) where systemd is unavailable, at most once per hour. OFF by default
# because recovery briefly drops ALL BLE on the host.
CONF_BLE_AUTO_RECOVER = "ble_auto_recover"
DEFAULT_BLE_AUTO_RECOVER = False
# Minimum seconds between two automatic bluetooth-stack recoveries (blast-radius guard).
BLE_AUTO_RECOVER_MIN_INTERVAL = 3600

# Opt-in "cloud on demand": on a BLE-capable (dual-path) enrollment, tear down
# the persistent SignalR/cloud session while the BLE link is healthy and stable,
# bringing it back only when BLE drops or degrades. Removes the always-on cloud
# footprint (24/7 SignalR + 60s polling) for users who want BLE-primary. Default
# OFF. NEVER suppresses cloud on a cloud-only enrollment — it is gated on
# ble_enabled + a live, non-degraded BLE link — and backs off automatically on
# flappy BLE via the stability grace window below (#19). Needs a one-time cloud
# login at setup like every BLE install; this only changes RUNTIME behaviour.
CONF_CLOUD_ON_DEMAND = "cloud_on_demand"
DEFAULT_CLOUD_ON_DEMAND = False
# BLE must hold a healthy, non-degraded link for this many seconds before the
# cloud session is torn down — guards against flappy-BLE churn (#19).
CLOUD_ON_DEMAND_BLE_STABLE_SECONDS = 120

# NOTE: CONF_CLOUD_FALLBACK / CONF_BLE_ACK_TIMEOUT / DEFAULT_/MIN_/MAX_BLE_ACK_TIMEOUT
# existed up to v2.62.23 and were deprecated in v2.62.24 when the BLE write
# path was removed (SCU firmware 1.12.0.0 silently drops all BLE setValues).
# They have been removed entirely; HA simply ignores unknown keys in older
# config-entry options dicts, so dropping the Python identifiers is safe.

# Default diesel tank capacity (litres) — user can override in Options
# Common Sprinter tanks: 71 L (314/316 CDI), 93 L (419/519 CDI standard)
DEFAULT_TANK_CAPACITY_LITERS = 93

# Platforms
PLATFORMS = ["sensor", "binary_sensor", "device_tracker", "light", "switch", "climate", "select", "number", "button", "cover"]
