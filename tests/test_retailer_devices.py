"""Tests for F10 Phase 7a stable per-retailer Home Assistant devices."""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.components.select.const import ATTR_OPTION, SERVICE_SELECT_OPTION
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.price_watch.api import (
    PriceWatchApiResponseError,
    PriceWatchRetailer,
    PriceWatchRetailerAttempt,
    PriceWatchRetailerDiagnosticResult,
    PriceWatchRetailerWatchImpact,
    PriceWatchSummary,
)
from custom_components.price_watch.const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    DATA_CLIENTS,
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


def _summary() -> PriceWatchSummary:
    return PriceWatchSummary(
        enabled_watches=0,
        target_matches=0,
        stale=0,
        failed=0,
        latest_check_at="2026-08-22T07:00:00.000Z",
        target_matching_watch_ids=(),
    )


def _retailer(
    retailer_id: str = "lorna_jane",
    *,
    enabled: bool = True,
    acquisition_methods: tuple[str, ...] = ("http",),
    status: str = "healthy",
    preferred_strategy: str = "auto",
    active_strategy: str = "http",
    effective_strategy_reason: str = "auto_lowest_cost",
    active_watch_count: int = 3,
    affected_watch_count: int = 0,
    cooldown_until: str | None = None,
    last_success: PriceWatchRetailerAttempt | None = None,
    last_failure: PriceWatchRetailerAttempt | None = None,
) -> PriceWatchRetailer:
    return PriceWatchRetailer(
        retailer_id=retailer_id,
        enabled=enabled,
        acquisition_methods=acquisition_methods,
        interpretation_mode="legacy_adapter",
        status=status,
        preferred_strategy=preferred_strategy,
        active_strategy=active_strategy,
        effective_strategy_reason=effective_strategy_reason,
        watch_impact=PriceWatchRetailerWatchImpact(
            active_watch_count=active_watch_count,
            affected_watch_count=affected_watch_count,
        ),
        cooldown_until=cooldown_until,
        last_success=last_success,
        last_failure=last_failure,
    )


