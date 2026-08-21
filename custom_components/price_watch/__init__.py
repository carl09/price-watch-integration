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
    DATA_CLIENTS,
    DATA_COORDINATORS,
    DATA_SENSOR_MANAGERS,
    DOMAIN,
)
from .coordinator import PriceWatchCoordinator
from .services import async_register_services, async_unregister_services


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
        await async_unload_entry(hass, entry)
        raise
    except ConfigEntryNotReady:
        await async_unload_entry(hass, entry)
        raise
    await hass.config_entries.async_forward_entry_setups(
        entry, ["sensor", "binary_sensor"]
    )
    async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove Price Watch runtime data for an unloaded config entry."""
    if not await hass.config_entries.async_unload_platforms(
        entry, ["sensor", "binary_sensor"]
    ):
        return False
    domain_data = hass.data.get(DOMAIN)
    if domain_data is None:
        return True
    clients = domain_data.get(DATA_CLIENTS)
    coordinators = domain_data.get(DATA_COORDINATORS)
    sensor_managers = domain_data.get(DATA_SENSOR_MANAGERS)
    if clients is not None:
        clients.pop(entry.entry_id, None)
    if coordinators is not None:
        coordinators.pop(entry.entry_id, None)
    if sensor_managers is not None:
        manager = sensor_managers.pop(entry.entry_id, None)
        if manager is not None:
            manager.stop()
    if not domain_data.get(DATA_CLIENTS):
        async_unregister_services(hass)
    if (
        not domain_data.get(DATA_CLIENTS)
        and not domain_data.get(DATA_COORDINATORS)
        and not domain_data.get(DATA_SENSOR_MANAGERS)
    ):
        hass.data.pop(DOMAIN)
    return True
