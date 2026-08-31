"""Regression tests for guarded product images and HA's standard image proxy."""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, patch

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.price_watch.api import (
    PriceWatchApiClient,
    PriceWatchApiResponseError,
    PriceWatchCurrentObservation,
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
    DATA_IMAGE_MANAGERS,
    DATA_IMAGE_PROXIES,
    DOMAIN,
    SERVICE_RELOAD_IMAGE,
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
        status: int = 200,
        content_length: int | None = None,
    ) -> None:
        self.status = status
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
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


def _image_entity_id(hass) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "image", DOMAIN, "watch_watch-one_product_image"
    )
    assert entity_id is not None
    return entity_id


async def test_ha_standard_image_proxy_accepts_its_managed_token(
    hass, hass_client_no_auth
):
    """The normal HA image route serves guarded bytes with its own token."""
    entry = await _setup_entry(hass, (_watch(),))
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]
    client.async_get_product_image = AsyncMock(
        return_value=PriceWatchProductImage(b"image-data", "image/jpeg")
    )
    image_entity_id = _image_entity_id(hass)
    state = hass.states.get(image_entity_id)
    assert state is not None
    picture_url = state.attributes["entity_picture"]
    assert state.attributes["image_status"] == "unavailable"
    assert state.attributes["image_last_updated"]
    assert set(state.attributes) >= {"image_status", "image_last_updated"}

    assert picture_url.startswith(f"/api/image_proxy/{image_entity_id}?token=")
    assert "/api/price_watch/image/" not in picture_url
    assert "price-watch.test" not in str(state.attributes)
    assert _CAPABILITY_TOKEN not in str(state.attributes)

    response = await (await hass_client_no_auth()).get(picture_url)

    assert response.status == 200
    assert await response.read() == b"image-data"
    assert response.content_type == "image/jpeg"
    client.async_get_product_image.assert_awaited_once_with(
        _CAPABILITY_URL, request_id=ANY
    )
    await _unload_entry(hass, entry)


async def test_ha_image_proxy_rejects_missing_and_invalid_tokens(
    hass, hass_client_no_auth
):
    """HA's built-in image endpoint owns standard browser token validation."""
    entry = await _setup_entry(hass, (_watch(),))
    image_entity_id = _image_entity_id(hass)
    client = await hass_client_no_auth()

    missing_token = await client.get(f"/api/image_proxy/{image_entity_id}")
    invalid_token = await client.get(
        f"/api/image_proxy/{image_entity_id}?token=not-a-valid-token"
    )

    assert missing_token.status == 403
    assert invalid_token.status == 403
    await _unload_entry(hass, entry)


async def test_old_custom_image_proxy_route_is_not_registered(hass, hass_client):
    """The previous custom route is absent after switching to ImageEntity."""
    entry = await _setup_entry(hass, (_watch(),))

    response = await (await hass_client()).get("/api/price_watch/image/watch-one")

    assert response.status == 404
    await _unload_entry(hass, entry)


async def test_image_platform_unload_removes_its_manager(hass):
    """The image platform removes its coordinator listener during unload."""
    entry = await _setup_entry(hass, (_watch(),))

    assert entry.entry_id in hass.data[DOMAIN][DATA_IMAGE_MANAGERS]

    await _unload_entry(hass, entry)

    assert DOMAIN not in hass.data


async def test_ha_image_proxy_cannot_serve_unknown_or_removed_watches(
    hass, hass_client
):
    """Unknown images 404; removed watch images fail without upstream access."""
    entry = await _setup_entry(hass, (_watch(),))
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]
    client.async_get_product_image = AsyncMock()
    web_client = await hass_client()

    unknown = await web_client.get("/api/image_proxy/image.unknown?token=invalid")
    assert unknown.status == 404

    image_entity_id = _image_entity_id(hass)
    picture_url = hass.states.get(image_entity_id).attributes["entity_picture"]
    coordinator = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id]
    coordinator.async_set_updated_data(
        PriceWatchCoordinatorData(summary=_summary(), watches=(), events=())
    )
    await hass.async_block_till_done()
    removed = await web_client.get(picture_url)

    assert removed.status == 500
    assert client.async_get_product_image.await_count == 0
    await _unload_entry(hass, entry)


async def test_ha_image_proxy_returns_safe_upstream_failure(hass, hass_client, caplog):
    """A failed guarded fetch cannot disclose private capability information."""
    entry = await _setup_entry(hass, (_watch(),))
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]
    client.async_get_product_image = AsyncMock(side_effect=PriceWatchTransportError)
    image_entity_id = _image_entity_id(hass)
    picture_url = hass.states.get(image_entity_id).attributes["entity_picture"]

    response = await (await hass_client()).get(picture_url)

    assert response.status == 500
    assert _CAPABILITY_URL not in await response.text()
    assert _CAPABILITY_TOKEN not in caplog.text
    assert "price-watch.test" not in caplog.text
    await _unload_entry(hass, entry)


