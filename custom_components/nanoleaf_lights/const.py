"""Constants for the Enhanced Nanoleaf Light integration."""

from datetime import timedelta

DOMAIN = "nanoleaf_lights"

# Config entry data keys
CONF_IP_ADDRESS = "ip_address"
CONF_PORT = "port"
CONF_MODEL = "model"
CONF_TOKEN = "token"
CONF_NAME = "name"
CONF_EUI64 = "eui64"

# Config entry option keys
CONF_DEVICE_LINK_MODE = "device_link_mode"
CONF_COMPANION_DEVICE_ID = "companion_device_id"
CONF_COMPANION_SERIAL_NUMBER = "companion_serial_number"

DEVICE_LINK_MODE_STANDALONE = "standalone"
DEVICE_LINK_MODE_AUTO = "auto"
DEVICE_LINK_MODE_COMPANION = "companion"

NATIVE_COMPANION_DOMAINS = {"matter", "homekit_controller"}

# Defaults
DEFAULT_PORT = 5653

# Coordinator
UPDATE_INTERVAL = timedelta(seconds=5)

# Storage
STORAGE_DIR = "nanoleaf_lights"
SCENES_FILENAME = "scenes.json"

# Light
MIN_COLOR_TEMP_KELVIN = 2700
MAX_COLOR_TEMP_KELVIN = 6500
