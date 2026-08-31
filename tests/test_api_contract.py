"""Focused API-boundary proofs for the HA MVP contract."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.price_watch.api import (
    PriceWatchApiClient,
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


async def test_json_responses_are_bounded_before_parsing():
    client = PriceWatchApiClient(
        "https://price-watch.test", "token", _LargeBodySession()
    )
    with pytest.raises(PriceWatchInvalidResponseError):
        await client.async_get_summary()


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
    for value in (
        "http://service.example",
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