async def _setup_entry(hass, retailers: tuple[PriceWatchRetailer, ...]):
    config_entry = _entry()
    config_entry.add_to_hass(hass)
    with patch(
        "custom_components.price_watch.api.PriceWatchApiClient.async_get_summary",
        new=AsyncMock(return_value=_summary()),
    ), patch(
        "custom_components.price_watch.api.PriceWatchApiClient.async_get_watches",
        new=AsyncMock(return_value=()),
    ), patch(
        "custom_components.price_watch.api.PriceWatchApiClient.async_get_events",
        new=AsyncMock(return_value=()),
    ), patch(
        "custom_components.price_watch.api.PriceWatchApiClient.async_get_retailers",
        new=AsyncMock(return_value=retailers),
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    return config_entry


async def _unload_entry(hass, config_entry) -> None:
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


def _entity_id(entity_registry, platform: str, unique_id: str) -> str:
    entity_id = entity_registry.async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None
    return entity_id


#: Every retailer/watch action a control entity must never call as a side
#: effect of its own documented action.
_NO_SIDE_EFFECT_METHODS = (
    "async_check_all",
    "async_check_watch",
    "async_set_enabled",
    "async_test_retailer",
    "async_reset_retailer",
    "async_set_retailer_enabled",
    "async_set_retailer_preferred_strategy",
)


def _patch_other_actions(stack: ExitStack, client, *, exclude: tuple[str, ...] = ()):
    """Patch every action method except `exclude` so a side effect is caught."""
    mocks: dict[str, AsyncMock] = {}
    for name in _NO_SIDE_EFFECT_METHODS:
        if name in exclude:
            continue
        mocks[name] = stack.enter_context(patch.object(client, name, new=AsyncMock()))
    return mocks


def _patch_full_refresh_routes(stack: ExitStack, client) -> None:
    """Keep the non-retailer coordinator refresh routes safely mocked.

    A control entity requests a full coordinator refresh after a successful
    action; that refresh fetches summary/watches/events/retailers together,
    so every route must stay mocked for the action's duration. Callers patch
    `async_get_retailers` themselves with the scenario-specific result.
    """
    stack.enter_context(
        patch.object(client, "async_get_summary", new=AsyncMock(return_value=_summary()))
    )
    stack.enter_context(
        patch.object(client, "async_get_watches", new=AsyncMock(return_value=()))
    )
    stack.enter_context(
        patch.object(client, "async_get_events", new=AsyncMock(return_value=()))
    )


_ALL_RETAILER_ENTITIES = (
    ("sensor", "status"),
    ("sensor", "active_strategy"),
    ("sensor", "last_success"),
    ("sensor", "last_failure"),
    ("sensor", "watch_impact"),
    ("select", "preferred_strategy"),
    ("switch", "enabled"),
    ("button", "test"),
    ("button", "reset"),
)


async def test_retailer_id_creates_a_stable_device_and_every_required_entity(hass):
    """One retailer creates exactly one device with all 9 documented entities."""
    retailer = _retailer("lorna_jane")
    entry = await _setup_entry(hass, (retailer,))
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    device = device_registry.async_get_device(
        identifiers={(DOMAIN, "retailer_lorna_jane")}
    )
    assert device is not None
    assert device.name == "Lorna Jane"
    assert device.manufacturer == "Price Watch"

    for platform, suffix in _ALL_RETAILER_ENTITIES:
        entity_id = _entity_id(
            entity_registry, platform, f"retailer_lorna_jane_{suffix}"
        )
        entity = entity_registry.async_get(entity_id)
        assert entity is not None
        assert entity.device_id == device.id

    await _unload_entry(hass, entry)


async def test_device_identity_and_entities_are_stable_across_a_coordinator_refresh(
    hass,
):
    """A later coordinator snapshot cannot create a second device or entity set."""
    retailer = _retailer("lorna_jane")
    entry = await _setup_entry(hass, (retailer,))
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    device = device_registry.async_get_device(
        identifiers={(DOMAIN, "retailer_lorna_jane")}
    )
    status_entity_id = _entity_id(
        entity_registry, "sensor", "retailer_lorna_jane_status"
    )

    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    coordinator.async_set_updated_data(
        PriceWatchCoordinatorData(
            summary=_summary(),
            watches=(),
            events=(),
            retailers=(_retailer("lorna_jane", status="degraded"),),
        )
    )
    await hass.async_block_till_done()

    assert (
        device_registry.async_get_device(
            identifiers={(DOMAIN, "retailer_lorna_jane")}
        ).id
        == device.id
    )
    assert (
        _entity_id(entity_registry, "sensor", "retailer_lorna_jane_status")
        == status_entity_id
    )
    assert hass.states.get(status_entity_id).state == "degraded"
    await _unload_entry(hass, entry)


async def test_two_retailers_create_two_independent_devices(hass):
    """Every retailer entity belongs only to its own retailer's device."""
    first = _retailer("lorna_jane")
    second = _retailer("kmart_au", acquisition_methods=("http", "browser"))
    entry = await _setup_entry(hass, (first, second))
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    first_device = device_registry.async_get_device(
        identifiers={(DOMAIN, "retailer_lorna_jane")}
    )
    second_device = device_registry.async_get_device(
        identifiers={(DOMAIN, "retailer_kmart_au")}
    )
    assert first_device is not None
    assert second_device is not None
    assert first_device.id != second_device.id
    assert second_device.name == "Kmart Au"

    for retailer, device in ((first, first_device), (second, second_device)):
        for platform, suffix in _ALL_RETAILER_ENTITIES:
            entity_id = _entity_id(
                entity_registry, platform, f"retailer_{retailer.retailer_id}_{suffix}"
            )
            entity = entity_registry.async_get(entity_id)
            assert entity is not None
            assert entity.device_id == device.id
    await _unload_entry(hass, entry)


@pytest.mark.parametrize(
    "status", ("healthy", "degraded", "rate_limited", "blocked", "disabled", "unknown")
)
async def test_status_sensor_reports_every_documented_service_state(hass, status):
    """Every bounded retailer status the service can report is presented as-is."""
    entry = await _setup_entry(hass, (_retailer("lorna_jane", status=status),))
    entity_registry = er.async_get(hass)
    state = hass.states.get(
        _entity_id(entity_registry, "sensor", "retailer_lorna_jane_status")
    )

    assert state is not None
    assert state.state == status
    await _unload_entry(hass, entry)


async def test_status_sensor_never_locally_recomputes_from_enabled_or_impact(hass):
    """A disabled-but-healthy retailer must still report the raw service status."""
    retailer = _retailer("lorna_jane", status="healthy", enabled=False)
    entry = await _setup_entry(hass, (retailer,))
    entity_registry = er.async_get(hass)
    status_state = hass.states.get(
        _entity_id(entity_registry, "sensor", "retailer_lorna_jane_status")
    )
    switch_state = hass.states.get(
        _entity_id(entity_registry, "switch", "retailer_lorna_jane_enabled")
    )

    assert status_state is not None
    assert status_state.state == "healthy"
    assert switch_state is not None
    assert switch_state.state == "off"
    await _unload_entry(hass, entry)


async def test_retailer_entities_are_unavailable_after_a_coordinator_failure(hass):
    """A coordinator/API error must present retailer entities as unavailable, never healthy."""
    entry = await _setup_entry(hass, (_retailer("lorna_jane", status="healthy"),))
    entity_registry = er.async_get(hass)
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]

    with patch.object(
        coordinator,
        "_async_update_data",
        new=AsyncMock(side_effect=UpdateFailed("unavailable")),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    for platform, suffix in _ALL_RETAILER_ENTITIES:
        entity_id = _entity_id(
            entity_registry, platform, f"retailer_lorna_jane_{suffix}"
        )
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == STATE_UNAVAILABLE
    await _unload_entry(hass, entry)


async def test_retailer_missing_from_a_later_snapshot_becomes_unavailable(hass):
    """A retailer dropped from the service response cannot keep reporting healthy."""
    entry = await _setup_entry(hass, (_retailer("lorna_jane"),))
    entity_registry = er.async_get(hass)
    status_entity_id = _entity_id(
        entity_registry, "sensor", "retailer_lorna_jane_status"
    )
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]

    coordinator.async_set_updated_data(
        PriceWatchCoordinatorData(summary=_summary(), watches=(), events=(), retailers=())
    )
    await hass.async_block_till_done()

    assert hass.states.get(status_entity_id).state == STATE_UNAVAILABLE
    await _unload_entry(hass, entry)


