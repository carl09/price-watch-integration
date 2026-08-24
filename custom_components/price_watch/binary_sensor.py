"""Price Watch target-match binary sensor."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import PriceWatchWatch
from .const import DATA_BINARY_SENSOR_MANAGERS, DATA_COORDINATORS, DOMAIN
from .coordinator import PriceWatchCoordinator
from .sensor import PriceWatchWatchEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add aggregate and device-linked target-match binary sensors."""
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    manager = PriceWatchBinarySensorManager(coordinator, async_add_entities)
    hass.data[DOMAIN].setdefault(DATA_BINARY_SENSOR_MANAGERS, {})[
        entry.entry_id
    ] = manager
    async_add_entities(
        [
            PriceWatchTargetMatchBinarySensor(coordinator, entry),
            PriceWatchMonitorHealthBinarySensor(coordinator, entry),
            *manager.add_new_watch_binary_sensors(),
        ]
    )


class PriceWatchTargetMatchBinarySensor(
    CoordinatorEntity[PriceWatchCoordinator], BinarySensorEntity
):
    """Expose the service's current target-match state."""

    _attr_name = "Price Watch Target Match"

    def __init__(
        self, coordinator: PriceWatchCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_target_match"

    @property
    def is_on(self) -> bool | None:
        """Return whether the service reports any target-matching watches."""
        if self.coordinator.data is None:
            return None
        return bool(self.coordinator.data.summary.target_matching_watch_ids)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Return target facts supplied by the Price Watch service."""
        if self.coordinator.data is None:
            return None
        summary = self.coordinator.data.summary
        return {
            "target_matches": summary.target_matches,
            "target_matching_watch_ids": list(summary.target_matching_watch_ids),
            "latest_check_at": summary.latest_check_at,
        }


class PriceWatchMonitorHealthBinarySensor(
    CoordinatorEntity[PriceWatchCoordinator], BinarySensorEntity
):
    """Expose only service-reported monitoring health facts."""

    _attr_name = "Price Watch Monitor Health"

    def __init__(
        self, coordinator: PriceWatchCoordinator, entry: ConfigEntry
    ) -> None:
        """Set a stable aggregate monitor-health entity identity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_monitor_health"

    @property
    def is_on(self) -> bool | None:
        """Return healthy only when service stale and failed counts are zero."""
        if not self.available or self.coordinator.data is None:
            return None
        summary = self.coordinator.data.summary
        return summary.stale == 0 and summary.failed == 0

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose safe aggregate service facts for dashboards and automations."""
        if not self.available or self.coordinator.data is None:
            return None
        summary = self.coordinator.data.summary
        return {
            "stale": summary.stale,
            "failed": summary.failed,
            "enabled_watches": summary.enabled_watches,
            "latest_check_at": summary.latest_check_at,
        }


class PriceWatchWatchTargetMatchBinarySensor(
    PriceWatchWatchEntity, BinarySensorEntity
):
    """Expose the service-provided target state for one device-linked watch."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: PriceWatchCoordinator, watch: PriceWatchWatch
    ) -> None:
        """Set a stable target-match entity identity."""
        super().__init__(coordinator, watch)
        self._attr_unique_id = f"watch_{watch.id}_target_match"
        self._attr_name = "Target match"
        self._set_legacy_entity_id(
            "binary_sensor", f"{watch.title} Target Match"
        )

    @property
    def is_on(self) -> bool | None:
        """Return only the target membership supplied by the service summary."""
        watch = self._watch()
        if watch is None or self.coordinator.data is None:
            return None
        return watch.id in self.coordinator.data.summary.target_matching_watch_ids


class PriceWatchBinarySensorManager:
    """Create one target-match entity for each dynamically discovered watch."""

    def __init__(
        self,
        coordinator: PriceWatchCoordinator,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        """Subscribe once to shared coordinator updates."""
        self._coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._watch_ids: set[str] = set()
        self._unsub = coordinator.async_add_listener(
            self._handle_coordinator_update
        )

    def add_new_watch_binary_sensors(
        self,
    ) -> list[PriceWatchWatchTargetMatchBinarySensor]:
        """Return entities for watch IDs not already registered on this platform."""
        if self._coordinator.data is None:
            return []
        new_watches = [
            watch
            for watch in self._coordinator.data.watches
            if watch.id not in self._watch_ids
        ]
        self._watch_ids.update(watch.id for watch in new_watches)
        return [
            PriceWatchWatchTargetMatchBinarySensor(self._coordinator, watch)
            for watch in new_watches
        ]

    def _handle_coordinator_update(self) -> None:
        """Register entities discovered after the initial coordinator refresh."""
        sensors = self.add_new_watch_binary_sensors()
        if sensors:
            self._async_add_entities(sensors)

    def stop(self) -> None:
        """Unregister the coordinator callback during config-entry unload."""
        self._unsub()
