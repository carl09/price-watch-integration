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
    DATA_IMAGE_PROXIES,
    DATA_IMAGE_PROXY_VIEW,
    DATA_SENSOR_MANAGERS,
    DOMAIN,
)
from .coordinator import PriceWatchCoordinator
from .image_proxy import PriceWatchImageProxy, PriceWatchImageProxyView
from .services import async_register_services, async_unregister_services


async def async_setup(hass: HomeAssistant, config: dict[str, object]) -> bool:
    """Register the authenticated image proxy once for this HA instance."""
    _register_image_proxy_view(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Price Watch runtime data for a config entry."""
    _register_image_proxy_view(hass)
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
        PriceWatchImageProxy(coordinator, client)
    )
    await hass.config_entries.async_forward_entry_setups(
        entry, ["sensor", "binary_sensor"]
    )
    async_register_services(hass)
    return True


def _register_image_proxy_view(hass: HomeAssistant) -> None:
    """Register the HTTP view once after Home Assistant's HTTP server is ready."""
    runtime_data = hass.data.setdefault(DOMAIN, {})
    if DATA_IMAGE_PROXY_VIEW in runtime_data or hass.http is None:
        return
    view = PriceWatchImageProxyView(hass)
    hass.http.register_view(view)
    runtime_data[DATA_IMAGE_PROXY_VIEW] = view


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove Price Watch runtime data for an unloaded config entry."""
    if not await hass.config_entries.async_unload_platforms(
        entry, ["sensor", "binary_sensor"]
    ):
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
    sensor_managers = domain_data.get(DATA_SENSOR_MANAGERS)
    binary_sensor_managers = domain_data.get(DATA_BINARY_SENSOR_MANAGERS)
    if clients is not None:
        clients.pop(entry.entry_id, None)
    if coordinators is not None:
        coordinators.pop(entry.entry_id, None)
    if image_proxies is not None:
        image_proxy = image_proxies.pop(entry.entry_id, None)
        if image_proxy is not None:
            image_proxy.stop()
    if sensor_managers is not None:
        manager = sensor_managers.pop(entry.entry_id, None)
        if manager is not None:
            manager.stop()
    if binary_sensor_managers is not None:
        manager = binary_sensor_managers.pop(entry.entry_id, None)
        if manager is not None:
            manager.stop()
    if not domain_data.get(DATA_CLIENTS):
        async_unregister_services(hass)
    if (
        not domain_data.get(DATA_CLIENTS)
        and not domain_data.get(DATA_COORDINATORS)
        and not domain_data.get(DATA_IMAGE_PROXIES)
        and not domain_data.get(DATA_SENSOR_MANAGERS)
        and not domain_data.get(DATA_BINARY_SENSOR_MANAGERS)
        and not domain_data.get(DATA_IMAGE_PROXY_VIEW)
    ):
        hass.data.pop(DOMAIN)
