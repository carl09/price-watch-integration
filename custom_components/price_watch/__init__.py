"""Price Watch Home Assistant integration bootstrap."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PriceWatchApiClient
from .const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    DATA_BINARY_SENSOR_MANAGERS,
    DATA_CLIENTS,
    DATA_COORDINATORS,
    DATA_IMAGE_ENTITY_IDS,
    DATA_IMAGE_MANAGERS,
    DATA_IMAGE_PROXIES,
    DATA_NUMBER_MANAGERS,
    DATA_WATCH_SWITCH_MANAGERS,
    DATA_RETAILER_BUTTON_MANAGERS,
    DATA_RETAILER_SELECT_MANAGERS,
    DATA_RETAILER_SENSOR_MANAGERS,
    DATA_RETAILER_SWITCH_MANAGERS,
    DATA_SENSOR_MANAGERS,
    DOMAIN,
)
from .coordinator import PriceWatchCoordinator
from .image_proxy import PriceWatchImageCache
from .services import async_register_services, async_unregister_services

#: F10 Phase 7a adds retailer select/switch/button platforms alongside the
#: existing watch/summary sensor, binary_sensor and image platforms.
_PLATFORMS = [
    "sensor",
    "binary_sensor",
    "image",
    "number",
    "select",
    "switch",
    "button",
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Price Watch runtime data for a config entry."""
    client = PriceWatchApiClient(
        entry.data[CONF_BASE_URL],
        entry.data[CONF_API_TOKEN],
        async_get_clientsession(hass),
    )
    coordinator = PriceWatchCoordinator(hass, entry, client)
    runtime_data = hass.data.setdefault(DOMAIN, {})
    runtime_data.setdefault(DATA_CLIENTS, {})[entry.entry_id] = client
    runtime_data.setdefault(DATA_COORDINATORS, {})[entry.entry_id] = coordinator
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        _cleanup_entry_runtime_data(hass, entry)
        raise
    except ConfigEntryNotReady:
        _cleanup_entry_runtime_data(hass, entry)
        raise
    runtime_data.setdefault(DATA_IMAGE_PROXIES, {})[entry.entry_id] = (
        PriceWatchImageCache(hass, coordinator, client)
    )
    runtime_data.setdefault(DATA_IMAGE_ENTITY_IDS, {})[entry.entry_id] = {}
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove Price Watch runtime data for an unloaded config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, _PLATFORMS):
        return False
    _cleanup_entry_runtime_data(hass, entry)
    return True


def _cleanup_entry_runtime_data(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove runtime state without assuming entity platforms were loaded."""
    domain_data = hass.data.get(DOMAIN)
    if domain_data is None:
        return
    clients = domain_data.get(DATA_CLIENTS)
    coordinators = domain_data.get(DATA_COORDINATORS)
    image_proxies = domain_data.get(DATA_IMAGE_PROXIES)
    image_entity_ids = domain_data.get(DATA_IMAGE_ENTITY_IDS)
    image_managers = domain_data.get(DATA_IMAGE_MANAGERS)
    sensor_managers = domain_data.get(DATA_SENSOR_MANAGERS)
    binary_sensor_managers = domain_data.get(DATA_BINARY_SENSOR_MANAGERS)
    retailer_sensor_managers = domain_data.get(DATA_RETAILER_SENSOR_MANAGERS)
    retailer_select_managers = domain_data.get(DATA_RETAILER_SELECT_MANAGERS)
    retailer_switch_managers = domain_data.get(DATA_RETAILER_SWITCH_MANAGERS)
    number_managers = domain_data.get(DATA_NUMBER_MANAGERS)
    watch_switch_managers = domain_data.get(DATA_WATCH_SWITCH_MANAGERS)
    retailer_button_managers = domain_data.get(DATA_RETAILER_BUTTON_MANAGERS)
    if clients is not None:
        clients.pop(entry.entry_id, None)
    if coordinators is not None:
        coordinators.pop(entry.entry_id, None)
    if image_proxies is not None:
        image_proxy = image_proxies.pop(entry.entry_id, None)
        if image_proxy is not None:
            image_proxy.stop()
    if image_entity_ids is not None:
        image_entity_ids.pop(entry.entry_id, None)
    if image_managers is not None:
        image_manager = image_managers.pop(entry.entry_id, None)
        if image_manager is not None:
            image_manager.stop()
    if sensor_managers is not None:
        manager = sensor_managers.pop(entry.entry_id, None)
        if manager is not None:
            manager.stop()
    if binary_sensor_managers is not None:
        manager = binary_sensor_managers.pop(entry.entry_id, None)
        if manager is not None:
            manager.stop()
    if number_managers is not None:
        manager = number_managers.pop(entry.entry_id, None)
        if manager is not None:
            manager.stop()
    if watch_switch_managers is not None:
        manager = watch_switch_managers.pop(entry.entry_id, None)
        if manager is not None:
            manager.stop()
    for retailer_managers in (
        retailer_sensor_managers,
        retailer_select_managers,
        retailer_switch_managers,
        retailer_button_managers,
    ):
        if retailer_managers is not None:
            manager = retailer_managers.pop(entry.entry_id, None)
            if manager is not None:
                manager.stop()
    if not domain_data.get(DATA_CLIENTS):
        async_unregister_services(hass)
    if (
        not domain_data.get(DATA_CLIENTS)
        and not domain_data.get(DATA_COORDINATORS)
        and not domain_data.get(DATA_IMAGE_PROXIES)
        and not domain_data.get(DATA_IMAGE_ENTITY_IDS)
        and not domain_data.get(DATA_IMAGE_MANAGERS)
        and not domain_data.get(DATA_NUMBER_MANAGERS)
        and not domain_data.get(DATA_WATCH_SWITCH_MANAGERS)
        and not domain_data.get(DATA_SENSOR_MANAGERS)
        and not domain_data.get(DATA_BINARY_SENSOR_MANAGERS)
        and not domain_data.get(DATA_RETAILER_SENSOR_MANAGERS)
        and not domain_data.get(DATA_RETAILER_SELECT_MANAGERS)
        and not domain_data.get(DATA_RETAILER_SWITCH_MANAGERS)
        and not domain_data.get(DATA_RETAILER_BUTTON_MANAGERS)
    ):
        hass.data.pop(DOMAIN)
