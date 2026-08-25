"""Regression tests for the authenticated Home Assistant product-image proxy."""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.price_watch.api import (
    PriceWatchApiClient,
    PriceWatchCurrentObservation,
    PriceWatchEvent,
    PriceWatchInvalidResponseError,
    PriceWatchProductImage,
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
    DATA_IMAGE_PROXIES,
    DATA_IMAGE_PROXY_VIEW,
    DOMAIN,
)
from custom_components.price_watch.coordinator import PriceWatchCoordinatorData

pytestmark = pytest.mark.asyncio

_CAPABILITY_TOKEN = "a" * 43
_CAPABILITY_URL = (
    "http://price-watch.test:8787/v1/watches/watch-one/image?"
    f"token={_CAPABILITY_TOKEN}"
)


class _ImageContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def read(self, limit: int) -> bytes:
        return self._body


class _ImageResponse:
    def __init__(
        self,
        body: bytes,
        content_type: str,
        *,
        content_length: int | None = None,
    ) -> None:
        self.status = 200
        self.headers = {"Content-Type": content_type}
        self.content_length = content_length
        self.content = _ImageContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _ImageSession:
    def __init__(self, response: _ImageResponse) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []

    def get(self, url: str, **kwargs):
        self.requests.append({"url": url, **kwargs})
        return self.response


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
        latest_check_at="2026-08-25T04:00:00.000Z",
        target_matching_watch_ids=(),
    )


def _watch(
    watch_id: str = "watch-one",
    *,
    product_image_url: str | None = _CAPABILITY_URL,
    observation_id: str = "observation-one",
) -> PriceWatchWatch:
    return PriceWatchWatch(
        id=watch_id,
        retailer_id="lorna_jane_au",
        product_url="https://www.lornajane.com.au/products/example",
        title="Heritage Shorts",
        variant=PriceWatchVariant(
            retailer_variant_id="48573064806635",
            options={"Colour": "Canyon", "Size": "XS"},
        ),
        enabled=True,
        target_price_cents=8500,
        check_interval_minutes=60,
        current_observation_id=observation_id,
        current_observation=PriceWatchCurrentObservation(
            id=observation_id,
            checked_at="2026-08-25T04:00:00.000Z",
            status="available",
            price_cents=8500,
            compare_at_price_cents=None,
            currency="AUD",
            selected_variant_label="Canyon / XS",
            error_code=None,
        ),
        last_successful_check_at="2026-08-25T04:00:00.000Z",
        last_attempt_at="2026-08-25T04:00:00.000Z",
        product_image_url=product_image_url,
    )


async def _setup_entry(hass, watches: tuple[PriceWatchWatch, ...]):
    hass.http = MagicMock()
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


async def _unload_entry(hass, config_entry) -> None:
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_entity_picture_uses_local_proxy_without_capability_data(hass):
    """Entity state must not expose the App hostname or capability token."""
    entry = await _setup_entry(hass, (_watch(),))

    state = hass.states.get("sensor.heritage_shorts_canyon_xs")
    assert state is not None
    assert state.attributes["entity_picture"] == "/api/price_watch/image/watch-one"
    assert "price-watch.test" not in str(state.attributes)
    assert _CAPABILITY_TOKEN not in str(state.attributes)

    view = hass.data[DOMAIN][DATA_IMAGE_PROXY_VIEW]
    assert view.url == "/api/price_watch/image/{watch_id}"
    assert view.requires_auth is True
    await _unload_entry(hass, entry)


async def test_image_proxy_rejects_unknown_and_removed_watches(hass):
    """Only watches in the current coordinator snapshot can be served."""
    entry = await _setup_entry(hass, (_watch(),))
    view = hass.data[DOMAIN][DATA_IMAGE_PROXY_VIEW]
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]
    client.async_get_product_image = AsyncMock()

    with pytest.raises(web.HTTPNotFound):
        await view.get(MagicMock(), "unknown-watch")
    assert client.async_get_product_image.await_count == 0

    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    coordinator.async_set_updated_data(
        PriceWatchCoordinatorData(summary=_summary(), watches=(), events=())
    )
    with pytest.raises(web.HTTPNotFound):
        await view.get(MagicMock(), "watch-one")
    assert client.async_get_product_image.await_count == 0
    await _unload_entry(hass, entry)


