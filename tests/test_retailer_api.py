"""Tests for the F10 Phase 7a retailer operational API client behaviour."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from custom_components.price_watch.api import (
    PriceWatchApiClient,
    PriceWatchApiResponseError,
    PriceWatchInvalidResponseError,
)

pytestmark = pytest.mark.asyncio


class _Response:
    """A minimal aiohttp-shaped async context manager response."""

    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def json(self, *, content_type=None) -> object:
        return self._payload


class _RecordingSession:
    """Records every request so tests can assert exact endpoint/body/headers."""

    def __init__(self, status: int = 200, payload: object | None = None) -> None:
        self.status = status
        self.payload = payload if payload is not None else {}
        self.requests: list[dict[str, object]] = []

    def get(self, url: str, **kwargs):
        self.requests.append({"method": "GET", "url": url, **kwargs})
        return _Response(self.status, self.payload)

    def post(self, url: str, **kwargs):
        self.requests.append({"method": "POST", "url": url, **kwargs})
        return _Response(self.status, self.payload)

    def patch(self, url: str, **kwargs):
        self.requests.append({"method": "PATCH", "url": url, **kwargs})
        return _Response(self.status, self.payload)


def _retailer_payload(**overrides: object) -> dict[str, object]:
    """Return a minimal, fully valid Phase 6a Retailer response payload."""
    payload: dict[str, object] = {
        "retailer_id": "lorna_jane",
        "enabled": True,
        "acquisition_methods": ["http"],
        "interpretation_mode": "legacy_adapter",
        "status": "healthy",
        "preferred_strategy": "auto",
        "active_strategy": "http",
        "effective_strategy_reason": "auto_lowest_cost",
        "metrics": [],
        "watch_impact": {"active_watch_count": 3, "affected_watch_count": 0},
    }
    payload.update(overrides)
    return payload


def _client(session) -> PriceWatchApiClient:
    return PriceWatchApiClient("http://price-watch.test:8787", "test-token", session)


async def test_get_retailers_parses_a_minimal_valid_summary():
    """The bounded list route parses every documented required field."""
    session = _RecordingSession(payload={"data": [_retailer_payload()]})
    client = _client(session)

    retailers = await client.async_get_retailers()

    assert len(retailers) == 1
    retailer = retailers[0]
    assert retailer.retailer_id == "lorna_jane"
    assert retailer.enabled is True
    assert retailer.acquisition_methods == ("http",)
    assert retailer.status == "healthy"
    assert retailer.preferred_strategy == "auto"
    assert retailer.active_strategy == "http"
    assert retailer.effective_strategy_reason == "auto_lowest_cost"
    assert retailer.watch_impact.active_watch_count == 3
    assert retailer.watch_impact.affected_watch_count == 0
    assert retailer.last_success is None
    assert retailer.last_failure is None
    assert retailer.cooldown_until is None
    assert session.requests[0]["url"].endswith("/v1/retailers")


async def test_get_retailers_parses_optional_attempts_and_cooldown():
    """Optional bounded attempt/cooldown fields are parsed when present."""
    payload = _retailer_payload(
        status="rate_limited",
        cooldown_until="2026-08-26T06:00:00.000Z",
        last_success={
            "acquisition_method": "http",
            "occurred_at": "2026-08-25T00:00:00.000Z",
        },
        last_failure={
            "acquisition_method": "http",
            "occurred_at": "2026-08-26T00:00:00.000Z",
            "failure_classification": "rate_limited",
        },
    )
    session = _RecordingSession(payload={"data": [payload]})
    client = _client(session)

    (retailer,) = await client.async_get_retailers()

    assert retailer.status == "rate_limited"
    assert retailer.cooldown_until == "2026-08-26T06:00:00.000Z"
    assert retailer.last_success is not None
    assert retailer.last_success.acquisition_method == "http"
    assert retailer.last_success.failure_classification is None
    assert retailer.last_failure is not None
    assert retailer.last_failure.failure_classification == "rate_limited"


@pytest.mark.parametrize(
    "overrides",
    (
        {"status": "on_fire"},
        {"preferred_strategy": "quantum"},
        {"active_strategy": "quantum"},
        {"interpretation_mode": "magic"},
        {"effective_strategy_reason": "vibes"},
        {"acquisition_methods": []},
        {"acquisition_methods": ["teleport"]},
        {"enabled": "yes"},
        {"watch_impact": {"active_watch_count": -1, "affected_watch_count": 0}},
    ),
)
async def test_get_retailers_rejects_an_untrusted_or_malformed_field(overrides):
    """An unrecognised or out-of-contract field must never be trusted."""
    session = _RecordingSession(
        payload={"data": [_retailer_payload(**overrides)]}
    )
    client = _client(session)

    with pytest.raises(PriceWatchInvalidResponseError):
        await client.async_get_retailers()


async def test_set_retailer_enabled_sends_a_strict_patch_body():
    """Only the documented enabled field reaches the PATCH request body."""
    session = _RecordingSession(payload=_retailer_payload(enabled=False))
    client = _client(session)

    retailer = await client.async_set_retailer_enabled("lorna_jane", False)

    assert retailer.enabled is False
    request = session.requests[0]
    assert request["method"] == "PATCH"
    assert request["url"] == "http://price-watch.test:8787/v1/retailers/lorna_jane"
    assert request["json"] == {"enabled": False}
    assert request["headers"]["Idempotency-Key"]
    assert UUID(request["headers"]["Idempotency-Key"])


async def test_set_retailer_preferred_strategy_sends_a_strict_patch_body():
    """Only the documented preferred_strategy field reaches the PATCH body."""
    session = _RecordingSession(
        payload=_retailer_payload(preferred_strategy="http")
    )
    client = _client(session)

    retailer = await client.async_set_retailer_preferred_strategy(
        "lorna_jane", "http"
    )

    assert retailer.preferred_strategy == "http"
    request = session.requests[0]
    assert request["method"] == "PATCH"
    assert request["json"] == {"preferred_strategy": "http"}


async def test_set_retailer_preferred_strategy_rejects_an_unrecognised_value():
    """The client never sends a strategy value it does not itself recognise."""
    client = _client(_RecordingSession())

    with pytest.raises(ValueError):
        await client.async_set_retailer_preferred_strategy(
            "lorna_jane", "quantum_capture"
        )


async def test_reset_retailer_posts_an_empty_body_to_the_reset_route():
    """Reset never sends operator/config input; it is a bare POST action."""
    session = _RecordingSession(payload=_retailer_payload())
    client = _client(session)

    await client.async_reset_retailer("lorna_jane")

    request = session.requests[0]
    assert request["method"] == "POST"
    assert request["url"] == (
        "http://price-watch.test:8787/v1/retailers/lorna_jane/reset"
    )
    assert request["json"] == {}
    assert request["headers"]["Idempotency-Key"]


async def test_test_retailer_posts_an_empty_body_without_a_watch_id():
    """No watch_id means the service selects its own configured watch."""
    session = _RecordingSession(
        payload={
            "retailer_id": "lorna_jane",
            "watch_id": "watch-1",
            "acquisition_method": "http",
            "outcome": "success",
            "tested_at": "2026-08-26T00:00:00.000Z",
        }
    )
    client = _client(session)

    result = await client.async_test_retailer("lorna_jane")

    assert result.outcome == "success"
    assert result.watch_id == "watch-1"
    request = session.requests[0]
    assert request["method"] == "POST"
    assert request["url"] == (
        "http://price-watch.test:8787/v1/retailers/lorna_jane/test"
    )
    assert request["json"] == {}


async def test_test_retailer_posts_the_caller_supplied_watch_id():
    """A caller-supplied watch_id is the only permitted body field."""
    session = _RecordingSession(
        payload={
            "retailer_id": "lorna_jane",
            "watch_id": "watch-2",
            "outcome": "check_failed",
            "tested_at": "2026-08-26T00:00:00.000Z",
        }
    )
    client = _client(session)

    result = await client.async_test_retailer("lorna_jane", "watch-2")

    assert result.outcome == "check_failed"
    assert result.acquisition_method is None
    request = session.requests[0]
    assert request["json"] == {"watch_id": "watch-2"}


@pytest.mark.parametrize(
    "outcome", ("success", "check_failed", "cooldown", "halted", "disabled")
)
async def test_test_retailer_parses_every_documented_outcome(outcome):
    """Every bounded diagnostic outcome the service can report parses safely."""
    session = _RecordingSession(
        payload={
            "retailer_id": "lorna_jane",
            "watch_id": "watch-1",
            "outcome": outcome,
            "tested_at": "2026-08-26T00:00:00.000Z",
        }
    )
    client = _client(session)

    result = await client.async_test_retailer("lorna_jane")

    assert result.outcome == outcome


async def test_test_retailer_rejects_an_unrecognised_outcome():
    """An outcome outside the documented bounded set is never trusted."""
    session = _RecordingSession(
        payload={
            "retailer_id": "lorna_jane",
            "watch_id": "watch-1",
            "outcome": "definitely_worked",
            "tested_at": "2026-08-26T00:00:00.000Z",
        }
    )
    client = _client(session)

    with pytest.raises(PriceWatchInvalidResponseError):
        await client.async_test_retailer("lorna_jane")


async def test_retailer_actions_propagate_a_service_rejection():
    """A 400/404/409 API rejection is never masked as a successful update."""
    session = _RecordingSession(status=400, payload={"error": {"code": "unsupported_strategy"}})
    client = _client(session)

    with pytest.raises(PriceWatchApiResponseError) as excinfo:
        await client.async_set_retailer_preferred_strategy("lorna_jane", "browser")
    assert excinfo.value.http_status == 400
    assert excinfo.value.service_code == "unsupported_strategy"


async def test_retailer_ids_are_url_encoded_in_every_route():
    """A retailer ID with reserved characters cannot escape its own path segment."""
    session = _RecordingSession(payload=_retailer_payload(retailer_id="odd/id"))
    client = _client(session)

    await client.async_set_retailer_enabled("odd/id", True)
    await client.async_reset_retailer("odd/id")

    session.payload = {
        "retailer_id": "odd/id",
        "watch_id": "watch-1",
        "outcome": "success",
        "tested_at": "2026-08-26T00:00:00.000Z",
    }
    await client.async_test_retailer("odd/id")

    for request in session.requests:
        assert "odd/id" not in request["url"]
        assert "odd%2Fid" in request["url"]


async def test_get_retailers_rejects_a_non_list_data_field():
    """A malformed top-level response must never be treated as an empty list."""
    client = _client(_RecordingSession(payload={"data": "not-a-list"}))

    with pytest.raises(PriceWatchInvalidResponseError):
        await client.async_get_retailers()
