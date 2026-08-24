"""Price Watch summary sensor."""

from __future__ import annotations

from decimal import Decimal

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .api import PriceWatchEvent, PriceWatchWatch
from .const import DATA_COORDINATORS, DATA_SENSOR_MANAGERS, DOMAIN
from .coordinator import PriceWatchCoordinator

_TRUSTED_FAILURE_ERROR_CODES = frozenset(
    {
        "adapter_exception",
        "blocked",
        "error",
        "invalid_request",
        "not_found",
        "rate_limited",
        "unsupported",
    }
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add coordinator-backed Price Watch sensors from the initial snapshot."""
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    manager = PriceWatchSensorManager(coordinator, async_add_entities)
    hass.data[DOMAIN].setdefault(DATA_SENSOR_MANAGERS, {})[entry.entry_id] = manager
    async_add_entities(
        [
            PriceWatchSummarySensor(coordinator, entry),
            PriceWatchLatestTargetEventSensor(coordinator, entry),
            PriceWatchLatestFailureEventSensor(coordinator, entry),
            *manager.add_new_watch_sensors(),
        ]
    )


class PriceWatchSummarySensor(CoordinatorEntity[PriceWatchCoordinator], SensorEntity):
    """Expose service summary facts without recreating Price Watch rules."""

    _attr_name = "Price Watch Summary"

    def __init__(
        self, coordinator: PriceWatchCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_summary"

    @property
    def native_value(self) -> int | None:
        """Return the enabled watch count from the service summary."""
        return (
            self.coordinator.data.summary.enabled_watches
            if self.coordinator.data
            else None
        )

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Return coordinator-derived service facts only."""
        if self.coordinator.data is None:
            return None
        summary = self.coordinator.data.summary
        return {
            "target_matches": summary.target_matches,
            "target_matching_watch_ids": list(summary.target_matching_watch_ids),
            "stale": summary.stale,
            "failed": summary.failed,
            "latest_check_at": summary.latest_check_at,
            "last_successful_coordinator_refresh": self.coordinator.last_successful_refresh_at,
        }


class PriceWatchLatestTargetEventSensor(
    CoordinatorEntity[PriceWatchCoordinator], SensorEntity
):
    """Expose the latest immutable target event supplied by Price Watch."""

    _attr_name = "Price Watch Latest Target Event"

    def __init__(
        self, coordinator: PriceWatchCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_latest_target_event"

    def _event(self) -> PriceWatchEvent | None:
        """Select the latest service-produced target event without deriving one."""
        if self.coordinator.data is None:
            return None
        return max(
            (
                event
                for event in self.coordinator.data.events
                if event.type == "target_reached"
            ),
            key=lambda event: event.occurred_at,
            default=None,
        )

    @property
    def native_value(self) -> str:
        """Return the immutable event ID, suitable for automation triggers."""
        event = self._event()
        return event.id if event is not None else "none"

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose only the event fields safe for Home Assistant automations."""
        event = self._event()
        if event is None:
            return None
        attributes: dict[str, object] = {
            "watch_id": event.watch_id,
            "occurred_at": event.occurred_at,
            "deduplication_key": event.deduplication_key,
            "event_type": event.type,
        }
        target_price_cents = event.data.get("target_price_cents")
        if (
            isinstance(target_price_cents, int)
            and not isinstance(target_price_cents, bool)
            and target_price_cents >= 0
        ):
            attributes["target_price_cents"] = target_price_cents
        return attributes


class PriceWatchLatestFailureEventSensor(
    CoordinatorEntity[PriceWatchCoordinator], SensorEntity
):
    """Expose the latest immutable service-produced check-failure event."""

    _attr_name = "Price Watch Latest Failure Event"

    def __init__(
        self, coordinator: PriceWatchCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_latest_failure_event"

    def _event(self) -> PriceWatchEvent | None:
        """Select a service event without deriving monitoring failures locally."""
        if self.coordinator.data is None:
            return None
        return max(
            (
                event
                for event in self.coordinator.data.events
                if event.type == "check_failed"
            ),
            key=lambda event: (event.occurred_at, event.id),
            default=None,
        )

    @property
    def native_value(self) -> str:
        """Return the immutable failure event ID for notification automations."""
        event = self._event()
        return event.id if event is not None else "none"

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose only allow-listed immutable failure-event details."""
        event = self._event()
        if event is None:
            return None
        attributes: dict[str, object] = {
            "watch_id": event.watch_id,
            "occurred_at": event.occurred_at,
            "deduplication_key": event.deduplication_key,
            "event_type": event.type,
        }
        error_code = event.data.get("error_code")
        if (
            isinstance(error_code, str)
            and error_code in _TRUSTED_FAILURE_ERROR_CODES
        ):
            attributes["error_code"] = error_code
        return attributes


class PriceWatchWatchEntity(CoordinatorEntity[PriceWatchCoordinator]):
    """Share stable device identity and coordinator lookup across watch entities."""

    def __init__(self, coordinator: PriceWatchCoordinator, watch: PriceWatchWatch) -> None:
        """Capture immutable entity/device identity from the initial snapshot."""
        super().__init__(coordinator)
        self._watch_id = watch.id
        self._initial_title = watch.title
        self._initial_variant_label = (
            watch.current_observation.selected_variant_label
            if watch.current_observation is not None
            else None
        )

    def _watch(self) -> PriceWatchWatch | None:
        """Return this watch from the latest shared coordinator snapshot."""
        if self.coordinator.data is None:
            return None
        return next(
            (
                watch
                for watch in self.coordinator.data.watches
                if watch.id == self._watch_id
            ),
            None,
        )

    def _set_legacy_entity_id(self, platform: str, legacy_name: str) -> None:
        """Keep generated IDs from the original per-watch entity names."""
        self.entity_id = f"{platform}.{slugify(legacy_name)}"

    @property
    def available(self) -> bool:
        """Mark removed watches unavailable without removing their registry entry."""
        return super().available and self._watch() is not None

    @property
    def device_info(self) -> DeviceInfo:
        """Group watch entities by the immutable Price Watch ID."""
        watch = self._watch()
        title = watch.title if watch is not None else self._initial_title
        observation = watch.current_observation if watch is not None else None
        variant_label = (
            observation.selected_variant_label
            if observation is not None
            else self._initial_variant_label
        )
        return DeviceInfo(
            identifiers={(DOMAIN, self._watch_id)},
            name=f"{title} ({variant_label})" if variant_label else title,
            manufacturer="Price Watch",
            model=watch.retailer_id if watch is not None else None,
            configuration_url=watch.product_url if watch is not None else None,
        )


class PriceWatchWatchSensor(PriceWatchWatchEntity, SensorEntity):
    """Expose the existing current-price sensor for a single watch."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "AUD"

    def __init__(self, coordinator: PriceWatchCoordinator, watch: PriceWatchWatch) -> None:
        super().__init__(coordinator, watch)
        self._attr_unique_id = f"watch_{watch.id}"
        label = (
            watch.current_observation.selected_variant_label
            if watch.current_observation is not None
            else None
        )
        self._attr_name = "Current price"
        legacy_name = (
            f"{watch.title} ({label})" if label else watch.title
        )
        self._set_legacy_entity_id("sensor", legacy_name)

    @property
    def native_value(self) -> Decimal | None:
        """Return an available selected-variant price in AUD."""
        watch = self._watch()
        observation = watch.current_observation if watch is not None else None
        if (
            observation is None
            or observation.status != "available"
            or observation.price_cents is None
        ):
            return None
        return Decimal(observation.price_cents) / Decimal(100)

    @property
    def entity_picture(self) -> str | None:
        """Use only the validated local Price Watch image endpoint."""
        watch = self._watch()
        return watch.product_image_url if watch is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Return safe current facts without requesting additional watch data."""
        watch = self._watch()
        if watch is None:
            return None
        observation = watch.current_observation
        attributes: dict[str, object] = {
            "watch_id": watch.id,
            "retailer_id": watch.retailer_id,
            "product_url": watch.product_url,
            "retailer_variant_id": watch.variant.retailer_variant_id,
            "selected_variant_label": (
                observation.selected_variant_label if observation is not None else None
            ),
            "target_price_cents": watch.target_price_cents,
            "current_status": observation.status if observation is not None else None,
            "current_observation_timestamp": (
                observation.checked_at if observation is not None else None
            ),
            "enabled": watch.enabled,
        }
        if observation is not None and observation.compare_at_price_cents is not None:
            attributes["compare_at_price_cents"] = observation.compare_at_price_cents
        if observation is not None and observation.error_code is not None:
            attributes["error_code"] = observation.error_code
        return attributes


class PriceWatchTargetPriceSensor(PriceWatchWatchEntity, SensorEntity):
    """Expose a meaningful configured target price for one watch."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "AUD"

    def __init__(self, coordinator: PriceWatchCoordinator, watch: PriceWatchWatch) -> None:
        """Set a stable target-price entity identity."""
        super().__init__(coordinator, watch)
        self._attr_unique_id = f"watch_{watch.id}_target_price"
        self._attr_name = "Target price"
        self._set_legacy_entity_id("sensor", f"{watch.title} Target Price")

    @property
    def native_value(self) -> Decimal | None:
        """Return the configured target in AUD, if one exists."""
        watch = self._watch()
        if watch is None or watch.target_price_cents is None:
            return None
        return Decimal(watch.target_price_cents) / Decimal(100)


class PriceWatchStatusSensor(PriceWatchWatchEntity, SensorEntity):
    """Expose the current service-produced observation status."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PriceWatchCoordinator, watch: PriceWatchWatch) -> None:
        """Set a stable status entity identity."""
        super().__init__(coordinator, watch)
        self._attr_unique_id = f"watch_{watch.id}_status"
        self._attr_name = "Status"
        self._set_legacy_entity_id("sensor", f"{watch.title} Status")

    @property
    def native_value(self) -> str | None:
        """Return the authoritative selected-variant observation status."""
        watch = self._watch()
        if watch is None or watch.current_observation is None:
            return None
        return watch.current_observation.status


class PriceWatchLastCheckedSensor(PriceWatchWatchEntity, SensorEntity):
    """Expose the current observation timestamp without another API call."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_has_entity_name = True

    def __init__(self, coordinator: PriceWatchCoordinator, watch: PriceWatchWatch) -> None:
        """Set a stable last-observation entity identity."""
        super().__init__(coordinator, watch)
        self._attr_unique_id = f"watch_{watch.id}_last_checked"
        self._attr_name = "Last checked"
        self._set_legacy_entity_id("sensor", f"{watch.title} Last Checked")

    @property
    def native_value(self):
        """Return the service observation time as a Home Assistant timestamp."""
        watch = self._watch()
        observation = watch.current_observation if watch is not None else None
        if observation is None:
            return None
        return dt_util.parse_datetime(observation.checked_at)


class PriceWatchSensorManager:
    """Create one persistent entity for each watch discovered by the coordinator."""

    def __init__(
        self,
        coordinator: PriceWatchCoordinator,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        self._coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._watch_ids: set[str] = set()
        self._unsub = coordinator.async_add_listener(
            self._handle_coordinator_update
        )

    def add_new_watch_sensors(self) -> list[SensorEntity]:
        """Return sensors for watches not previously seen by this entry."""
        if self._coordinator.data is None:
            return []
        new_watches = [
            watch
            for watch in self._coordinator.data.watches
            if watch.id not in self._watch_ids
        ]
        self._watch_ids.update(watch.id for watch in new_watches)
        entities: list[SensorEntity] = []
        for watch in new_watches:
            entities.extend(
                [
                    PriceWatchWatchSensor(self._coordinator, watch),
                    PriceWatchTargetPriceSensor(self._coordinator, watch),
                    PriceWatchStatusSensor(self._coordinator, watch),
                    PriceWatchLastCheckedSensor(self._coordinator, watch),
                ]
            )
        return entities

    def _handle_coordinator_update(self) -> None:
        """Register entities introduced by a later shared coordinator snapshot."""
        sensors = self.add_new_watch_sensors()
        if sensors:
            self._async_add_entities(sensors)

    def stop(self) -> None:
        """Unregister the coordinator callback during config-entry unload."""
        self._unsub()
