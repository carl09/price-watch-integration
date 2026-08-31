"""Home Assistant image entities for Price Watch products."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.util import dt as dt_util

from .api import (
    PriceWatchApiResponseError,
    PriceWatchAuthenticationError,
    PriceWatchInvalidResponseError,
    PriceWatchTimeoutError,
    PriceWatchTransportError,
    PriceWatchWatch,
)
from .const import (
    DATA_IMAGE_MANAGERS,
    DATA_IMAGE_PROXIES,
    DATA_COORDINATORS,
    DATA_IMAGE_ENTITY_IDS,
    DOMAIN,
    SIGNAL_IMAGE_ENTITY_REGISTERED,
    SIGNAL_IMAGE_RELOAD,
)
from .coordinator import PriceWatchCoordinator
from .image_proxy import PriceWatchImageCache, PriceWatchImageUnavailable
from .observability import log_failure, log_success
from .sensor import PriceWatchWatchEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add coordinator-backed product image entities."""
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    image_cache = hass.data[DOMAIN][DATA_IMAGE_PROXIES][entry.entry_id]
    manager = PriceWatchImageManager(
        hass, coordinator, image_cache, async_add_entities
    )
    hass.data[DOMAIN].setdefault(DATA_IMAGE_MANAGERS, {})[entry.entry_id] = manager
    async_add_entities(manager.add_new_watch_images())


class PriceWatchWatchImage(PriceWatchWatchEntity, ImageEntity):
    """Expose one watch product image through Home Assistant's image proxy."""

    _attr_has_entity_name = True
    _attr_name = "Product image"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: PriceWatchCoordinator,
        image_cache: PriceWatchImageCache,
        watch: PriceWatchWatch,
    ) -> None:
        """Set stable watch identity and initialise HA-managed image tokens."""
        PriceWatchWatchEntity.__init__(self, coordinator, watch)
        ImageEntity.__init__(self, hass)
        self._image_cache = image_cache
        self._attr_unique_id = f"watch_{watch.id}_product_image"
        self._image_source = self._source(watch)
        self._attr_image_last_updated = dt_util.utcnow()
        self._image_status = "unavailable"

    @property
    def available(self) -> bool:
        """Only serve an image for a watch with a current image capability."""
        watch = self._watch()
        return (
            super().available
            and watch is not None
            and watch.product_image_url is not None
        )

    @property
    def image_last_updated(self) -> datetime | None:
        """Return the last coordinator change affecting this image source."""
        return self._attr_image_last_updated

    @property
    def content_type(self) -> str:
        """Return the media type established by the guarded image fetch."""
        return self._attr_content_type

    async def async_added_to_hass(self) -> None:
        """Register this HA-managed image with its matching current-price sensor."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_IMAGE_RELOAD,
                self._handle_image_reload,
            )
        )
        image_ids = self.hass.data[DOMAIN][DATA_IMAGE_ENTITY_IDS][
            self.coordinator.entry_id
        ]
        image_ids[self._watch_id] = self.entity_id
        async_dispatcher_send(
            self.hass,
            SIGNAL_IMAGE_ENTITY_REGISTERED,
            self.coordinator.entry_id,
            self._watch_id,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Clear the association when HA removes or disables this image entity."""
        image_ids = (
            self.hass.data.get(DOMAIN, {})
            .get(DATA_IMAGE_ENTITY_IDS, {})
            .get(self.coordinator.entry_id, {})
        )
        if image_ids.get(self._watch_id) == self.entity_id:
            image_ids.pop(self._watch_id)
            async_dispatcher_send(
                self.hass,
                SIGNAL_IMAGE_ENTITY_REGISTERED,
                self.coordinator.entry_id,
                self._watch_id,
            )
        await super().async_will_remove_from_hass()

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose only bounded image health metadata."""
        return {
            "image_status": self._image_status,
            "image_last_updated": self._attr_image_last_updated.isoformat()
            if self._attr_image_last_updated is not None
            else None,
        }

    @callback
    def _handle_image_reload(self, entry_id: str, watch_id: str) -> None:
        """Invalidate the HA image proxy state for this watch only."""
        if entry_id != self.coordinator.entry_id or watch_id != self._watch_id:
            return
        self._attr_image_last_updated = dt_util.utcnow()
        self._image_status = "refresh_requested"
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        """Fetch guarded product-image bytes for HA's standard image endpoint."""
        request_id = str(uuid4())
        try:
            image = await self._image_cache.async_get_image(
                self._watch_id, request_id=request_id
            )
        except PriceWatchImageUnavailable:
            self._image_status = "unavailable"
            return None
        except (
            PriceWatchAuthenticationError,
            PriceWatchTimeoutError,
            PriceWatchTransportError,
            PriceWatchInvalidResponseError,
            PriceWatchApiResponseError,
            ValueError,
        ) as err:
            self._image_status = "unavailable"
            log_failure(
                "image_entity",
                "/v1/watches/{watch_id}/image",
                err,
                watch_id=self._watch_id,
                request_id=request_id,
            )
            return None
        self._attr_content_type = image.content_type
        self._image_status = "available"
        log_success(
            "image_entity",
            "/v1/watches/{watch_id}/image",
            watch_id=self._watch_id,
            request_id=request_id,
        )
        return image.content

    def _handle_coordinator_update(self) -> None:
        """Refresh HA's image version when the cached source is superseded."""
        watch = self._watch()
        source = self._source(watch) if watch is not None else None
        if source != self._image_source:
            self._image_source = source
            self._attr_image_last_updated = dt_util.utcnow()
        super()._handle_coordinator_update()

    @staticmethod
    def _source(watch: PriceWatchWatch) -> tuple[str | None, str | None]:
        """Identify the cache key inputs that invalidate product-image bytes."""
        return (watch.product_image_url, watch.current_observation_id)


class PriceWatchImageManager:
    """Create one persistent product image entity for each discovered watch."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: PriceWatchCoordinator,
        image_cache: PriceWatchImageCache,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._image_cache = image_cache
        self._async_add_entities = async_add_entities
        self._watch_ids: set[str] = set()
        self._unsub = coordinator.async_add_listener(
            self._handle_coordinator_update
        )

    def add_new_watch_images(self) -> list[PriceWatchWatchImage]:
        """Return image entities for watches not previously seen by this entry."""
        if self._coordinator.data is None:
            return []
        new_watches = [
            watch
            for watch in self._coordinator.data.watches
            if watch.id not in self._watch_ids
        ]
        self._watch_ids.update(watch.id for watch in new_watches)
        return [
            PriceWatchWatchImage(
                self._hass, self._coordinator, self._image_cache, watch
            )
            for watch in new_watches
        ]

    def _handle_coordinator_update(self) -> None:
        """Register images introduced by a later coordinator snapshot."""
        images = self.add_new_watch_images()
        if images:
            self._async_add_entities(images)

    def stop(self) -> None:
        """Unregister the coordinator callback during config-entry unload."""
        self._unsub()