async def test_reload_image_invalidates_one_watch_and_refreshes_image_proxy(hass, hass_client_no_auth):
    """Reloads one cache entry without checking or changing service state."""
    entry = await _setup_entry(hass, (_watch(),))
    image_cache = hass.data[DOMAIN][DATA_IMAGE_PROXIES][entry.entry_id]
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]
    client.async_get_product_image = AsyncMock(
        side_effect=(
            PriceWatchProductImage(b"first", "image/jpeg"),
            PriceWatchProductImage(b"second", "image/jpeg"),
        )
    )
    image_entity_id = _image_entity_id(hass)
    picture_url = hass.states.get(image_entity_id).attributes["entity_picture"]
    web_client = await hass_client_no_auth()

    first = await web_client.get(picture_url)
    assert first.status == 200
    assert await first.read() == b"first"
    before = hass.states.get(image_entity_id)
    assert before is not None
    before_updated = before.attributes["image_last_updated"]
    observations_before = hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id].data.watches[0].current_observation_id

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RELOAD_IMAGE,
        {"watch_id": "watch-one"},
        blocking=True,
    )
    await hass.async_block_till_done()
    after = hass.states.get(image_entity_id)
    assert after is not None
    assert after.attributes["image_last_updated"] != before_updated
    assert after.attributes["image_status"] == "refresh_requested"

    second = await web_client.get(picture_url)
    assert second.status == 200
    assert await second.read() == b"second"
    assert client.async_get_product_image.await_count == 2
    assert hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id].data.watches[0].current_observation_id == observations_before
    await _unload_entry(hass, entry)


async def test_reload_image_failure_preserves_prior_image_without_check(hass):
    """A failed same-source reload retains prior bytes and service state."""
    entry = await _setup_entry(hass, (_watch(),))
    image_cache = hass.data[DOMAIN][DATA_IMAGE_PROXIES][entry.entry_id]
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]
    check_watch = AsyncMock()
    client.async_check_watch = check_watch
    client.async_get_product_image = AsyncMock(
        side_effect=(PriceWatchProductImage(b"first", "image/jpeg"), PriceWatchTransportError)
    )
    assert (
        await image_cache.async_get_image("watch-one", request_id="a" * 36)
    ).content == b"first"
    before_updated = hass.states.get(_image_entity_id(hass)).attributes[
        "image_last_updated"
    ]

    with pytest.raises(Exception):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RELOAD_IMAGE,
            {"watch_id": "watch-one"},
            blocking=True,
        )

    assert (
        await image_cache.async_get_image("watch-one", request_id="b" * 36)
    ).content == b"first"
    assert (
        hass.states.get(_image_entity_id(hass)).attributes["image_last_updated"]
        == before_updated
    )
    check_watch.assert_not_awaited()
    await _unload_entry(hass, entry)


async def test_reload_image_failure_has_no_check_or_observation_side_effect(hass):
    """An unknown watch reload fails without invoking the API or changing state."""
    entry = await _setup_entry(hass, (_watch(),))
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]
    check_watch = AsyncMock()
    client.async_check_watch = check_watch
    with pytest.raises(Exception):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RELOAD_IMAGE,
            {"watch_id": "missing-watch"},
            blocking=True,
        )
    check_watch.assert_not_awaited()
    await _unload_entry(hass, entry)


async def test_image_cache_invalidates_for_observation_and_image_changes(hass):
    """A new observation or capability URL cannot reuse stale cached bytes."""
    entry = await _setup_entry(hass, (_watch(),))
    image_cache = hass.data[DOMAIN][DATA_IMAGE_PROXIES][entry.entry_id]
    client = hass.data[DOMAIN][DATA_CLIENTS][entry.entry_id]
    client.async_get_product_image = AsyncMock(
        side_effect=(
            PriceWatchProductImage(b"first", "image/jpeg"),
            PriceWatchProductImage(b"second", "image/jpeg"),
            PriceWatchProductImage(b"third", "image/jpeg"),
        )
    )

    first = await image_cache.async_get_image("watch-one", request_id="a" * 36)
    cached = await image_cache.async_get_image("watch-one", request_id="b" * 36)
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
    refreshed = await image_cache.async_get_image("watch-one", request_id="c" * 36)
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
    changed = await image_cache.async_get_image("watch-one", request_id="d" * 36)
    assert changed.content == b"third"
    assert client.async_get_product_image.await_count == 3
    await _unload_entry(hass, entry)


@pytest.mark.parametrize(
    ("content_type", "body", "content_length", "status", "error_type"),
    (
        ("", b"image", None, 200, PriceWatchInvalidResponseError),
        ("text/html", b"<html>", None, 200, PriceWatchInvalidResponseError),
        ("application/octet-stream", b"image", None, 200, PriceWatchInvalidResponseError),
        ("image/jpeg", b"image", 5 * 1024 * 1024 + 1, 200, PriceWatchInvalidResponseError),
        ("image/png", b"x" * (5 * 1024 * 1024 + 1), None, 200, PriceWatchInvalidResponseError),
        ("image/jpeg", b"", None, 302, PriceWatchApiResponseError),
    ),
)
async def test_image_client_rejects_invalid_oversized_and_redirect_responses(
    content_type, body, content_length, status, error_type
):
    """The client bounds bytes, validates media, and never follows redirects."""
    session = _ImageSession(
        _ImageResponse(
            body, content_type, status=status, content_length=content_length
        )
    )
    client = PriceWatchApiClient(
        "http://price-watch.test:8787", "test-token", session
    )

    with pytest.raises(error_type) as exc_info:
        await client.async_get_product_image(_CAPABILITY_URL)

    assert _CAPABILITY_URL not in str(exc_info.value)
    assert session.requests[0]["url"] == _CAPABILITY_URL
    assert session.requests[0]["allow_redirects"] is False


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
    assert len(session.requests) == 1
    request = session.requests[0]
    assert request["url"] == _CAPABILITY_URL
    assert "Authorization" in request["headers"]
    assert request["headers"]["request-id"] == ANY
    assert request["allow_redirects"] is False
