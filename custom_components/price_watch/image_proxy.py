"""Authenticated Home Assistant proxy for Price Watch product images."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .api import (
    PriceWatchApiClient,
    PriceWatchApiResponseError,
    PriceWatchAuthenticationError,
    PriceWatchInvalidResponseError,
    PriceWatchProductImage,
    PriceWatchTimeoutError,
    PriceWatchTransportError,
    PriceWatchWatch,
)
from .const import DATA_IMAGE_PROXIES, DOMAIN
from .coordinator import PriceWatchCoordinator
from .observability import log_failure, log_success


class PriceWatchImageUnavailable(Exception):
    """The requested watch has no current, proxyable product image."""


@dataclass(frozen=True)
class _CachedProductImage:
    """Bytes valid for one watch image capability and observation."""

    product_image_url: str
    observation_id: str | None
    image: PriceWatchProductImage


class PriceWatchImageProxy:
    """Fetch and cache only image capabilities present in coordinator data."""

    def __init__(
        self, coordinator: PriceWatchCoordinator, client: PriceWatchApiClient
    ) -> None:
        self._coordinator = coordinator
        self._client = client
        self._cache: dict[str, _CachedProductImage] = {}
        self._lock = asyncio.Lock()
        self._unsub = coordinator.async_add_listener(self._handle_coordinator_update)

    async def async_get_image(
        self, watch_id: str, *, request_id: str
    ) -> PriceWatchProductImage:
        """Return a current watch image without accepting a caller-provided URL."""
        watch = self._watch(watch_id)
        if watch is None or watch.product_image_url is None:
            raise PriceWatchImageUnavailable
        cached = self._cache.get(watch_id)
        if self._cache_matches(cached, watch):
            return cached.image

        async with self._lock:
            watch = self._watch(watch_id)
            if watch is None or watch.product_image_url is None:
                raise PriceWatchImageUnavailable
            cached = self._cache.get(watch_id)
            if self._cache_matches(cached, watch):
                return cached.image
            image = await self._client.async_get_product_image(
                watch.product_image_url, request_id=request_id
            )
            current_watch = self._watch(watch_id)
            if current_watch is None or not self._same_image_source(
                current_watch, watch
            ):
                raise PriceWatchImageUnavailable
            self._cache[watch_id] = _CachedProductImage(
                product_image_url=watch.product_image_url,
                observation_id=watch.current_observation_id,
                image=image,
            )
            return image

    def stop(self) -> None:
        """Release the coordinator listener and discard private image bytes."""
        self._unsub()
        self._cache.clear()

    def _handle_coordinator_update(self) -> None:
        """Discard bytes once their capability URL or observation is superseded."""
        for watch_id, cached in tuple(self._cache.items()):
            watch = self._watch(watch_id)
            if watch is None or not self._cache_matches(cached, watch):
                self._cache.pop(watch_id, None)

    def _watch(self, watch_id: str) -> PriceWatchWatch | None:
        """Return only a watch from the current coordinator snapshot."""
        data = self._coordinator.data
        if data is None:
            return None
        return next((watch for watch in data.watches if watch.id == watch_id), None)

    @staticmethod
    def _cache_matches(
        cached: _CachedProductImage | None, watch: PriceWatchWatch
    ) -> bool:
        return (
            cached is not None
            and cached.product_image_url == watch.product_image_url
            and cached.observation_id == watch.current_observation_id
        )

    @staticmethod
    def _same_image_source(
        current_watch: PriceWatchWatch, fetched_watch: PriceWatchWatch
    ) -> bool:
        return (
            current_watch.product_image_url == fetched_watch.product_image_url
            and current_watch.current_observation_id
            == fetched_watch.current_observation_id
        )


class PriceWatchImageProxyView(HomeAssistantView):
    """Serve cached Price Watch images through authenticated Home Assistant."""

    url = "/api/price_watch/image/{watch_id}"
    name = "api:price_watch:image"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Retain Home Assistant only; image sources remain coordinator-owned."""
        self._hass = hass

    async def get(self, request: web.Request, watch_id: str) -> web.Response:
        """Return one approved image or a generic HTTP failure."""
        proxy = self._image_proxy()
        if proxy is None:
            raise web.HTTPNotFound
        request_id = str(uuid4())
        try:
            image = await proxy.async_get_image(watch_id, request_id=request_id)
        except PriceWatchImageUnavailable:
            raise web.HTTPNotFound from None
        except (
            PriceWatchAuthenticationError,
            PriceWatchTimeoutError,
            PriceWatchTransportError,
            PriceWatchInvalidResponseError,
            PriceWatchApiResponseError,
            ValueError,
        ) as err:
            log_failure(
                "image_proxy",
                "/v1/watches/{watch_id}/image",
                err,
                watch_id=watch_id,
                request_id=request_id,
            )
            raise web.HTTPBadGateway from None
        log_success(
            "image_proxy",
            "/v1/watches/{watch_id}/image",
            watch_id=watch_id,
            request_id=request_id,
        )
        return web.Response(
            body=image.content,
            content_type=image.content_type,
            headers={"Cache-Control": "no-store"},
        )

    def _image_proxy(self) -> PriceWatchImageProxy | None:
        """Return the configured integration proxy without taking request input."""
        proxies = self._hass.data.get(DOMAIN, {}).get(DATA_IMAGE_PROXIES, {})
        return next(iter(proxies.values()), None)
