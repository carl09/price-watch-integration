"""Coordinated Price Watch service-state refreshes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    PriceWatchApiClient,
    PriceWatchApiResponseError,
    PriceWatchAuthenticationError,
    PriceWatchEvent,
    PriceWatchInvalidResponseError,
    PriceWatchSummary,
    PriceWatchTimeoutError,
    PriceWatchTransportError,
    PriceWatchWatch,
)
from .const import COORDINATOR_UPDATE_INTERVAL, DOMAIN
from .observability import log_failure, log_success

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceWatchCoordinatorData:
    """Immutable service facts shared by future Price Watch entities."""

    summary: PriceWatchSummary
    watches: tuple[PriceWatchWatch, ...]
    events: tuple[PriceWatchEvent, ...]


class PriceWatchCoordinator(DataUpdateCoordinator[PriceWatchCoordinatorData]):
    """Refresh Price Watch API facts without triggering retailer checks."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: PriceWatchApiClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=COORDINATOR_UPDATE_INTERVAL,
            always_update=False,
            config_entry=config_entry,
        )
        self._client = client
        self.last_successful_refresh_at: datetime | None = None

    async def _async_update_data(self) -> PriceWatchCoordinatorData:
        """Fetch all read-only service facts for Home Assistant."""
        operation = "coordinator_refresh"
        route = "/v1/summary,/v1/watches,/v1/events"
        try:
            summary = await self._client.async_get_summary()
            watches = await self._client.async_get_watches()
            events = await self._client.async_get_events()
        except PriceWatchAuthenticationError as err:
            log_failure(operation, route, err)
            raise ConfigEntryAuthFailed from err
        except (
            PriceWatchTimeoutError,
            PriceWatchTransportError,
            PriceWatchInvalidResponseError,
            PriceWatchApiResponseError,
        ) as err:
            log_failure(operation, route, err)
            raise UpdateFailed("Unable to refresh Price Watch service data") from err
        data = PriceWatchCoordinatorData(
            summary=summary,
            watches=watches,
            events=events,
        )
        self.last_successful_refresh_at = dt_util.utcnow()
        log_success(operation, route)
        return data
