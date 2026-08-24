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
