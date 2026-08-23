"""Coordinated Price Watch service-state refreshes."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar
from uuid import uuid4

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
_T = TypeVar("_T")


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
        try:
            summary = await self._async_fetch(
                operation,
                "/v1/summary",
                lambda request_id: self._client.async_get_summary(
                    request_id=request_id
                ),
            )
            watches = await self._async_fetch(
                operation,
                "/v1/watches",
                lambda request_id: self._client.async_get_watches(
                    request_id=request_id
                ),
            )
            events = await self._async_fetch(
                operation,
                "/v1/events",
                lambda request_id: self._client.async_get_events(
                    request_id=request_id
                ),
            )
        except PriceWatchAuthenticationError as err:
            raise ConfigEntryAuthFailed from err
        except (
            PriceWatchTimeoutError,
            PriceWatchTransportError,
            PriceWatchInvalidResponseError,
            PriceWatchApiResponseError,
        ) as err:
            raise UpdateFailed("Unable to refresh Price Watch service data") from err
        data = PriceWatchCoordinatorData(
            summary=summary,
            watches=watches,
            events=events,
        )
        self.last_successful_refresh_at = dt_util.utcnow()
        return data

    async def _async_fetch(
        self,
        operation: str,
        route: str,
        fetch: Callable[[str], Awaitable[_T]],
    ) -> _T:
        """Fetch one route with a unique log-correlated request ID."""
        request_id = str(uuid4())
        try:
            data = await fetch(request_id)
        except (
            PriceWatchAuthenticationError,
            PriceWatchTimeoutError,
            PriceWatchTransportError,
            PriceWatchInvalidResponseError,
            PriceWatchApiResponseError,
        ) as err:
            log_failure(operation, route, err, request_id=request_id)
            raise
        log_success(operation, route, request_id=request_id)
        return data
