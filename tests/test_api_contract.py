"""Focused API-boundary proofs for the HA MVP contract."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from custom_components.price_watch.api import (
    PriceWatchApiClient,
    PriceWatchApiResponseError,
    PriceWatchInvalidResponseError,
    normalise_base_url,
)

pytestmark = pytest.mark.asyncio


class _Response:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def json(self, *, content_type=None):
        return self._payload


class _LargeBodyResponse(_Response):
    @property
    def content(self):
        return self

    async def read(self, size: int) -> bytes:
        return b"{" + (b"x" * size)


class _LargeBodySession:
    def get(self, url: str, **kwargs):
        return _LargeBodyResponse({})


class _RedirectSession:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def _response(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        return _Response({}, status=302)

    def get(self, url: str, **kwargs):
        return self._response("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self._response("POST", url, **kwargs)

    def patch(self, url: str, **kwargs):
        return self._response("PATCH", url, **kwargs)


class _Session:
    def __init__(self, payloads: list[object]) -> None:
        self.payloads = iter(payloads)
        self.requests: list[dict[str, object]] = []

    def get(self, url: str, **kwargs):
        self.requests.append({"method": "GET", "url": url, **kwargs})
        return _Response(next(self.payloads))

    def post(self, url: str, **kwargs):
        self.requests.append({"method": "POST", "url": url, **kwargs})
        return _Response(next(self.payloads))

    def patch(self, url: str, **kwargs):
        self.requests.append({"method": "PATCH", "url": url, **kwargs})
        return _Response(next(self.payloads))


def _watch(watch_id: str) -> dict[str, object]:
    return {
        "id": watch_id,
        "retailer_id": "lorna_jane",
        "product_url": "https://www.lornajane.com.au/products/example",
        "title": "Example",
        "variant": {"retailer_variant_id": watch_id},
        "enabled": True,
        "target_price_cents": None,
        "check_interval_minutes": 60,
        "current_observation_id": None,
        "current_observation": None,
        "last_successful_check_at": None,
        "last_attempt_at": None,
        "last_attempt_status": "check_failed",
        "last_attempt_error_code": "blocked",
        "product_image_retry_available": False,
    }


async def test_watch_retrieval_follows_five_pages_and_requests_100_per_page():
    pages = [
        {"data": [_watch("watch-1")], "next_cursor": "cursor-1"},
        {"data": [_watch("watch-2")], "next_cursor": None},
    ]
    session = _Session(pages)
    client = PriceWatchApiClient("https://price-watch.test", "token", session)

    watches = await client.async_get_watches()

    assert [watch.id for watch in watches] == ["watch-1", "watch-2"]
    assert [request["url"] for request in session.requests] == [
        "https://price-watch.test/v1/watches?limit=100",
        "https://price-watch.test/v1/watches?limit=100&cursor=cursor-1",
    ]


@pytest.mark.parametrize(
    ("field", "replacement", "expected_path", "expected_reason"),
    (
        (
            "product_image_url",
            "https://evil.example/image?token=secret-token",
            "data[*].product_image_url",
            "unsafe_url",
        ),
        ("variant", {}, "data[*].variant", "selector_required"),
        (
            "current_observation",
            {"id": "observation-one"},
            "data[*].current_observation.checked_at",
            "required_non_empty_string",
        ),
    ),
)
async def test_watch_parser_reports_bounded_safe_field_diagnostics(
    field, replacement, expected_path, expected_reason
):
    payload = _watch("watch-one")
    payload[field] = deepcopy(replacement)
    client = PriceWatchApiClient("https://price-watch.test", "token", AsyncMock())

    with pytest.raises(PriceWatchInvalidResponseError) as caught:
        client._parse_watch(payload)

    assert caught.value.field_path == expected_path
    assert caught.value.reason == expected_reason
    assert "secret-token" not in str(caught.value)


async def test_watch_retrieval_reports_safe_generic_payload_diagnostic():
    client = PriceWatchApiClient(
        "https://price-watch.test", "token", _Session([{"data": "not-a-list"}])
    )

    with pytest.raises(PriceWatchInvalidResponseError) as caught:
        await client.async_get_watches()

    assert caught.value.field_path == "data"
    assert caught.value.reason == "expected_list"


async def test_watch_retrieval_fails_closed_on_a_sixth_page():
    pages = [
        {"data": [_watch(f"watch-{page}")], "next_cursor": f"cursor-{page + 1}"}
        for page in range(5)
    ]
    session = _Session(pages)
    client = PriceWatchApiClient("https://price-watch.test", "token", session)

    with pytest.raises(PriceWatchInvalidResponseError):
        await client.async_get_watches()
    assert len(session.requests) == 5


async def test_latest_events_are_type_filtered_and_deterministically_sorted():
    event = lambda event_id, occurred_at: {
        "event_id": event_id,
        "watch_id": "watch-1",
        "observation_id": None,
        "type": "target_reached",
        "occurred_at": occurred_at,
        "deduplication_key": f"target_reached:watch-1:{event_id}",
        "data": {},
    }
    session = _Session(
        [{"data": [event("event-a", "2026-08-01T00:00:00Z"), event("event-z", "2026-08-01T00:00:00Z")]}]
    )
    client = PriceWatchApiClient("https://price-watch.test", "token", session)

    events = await client.async_get_events(event_type="target_reached", limit=2)

    assert [item.id for item in events] == ["event-z", "event-a"]
    assert session.requests[0]["url"] == "https://price-watch.test/v1/events?limit=2&type=target_reached"


@pytest.mark.parametrize("operation", ["health", "summary", "mutation"])
async def test_authenticated_json_requests_never_follow_redirects(operation):
    session = _RedirectSession()
    client = PriceWatchApiClient("https://service.example", "secret-token", session)
    with pytest.raises(PriceWatchApiResponseError):
        if operation == "health":
            await client.async_get_health()
        elif operation == "summary":
            await client.async_get_summary()
        else:
            await client.async_check_all(idempotency_key="redirect-test")
    assert len(session.requests) == 1
    request = session.requests[0]
    assert request["allow_redirects"] is False
    assert request["url"].startswith("https://service.example/")
    assert request["headers"]["Authorization"] == "Bearer secret-token"
    assert "evil.example" not in str(session.requests)


async def test_json_responses_are_bounded_before_parsing():
    client = PriceWatchApiClient(
        "https://price-watch.test", "token", _LargeBodySession()
    )
    with pytest.raises(PriceWatchInvalidResponseError):
        await client.async_get_summary()


async def test_target_price_mutation_generates_internal_idempotency_and_validates_cents():
    session = _Session([_watch("watch-1")])
    client = PriceWatchApiClient("https://price-watch.test", "token", session)
    await client.async_set_target_price("watch-1", 1234)
    request = session.requests[0]
    assert request["method"] == "PATCH"
    assert request["json"] == {"target_price_cents": 1234}
    assert isinstance(request["headers"]["Idempotency-Key"], str)
    with pytest.raises(ValueError):
        await client.async_set_target_price("watch-1", -1)
    with pytest.raises(ValueError):
        await client.async_set_target_price("watch-1", True)


async def test_image_retry_uses_authenticated_idempotent_post():
    session = _Session([{"status": "image_cached"}, {"status": "image_cached"}])
    client = PriceWatchApiClient("https://price-watch.test", "token", session)

    first = await client.async_retry_product_image(
        "watch-1", idempotency_key="image-retry-1"
    )
    replay = await client.async_retry_product_image(
        "watch-1", idempotency_key="image-retry-1"
    )

    assert first.status == "image_cached"
    assert replay == first
    assert [request["method"] for request in session.requests] == ["POST", "POST"]
    assert [request["url"] for request in session.requests] == [
        "https://price-watch.test/v1/watches/watch-1/image/retry",
        "https://price-watch.test/v1/watches/watch-1/image/retry",
    ]
    assert [request["headers"]["Idempotency-Key"] for request in session.requests] == [
        "image-retry-1",
        "image-retry-1",
    ]


async def test_action_key_format_is_bounded():
    client = PriceWatchApiClient("https://price-watch.test", "token", _Session([]))
    with pytest.raises(ValueError):
        await client.async_check_all(idempotency_key="bad/key")
    with pytest.raises(ValueError):
        await client.async_check_all(idempotency_key="x" * 129)


async def test_action_key_is_forwarded_unchanged_for_retries():
    payload = {"id": "obs-1", "watch_id": "watch-1", "checked_at": "2026-08-01T00:00:00Z", "status": "blocked", "currency": "AUD"}
    session = _Session([payload, payload])
    client = PriceWatchApiClient("https://price-watch.test", "token", session)

    await client.async_check_watch("watch-1", idempotency_key="card-action-1")
    await client.async_check_watch("watch-1", idempotency_key="card-action-1")

    assert [request["headers"]["Idempotency-Key"] for request in session.requests] == [
        "card-action-1",
        "card-action-1",
    ]


async def test_service_and_product_url_boundaries():
    assert normalise_base_url("https://service.example/path/") == "https://service.example/path"
    assert normalise_base_url("http://price-watch.test:8787") == "http://price-watch.test:8787"
    assert normalise_base_url("HTTP://HomeAssistant.Local.:8787/") == "http://homeassistant.local:8787"
    for value in (
        "http://service.example",
        "http://evil.local",
        "http://.local",
        "http://evil..local",
        "http://192.168.1.10:8787",
        "http://127.0.0.1:8787",
        "http://[malformed",
        "http://homeassistant.local:8787?token=secret",
        "https://user:password@service.example",
        "https://service.example?token=secret",
    ):
        with pytest.raises(ValueError):
            normalise_base_url(value)

    client = PriceWatchApiClient("https://service.example", "token", AsyncMock())
    payload = _watch("watch-1")
    payload["product_url"] = "http://www.lornajane.com.au/product"
    with pytest.raises(PriceWatchInvalidResponseError):
        client._parse_watch(payload)
