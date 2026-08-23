"""Tests for safe structured integration operation logs."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.price_watch.api import (
    PriceWatchApiClient,
    PriceWatchApiResponseError,
    PriceWatchAuthenticationError,
    PriceWatchCurrentObservation,
    PriceWatchInvalidResponseError,
    PriceWatchSummary,
    PriceWatchTimeoutError,
    PriceWatchTransportError,
    PriceWatchVariant,
    PriceWatchWatch,
)
from custom_components.price_watch.const import (
    ATTR_ENABLED,
    ATTR_WATCH_ID,
    DATA_CLIENTS,
    DATA_COORDINATORS,
    DOMAIN,
    SERVICE_ADD_TO_SHOPPING_LIST,
    SERVICE_CHECK_ALL,
    SERVICE_CHECK_WATCH,
    SERVICE_SET_ENABLED,
    SHOPPING_LIST_ADD_ITEM,
    SHOPPING_LIST_DOMAIN,
    SHOPPING_LIST_ITEM_NAME,
)
from custom_components.price_watch.coordinator import (
    PriceWatchCoordinator,
    PriceWatchCoordinatorData,
)
from custom_components.price_watch.services import async_register_services

pytestmark = pytest.mark.asyncio

_LOGGER_NAME = "custom_components.price_watch"
_BEARER_TOKEN = "Bearer representative-api-token"
_IMAGE_CAPABILITY_URL = (
    "https://price-watch.example/v1/watches/watch-one/image?capability=image-token"
)
_RETAILER_URL = "https://retailer.example/products/private-shorts"


def _watch() -> PriceWatchWatch:
    return PriceWatchWatch(
        id="watch-one",
        retailer_id="retailer",
        product_url=_RETAILER_URL,
        title="Private Shorts",
        variant=PriceWatchVariant(retailer_variant_id="variant-one", options=None),
        enabled=True,
        target_price_cents=5000,
        check_interval_minutes=60,
        current_observation_id="observation-one",
        current_observation=PriceWatchCurrentObservation(
            id="observation-one",
            checked_at="2026-08-22T07:00:00.000Z",
            status="available",
            price_cents=5000,
            compare_at_price_cents=None,
            currency="AUD",
            selected_variant_label="Small",
            error_code=None,
        ),
        last_successful_check_at="2026-08-22T07:00:00.000Z",
        last_attempt_at="2026-08-22T07:00:00.000Z",
        product_image_url=_IMAGE_CAPABILITY_URL,
    )


def _messages(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _LOGGER_NAME
    ]


def _assert_no_sensitive_values(caplog) -> None:
    logs = "\n".join(_messages(caplog))
    assert _BEARER_TOKEN not in logs
    assert _IMAGE_CAPABILITY_URL not in logs
    assert _RETAILER_URL not in logs


async def test_coordinator_logs_each_failure_classification_without_secrets(
    hass, caplog
):
    """Coordinator failures remain structured and omit exception details."""
    cases = (
        (
            PriceWatchAuthenticationError(401),
            "authentication",
            ConfigEntryAuthFailed,
            "http_status=401",
        ),
        (PriceWatchTimeoutError(_BEARER_TOKEN), "timeout", UpdateFailed, None),
        (
            PriceWatchTransportError(_RETAILER_URL),
            "transport",
            UpdateFailed,
            None,
        ),
        (
            PriceWatchInvalidResponseError(_IMAGE_CAPABILITY_URL),
            "invalid_response",
            UpdateFailed,
            None,
        ),
        (
            PriceWatchApiResponseError(503, "upstream_unavailable"),
            "api_response",
            UpdateFailed,
            "http_status=503 service_code=upstream_unavailable",
        ),
    )

    for error, category, expected_exception, details in cases:
        client = AsyncMock(spec=PriceWatchApiClient)
        client.async_get_summary.side_effect = error
        coordinator = PriceWatchCoordinator(
            hass,
            MockConfigEntry(domain=DOMAIN),
            client,
        )
        caplog.clear()
        caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

        with pytest.raises(expected_exception):
            await coordinator._async_update_data()

        logs = "\n".join(_messages(caplog))
        assert (
            "operation=coordinator_refresh "
            "route=/v1/summary,/v1/watches,/v1/events "
            f"outcome=failed failure={category}"
        ) in logs
        if details is not None:
            assert details in logs
        _assert_no_sensitive_values(caplog)


async def test_services_log_success_at_debug_without_sensitive_values(hass, caplog):
    """All public actions emit concise debug-only success records."""
    client = AsyncMock(spec=PriceWatchApiClient)
    coordinator = SimpleNamespace(
        data=PriceWatchCoordinatorData(
            summary=PriceWatchSummary(1, 0, 0, 0, None),
            watches=(_watch(),),
            events=(),
        ),
        async_request_refresh=AsyncMock(),
    )
    hass.data[DOMAIN] = {
        DATA_CLIENTS: {"entry": client},
        DATA_COORDINATORS: {"entry": coordinator},
    }
    shopping_list_calls: list[dict[str, object]] = []

    async def add_item(call) -> None:
        shopping_list_calls.append(dict(call.data))

    hass.services.async_register(
        SHOPPING_LIST_DOMAIN, SHOPPING_LIST_ADD_ITEM, add_item
    )
    async_register_services(hass)
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)

    await hass.services.async_call(DOMAIN, SERVICE_CHECK_ALL, blocking=True)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_CHECK_WATCH,
        {ATTR_WATCH_ID: "watch-one"},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_ENABLED,
        {ATTR_WATCH_ID: "watch-one", ATTR_ENABLED: False},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_TO_SHOPPING_LIST,
        {ATTR_WATCH_ID: "watch-one"},
        blocking=True,
    )

    logs = "\n".join(_messages(caplog))
    for operation, route in (
        (SERVICE_CHECK_ALL, "/v1/checks"),
        (SERVICE_CHECK_WATCH, "/v1/watches/{watch_id}/check"),
        (SERVICE_SET_ENABLED, "/v1/watches/{watch_id}"),
        (SERVICE_ADD_TO_SHOPPING_LIST, "shopping_list.add_item"),
    ):
        assert f"operation={operation} route={route} outcome=succeeded" in logs
    assert "watch_id=watch-one" in logs
    assert shopping_list_calls
    _assert_no_sensitive_values(caplog)


async def test_services_log_failures_with_safe_api_response_metadata(hass, caplog):
    """API action and Shopping List failures have classified safe metadata."""
    client = AsyncMock(spec=PriceWatchApiClient)
    coordinator = SimpleNamespace(data=None, async_request_refresh=AsyncMock())
    hass.data[DOMAIN] = {
        DATA_CLIENTS: {"entry": client},
        DATA_COORDINATORS: {"entry": coordinator},
    }
    hass.services.async_register(
        SHOPPING_LIST_DOMAIN, SHOPPING_LIST_ADD_ITEM, AsyncMock()
    )
    async_register_services(hass)
    hass.services.async_remove(SHOPPING_LIST_DOMAIN, SHOPPING_LIST_ADD_ITEM)
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

    client.async_check_all.side_effect = PriceWatchApiResponseError(
        429, "rate_limited"
    )
    with pytest.raises(Exception):
        await hass.services.async_call(DOMAIN, SERVICE_CHECK_ALL, blocking=True)

    client.async_check_watch.side_effect = PriceWatchTimeoutError(_BEARER_TOKEN)
    with pytest.raises(Exception):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CHECK_WATCH,
            {ATTR_WATCH_ID: "watch-one"},
            blocking=True,
        )

    client.async_set_enabled.side_effect = PriceWatchTransportError(_RETAILER_URL)
    with pytest.raises(Exception):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_ENABLED,
            {ATTR_WATCH_ID: "watch-one", ATTR_ENABLED: False},
            blocking=True,
        )

    with pytest.raises(Exception):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_TO_SHOPPING_LIST,
            {ATTR_WATCH_ID: "watch-one"},
            blocking=True,
        )

    logs = "\n".join(_messages(caplog))
    assert (
        "operation=check_all route=/v1/checks outcome=failed "
        "failure=api_response http_status=429 service_code=rate_limited"
    ) in logs
    assert (
        "operation=check_watch route=/v1/watches/{watch_id}/check "
        "outcome=failed failure=timeout watch_id=watch-one"
    ) in logs
    assert (
        "operation=set_enabled route=/v1/watches/{watch_id} "
        "outcome=failed failure=transport watch_id=watch-one"
    ) in logs
    assert (
        "operation=add_to_shopping_list route=shopping_list.add_item "
        "outcome=failed failure=api_response watch_id=watch-one "
        "service_code=shopping_list_unavailable"
    ) in logs
    _assert_no_sensitive_values(caplog)