async def test_active_strategy_and_impact_sensors_expose_only_safe_attributes(hass):
    """Attributes are bounded values only; never raw capture/URLs/tokens."""
    retailer = _retailer(
        "lorna_jane",
        status="rate_limited",
        cooldown_until="2026-08-26T06:00:00.000Z",
        last_success=PriceWatchRetailerAttempt(
            acquisition_method="http", occurred_at="2026-08-25T00:00:00.000Z"
        ),
        last_failure=PriceWatchRetailerAttempt(
            acquisition_method="http",
            occurred_at="2026-08-26T00:00:00.000Z",
            failure_classification="rate_limited",
        ),
    )
    entry = await _setup_entry(hass, (retailer,))
    entity_registry = er.async_get(hass)

    status_state = hass.states.get(
        _entity_id(entity_registry, "sensor", "retailer_lorna_jane_status")
    )
    assert status_state.attributes["cooldown_until"] == "2026-08-26T06:00:00.000Z"
    assert status_state.attributes["enabled"] is True

    strategy_state = hass.states.get(
        _entity_id(entity_registry, "sensor", "retailer_lorna_jane_active_strategy")
    )
    assert strategy_state.attributes["effective_strategy_reason"] == "auto_lowest_cost"

    success_state = hass.states.get(
        _entity_id(entity_registry, "sensor", "retailer_lorna_jane_last_success")
    )
    assert success_state.attributes["acquisition_method"] == "http"

    failure_state = hass.states.get(
        _entity_id(entity_registry, "sensor", "retailer_lorna_jane_last_failure")
    )
    assert failure_state.attributes["failure_classification"] == "rate_limited"

    impact_state = hass.states.get(
        _entity_id(entity_registry, "sensor", "retailer_lorna_jane_watch_impact")
    )
    assert impact_state.state == "3"
    assert impact_state.attributes["affected_watch_count"] == 0

    for state in (
        status_state,
        strategy_state,
        success_state,
        failure_state,
        impact_state,
    ):
        text = str(state.attributes)
        assert "http://" not in text
        assert "https://" not in text
        assert "token" not in text.lower()
        assert "cookie" not in text.lower()
        assert "redacted-test-token" not in text
    await _unload_entry(hass, entry)


async def test_preferred_strategy_select_options_are_limited_to_supported_methods(
    hass,
):
    """Options are `auto` plus only this retailer's own supported strategies."""
    retailer = _retailer("lorna_jane", acquisition_methods=("http", "browser"))
    entry = await _setup_entry(hass, (retailer,))
    entity_registry = er.async_get(hass)
    entity_id = _entity_id(
        entity_registry, "select", "retailer_lorna_jane_preferred_strategy"
    )

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["options"] == ["auto", "http", "browser"]
    assert state.state == "auto"
    await _unload_entry(hass, entry)


