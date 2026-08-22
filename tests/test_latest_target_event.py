"""Tests for the service-produced target-event sensor."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.price_watch.api import PriceWatchEvent, PriceWatchSummary
from custom_components.price_watch.const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    DATA_COORDINATORS,
    DOMAIN,
)

pytestmark = pytest.mark.asyncio


def entry() -> MockConfigEntry:
    """Create an isolated, synthetic integration entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BASE_URL: "http://price-watch.test:8787",
            CONF_API_TOKEN: "redacted-test-token",
        },
    )


def event(
    event_id: str,
    occurred_at: str,
    *,
    event_type: str = "target_reached",
    data: dict[str, object] | None = None,
) -> PriceWatchEvent:
    """Build a synthetic immutable service event."""
    return PriceWatchEvent(
        id=event_id,
        watch_id="watch-example",
        observation_id="observation-example",
        type=event_type,
        occurred_at=occurred_at,
        deduplication_key=f"{event_type}:watch-example:{event_id}",
        data=data if data is not None else {"target_price_cents": 5000},
    )


def summary() -> PriceWatchSummary:
    """Build the minimal valid summary required for coordinator setup."""
    return PriceWatchSummary(
        enabled_watches=1,
        target_matches=1,
        stale=0,
        failed=0,
        latest_check_at="2026-08-22T07:00:00.000Z",
        target_matching_watch_ids=("watch-example",),
    )


async def setup_entry(hass, events: tuple[PriceWatchEvent, ...]):
    """Set up the integration with an injected service event snapshot."""
    config_entry = entry()
    config_entry.add_to_hass(hass)
    with patch(
        "custom_components.price_watch.api.PriceWatchApiClient.async_get_summary",
        new=AsyncMock(return_value=summary()),
    ), patch(
        "custom_components.price_watch.api.PriceWatchApiClient.async_get_watches",
        new=AsyncMock(return_value=()),
    ), patch(
        "custom_components.price_watch.api.PriceWatchApiClient.async_get_events",
        new=AsyncMock(return_value=events),
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    return config_entry


async def unload_entry(hass, config_entry) -> None:
    """Unload each test entry before the HA harness checks resource cleanup."""
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_latest_target_event_is_none_when_no_target_events_exist(hass):
    """No target event produces no synthetic event state or attributes."""
    config_entry = await setup_entry(
        hass,
        (
            event(
                "price-drop",
                "2026-08-22T07:00:00.000Z",
                event_type="price_dropped",
            ),
        ),
    )

    state = hass.states.get("sensor.price_watch_latest_target_event")

    assert state is not None
    assert state.state == "none"
    assert set(state.attributes) == {"friendly_name"}
    await unload_entry(hass, config_entry)


async def test_latest_target_event_exposes_only_safe_event_fields(hass):
    """One target event uses its immutable ID and an allow-listed data field."""
    config_entry = await setup_entry(
        hass,
        (
            event(
                "event-target",
                "2026-08-22T07:00:00.000Z",
                data={
                    "target_price_cents": 5000,
                        "unapproved_data": "must-not-be-exposed",
                },
            ),
        ),
    )

    state = hass.states.get("sensor.price_watch_latest_target_event")

    assert state is not None
    assert state.state == "event-target"
    assert state.attributes == {
        "watch_id": "watch-example",
        "occurred_at": "2026-08-22T07:00:00.000Z",
        "deduplication_key": "target_reached:watch-example:event-target",
        "event_type": "target_reached",
        "target_price_cents": 5000,
        "friendly_name": "Price Watch Latest Target Event",
    }
    assert "redacted-test-token" not in state.state
    assert "unapproved_data" not in state.attributes
    await unload_entry(hass, config_entry)


async def test_latest_target_event_selects_newest_target_event(hass):
    """Later non-target events cannot displace the latest target event."""
    config_entry = await setup_entry(
        hass,
        (
            event("event-old", "2026-08-22T07:00:00.000Z"),
            event(
                "event-price",
                "2026-08-22T09:00:00.000Z",
                event_type="price_dropped",
            ),
            event("event-new", "2026-08-22T08:00:00.000Z"),
        ),
    )

    state = hass.states.get("sensor.price_watch_latest_target_event")

    assert state is not None
    assert state.state == "event-new"
    await unload_entry(hass, config_entry)


async def test_unchanged_target_event_id_does_not_update_state(hass):
    """Refreshing an identical event snapshot does not create a new event state."""
    events = (event("event-target", "2026-08-22T07:00:00.000Z"),)
    config_entry = await setup_entry(hass, events)
    before = hass.states.get("sensor.price_watch_latest_target_event")
    assert before is not None
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][config_entry.entry_id]

    with patch(
        "custom_components.price_watch.api.PriceWatchApiClient.async_get_summary",
        new=AsyncMock(return_value=summary()),
    ), patch(
        "custom_components.price_watch.api.PriceWatchApiClient.async_get_watches",
        new=AsyncMock(return_value=()),
    ), patch(
        "custom_components.price_watch.api.PriceWatchApiClient.async_get_events",
        new=AsyncMock(return_value=events),
    ):
        await coordinator.async_request_refresh()
        await hass.async_block_till_done()

    after = hass.states.get("sensor.price_watch_latest_target_event")
    assert after is not None
    assert after.state == "event-target"
    assert after.last_changed == before.last_changed
    await unload_entry(hass, config_entry)


async def test_latest_target_event_is_unavailable_after_refresh_failure(hass):
    """A failed refresh cannot present the prior event as a fresh event."""
    config_entry = await setup_entry(
        hass,
        (event("event-target", "2026-08-22T07:00:00.000Z"),),
    )
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][config_entry.entry_id]

    with patch.object(
        coordinator,
        "_async_update_data",
        new=AsyncMock(side_effect=UpdateFailed("unavailable")),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    state = hass.states.get("sensor.price_watch_latest_target_event")
    assert state is not None
    assert state.state == STATE_UNAVAILABLE
    await unload_entry(hass, config_entry)
