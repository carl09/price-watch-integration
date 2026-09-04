"""Price Watch retailer diagnostic-test and reset button entities."""

from __future__ import annotations

from uuid import uuid4

from homeassistant.components.button import ButtonEntity
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
    DATA_RETAILER_BUTTON_MANAGERS,
    DOMAIN,
)
from .coordinator import PriceWatchCoordinator
from .observability import log_failure, log_success
from .sensor import PriceWatchRetailerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add coordinator-backed retailer diagnostic-test and reset buttons."""
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    manager = PriceWatchRetailerButtonManager(coordinator, async_add_entities)
    hass.data[DOMAIN].setdefault(DATA_RETAILER_BUTTON_MANAGERS, {})[
        entry.entry_id
    ] = manager
    async_add_entities(manager.add_new_retailer_buttons())


class PriceWatchRetailerTestButton(PriceWatchRetailerEntity, ButtonEntity):
    """Run a controlled diagnostic test with no target-event side effect.

    This only calls the authenticated Phase 6a test action. It never
    persists a watch observation, target event or notification; the service
    enforces that boundary independently of this integration.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: PriceWatchCoordinator, retailer_id: str
    ) -> None:
        """Set a stable test-button entity identity."""
        super().__init__(coordinator, retailer_id)
        self._attr_unique_id = f"retailer_{retailer_id}_test"
        self._attr_name = "Test retailer"

    async def async_press(self) -> None:
        """Request the controlled diagnostic test through the API only."""
        client: PriceWatchApiClient = self.hass.data[DOMAIN][DATA_CLIENTS][
            self.coordinator.entry_id
        ]
        route = f"/v1/retailers/{self._retailer_id}/test"
        request_id = str(uuid4())
        try:
            await client.async_test_retailer(
                self._retailer_id,
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
            log_failure("retailer_test", route, err, request_id=request_id)
            raise HomeAssistantError(
                "Price Watch retailer diagnostic test failed"
            ) from err
        await self.coordinator.async_request_refresh()
        log_success("retailer_test", route, request_id=request_id)


class PriceWatchRetailerResetButton(PriceWatchRetailerEntity, ButtonEntity):
    """Clear only durable cooldown/escalation state for one retailer.

    This only calls the authenticated Phase 6a reset action. It never
    touches watch observations, events or the operator's preferred
    strategy; the service enforces that boundary independently of this
    integration.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: PriceWatchCoordinator, retailer_id: str
    ) -> None:
        """Set a stable reset-button entity identity."""
        super().__init__(coordinator, retailer_id)
        self._attr_unique_id = f"retailer_{retailer_id}_reset"
        self._attr_name = "Reset acquisition state"

    async def async_press(self) -> None:
        """Request the durable operational-state reset through the API only."""
        client: PriceWatchApiClient = self.hass.data[DOMAIN][DATA_CLIENTS][
            self.coordinator.entry_id
        ]
        route = f"/v1/retailers/{self._retailer_id}/reset"
        request_id = str(uuid4())
        try:
            await client.async_reset_retailer(
                self._retailer_id,
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
            log_failure("retailer_reset", route, err, request_id=request_id)
            raise HomeAssistantError(
                "Price Watch retailer operational-state reset failed"
            ) from err
        await self.coordinator.async_request_refresh()
        log_success("retailer_reset", route, request_id=request_id)


class PriceWatchRetailerButtonManager:
    """Create test/reset buttons for each retailer discovered by the coordinator."""

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

    def add_new_retailer_buttons(self) -> list[ButtonEntity]:
        """Return buttons for retailers not previously seen by this entry."""
        if self._coordinator.data is None:
            return []
        new_ids = [
            retailer.retailer_id
            for retailer in self._coordinator.data.retailers
            if retailer.retailer_id not in self._retailer_ids
        ]
        self._retailer_ids.update(new_ids)
        entities: list[ButtonEntity] = []
        for retailer_id in new_ids:
            entities.extend(
                [
                    PriceWatchRetailerTestButton(self._coordinator, retailer_id),
                    PriceWatchRetailerResetButton(self._coordinator, retailer_id),
                ]
            )
        return entities

    def _handle_coordinator_update(self) -> None:
        """Register entities introduced by a later coordinator snapshot."""
        buttons = self.add_new_retailer_buttons()
        if buttons:
            self._async_add_entities(buttons)

    def stop(self) -> None:
        """Unregister the coordinator callback during config-entry unload."""
        self._unsub()
