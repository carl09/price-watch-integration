"""Price Watch retailer enabled switch entity."""

from __future__ import annotations

from uuid import uuid4

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import (
    PriceWatchApiClient,
    PriceWatchApiResponseError,
    PriceWatchAuthenticationError,
    PriceWatchInvalidResponseError,
    PriceWatchTimeoutError,
    PriceWatchTransportError,
)
from .const import (
    DATA_CLIENTS,
    DATA_COORDINATORS,
    DATA_RETAILER_SWITCH_MANAGERS,
    DOMAIN,
)
from .coordinator import PriceWatchCoordinator
from .observability import log_failure, log_success
from .sensor import PriceWatchRetailerEntity

_RETAILER_PATCH_ROUTE = "/v1/retailers/{id}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add coordinator-backed retailer enabled switches."""
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    manager = PriceWatchRetailerSwitchManager(coordinator, async_add_entities)
    hass.data[DOMAIN].setdefault(DATA_RETAILER_SWITCH_MANAGERS, {})[
        entry.entry_id
    ] = manager
    async_add_entities(manager.add_new_retailer_switches())


class PriceWatchRetailerEnabledSwitch(PriceWatchRetailerEntity, SwitchEntity):
    """Enable/disable one retailer through the authenticated API only."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: PriceWatchCoordinator, retailer_id: str
    ) -> None:
        """Set a stable enabled-switch entity identity."""
        super().__init__(coordinator, retailer_id)
        self._attr_unique_id = f"retailer_{retailer_id}_enabled"
        self._attr_name = "Enabled"

    @property
    def is_on(self) -> bool | None:
        """Return the durable enabled state reported by the service."""
        retailer = self._retailer()
        return retailer.enabled if retailer is not None else None

    async def async_turn_on(self, **kwargs: object) -> None:
        """Enable this retailer through the authenticated API only."""
        await self._async_set_enabled(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        """Disable this retailer through the authenticated API only."""
        await self._async_set_enabled(False)

    async def _async_set_enabled(self, enabled: bool) -> None:
        """Set the durable enabled flag and request a coordinator refresh."""
        client: PriceWatchApiClient = self.hass.data[DOMAIN][DATA_CLIENTS][
            self.coordinator.entry_id
        ]
        request_id = str(uuid4())
        try:
            await client.async_set_retailer_enabled(
                self._retailer_id,
                enabled,
                idempotency_key=str(uuid4()),
                request_id=request_id,
            )
        except (
            PriceWatchAuthenticationError,
            PriceWatchTimeoutError,
            PriceWatchTransportError,
            PriceWatchInvalidResponseError,
            PriceWatchApiResponseError,
        ) as err:
            log_failure(
                "retailer_set_enabled",
                _RETAILER_PATCH_ROUTE,
                err,
                request_id=request_id,
            )
            raise HomeAssistantError(
                "Price Watch could not update the retailer enabled state"
            ) from err
        await self.coordinator.async_request_refresh()
        log_success(
            "retailer_set_enabled", _RETAILER_PATCH_ROUTE, request_id=request_id
        )


class PriceWatchRetailerSwitchManager:
    """Create an enabled switch for each retailer discovered by the coordinator."""

    def __init__(
        self,
        coordinator: PriceWatchCoordinator,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        """Subscribe once to shared coordinator updates."""
        self._coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._retailer_ids: set[str] = set()
        self._unsub = coordinator.async_add_listener(
            self._handle_coordinator_update
        )

    def add_new_retailer_switches(self) -> list[PriceWatchRetailerEnabledSwitch]:
        """Return switches for retailers not previously seen by this entry."""
        if self._coordinator.data is None:
            return []
        new_ids = [
            retailer.retailer_id
            for retailer in self._coordinator.data.retailers
            if retailer.retailer_id not in self._retailer_ids
        ]
        self._retailer_ids.update(new_ids)
        return [
            PriceWatchRetailerEnabledSwitch(self._coordinator, retailer_id)
            for retailer_id in new_ids
        ]

    def _handle_coordinator_update(self) -> None:
        """Register entities introduced by a later coordinator snapshot."""
        switches = self.add_new_retailer_switches()
        if switches:
            self._async_add_entities(switches)

    def stop(self) -> None:
        """Unregister the coordinator callback during config-entry unload."""
        self._unsub()