async def test_image_proxy_fetches_only_approved_capability_and_returns_bytes(hass):
    """The view passes only the coordinator-held capability URL to the client."""
    entry = await _setup_entry(hass, (_watch(),))
    view = hass.data[DOMAIN][DATA_IMAGE_PROXY_VIEW]
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]
    client.async_get_product_image = AsyncMock(
        return_value=PriceWatchProductImage(b"image-data", "image/jpeg")
    )

    response = await view.get(MagicMock(), "watch-one")

    assert response.status == 200
    assert response.body == b"image-data"
    assert response.content_type == "image/jpeg"
    assert response.headers["Cache-Control"] == "no-store"
    client.async_get_product_image.assert_awaited_once_with(
        _CAPABILITY_URL, request_id=ANY
    )
    await _unload_entry(hass, entry)


async def test_image_proxy_uses_safe_failure_for_upstream_errors(hass, caplog):
    """Upstream errors must not reveal the capability URL in response or logs."""
    entry = await _setup_entry(hass, (_watch(),))
    view = hass.data[DOMAIN][DATA_IMAGE_PROXY_VIEW]
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]
    client.async_get_product_image = AsyncMock(side_effect=PriceWatchTransportError)

    with pytest.raises(web.HTTPBadGateway) as exc_info:
        await view.get(MagicMock(), "watch-one")

    assert _CAPABILITY_URL not in str(exc_info.value)
    assert _CAPABILITY_TOKEN not in caplog.text
    assert "price-watch.test" not in caplog.text
    await _unload_entry(hass, entry)


async def test_image_proxy_invalidates_cache_for_observation_and_image_changes(hass):
    """A new observation or capability URL cannot reuse stale cached bytes."""
    entry = await _setup_entry(hass, (_watch(),))
    proxy = hass.data[DOMAIN][DATA_IMAGE_PROXIES][entry.entry_id]
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]
    client.async_get_product_image = AsyncMock(
        side_effect=(
            PriceWatchProductImage(b"first", "image/jpeg"),
            PriceWatchProductImage(b"second", "image/jpeg"),
            PriceWatchProductImage(b"third", "image/jpeg"),
        )
    )

    first = await proxy.async_get_image("watch-one", request_id="a" * 36)
    cached = await proxy.async_get_image("watch-one", request_id="b" * 36)
    assert first.content == b"first"
    assert cached.content == b"first"

    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    coordinator.async_set_updated_data(
        PriceWatchCoordinatorData(
            summary=_summary("watch-one"),
            watches=(_watch(observation_id="observation-two"),),
            events=(),
        )
    )
    refreshed = await proxy.async_get_image("watch-one", request_id="c" * 36)
    assert refreshed.content == b"second"

    changed_url = _CAPABILITY_URL.replace("a" * 43, "b" * 43)
    coordinator.async_set_updated_data(
        PriceWatchCoordinatorData(
            summary=_summary("watch-one"),
            watches=(
                _watch(
                    product_image_url=changed_url,
                    observation_id="observation-two",
                ),
            ),
            events=(),
        )
    )
    changed = await proxy.async_get_image("watch-one", request_id="d" * 36)
    assert changed.content == b"third"
    assert client.async_get_product_image.await_count == 3
    await _unload_entry(hass, entry)


@pytest.mark.parametrize(
    ("content_type", "body", "content_length"),
    (
        ("", b"image", None),
        ("text/html", b"<html>", None),
        ("application/octet-stream", b"image", None),
        ("image/jpeg", b"image", 5 * 1024 * 1024 + 1),
        ("image/png", b"x" * (5 * 1024 * 1024 + 1), None),
    ),
)
async def test_image_client_rejects_invalid_or_oversized_responses(
    content_type, body, content_length
):
    """The client bounds image bytes and accepts only explicitly supported media."""
    session = _ImageSession(
        _ImageResponse(body, content_type, content_length=content_length)
    )
    client = PriceWatchApiClient(
        "http://price-watch.test:8787", "test-token", session
    )

    with pytest.raises(PriceWatchInvalidResponseError) as exc_info:
        await client.async_get_product_image(_CAPABILITY_URL)

    assert _CAPABILITY_URL not in str(exc_info.value)
    assert session.requests[0]["url"] == _CAPABILITY_URL


async def test_image_client_returns_allowed_image_without_redirects():
    """The internal client returns approved bytes with redirects disabled."""
    session = _ImageSession(
        _ImageResponse(b"image", "image/webp; charset=binary")
    )
    client = PriceWatchApiClient(
        "http://price-watch.test:8787", "test-token", session
    )

    image = await client.async_get_product_image(_CAPABILITY_URL)

    assert image == PriceWatchProductImage(b"image", "image/webp")
    assert session.requests == [
        {
            "url": _CAPABILITY_URL,
            "headers": {
                "Authorization": "Bearer test-token",
                "request-id": ANY,
            },
            "allow_redirects": False,
        }
    ]
