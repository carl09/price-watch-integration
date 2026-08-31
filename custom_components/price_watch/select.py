"""Price Watch retailer preferred-strategy select entity."""

from __future__ import annotations

from uuid import uuid4

from homeassistant.components.select import SelectEntity
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
    DATA_RETAILER_SELECT_MANAGERS,
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
    """Add coordinator-backed retailer preferred-strategy selects."""
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    manager = PriceWatchRetailerSelectManager(coordinator, async_add_entities)
    hass.data[DOMAIN].setdefault(DATA_RETAILER_SELECT_MANAGERS, {})[
        entry.entry_id
    ] = manager
    async_add_entities(manager.add_new_retailer_selects())


class PriceWatchRetailerPreferredStrategySelect(
    PriceWatchRetailerEntity, SelectEntity
):
    """Set the operator's preferred retailer acquisition strategy.

    Options are always `auto` plus only the strategies this retailer's
    adapter actually supports; the authenticated service independently
    rejects any strategy it does not support for this retailer.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: PriceWatchCoordinator, retailer_id: str
    ) -> None:
        """Set a stable preferred-strategy entity identity."""
        super().__init__(coordinator, retailer_id)
        self._attr_unique_id = f"retailer_{retailer_id}_preferred_strategy"
        self._attr_name = "Preferred strategy"

    @property
    def options(self) -> list[str]:
        """Return `auto` plus only this retailer's supported strategies."""
        retailer = self._retailer()
        methods = retailer.acquisition_methods if retailer is not None else ()
        return ["auto", *methods]

    @property
    def current_option(self) -> str | None:
        """Return the operator's current preference reported by the service."""
        retailer = self._retailer()
        return retailer.preferred_strategy if retailer is not None else None

    async def async_select_option(self, option: str) -> None:
        """Set the preferred strategy through the authenticated API only."""
        client: PriceWatchApiClient = self.hass.data[DOMAIN][DATA_CLIENTS][
            self.coordinator.entry_id
        ]
        request_id = str(uuid4())
        try:
            await client.async_set_retailer_preferred_strategy(
                self._retailer_id,
                option,
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
                "retailer_set_preferred_strategy",
                _RETAILER_PATCH_ROUTE,
                err,
                request_id=request_id,
            )
            raise HomeAssistantError(
                "Price Watch could not set the preferred strategy"
            ) from err
        await self.coordinator.async_request_refresh()
        log_success(
            "retailer_set_preferred_strategy",
            _RETAILER_PATCH_ROUTE,
            request_id=request_id,
        )


class PriceWatchRetailerSelectManager:
    """Create a preferred-strategy select for each discovered retailer."""

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

    def add_new_retailer_selects(
        self,
    ) -> list[PriceWatchRetailerPreferredStrategySelect]:
        """Return selects for retailers not previously seen by this entry."""
        if self._coordinator.data is None:
            return []
        new_ids = [
            retailer.retailer_id
            for retailer in self._coordinator.data.retailers
            if retailer.retailer_id not in self._retailer_ids
        ]
        self._retailer_ids.update(new_ids)
        return [
            PriceWatchRetailerPreferredStrategySelect(self._coordinator, retailer_id)
            for retailer_id in new_ids
        ]

    def _handle_coordinator_update(self) -> None:
        """Register entities introduced by a later coordinator snapshot."""
        selects = self.add_new_retailer_selects()
        if selects:
            self._async_add_entities(selects)

    def stop(self) -> None:
        """Unregister the coordinator callback during config-entry unload."""
        self._unsub()
