"""Price Watch per-watch target-price number controls."""

from __future__ import annotations

import math
from decimal import Decimal
from uuid import uuid4

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import (
    PriceWatchApiClient,
    PriceWatchApiResponseError,
    PriceWatchAuthenticationError,
    PriceWatchInvalidResponseError,
    PriceWatchTimeoutError,
    PriceWatchTransportError,
    PriceWatchWatch,
)
from .const import DATA_CLIENTS, DATA_COORDINATORS, DATA_NUMBER_MANAGERS, DOMAIN
from .coordinator import PriceWatchCoordinator
from .observability import log_failure, log_success
from .sensor import PriceWatchWatchEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add one target-price number control for each discovered watch."""
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    manager = PriceWatchNumberManager(coordinator, hass, async_add_entities)
    hass.data.setdefault(DOMAIN, {}).setdefault(DATA_NUMBER_MANAGERS, {})[
        entry.entry_id
    ] = manager
    async_add_entities(manager.add_new_watch_numbers())


class PriceWatchTargetPriceNumber(PriceWatchWatchEntity, NumberEntity):
    """Set a watch target without triggering acquisition or alert evaluation."""

    _attr_device_class = NumberDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "AUD"
    _attr_native_min_value = 0
    _attr_native_step = 0.01
    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PriceWatchCoordinator,
        hass: HomeAssistant,
        watch: PriceWatchWatch,
    ) -> None:
        super().__init__(coordinator, watch)
        self._hass = hass
        self._attr_unique_id = f"watch_{watch.id}_target_price_control"
        self._attr_name = "Target price control"
        self._set_legacy_entity_id("number", f"{watch.title} Target Price Control")

    @property
    def native_value(self) -> float | None:
        watch = self._watch()
        if watch is None or watch.target_price_cents is None:
            return None
        return float(Decimal(watch.target_price_cents) / Decimal(100))

    async def async_set_native_value(self, value: float) -> None:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("target price must be a non-negative finite amount")
        cents = round(value * 100)
        if abs(value * 100 - cents) > 1e-6:
            raise ValueError("target price must resolve to whole AUD cents")
        watch_id = self._watch_id
        client: PriceWatchApiClient = self._hass.data[DOMAIN][DATA_CLIENTS][
            self.coordinator.entry_id
        ]
        request_id = str(uuid4())
        try:
            await client.async_set_target_price(
                watch_id, cents, request_id=request_id
            )
        except (
            PriceWatchAuthenticationError,
            PriceWatchTimeoutError,
            PriceWatchTransportError,
            PriceWatchInvalidResponseError,
            PriceWatchApiResponseError,
            ValueError,
        ) as err:
            log_failure(
                "set_target_price",
                "/v1/watches/{watch_id}",
                err,
                watch_id=watch_id,
                request_id=request_id,
            )
            raise
        await self.coordinator.async_request_refresh()
        log_success(
            "set_target_price",
            "/v1/watches/{watch_id}",
            watch_id=watch_id,
            request_id=request_id,
        )


class PriceWatchNumberManager:
    """Create stable target-price controls for dynamically discovered watches."""

    def __init__(
        self,
        coordinator: PriceWatchCoordinator,
        hass: HomeAssistant,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        self._coordinator = coordinator
        self._hass = hass
        self._async_add_entities = async_add_entities
        self._watch_ids: set[str] = set()
        self._unsub = coordinator.async_add_listener(self._handle_coordinator_update)

    def add_new_watch_numbers(self) -> list[PriceWatchTargetPriceNumber]:
        if self._coordinator.data is None:
            return []
        new_watches = [
            watch
            for watch in self._coordinator.data.watches
            if watch.id not in self._watch_ids
        ]
        self._watch_ids.update(watch.id for watch in new_watches)
        return [
            PriceWatchTargetPriceNumber(self._coordinator, self._hass, watch)
            for watch in new_watches
        ]

    def _handle_coordinator_update(self) -> None:
        numbers = self.add_new_watch_numbers()
        if numbers:
            self._async_add_entities(numbers)

    def stop(self) -> None:
        self._unsub()