async def test_preferred_strategy_select_calls_the_strict_api_and_refreshes(hass):
    """Selecting a strategy calls only the documented PATCH action."""
    entry = await _setup_entry(hass, (_retailer("lorna_jane"),))
    entity_registry = er.async_get(hass)
    entity_id = _entity_id(
        entity_registry, "select", "retailer_lorna_jane_preferred_strategy"
    )
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]

    with ExitStack() as stack:
        other_actions = _patch_other_actions(
            stack, client, exclude=("async_set_retailer_preferred_strategy",)
        )
        _patch_full_refresh_routes(stack, client)
        set_strategy = stack.enter_context(
            patch.object(
                client,
                "async_set_retailer_preferred_strategy",
                new=AsyncMock(
                    return_value=_retailer("lorna_jane", preferred_strategy="http")
                ),
            )
        )
        stack.enter_context(
            patch.object(
                client,
                "async_get_retailers",
                new=AsyncMock(
                    return_value=(_retailer("lorna_jane", preferred_strategy="http"),)
                ),
            )
        )
        await hass.services.async_call(
            "select",
            SERVICE_SELECT_OPTION,
            {ATTR_ENTITY_ID: entity_id, ATTR_OPTION: "http"},
            blocking=True,
        )
        await hass.async_block_till_done()

    set_strategy.assert_awaited_once()
    args, _kwargs = set_strategy.await_args
    assert args[0] == "lorna_jane"
    assert args[1] == "http"

    for mock in other_actions.values():
        mock.assert_not_called()
    await _unload_entry(hass, entry)


async def test_preferred_strategy_select_raises_and_stays_safe_on_api_failure(hass):
    """A rejected strategy change never silently succeeds or crashes the entity."""
    entry = await _setup_entry(hass, (_retailer("lorna_jane"),))
    entity_registry = er.async_get(hass)
    entity_id = _entity_id(
        entity_registry, "select", "retailer_lorna_jane_preferred_strategy"
    )
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]

    with patch.object(
        client,
        "async_set_retailer_preferred_strategy",
        new=AsyncMock(
            side_effect=PriceWatchApiResponseError(400, "unsupported_strategy")
        ),
    ):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "select",
                SERVICE_SELECT_OPTION,
                {ATTR_ENTITY_ID: entity_id, ATTR_OPTION: "browser"},
                blocking=True,
            )
        await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "auto"
    await _unload_entry(hass, entry)


async def test_enabled_switch_turn_on_and_off_call_the_strict_api_only(hass):
    """The switch calls only the documented enabled PATCH action, never a check."""
    entry = await _setup_entry(hass, (_retailer("lorna_jane", enabled=True),))
    entity_registry = er.async_get(hass)
    entity_id = _entity_id(entity_registry, "switch", "retailer_lorna_jane_enabled")
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]

    with ExitStack() as stack:
        other_actions = _patch_other_actions(
            stack, client, exclude=("async_set_retailer_enabled",)
        )
        _patch_full_refresh_routes(stack, client)
        set_enabled = stack.enter_context(
            patch.object(
                client,
                "async_set_retailer_enabled",
                new=AsyncMock(return_value=_retailer("lorna_jane", enabled=False)),
            )
        )
        get_retailers = stack.enter_context(
            patch.object(
                client,
                "async_get_retailers",
                new=AsyncMock(return_value=(_retailer("lorna_jane", enabled=False),)),
            )
        )
        await hass.services.async_call(
            "switch",
            SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )
        await hass.async_block_till_done()

        set_enabled.assert_awaited_once()
        args, _kwargs = set_enabled.await_args
        assert args == ("lorna_jane", False)
        for mock in other_actions.values():
            mock.assert_not_called()

        set_enabled.reset_mock()
        set_enabled.return_value = _retailer("lorna_jane", enabled=True)
        get_retailers.return_value = (_retailer("lorna_jane", enabled=True),)

        await hass.services.async_call(
            "switch",
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )
        await hass.async_block_till_done()

    set_enabled.assert_awaited_once()
    args, _kwargs = set_enabled.await_args
    assert args == ("lorna_jane", True)
    await _unload_entry(hass, entry)


async def test_enabled_switch_raises_and_stays_safe_on_api_failure(hass):
    """A rejected enabled change never silently succeeds or crashes the entity."""
    entry = await _setup_entry(hass, (_retailer("lorna_jane", enabled=True),))
    entity_registry = er.async_get(hass)
    entity_id = _entity_id(entity_registry, "switch", "retailer_lorna_jane_enabled")
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]

    with patch.object(
        client,
        "async_set_retailer_enabled",
        new=AsyncMock(side_effect=PriceWatchApiResponseError(500)),
    ):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "switch",
                SERVICE_TURN_OFF,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
            )
        await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_ON
    await _unload_entry(hass, entry)


