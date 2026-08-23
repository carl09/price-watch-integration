"""Constants for the Price Watch integration."""

from datetime import timedelta

DOMAIN = "price_watch"

CONF_BASE_URL = "base_url"
CONF_API_TOKEN = "api_token"

DATA_CLIENTS = "clients"
DATA_COORDINATORS = "coordinators"
DATA_SENSOR_MANAGERS = "sensor_managers"
DATA_BINARY_SENSOR_MANAGERS = "binary_sensor_managers"
DEFAULT_TITLE = "Price Watch"
HEALTH_PATH = "/v1/health"
SUMMARY_PATH = "/v1/summary"
WATCHES_PATH = "/v1/watches"
EVENTS_PATH = "/v1/events"
CHECKS_PATH = "/v1/checks"
REQUEST_TIMEOUT_SECONDS = 10
REQUEST_ID_HEADER = "request-id"
SERVICE_CHECK_ALL = "check_all"
SERVICE_CHECK_WATCH = "check_watch"
SERVICE_SET_ENABLED = "set_enabled"
SERVICE_ADD_TO_SHOPPING_LIST = "add_to_shopping_list"
ATTR_WATCH_ID = "watch_id"
ATTR_ENABLED = "enabled"
SHOPPING_LIST_DOMAIN = "shopping_list"
SHOPPING_LIST_ADD_ITEM = "add_item"
SHOPPING_LIST_ITEM_NAME = "name"
# This refreshes Home Assistant's view of service state; it never checks retailers.
COORDINATOR_UPDATE_INTERVAL = timedelta(minutes=5)
