"""Price Watch target-match binary sensor."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_COORDINATORS, DOMAIN
from .coordinator import PriceWatchCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the coordinator-backed Price Watch target-match sensor."""
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    async_add_entities([PriceWatchTargetMatchBinarySensor(coordinator, entry)])


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
