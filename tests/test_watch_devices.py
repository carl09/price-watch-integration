"""Tests for stable per-watch Home Assistant devices."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.price_watch.api import (
    PriceWatchCurrentObservation,
    PriceWatchEvent,
    PriceWatchSummary,
    PriceWatchVariant,
    PriceWatchWatch,
)
from custom_components.price_watch.const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    DATA_COORDINATORS,
    DOMAIN,
)
from custom_components.price_watch.coordinator import PriceWatchCoordinatorData

pytestmark = pytest.mark.asyncio


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BASE_URL: "http://price-watch.test:8787",
            CONF_API_TOKEN: "redacted-test-token",
        },
    )


def _summary(*watch_ids: str) -> PriceWatchSummary:
    return PriceWatchSummary(
        enabled_watches=len(watch_ids),
        target_matches=0,
        stale=0,
        failed=0,
        latest_check_at="2026-08-22T07:00:00.000Z",
        target_matching_watch_ids=(),
    )


def _watch(
    watch_id: str,
    *,
    title: str = "Heritage Shorts",
    variant_label: str = "Canyon / XS",
    enabled: bool = True,
) -> PriceWatchWatch:
    return PriceWatchWatch(
        id=watch_id,
        retailer_id="lorna_jane_au",
        product_url="https://www.lornajane.com.au/products/example",
        title=title,
        variant=PriceWatchVariant(
            retailer_variant_id="48573064806635",
            options={"Colour": "Canyon", "Size": "XS"},
        ),
        enabled=enabled,
        target_price_cents=8500,
        check_interval_minutes=60,
        current_observation_id="obs_1",
        current_observation=PriceWatchCurrentObservation(
            id="obs_1",
            checked_at="2026-08-22T07:00:00.000Z",
            status="available",
            price_cents=8500,
            compare_at_price_cents=None,
            currency="AUD",
            selected_variant_label=variant_label,
            error_code=None,
        ),
        last_successful_check_at="2026-08-22T07:00:00.000Z",
        last_attempt_at="2026-08-22T07:00:00.000Z",
    )


async def _setup_entry(hass, watches: tuple[PriceWatchWatch, ...]):
    config_entry = _entry()
    config_entry.add_to_hass(hass)
    with patch(
        "custom_components.price_watch.api.PriceWatchApiClient.async_get_summary",
        new=AsyncMock(return_value=_summary(*(watch.id for watch in watches))),
    ), patch(
        "custom_components.price_watch.api.PriceWatchApiClient.async_get_watches",
        new=AsyncMock(return_value=watches),
    ), patch(
        "custom_components.price_watch.api.PriceWatchApiClient.async_get_events",
        new=AsyncMock(return_value=()),
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    return config_entry


def _entity_id(entity_registry, platform: str, unique_id: str) -> str:
    entity_id = entity_registry.async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None
    return entity_id


async def test_watch_id_creates_stable_device_and_existing_price_entity(hass):
    """Title and variant changes cannot alter device or primary entity identity."""
    watch = _watch("watch-one")
    entry = await _setup_entry(hass, (watch,))
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    device = device_registry.async_get_device(identifiers={(DOMAIN, watch.id)})
    assert device is not None
    assert device.name == "Heritage Shorts (Canyon / XS)"
    assert device.manufacturer == "Price Watch"
    assert device.model == "lorna_jane_au"
    assert device.configuration_url == watch.product_url

    price_entity_id = _entity_id(entity_registry, "sensor", "watch_watch-one")
    price_entry = entity_registry.async_get(price_entity_id)
    assert price_entry is not None
    assert price_entry.device_id == device.id

    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    renamed_watch = _watch(
        "watch-one",
        title="Renamed Shorts",
        variant_label="Canyon / S",
    )
    coordinator.async_set_updated_data(
        PriceWatchCoordinatorData(
            summary=_summary("watch-one"),
            watches=(renamed_watch,),
            events=(),
        )
    )
    await hass.async_block_till_done()

    assert (
        device_registry.async_get_device(identifiers={(DOMAIN, "watch-one")}).id
        == device.id
    )
    assert _entity_id(entity_registry, "sensor", "watch_watch-one") == price_entity_id


async def test_two_watches_create_distinct_devices_and_linked_entities(hass):
    """Every useful per-watch entity belongs to its corresponding device."""
    first = _watch("watch-one")
    second = _watch("watch-two", title="Second Shorts")
    await _setup_entry(hass, (first, second))
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    first_device = device_registry.async_get_device(identifiers={(DOMAIN, first.id)})
    second_device = device_registry.async_get_device(
        identifiers={(DOMAIN, second.id)}
    )
    assert first_device is not None
    assert second_device is not None
    assert first_device.id != second_device.id

    for watch, device in ((first, first_device), (second, second_device)):
        for platform, unique_id in (
            ("sensor", f"watch_{watch.id}"),
            ("sensor", f"watch_{watch.id}_target_price"),
            ("sensor", f"watch_{watch.id}_status"),
            ("sensor", f"watch_{watch.id}_last_checked"),
            ("binary_sensor", f"watch_{watch.id}_target_match"),
        ):
            entity_id = _entity_id(entity_registry, platform, unique_id)
            entity = entity_registry.async_get(entity_id)
            assert entity is not None
            assert entity.device_id == device.id


async def test_missing_watch_becomes_unavailable_without_removing_its_device(hass):
    """A disappeared watch cannot keep displaying a current price."""
    watch = _watch("watch-one")
    entry = await _setup_entry(hass, (watch,))
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    price_entity_id = _entity_id(entity_registry, "sensor", "watch_watch-one")
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]

    coordinator.async_set_updated_data(
        PriceWatchCoordinatorData(summary=_summary(), watches=(), events=())
    )
    await hass.async_block_till_done()

    state = hass.states.get(price_entity_id)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE
    assert (
        device_registry.async_get_device(identifiers={(DOMAIN, watch.id)}) is not None
    )


async def test_disabled_watch_retains_its_device_and_safe_primary_attributes(hass):
    """Disabled watches remain stable devices instead of being recreated."""
    watch = _watch("watch-one", enabled=False)
    await _setup_entry(hass, (watch,))
    entity_registry = er.async_get(hass)
    price_entity_id = _entity_id(entity_registry, "sensor", "watch_watch-one")

    state = hass.states.get(price_entity_id)
    assert state is not None
    assert state.attributes["enabled"] is False
    assert state.attributes["retailer_variant_id"] == "48573064806635"
    assert "redacted-test-token" not in str(state.attributes)