async def test_test_button_calls_only_the_test_action_with_no_other_side_effect(hass):
    """The test button never triggers a check, notification, or reset action."""
    entry = await _setup_entry(hass, (_retailer("lorna_jane"),))
    entity_registry = er.async_get(hass)
    entity_id = _entity_id(entity_registry, "button", "retailer_lorna_jane_test")
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]

    with ExitStack() as stack:
        other_actions = _patch_other_actions(
            stack, client, exclude=("async_test_retailer",)
        )
        _patch_full_refresh_routes(stack, client)
        stack.enter_context(
            patch.object(
                client,
                "async_get_retailers",
                new=AsyncMock(return_value=(_retailer("lorna_jane"),)),
            )
        )
        test_retailer = stack.enter_context(
            patch.object(
                client,
                "async_test_retailer",
                new=AsyncMock(
                    return_value=PriceWatchRetailerDiagnosticResult(
                        retailer_id="lorna_jane",
                        watch_id="watch-1",
                        outcome="success",
                        tested_at="2026-08-26T00:00:00.000Z",
                        acquisition_method="http",
                    )
                ),
            )
        )
        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )
        await hass.async_block_till_done()

    test_retailer.assert_awaited_once()
    args, _kwargs = test_retailer.await_args
    assert args[0] == "lorna_jane"
    for mock in other_actions.values():
        mock.assert_not_called()
    await _unload_entry(hass, entry)


async def test_test_button_raises_on_api_failure_without_a_silent_success(hass):
    """A failed diagnostic test surfaces as an error, never a silent no-op."""
    entry = await _setup_entry(hass, (_retailer("lorna_jane"),))
    entity_registry = er.async_get(hass)
    entity_id = _entity_id(entity_registry, "button", "retailer_lorna_jane_test")
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]

    with patch.object(
        client,
        "async_test_retailer",
        new=AsyncMock(side_effect=PriceWatchApiResponseError(409, "not_found")),
    ):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "button",
                SERVICE_PRESS,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
            )
        await hass.async_block_till_done()
    await _unload_entry(hass, entry)


async def test_reset_button_calls_only_the_reset_action_with_no_other_side_effect(
    hass,
):
    """The reset button never triggers a check, notification, or test action."""
    entry = await _setup_entry(hass, (_retailer("lorna_jane"),))
    entity_registry = er.async_get(hass)
    entity_id = _entity_id(entity_registry, "button", "retailer_lorna_jane_reset")
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]

    with ExitStack() as stack:
        other_actions = _patch_other_actions(
            stack, client, exclude=("async_reset_retailer",)
        )
        _patch_full_refresh_routes(stack, client)
        stack.enter_context(
            patch.object(
                client,
                "async_get_retailers",
                new=AsyncMock(return_value=(_retailer("lorna_jane"),)),
            )
        )
        reset_retailer = stack.enter_context(
            patch.object(
                client,
                "async_reset_retailer",
                new=AsyncMock(return_value=_retailer("lorna_jane")),
            )
        )
        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )
        await hass.async_block_till_done()

    reset_retailer.assert_awaited_once()
    args, _kwargs = reset_retailer.await_args
    assert args[0] == "lorna_jane"
    for mock in other_actions.values():
        mock.assert_not_called()
    await _unload_entry(hass, entry)


async def test_reset_button_raises_on_api_failure_without_a_silent_success(hass):
    """A failed reset surfaces as an error, never a silent no-op."""
    entry = await _setup_entry(hass, (_retailer("lorna_jane"),))
    entity_registry = er.async_get(hass)
    entity_id = _entity_id(entity_registry, "button", "retailer_lorna_jane_reset")
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]

    with patch.object(
        client,
        "async_reset_retailer",
        new=AsyncMock(side_effect=PriceWatchApiResponseError(500)),
    ):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "button",
                SERVICE_PRESS,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
            )
        await hass.async_block_till_done()
    await _unload_entry(hass, entry)


async def test_retailer_entity_states_never_expose_urls_tokens_or_selectors(hass):
    """No retailer entity attribute may leak a URL, token, cookie or selector."""
    retailer = _retailer(
        "lorna_jane",
        last_success=PriceWatchRetailerAttempt(
            acquisition_method="http", occurred_at="2026-08-25T00:00:00.000Z"
        ),
    )
    entry = await _setup_entry(hass, (retailer,))
    entity_registry = er.async_get(hass)

    for platform, suffix in _ALL_RETAILER_ENTITIES:
        entity_id = _entity_id(
            entity_registry, platform, f"retailer_lorna_jane_{suffix}"
        )
        state = hass.states.get(entity_id)
        assert state is not None
        text = str(state.as_dict())
        assert "http://price-watch.test" not in text
        assert "redacted-test-token" not in text
        assert "cookie" not in text.lower()
        assert "selector" not in text.lower()
    await _unload_entry(hass, entry)
