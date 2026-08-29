"""Constants for the STEINEL Connect BLE / Bluetooth Mesh integration.

Protocol details are reconstructed from a static analysis of the STEINEL
Connect Android app (4.1-62) and validated against a real STEINEL L 845 C,
see ``STEINEL_BLE_KOMMUNIKATION.md`` and ``STEINEL_BLE_TOOL.md`` in the
repository root. Bluetooth-Mesh model/opcode values below are the
Bluetooth-SIG-standardised constants from the Mesh Model specification and
are not STEINEL-specific secrets.
"""

from __future__ import annotations

DOMAIN = "steinel_ble"
MANUFACTURER = "STEINEL"

# STEINEL company identifier (Bluetooth SIG assigned number 0x0563 == 1379).
STEINEL_COMPANY_ID = 0x0563

# Proprietary STEINEL direct GATT channel (device info, identify, firmware
# update state machine, global reset). Independent from Bluetooth Mesh.
STEINEL_SERVICE = "2672b000-875a-47ff-b1ef-f3f6872db917"
STEINEL_RX = "2672b004-875a-47ff-b1ef-f3f6872db917"  # notify, device -> app
STEINEL_TX = "2672b005-875a-47ff-b1ef-f3f6872db917"  # write w/o response

# Standard Bluetooth Mesh Provisioning / Proxy GATT services & characteristics.
MESH_PROVISIONING_SERVICE = "00001827-0000-1000-8000-00805f9b34fb"
MESH_PROVISIONING_IN = "00002adb-0000-1000-8000-00805f9b34fb"
MESH_PROVISIONING_OUT = "00002adc-0000-1000-8000-00805f9b34fb"
MESH_PROXY_SERVICE = "00001828-0000-1000-8000-00805f9b34fb"
MESH_PROXY_IN = "00002add-0000-1000-8000-00805f9b34fb"
MESH_PROXY_OUT = "00002ade-0000-1000-8000-00805f9b34fb"

# Nordic (Buttonless + Secure) DFU, used for the validated firmware update path.
NORDIC_DFU_SERVICE = "0000fe59-0000-1000-8000-00805f9b34fb"
NORDIC_DFU_CONTROL = "8ec90001-f315-4f60-9fb8-838830daea50"
NORDIC_DFU_PACKET = "8ec90002-f315-4f60-9fb8-838830daea50"
NORDIC_DFU_BUTTONLESS = "8ec90003-f315-4f60-9fb8-838830daea50"

# Bluetooth-SIG-assigned Mesh Model IDs used to auto-detect lamp capabilities
# by attempting a Config Model App Bind for each and keeping the ones that
# succeed. These are public Mesh Model spec constants, not vendor-specific.
MODEL_GENERIC_ONOFF_SERVER = 0x1000
MODEL_LIGHT_LIGHTNESS_SERVER = 0x1300
MODEL_LIGHT_CTL_SERVER = 0x1303
MODEL_LIGHT_HSL_SERVER = 0x1307

CAPABILITY_ONOFF = "onoff"
CAPABILITY_LIGHTNESS = "lightness"
CAPABILITY_CTL = "ctl"
CAPABILITY_HSL = "hsl"

BIND_MODELS: dict[str, int] = {
    CAPABILITY_ONOFF: MODEL_GENERIC_ONOFF_SERVER,
    CAPABILITY_LIGHTNESS: MODEL_LIGHT_LIGHTNESS_SERVER,
    CAPABILITY_CTL: MODEL_LIGHT_CTL_SERVER,
    CAPABILITY_HSL: MODEL_LIGHT_HSL_SERVER,
}

# STEINEL vendor models (company id 0x0563), see
# STEINEL_BLE_KOMMUNIKATION.md section 4. Detected the same way as the SIG
# light models above (attempt a bind, keep it if it succeeds), but exposed
# as extra property/sensor entities rather than as light features.
MODEL_LIGHT_LC_EXTENSION = 0x1001
MODEL_SENSOR_EXTENSION = 0x1003

CAPABILITY_LIGHT_LC_EXTENSION = "light_lc_extension"
CAPABILITY_SENSOR_EXTENSION = "sensor_extension"

VENDOR_BIND_MODELS: dict[str, int] = {
    CAPABILITY_LIGHT_LC_EXTENSION: MODEL_LIGHT_LC_EXTENSION,
    CAPABILITY_SENSOR_EXTENSION: MODEL_SENSOR_EXTENSION,
}

# STEINEL Light CTL colour temperature range as used by the Mesh Model spec
# (raw Kelvin values used on the wire).
CTL_TEMP_MIN_KELVIN = 800
CTL_TEMP_MAX_KELVIN = 20000

CONF_UNICAST = "unicast"
CONF_DEVICE_KEY = "device_key"
CONF_ELEMENTS = "elements"
CONF_CAPABILITIES = "capabilities"
CONF_NAME = "name"

CONF_NET_KEY = "net_key"
CONF_APP_KEY = "app_key"
CONF_IV_INDEX = "iv_index"
CONF_SEQUENCE = "sequence"
CONF_SOURCE = "source"
CONF_TTL = "ttl"
CONF_NET_KEY_INDEX = "net_key_index"
CONF_APP_KEY_INDEX = "app_key_index"
CONF_NODES = "nodes"

CONF_FIRMWARE_URL = "firmware_url"
CONF_FIRMWARE_VERSION = "firmware_version"
CONF_FIRMWARE_HARDWARE = "firmware_hardware"
CONF_FIRMWARE_PRODUCT_ID = "firmware_product_id"
CONF_FIRMWARE_SHA256 = "firmware_sha256"

DEFAULT_TTL = 5
DEFAULT_SCAN_TIMEOUT = 12.0
DEFAULT_CONNECT_TIMEOUT = 12.0
DEFAULT_UPDATE_INTERVAL = 90
DEFAULT_DFU_TIMEOUT = 120.0

# A Bluetooth-proxied Mesh Proxy connection can fail transiently (weak RSSI,
# a proxy's connection slots briefly full, a mid-air collision with other
# BLE traffic); retry the whole connect-and-operate sequence a few times
# before surfacing an error, see coordinator.SteinelMeshHub._async_retry_mesh.
MESH_RETRY_ATTEMPTS = 3
MESH_RETRY_DELAY = 2.0

# How long a lamp's Mesh Proxy GATT connection is kept open after the last
# command/poll before it is closed again. Bluetooth-proxied connection setup
# (connect + service discovery) is by far the slowest part of any single
# command (often several seconds); reusing a recently-used connection for
# the next command/poll instead of reconnecting every time is what actually
# makes on/off and brightness changes feel responsive. Kept short so the
# proxy's limited connection slots (shared with other BLE devices) are
# freed again soon after use.
MESH_IDLE_DISCONNECT_SECONDS = 20.0
