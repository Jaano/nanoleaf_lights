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

# Defaults
DEFAULT_PORT = 5683

# Coordinator
UPDATE_INTERVAL = timedelta(seconds=5)

# Storage
STORAGE_DIR = "nanoleaf_lights"
SCENES_FILENAME = "scenes.json"

# Light
MIN_COLOR_TEMP_KELVIN = 2700
MAX_COLOR_TEMP_KELVIN = 6500

