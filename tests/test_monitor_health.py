"""Tests for service-provided monitor health and failure-event entities."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
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
    """Create an isolated integration entry with a synthetic service endpoint."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BASE_URL: "http://price-watch.test:8787",
            CONF_API_TOKEN: "redacted-test-token",
        },
    )


def summary(*, stale: int = 0, failed: int = 0) -> PriceWatchSummary:
    """Build a minimal service summary without deriving monitor health."""
    return PriceWatchSummary(
        enabled_watches=3,
        target_matches=0,
        stale=stale,
        failed=failed,
        latest_check_at="2026-08-24T00:00:00.000Z",
        target_matching_watch_ids=(),
    )


def event(
    event_id: str,
    occurred_at: str,
    *,
    event_type: str = "check_failed",
    data: dict[str, object] | None = None,
) -> PriceWatchEvent:
    """Build a synthetic immutable event from safe service-shaped fields."""
    return PriceWatchEvent(
        id=event_id,
        watch_id="watch-example",
        observation_id="observation-example",
        type=event_type,
        occurred_at=occurred_at,
        deduplication_key=f"{event_type}:watch-example:{event_id}",
        data=data if data is not None else {"error_code": "rate_limited"},
    )


async def setup_entry(
    hass,
    summary_value: PriceWatchSummary,
    events: tuple[PriceWatchEvent, ...] = (),
):
    """Set up the integration with only synthetic coordinator data."""
    config_entry = entry()
    config_entry.add_to_hass(hass)
    with patch(
        "custom_components.price_watch.api.PriceWatchApiClient.async_get_summary",
        new=AsyncMock(return_value=summary_value),
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
    """Unload the synthetic entry before the HA harness checks cleanup."""
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("summary_value", "expected_state"),
    [
        (summary(), STATE_ON),
        (summary(stale=1), STATE_OFF),
        (summary(failed=1), STATE_OFF),
    ],
)
async def test_monitor_health_uses_only_service_summary_counts(
    hass, summary_value, expected_state
):
    """Healthy, stale, and failed states map directly from service summary."""
    config_entry = await setup_entry(hass, summary_value)

    state = hass.states.get("binary_sensor.price_watch_monitor_health")

    assert state is not None
    assert state.state == expected_state
    assert state.attributes == {
        "stale": summary_value.stale,
        "failed": summary_value.failed,
        "enabled_watches": 3,
        "latest_check_at": "2026-08-24T00:00:00.000Z",
        "friendly_name": "Price Watch Monitor Health",
    }
    await unload_entry(hass, config_entry)


async def test_monitor_health_is_unavailable_after_coordinator_failure(hass):
    """An unavailable coordinator cannot present a stale summary as healthy."""
    config_entry = await setup_entry(hass, summary())
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][config_entry.entry_id]

    with patch.object(
        coordinator,
        "_async_update_data",
        new=AsyncMock(side_effect=UpdateFailed("unavailable")),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    state = hass.states.get("binary_sensor.price_watch_monitor_health")
    assert state is not None
    assert state.state == STATE_UNAVAILABLE
    await unload_entry(hass, config_entry)


async def test_latest_failure_event_selects_newest_immutable_failure(hass):
    """Only the newest check_failed event can provide the notification ID."""
    config_entry = await setup_entry(
        hass,
        summary(),
        (
            event("failure-old", "2026-08-24T00:00:00.000Z"),
            event(
                "price-event",
                "2026-08-24T02:00:00.000Z",
                event_type="price_dropped",
            ),
            event(
                "failure-new",
                "2026-08-24T01:00:00.000Z",
                data={
                    "error_code": "rate_limited",
                    "exception": "must-not-be-exposed",
                    "product_url": "must-not-be-exposed",
                },
            ),
        ),
    )

    state = hass.states.get("sensor.price_watch_latest_failure_event")

    assert state is not None
    assert state.state == "failure-new"
    assert state.attributes == {
        "watch_id": "watch-example",
        "occurred_at": "2026-08-24T01:00:00.000Z",
        "deduplication_key": "check_failed:watch-example:failure-new",
        "event_type": "check_failed",
        "error_code": "rate_limited",
        "friendly_name": "Price Watch Latest Failure Event",
    }
    assert "exception" not in state.attributes
    assert "product_url" not in state.attributes
    await unload_entry(hass, config_entry)


async def test_latest_failure_event_uses_id_to_break_timestamp_ties(hass):
    """Equal-time failure events remain stable when API response order reverses."""
    occurred_at = "2026-08-24T00:00:00.000Z"
    first = event("failure-a", occurred_at)
    latest = event("failure-z", occurred_at)
    config_entry = await setup_entry(hass, summary(), (first, latest))
    before = hass.states.get("sensor.price_watch_latest_failure_event")
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
        new=AsyncMock(return_value=(latest, first)),
    ):
        await coordinator.async_request_refresh()
        await hass.async_block_till_done()

    after = hass.states.get("sensor.price_watch_latest_failure_event")
    assert after is not None
    assert before.state == after.state == "failure-z"
    assert before.attributes == after.attributes
    assert after.last_changed == before.last_changed
    await unload_entry(hass, config_entry)


async def test_latest_failure_event_is_none_without_a_failure_event(hass):
    """No failure event remains distinct from an unavailable coordinator."""
    config_entry = await setup_entry(
        hass,
        summary(),
        (
            event(
                "target-event",
                "2026-08-24T00:00:00.000Z",
                event_type="target_reached",
            ),
        ),
    )

    state = hass.states.get("sensor.price_watch_latest_failure_event")

    assert state is not None
    assert state.state == "none"
    assert set(state.attributes) == {"friendly_name"}
    await unload_entry(hass, config_entry)


async def test_latest_failure_event_omits_untrusted_error_code(hass):
    """Arbitrary service error data cannot become a Home Assistant attribute."""
    config_entry = await setup_entry(
        hass,
        summary(),
        (
            event(
                "failure-untrusted",
                "2026-08-24T00:00:00.000Z",
                data={"error_code": "raw-retailer-response"},
            ),
        ),
    )

    state = hass.states.get("sensor.price_watch_latest_failure_event")

    assert state is not None
    assert state.state == "failure-untrusted"
    assert "error_code" not in state.attributes
    assert "raw-retailer-response" not in state.attributes.values()
    await unload_entry(hass, config_entry)


async def test_unchanged_failure_event_id_does_not_update_state(hass):
    """A deduplicated service event cannot retrigger a state-change automation."""
    events = (event("failure-event", "2026-08-24T00:00:00.000Z"),)
    config_entry = await setup_entry(hass, summary(), events)
    before = hass.states.get("sensor.price_watch_latest_failure_event")
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

    after = hass.states.get("sensor.price_watch_latest_failure_event")
    assert after is not None
    assert after.state == "failure-event"
    assert after.last_changed == before.last_changed
    await unload_entry(hass, config_entry)


async def test_latest_failure_event_is_unavailable_after_refresh_failure(hass):
    """Refresh failure cannot present an old immutable failure as current."""
    config_entry = await setup_entry(
        hass,
        summary(),
        (event("failure-event", "2026-08-24T00:00:00.000Z"),),
    )
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][config_entry.entry_id]

    with patch.object(
        coordinator,
        "_async_update_data",
        new=AsyncMock(side_effect=UpdateFailed("unavailable")),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    state = hass.states.get("sensor.price_watch_latest_failure_event")
    assert state is not None
    assert state.state == STATE_UNAVAILABLE
    await unload_entry(hass, config_entry)
