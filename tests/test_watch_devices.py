"""Tests for stable per-watch Home Assistant devices."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.price_watch.api import (
    PriceWatchApiClient,
    PriceWatchCurrentObservation,
    PriceWatchEnabledResult,
    PriceWatchEvent,
    PriceWatchInvalidResponseError,
    PriceWatchSummary,
    PriceWatchTransportError,
    PriceWatchVariant,
    PriceWatchWatch,
)
from custom_components.price_watch.const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    DATA_CLIENTS,
    DATA_COORDINATORS,
    DOMAIN,
    SERVICE_CHECK_ALL,
    SERVICE_CHECK_WATCH,
    SERVICE_RELOAD_IMAGE,
    SERVICE_SET_ENABLED,
    SERVICE_SET_TARGET_PRICE,
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
    product_image_url: str | None = None,
    product_image_retry_available: bool | None = None,
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
        last_attempt_status="available",
        last_attempt_error_code=None,
        product_image_url=product_image_url,
        product_image_retry_available=(
            product_image_retry_available
            if product_image_retry_available is not None
            else product_image_url is not None
        ),
    )


def _api_watch_payload(
    product_image_url: object | None = None,
    product_image_retry_available: bool | None = None,
) -> dict[str, object]:
    """Return a minimal watch response with an optional image endpoint."""
    payload: dict[str, object] = {
        "id": "watch-one",
        "retailer_id": "lorna_jane_au",
        "product_url": "https://www.lornajane.com.au/products/example",
        "title": "Heritage Shorts",
        "variant": {"retailer_variant_id": "48573064806635"},
        "enabled": True,
        "target_price_cents": 8500,
        "check_interval_minutes": 60,
        "current_observation_id": None,
        "current_observation": None,
        "last_successful_check_at": None,
        "last_attempt_at": None,
        "product_image_retry_available": (
            product_image_retry_available
            if product_image_retry_available is not None
            else product_image_url is not None
        ),
    }
    if product_image_url is not None:
        payload["product_image_url"] = product_image_url
    return payload


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
    ), patch(
        "custom_components.price_watch.api.PriceWatchApiClient.async_get_retailers",
        new=AsyncMock(return_value=()),
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    return config_entry


async def _unload_entry(hass, config_entry) -> None:
    """Unload each test entry before the HA harness checks resource cleanup."""
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_action_services_register_once_after_setup_and_unregister_on_unload(hass):
    """Action discovery follows successful entry setup and unload cleanup."""
    entry = await _setup_entry(hass, (_watch("watch-one"),))
    for service in (
        SERVICE_CHECK_ALL,
        SERVICE_CHECK_WATCH,
        SERVICE_SET_ENABLED,
        SERVICE_RELOAD_IMAGE,
        SERVICE_SET_TARGET_PRICE,
    ):
        assert hass.services.has_service(DOMAIN, service)
    await _unload_entry(hass, entry)
    for service in (
        SERVICE_CHECK_ALL,
        SERVICE_CHECK_WATCH,
        SERVICE_SET_ENABLED,
        SERVICE_RELOAD_IMAGE,
        SERVICE_SET_TARGET_PRICE,
    ):
        assert not hass.services.has_service(DOMAIN, service)


async def test_check_services_call_retained_api_routes_with_keys_and_refresh(hass):
    """Retained check services still forward their API actions and refresh state."""
    entry = await _setup_entry(hass, (_watch("watch-one"),))
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]
    client.async_check_all = AsyncMock()
    client.async_check_watch = AsyncMock()
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    coordinator.async_request_refresh = AsyncMock()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_CHECK_ALL,
        {"idempotency_key": "all-retry-1"},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        SERVICE_CHECK_WATCH,
        {"watch_id": "watch-one", "idempotency_key": "watch-retry-1"},
        blocking=True,
    )

    client.async_check_all.assert_awaited_once()
    assert client.async_check_all.await_args.kwargs["idempotency_key"] == "all-retry-1"
    client.async_check_watch.assert_awaited_once()
    assert client.async_check_watch.await_args.args == ("watch-one",)
    assert (
        client.async_check_watch.await_args.kwargs["idempotency_key"]
        == "watch-retry-1"
    )
    assert coordinator.async_request_refresh.await_count == 2
    await _unload_entry(hass, entry)


async def test_check_service_schemas_reject_missing_watch_and_bad_keys(hass):
    """Retained service schemas reject invalid calls before API side effects."""
    entry = await _setup_entry(hass, (_watch("watch-one"),))
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]
    client.async_check_all = AsyncMock()
    client.async_check_watch = AsyncMock()

    with pytest.raises(Exception):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CHECK_WATCH,
            {"watch_id": "watch-one", "idempotency_key": "bad/key"},
            blocking=True,
        )
    with pytest.raises(Exception):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CHECK_WATCH,
            {},
            blocking=True,
        )
    client.async_check_all.assert_not_awaited()
    client.async_check_watch.assert_not_awaited()
    await _unload_entry(hass, entry)


async def test_failed_entry_setup_does_not_register_action_services(hass):
    """A failed first refresh leaves no callable Price Watch actions."""
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(
        PriceWatchApiClient,
        "async_get_summary",
        new=AsyncMock(side_effect=PriceWatchTransportError()),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    for service in (
        SERVICE_CHECK_ALL,
        SERVICE_CHECK_WATCH,
        SERVICE_SET_ENABLED,
        SERVICE_RELOAD_IMAGE,
        SERVICE_SET_TARGET_PRICE,
    ):
        assert not hass.services.has_service(DOMAIN, service)


def _entity_id(entity_registry, platform: str, unique_id: str) -> str:
    entity_id = entity_registry.async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None
    return entity_id


async def test_watch_buttons_are_not_created_or_recreated(hass):
    """Per-watch action buttons and their coordinator lifecycle are removed."""
    entry = await _setup_entry(hass, (_watch("watch-one"),))
    entity_registry = er.async_get(hass)

    assert entity_registry.async_get_entity_id(
        "button", DOMAIN, "watch_watch-one_check"
    ) is None
    assert entity_registry.async_get_entity_id(
        "button", DOMAIN, "watch_watch-one_retry_image"
    ) is None
    assert "watch_button_managers" not in hass.data[DOMAIN]

    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    coordinator.async_set_updated_data(
        PriceWatchCoordinatorData(
            summary=_summary("watch-one"),
            watches=(_watch("watch-one"), _watch("watch-two")),
            events=(),
        )
    )
    await hass.async_block_till_done()
    assert entity_registry.async_get_entity_id(
        "button", DOMAIN, "watch_watch-two_check"
    ) is None
    assert entity_registry.async_get_entity_id(
        "button", DOMAIN, "watch_watch-two_retry_image"
    ) is None
    await _unload_entry(hass, entry)


async def test_watch_enabled_switch_calls_only_set_enabled_and_refreshes(hass):
    """The per-watch switch changes enablement without checking or observing."""
    watch = _watch("watch-one")
    entry = await _setup_entry(hass, (watch,))
    entity_id = _entity_id(er.async_get(hass), "switch", "watch_watch-one_enabled")
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]
    client.async_set_enabled = AsyncMock(
        return_value=PriceWatchEnabledResult("watch-one", False)
    )
    client.async_check_watch = AsyncMock()
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    coordinator.async_request_refresh = AsyncMock()

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": entity_id}, blocking=True
    )

    client.async_set_enabled.assert_awaited_once()
    assert client.async_set_enabled.await_args.args[:2] == ("watch-one", False)
    client.async_check_watch.assert_not_awaited()
    coordinator.async_request_refresh.assert_awaited_once()
    await _unload_entry(hass, entry)


async def test_watch_enabled_switch_failure_does_not_refresh_or_check(hass):
    """A failed enablement mutation leaves service state and checks untouched."""
    watch = _watch("watch-one")
    entry = await _setup_entry(hass, (watch,))
    entity_id = _entity_id(er.async_get(hass), "switch", "watch_watch-one_enabled")
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]
    client.async_set_enabled = AsyncMock(side_effect=PriceWatchTransportError())
    client.async_check_watch = AsyncMock()
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    coordinator.async_request_refresh = AsyncMock()

    with pytest.raises(Exception):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": entity_id}, blocking=True
        )

    client.async_check_watch.assert_not_awaited()
    coordinator.async_request_refresh.assert_not_awaited()
    await _unload_entry(hass, entry)


async def test_target_price_number_and_service_have_no_check_side_effect(hass):
    """Target changes update configuration only and use internal idempotency."""
    watch = _watch("watch-one")
    entry = await _setup_entry(hass, (watch,))
    entity_registry = er.async_get(hass)
    number_id = _entity_id(
        entity_registry, "number", "watch_watch-one_target_price_control"
    )
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]
    client.async_set_target_price = AsyncMock(return_value=watch)
    client.async_check_watch = AsyncMock()
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    coordinator.async_request_refresh = AsyncMock()

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": number_id, "value": 12.34},
        blocking=True,
    )

    client.async_set_target_price.assert_awaited_once()
    assert client.async_set_target_price.await_args.args[:2] == ("watch-one", 1234)
    assert "idempotency_key" not in client.async_set_target_price.await_args.kwargs
    client.async_check_watch.assert_not_awaited()
    coordinator.async_request_refresh.assert_awaited_once()
    await _unload_entry(hass, entry)


async def test_set_target_price_service_rejects_negative_without_side_effect(hass):
    watch = _watch("watch-one")
    entry = await _setup_entry(hass, (watch,))
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]
    client.async_set_target_price = AsyncMock(return_value=watch)
    with pytest.raises(Exception):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_TARGET_PRICE,
            {"watch_id": "watch-one", "target_price_cents": -1},
            blocking=True,
        )
    client.async_set_target_price.assert_not_awaited()
    await _unload_entry(hass, entry)


async def test_set_target_price_service_generates_internal_key_and_refreshes(hass):
    """The custom target service has no caller key and no check side effect."""
    watch = _watch("watch-one")
    entry = await _setup_entry(hass, (watch,))
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]
    client.async_set_target_price = AsyncMock(return_value=watch)
    client.async_check_watch = AsyncMock()
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    coordinator.async_request_refresh = AsyncMock()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_TARGET_PRICE,
        {"watch_id": "watch-one", "target_price_cents": 1234},
        blocking=True,
    )

    client.async_set_target_price.assert_awaited_once()
    assert client.async_set_target_price.await_args.args[:2] == ("watch-one", 1234)
    assert "idempotency_key" not in client.async_set_target_price.await_args.kwargs
    client.async_check_watch.assert_not_awaited()
    coordinator.async_request_refresh.assert_awaited_once()
    await _unload_entry(hass, entry)


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
    await _unload_entry(hass, entry)


async def test_price_sensor_associates_registered_image_entity_after_platform_setup(hass):
    """The primary sensor receives only its HA ImageEntity ID after registration."""
    watch = _watch(
        "watch-one",
        product_image_url="https://images.example.test/canyon-short.jpg",
    )
    entry = await _setup_entry(hass, (watch,))
    entity_registry = er.async_get(hass)
    price_entity_id = _entity_id(entity_registry, "sensor", "watch_watch-one")
    image_entity_id = _entity_id(
        entity_registry, "image", "watch_watch-one_product_image"
    )

    state = hass.states.get(price_entity_id)
    assert state is not None
    assert state.attributes["image_entity_id"] == image_entity_id
    assert "images.example.test" not in str(state.attributes)
    assert "token" not in str(state.attributes)
    await _unload_entry(hass, entry)


async def test_new_watch_receives_image_association_after_coordinator_refresh(hass):
    """Later image registration refreshes an already-created price sensor safely."""
    first = _watch("watch-one")
    second = _watch(
        "watch-two",
        title="Second Shorts",
        product_image_url="https://images.example.test/second-short.jpg",
    )
    entry = await _setup_entry(hass, (first,))
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    coordinator.async_set_updated_data(
        PriceWatchCoordinatorData(
            summary=_summary(first.id, second.id),
            watches=(first, second),
            events=(),
        )
    )
    await hass.async_block_till_done()

    entity_registry = er.async_get(hass)
    price_entity_id = _entity_id(entity_registry, "sensor", "watch_watch-two")
    image_entity_id = _entity_id(
        entity_registry, "image", "watch_watch-two_product_image"
    )
    state = hass.states.get(price_entity_id)
    assert state is not None
    assert state.attributes["image_entity_id"] == image_entity_id
    await _unload_entry(hass, entry)


async def test_two_watches_create_distinct_devices_and_linked_entities(hass):
    """Every useful per-watch entity belongs to its corresponding device."""
    first = _watch("watch-one")
    second = _watch("watch-two", title="Second Shorts")
    entry = await _setup_entry(hass, (first, second))
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
            ("image", f"watch_{watch.id}_product_image"),
            ("number", f"watch_{watch.id}_target_price_control"),
            ("switch", f"watch_{watch.id}_enabled"),
            ("binary_sensor", f"watch_{watch.id}_target_match"),
        ):
            entity_id = _entity_id(entity_registry, platform, unique_id)
            entity = entity_registry.async_get(entity_id)
            assert entity is not None
            assert entity.device_id == device.id
    await _unload_entry(hass, entry)


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
    await _unload_entry(hass, entry)


async def test_disabled_watch_retains_its_device_and_safe_primary_attributes(hass):
    """Disabled watches remain stable devices instead of being recreated."""
    watch = _watch("watch-one", enabled=False)
    entry = await _setup_entry(hass, (watch,))
    entity_registry = er.async_get(hass)
    price_entity_id = _entity_id(entity_registry, "sensor", "watch_watch-one")

    state = hass.states.get(price_entity_id)
    assert state is not None
    assert state.attributes["enabled"] is False
    assert state.attributes["retailer_variant_id"] == "48573064806635"
    assert state.attributes["variant_options"] == {"Colour": "Canyon", "Size": "XS"}
    assert state.attributes["last_attempt_status"] == "available"
    assert state.attributes["last_attempt_at"] == "2026-08-22T07:00:00.000Z"
    assert state.attributes["last_successful_check_at"] == "2026-08-22T07:00:00.000Z"
    assert "redacted-test-token" not in str(state.attributes)
    await _unload_entry(hass, entry)


async def test_watch_entities_keep_legacy_ids_and_use_concise_names(hass):
    """Names improve device presentation without changing generated entity IDs."""
    watch = _watch("watch-one")
    entry = await _setup_entry(hass, (watch,))
    entity_registry = er.async_get(hass)

    expected_entities = (
        ("sensor", "watch_watch-one", "Current price"),
        ("sensor", "watch_watch-one_target_price", "Target price"),
        ("sensor", "watch_watch-one_status", "Status"),
        ("sensor", "watch_watch-one_last_checked", "Last valid observation"),
        ("binary_sensor", "watch_watch-one_target_match", "Target match"),
    )
    for platform, unique_id, name in expected_entities:
        entity_id = _entity_id(entity_registry, platform, unique_id)
        entity = entity_registry.async_get(entity_id)
        assert entity is not None
        assert entity.original_name == name

    assert _entity_id(entity_registry, "sensor", "watch_watch-one") == (
        "sensor.heritage_shorts_canyon_xs"
    )
    assert _entity_id(entity_registry, "sensor", "watch_watch-one_target_price") == (
        "sensor.heritage_shorts_target_price"
    )
    assert _entity_id(entity_registry, "sensor", "watch_watch-one_status") == (
        "sensor.heritage_shorts_status"
    )
    assert _entity_id(entity_registry, "sensor", "watch_watch-one_last_checked") == (
        "sensor.heritage_shorts_last_checked"
    )
    assert _entity_id(
        entity_registry, "binary_sensor", "watch_watch-one_target_match"
    ) == ("binary_sensor.heritage_shorts_target_match")
    await _unload_entry(hass, entry)


async def test_primary_sensor_does_not_expose_image_capability_data(hass):
    """Product image data belongs only to the linked HA image entity."""
    watch = _watch(
        "watch-one",
        product_image_url=(
            "http://price-watch.test:8787/v1/watches/watch-one/image?"
            "token=" + "a" * 43
        ),
    )
    entry = await _setup_entry(hass, (watch,))
    entity_registry = er.async_get(hass)
    state = hass.states.get(_entity_id(entity_registry, "sensor", "watch_watch-one"))

    assert state is not None
    assert "price-watch.test" not in str(state.attributes)
    assert "token=" not in str(state.attributes)
    await _unload_entry(hass, entry)


async def test_legacy_cached_image_is_not_retryable_without_a_persisted_source():
    """The client carries server retry availability separately from its URL."""
    client = PriceWatchApiClient(
        "http://price-watch.test:8787",
        "test-token",
        AsyncMock(),
    )
    parsed_watch = client._parse_watch(
        _api_watch_payload(
            "http://price-watch.test:8787/v1/watches/watch-one/image?token="
            + "a" * 43,
            product_image_retry_available=False,
        )
    )
    assert parsed_watch.product_image_url is not None
    assert parsed_watch.product_image_retry_available is False


async def test_product_image_url_rejects_retailer_url():
    """A retailer-hosted image must never reach Home Assistant entity state."""
    client = PriceWatchApiClient(
        "http://price-watch.test:8787",
        "test-token",
        AsyncMock(),
    )
    payload = _api_watch_payload(
        "https://www.lornajane.com.au/images/example.jpg"
    )

    with pytest.raises(PriceWatchInvalidResponseError):
        client._parse_watch(payload)


async def test_product_image_url_accepts_exact_same_service_capability_endpoint():
    """An absolute image URL needs the expected watch route and token."""
    client = PriceWatchApiClient(
        "http://price-watch.test:8787",
        "test-token",
        AsyncMock(),
    )
    capability_token = "a" * 43

    parsed_watch = client._parse_watch(
        _api_watch_payload(
            "http://price-watch.test:8787/v1/watches/watch-one/image?token="
            + capability_token
        )
    )

    assert parsed_watch.product_image_url == (
        "http://price-watch.test:8787/v1/watches/watch-one/image?token="
        + capability_token
    )
    assert client._parse_watch(_api_watch_payload()).product_image_url is None


async def test_product_image_url_accepts_app_relative_capability_endpoint():
    """The App's relative capability link resolves only against this service."""
    client = PriceWatchApiClient(
        "http://price-watch.test:8787",
        "test-token",
        AsyncMock(),
    )
    capability_token = "a" * 43

    parsed_watch = client._parse_watch(
        _api_watch_payload(
            f"/v1/watches/watch-one/image?token={capability_token}"
        )
    )

    assert parsed_watch.product_image_url == (
        "http://price-watch.test:8787/v1/watches/watch-one/image?"
        f"token={capability_token}"
    )


@pytest.mark.parametrize(
    "image_url",
    (
        "/v1/watches/watch-one/image",
        "/v1/watches/watch-one/image?token=short",
        "/v1/watches/watch-one/image?token=" + "a" * 43 + "&extra=value",
        "/v1/watches/watch-one/other?token=" + "a" * 43,
        "http://price-watch.test:8787/v1/watches/watch-one/image",
        "http://price-watch.test:8787/v1/other?token=" + "a" * 43,
        "http://price-watch.test:8787/v1/watches/watch-two/image?token=" + "a" * 43,
        "//www.lornajane.com.au/v1/watches/watch-one/image?token=" + "a" * 43,
    ),
)
async def test_product_image_url_rejects_invalid_capability_endpoint(image_url):
    """Values cannot escape the expected App image capability route."""
    client = PriceWatchApiClient(
        "http://price-watch.test:8787",
        "test-token",
        AsyncMock(),
    )

    with pytest.raises(PriceWatchInvalidResponseError):
        client._parse_watch(_api_watch_payload(image_url))
