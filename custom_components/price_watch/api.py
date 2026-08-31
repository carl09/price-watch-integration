"""Minimal authenticated client for the Price Watch service."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit
from uuid import UUID, uuid4

import aiohttp

from .const import (
    CHECKS_PATH,
    EVENTS_PATH,
    HEALTH_PATH,
    REQUEST_ID_HEADER,
    REQUEST_TIMEOUT_SECONDS,
    RETAILERS_PATH,
    SUMMARY_PATH,
    WATCHES_PATH,
)

#: F10 Phase 7a — the bounded set of acquisition methods the service may
#: report. Mirrors the private service's `RetailerStrategyMethod` enum.
RETAILER_ACQUISITION_METHODS: tuple[str, ...] = (
    "http",
    "browser",
    "network_capture",
    "custom",
)
#: The operator-selectable preferred strategy is `auto` plus any acquisition
#: method; the service still rejects a method unsupported by a given
#: retailer.
RETAILER_PREFERRED_STRATEGIES: tuple[str, ...] = (
    "auto",
    *RETAILER_ACQUISITION_METHODS,
)
#: Bounded derived retailer health/status values. `contract_broken` is out of
#: Phase 6a/7a scope and is intentionally not included.
RETAILER_STATUSES: tuple[str, ...] = (
    "healthy",
    "degraded",
    "rate_limited",
    "blocked",
    "disabled",
    "unknown",
)
_RETAILER_INTERPRETATION_MODES = frozenset({"legacy_adapter", "contract", "custom"})
_RETAILER_EFFECTIVE_STRATEGY_REASONS = frozenset(
    {"auto_lowest_cost", "manual_preference", "temporary_escalation"}
)
_RETAILER_DIAGNOSTIC_OUTCOMES = frozenset(
    {"success", "check_failed", "cooldown", "halted", "disabled"}
)
_RETAILER_ACQUISITION_METHODS = frozenset(RETAILER_ACQUISITION_METHODS)
_RETAILER_PREFERRED_STRATEGIES = frozenset(RETAILER_PREFERRED_STRATEGIES)
_RETAILER_STATUSES = frozenset(RETAILER_STATUSES)


class PriceWatchApiError(Exception):
    """Base error raised by the Price Watch API client."""


class PriceWatchAuthenticationError(PriceWatchApiError):
    """The configured token was rejected by the service."""

    def __init__(self, http_status: int | None = None) -> None:
        self.http_status = http_status


class PriceWatchTransportError(PriceWatchApiError):
    """The service could not be reached."""


class PriceWatchTimeoutError(PriceWatchApiError):
    """The service did not respond before the request deadline."""


class PriceWatchInvalidResponseError(PriceWatchApiError):
    """The service returned an unexpected health response."""


class PriceWatchApiResponseError(PriceWatchApiError):
    """The service returned a non-success HTTP response."""

    def __init__(
        self, http_status: int, service_code: str | None = None
    ) -> None:
        self.http_status = http_status
        self.service_code = service_code


@dataclass(frozen=True)
class PriceWatchHealth:
    """Validated Price Watch health response."""

    version: str


@dataclass(frozen=True)
class PriceWatchSummary:
    """Validated Price Watch service summary."""

    enabled_watches: int
    target_matches: int
    stale: int
    failed: int
    latest_check_at: str | None
    target_matching_watch_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PriceWatchCurrentObservation:
    """Validated current observation returned with a watch."""

    id: str
    checked_at: str
    status: Literal["available", "out_of_stock"]
    price_cents: int | None
    compare_at_price_cents: int | None
    currency: str
    selected_variant_label: str | None
    error_code: str | None


@dataclass(frozen=True)
class PriceWatchVariant:
    """Validated exact service variant selector."""

    retailer_variant_id: str | None
    options: dict[str, str] | None


@dataclass(frozen=True)
class PriceWatchWatch:
    """Validated watch state returned by the Price Watch API."""

    id: str
    retailer_id: str
    product_url: str
    title: str
    variant: PriceWatchVariant
    enabled: bool
    target_price_cents: int | None
    check_interval_minutes: int
    current_observation_id: str | None
    current_observation: PriceWatchCurrentObservation | None
    last_successful_check_at: str | None
    last_attempt_at: str | None
    last_attempt_status: str | None = None
    last_attempt_error_code: str | None = None
    product_image_url: str | None = None


@dataclass(frozen=True)
class PriceWatchEvent:
    """Validated immutable Price Watch event."""

    id: str
    watch_id: str
    observation_id: str | None
    type: str
    occurred_at: str
    deduplication_key: str
    data: dict[str, Any]


@dataclass(frozen=True)
class PriceWatchCheckResult:
    """Validated check result returned by a manual action."""

    id: str
    watch_id: str
    checked_at: str
    status: str


@dataclass(frozen=True)
class PriceWatchEnabledResult:
    """Validated enabled-state result returned by a watch update."""

    id: str
    enabled: bool


@dataclass(frozen=True)
class PriceWatchRetailerAttempt:
    """A single bounded retailer acquisition attempt. Never raw capture."""

    acquisition_method: str
    occurred_at: str
    failure_classification: str | None = None


@dataclass(frozen=True)
class PriceWatchRetailerMetric:
    """Bounded per-acquisition-method retailer strategy metrics."""

    acquisition_method: str
    attempts: int
    successes: int
    success_rate_percent: float
    median_acquisition_duration_ms: float
    blocked_count: int
    blocked_rate_percent: float
    rate_limited_count: int
    rate_limited_rate_percent: float
    last_success_at: str | None
    failure_counts: dict[str, int]


@dataclass(frozen=True)
class PriceWatchRetailerWatchImpact:
    """Bounded active/affected watch counts for one retailer."""

    active_watch_count: int
    affected_watch_count: int


@dataclass(frozen=True)
class PriceWatchRetailer:
    """F10 Phase 7a bounded retailer operational summary.

    Never includes raw capture, HTML, selectors, cookies, credentials,
    tokens or an arbitrary retailer URL; the service itself excludes these
    from the Phase 6a API response.
    """

    retailer_id: str
    enabled: bool
    acquisition_methods: tuple[str, ...]
    interpretation_mode: str
    status: str
    preferred_strategy: str
    active_strategy: str
    effective_strategy_reason: str
    watch_impact: PriceWatchRetailerWatchImpact
    metrics: tuple[PriceWatchRetailerMetric, ...] = ()
    cooldown_until: str | None = None
    last_success: PriceWatchRetailerAttempt | None = None
    last_failure: PriceWatchRetailerAttempt | None = None


@dataclass(frozen=True)
class PriceWatchRetailerDiagnosticResult:
    """A bounded diagnostic test outcome. Never price/product data."""

    retailer_id: str
    watch_id: str
    outcome: str
    tested_at: str
    acquisition_method: str | None = None
    classification: str | None = None


@dataclass(frozen=True)
class PriceWatchProductImage:
    """Validated product-image bytes fetched from the Price Watch service."""

    content: bytes
    content_type: str


_TRUSTED_SERVICE_CODES = frozenset(
    {
        "idempotency_conflict",
        "idempotency_key_required",
        "internal_error",
        "not_found",
        "unauthorized",
        "unsupported_product_host",
        "unsupported_retailer",
        "unsupported_strategy",
        "validation_error",
    }
)
_ALLOWED_PRODUCT_IMAGE_CONTENT_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/webp"}
)
_WATCH_STATUSES = frozenset(
    {
        "available",
        "out_of_stock",
        "unavailable",
        "check_failed",
        "rate_limited",
        "blocked",
    }
)
_MAX_PRODUCT_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_JSON_RESPONSE_BYTES = 1 * 1024 * 1024


async def _read_bounded_json(response: aiohttp.ClientResponse) -> object:
    """Read JSON through a bounded body before parsing it."""
    content = getattr(response, "content", None)
    read = getattr(content, "read", None)
    if callable(read):
        raw = await read(_MAX_JSON_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_JSON_RESPONSE_BYTES:
            raise PriceWatchInvalidResponseError
        try:
            return json.loads(raw)
        except (TypeError, UnicodeDecodeError, ValueError) as err:
            raise PriceWatchInvalidResponseError("non-JSON response") from err
    try:
        return await response.json(content_type=None)
    except (aiohttp.ClientError, TypeError, ValueError) as err:
        raise PriceWatchInvalidResponseError("non-JSON response") from err


def _trusted_service_code(value: object) -> str | None:
    """Return only expected machine-readable service codes."""
    return value if isinstance(value, str) and value in _TRUSTED_SERVICE_CODES else None


def _is_local_http_host(hostname: str) -> bool:
    """Permit HTTP only for explicit local/test API endpoints."""
    host = hostname.lower().rstrip(".")
    return host in {"localhost", "homeassistant.local", "price-watch.test"}


def normalise_base_url(value: str) -> str:
    """Return a canonical HTTPS URL, or HTTP only for a local API endpoint."""
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
        port = parsed.port
    except (AttributeError, TypeError, ValueError) as err:
        raise ValueError("base URL is malformed") from err
    hostname = (hostname or "").lower().rstrip(".")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and not _is_local_http_host(hostname))
    ):
        raise ValueError("base URL must be HTTPS, or HTTP only for an approved local endpoint")
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = f"{hostname}:{port}" if port is not None else hostname
    return urlunsplit(
        (
            parsed.scheme,
            netloc,
            parsed.path.rstrip("/"),
            "",
            "",
        )
    )


class PriceWatchApiClient:
    """Authenticated, read-only client for the bootstrap health check."""

    def __init__(
        self,
        base_url: str,
        api_token: str,
        session: aiohttp.ClientSession,
    ) -> None:
        self._base_url = normalise_base_url(base_url)
        self._api_token = api_token
        self._session = session

    async def async_get_health(
        self, *, request_id: str | None = None
    ) -> PriceWatchHealth:
        """Validate the authenticated service health response."""
        payload = await self._async_get_json(HEALTH_PATH, request_id=request_id)
        if not isinstance(payload, dict):
            raise PriceWatchInvalidResponseError
        if payload.get("status") != "healthy" or payload.get("database") != "healthy":
            raise PriceWatchInvalidResponseError
        version = payload.get("version")
        if not isinstance(version, str) or not version:
            raise PriceWatchInvalidResponseError
        return PriceWatchHealth(version=version)

    async def async_get_summary(
        self, *, request_id: str | None = None
    ) -> PriceWatchSummary:
        """Return a validated service summary."""
        payload = await self._async_get_json(SUMMARY_PATH, request_id=request_id)
        if not isinstance(payload, dict):
            raise PriceWatchInvalidResponseError
        counts = ("enabled_watches", "target_matches", "stale", "failed")
        if any(
            not isinstance(payload.get(key), int) or payload[key] < 0
            for key in counts
        ):
            raise PriceWatchInvalidResponseError
        latest_check_at = payload.get("latest_check_at")
        if latest_check_at is not None and not isinstance(latest_check_at, str):
            raise PriceWatchInvalidResponseError
        target_ids = payload.get("target_matching_watch_ids", [])
        if (
            not isinstance(target_ids, list)
            or not all(isinstance(watch_id, str) for watch_id in target_ids)
            or len(target_ids) != payload["target_matches"]
        ):
            raise PriceWatchInvalidResponseError
        return PriceWatchSummary(
            enabled_watches=payload["enabled_watches"],
            target_matches=payload["target_matches"],
            stale=payload["stale"],
            failed=payload["failed"],
            latest_check_at=latest_check_at,
            target_matching_watch_ids=tuple(target_ids),
        )

    async def async_get_watches(
        self, *, request_id: str | None = None
    ) -> tuple[PriceWatchWatch, ...]:
        """Return a complete bounded watch snapshot (up to 500 watches)."""
        watches: list[PriceWatchWatch] = []
        cursor: str | None = None
        for page in range(5):
            path = f"{WATCHES_PATH}?limit=100"
            if cursor is not None:
                path += f"&cursor={quote(cursor, safe='')}"
            payload = await self._async_get_json(path, request_id=request_id)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise PriceWatchInvalidResponseError
            page_data = payload["data"]
            if len(page_data) > 100:
                raise PriceWatchInvalidResponseError
            watches.extend(self._parse_watch(item) for item in page_data)
            next_cursor = payload.get("next_cursor")
            if next_cursor is None:
                return tuple(watches)
            if not isinstance(next_cursor, str) or not next_cursor:
                raise PriceWatchInvalidResponseError
            cursor = next_cursor
        raise PriceWatchInvalidResponseError

    async def async_get_events(
        self,
        *,
        event_type: str | None = None,
        limit: int = 100,
        request_id: str | None = None,
    ) -> tuple[PriceWatchEvent, ...]:
        """Return a bounded, deterministic newest-first event snapshot."""
        if event_type not in {None, "target_reached", "check_failed"}:
            raise ValueError("event type is not supported")
        if not isinstance(limit, int) or limit < 1 or limit > 100:
            raise ValueError("event limit must be between 1 and 100")
        path = f"{EVENTS_PATH}?limit={limit}"
        if event_type is not None:
            path += f"&type={quote(event_type, safe='')}"
        payload = await self._async_get_json(path, request_id=request_id)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise PriceWatchInvalidResponseError
        if len(payload["data"]) > limit:
            raise PriceWatchInvalidResponseError
        events = [self._parse_event(item) for item in payload["data"]]
        events.sort(key=lambda event: (event.occurred_at, event.id), reverse=True)
        return tuple(events)

    async def async_get_product_image(
        self, product_image_url: str, *, request_id: str | None = None
    ) -> PriceWatchProductImage:
        """Fetch one previously validated Price Watch image capability URL."""
        if (
            not isinstance(product_image_url, str)
            or self._parse_product_image_url(product_image_url) != product_image_url
        ):
            raise ValueError("product image URL must be a validated capability URL")

        headers = {
            "Authorization": f"Bearer {self._api_token}",
            REQUEST_ID_HEADER: self._request_id(request_id),
        }
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                async with self._session.get(
                    product_image_url,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    if response.status in {401, 403}:
                        raise PriceWatchAuthenticationError(response.status)
                    if response.status != 200:
                        raise PriceWatchApiResponseError(response.status)
                    content_type = response.headers.get("Content-Type", "").split(
                        ";", 1
                    )[0].strip().lower()
                    if content_type not in _ALLOWED_PRODUCT_IMAGE_CONTENT_TYPES:
                        raise PriceWatchInvalidResponseError
                    content_length = response.content_length
                    if (
                        content_length is not None
                        and content_length > _MAX_PRODUCT_IMAGE_BYTES
                    ):
                        raise PriceWatchInvalidResponseError
                    content = await response.content.read(
                        _MAX_PRODUCT_IMAGE_BYTES + 1
                    )
                    if len(content) > _MAX_PRODUCT_IMAGE_BYTES:
                        raise PriceWatchInvalidResponseError
                    return PriceWatchProductImage(
                        content=content,
                        content_type=content_type,
                    )
        except TimeoutError as err:
            raise PriceWatchTimeoutError from err
        except aiohttp.ClientError as err:
            raise PriceWatchTransportError from err

    async def async_check_all(
        self,
        *,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> None:
        """Run due watches with one stable idempotency key per action.

        The Home Assistant action only needs the request to complete; the
        coordinator refresh fetches authoritative watch state afterwards.
        """
        await self._async_post_json(
            CHECKS_PATH,
            self._idempotency_key(idempotency_key),
            request_id=request_id,
        )

    async def async_check_watch(
        self,
        watch_id: str,
        *,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> PriceWatchCheckResult:
        """Run one watch with one stable idempotency key per action."""
        if not isinstance(watch_id, str) or not watch_id:
            raise ValueError("watch ID is required")
        payload = await self._async_post_json(
            f"{WATCHES_PATH}/{quote(watch_id, safe='')}/check",
            self._idempotency_key(idempotency_key),
            request_id=request_id,
        )
        return self._parse_check_result(payload)

    async def async_set_target_price(
        self,
        watch_id: str,
        target_price_cents: int,
        *,
        request_id: str | None = None,
    ) -> PriceWatchWatch:
        """Set a non-negative target without creating an observation or event."""
        if not isinstance(watch_id, str) or not watch_id:
            raise ValueError("watch ID is required")
        if (
            not isinstance(target_price_cents, int)
            or isinstance(target_price_cents, bool)
            or target_price_cents < 0
        ):
            raise ValueError("target price must be a non-negative integer")
        payload = await self._async_patch_json(
            f"{WATCHES_PATH}/{quote(watch_id, safe='')}",
            self._idempotency_key(None),
            {"target_price_cents": target_price_cents},
            request_id=request_id,
        )
        return self._parse_watch(payload)

    async def async_set_enabled(
        self,
        watch_id: str,
        enabled: bool,
        *,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> PriceWatchEnabledResult:
        """Set one watch's enabled state with one stable action key."""
        if not isinstance(watch_id, str) or not watch_id:
            raise ValueError("watch ID is required")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        payload = await self._async_patch_json(
            f"{WATCHES_PATH}/{quote(watch_id, safe='')}",
            self._idempotency_key(idempotency_key),
            {"enabled": enabled},
            request_id=request_id,
        )
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("id"), str)
            or not payload["id"]
            or not isinstance(payload.get("enabled"), bool)
        ):
            raise PriceWatchInvalidResponseError
        return PriceWatchEnabledResult(id=payload["id"], enabled=payload["enabled"])

    async def async_get_retailers(
        self, *, request_id: str | None = None
    ) -> tuple[PriceWatchRetailer, ...]:
        """Return validated F10 Phase 7a bounded retailer summaries."""
        payload = await self._async_get_json(RETAILERS_PATH, request_id=request_id)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise PriceWatchInvalidResponseError
        return tuple(self._parse_retailer(item) for item in payload["data"])

    async def async_set_retailer_enabled(
        self,
        retailer_id: str,
        enabled: bool,
        *,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> PriceWatchRetailer:
        """Enable/disable one retailer with a new idempotency key."""
        if not isinstance(retailer_id, str) or not retailer_id:
            raise ValueError("retailer ID is required")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        payload = await self._async_patch_json(
            f"{RETAILERS_PATH}/{quote(retailer_id, safe='')}",
            self._idempotency_key(idempotency_key),
            {"enabled": enabled},
            request_id=request_id,
        )
        return self._parse_retailer(payload)

    async def async_set_retailer_preferred_strategy(
        self,
        retailer_id: str,
        preferred_strategy: str,
        *,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> PriceWatchRetailer:
        """Set the operator's preferred strategy with a new idempotency key.

        Only sends a value this client recognises; the service independently
        rejects a strategy unsupported by this particular retailer.
        """
        if not isinstance(retailer_id, str) or not retailer_id:
            raise ValueError("retailer ID is required")
        if preferred_strategy not in _RETAILER_PREFERRED_STRATEGIES:
            raise ValueError("preferred strategy is not recognised")
        payload = await self._async_patch_json(
            f"{RETAILERS_PATH}/{quote(retailer_id, safe='')}",
            self._idempotency_key(idempotency_key),
            {"preferred_strategy": preferred_strategy},
            request_id=request_id,
        )
        return self._parse_retailer(payload)

    async def async_reset_retailer(
        self,
        retailer_id: str,
        *,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> PriceWatchRetailer:
        """Clear only durable cooldown/escalation state for one retailer.

        This never creates a watch observation, event or notification; the
        service enforces that boundary independently of this client.
        """
        if not isinstance(retailer_id, str) or not retailer_id:
            raise ValueError("retailer ID is required")
        payload = await self._async_post_json(
            f"{RETAILERS_PATH}/{quote(retailer_id, safe='')}/reset",
            self._idempotency_key(idempotency_key),
            request_id=request_id,
        )
        return self._parse_retailer(payload)

    async def async_test_retailer(
        self,
        retailer_id: str,
        watch_id: str | None = None,
        *,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> PriceWatchRetailerDiagnosticResult:
        """Run a controlled diagnostic test for one retailer.

        This never persists a watch observation or event; the service
        enforces that boundary independently of this client.
        """
        if not isinstance(retailer_id, str) or not retailer_id:
            raise ValueError("retailer ID is required")
        if watch_id is not None and (not isinstance(watch_id, str) or not watch_id):
            raise ValueError("watch ID must be a non-empty string")
        payload = await self._async_post_json(
            f"{RETAILERS_PATH}/{quote(retailer_id, safe='')}/test",
            self._idempotency_key(idempotency_key),
            {"watch_id": watch_id} if watch_id else None,
            request_id=request_id,
        )
        return self._parse_retailer_diagnostic_result(payload)

    async def _async_get_json(
        self, path: str, *, request_id: str | None = None
    ) -> object:
        """Fetch one authenticated JSON response without retaining raw payloads."""
        return await self._async_request_json("get", path, request_id=request_id)

    async def _async_post_json(
        self,
        path: str,
        idempotency_key: str,
        payload: dict[str, object] | None = None,
        *,
        request_id: str | None = None,
    ) -> object:
        """Post one authenticated JSON response without retaining raw payloads."""
        request_id = self._request_id(request_id)
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                async with self._session.post(
                    f"{self._base_url}{path}",
                    headers={
                        "Authorization": f"Bearer {self._api_token}",
                        "Idempotency-Key": idempotency_key,
                        REQUEST_ID_HEADER: request_id,
                    },
                    json=payload if payload is not None else {},
                    allow_redirects=False,
                ) as response:
                    if response.status in {401, 403}:
                        raise PriceWatchAuthenticationError(response.status)
                    if response.status != 200:
                        raise await self._async_api_response_error(response)
                    try:
                        return await _read_bounded_json(response)
                    except (aiohttp.ClientError, TypeError, ValueError) as err:
                        raise PriceWatchInvalidResponseError(
                            "non-JSON response"
                        ) from err
        except TimeoutError as err:
            raise PriceWatchTimeoutError from err
        except aiohttp.ClientError as err:
            raise PriceWatchTransportError from err

    async def _async_patch_json(
        self,
        path: str,
        idempotency_key: str,
        payload: dict[str, object],
        *,
        request_id: str | None = None,
    ) -> object:
        """Patch one authenticated JSON response without retaining raw payloads."""
        request_id = self._request_id(request_id)
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                async with self._session.patch(
                    f"{self._base_url}{path}",
                    headers={
                        "Authorization": f"Bearer {self._api_token}",
                        "Idempotency-Key": idempotency_key,
                        REQUEST_ID_HEADER: request_id,
                    },
                    json=payload,
                    allow_redirects=False,
                ) as response:
                    if response.status in {401, 403}:
                        raise PriceWatchAuthenticationError(response.status)
                    if response.status != 200:
                        raise await self._async_api_response_error(response)
                    try:
                        return await _read_bounded_json(response)
                    except (aiohttp.ClientError, TypeError, ValueError) as err:
                        raise PriceWatchInvalidResponseError(
                            "non-JSON response"
                        ) from err
        except TimeoutError as err:
            raise PriceWatchTimeoutError from err
        except aiohttp.ClientError as err:
            raise PriceWatchTransportError from err

    async def _async_request_json(
        self,
        method: str,
        path: str,
        additional_headers: dict[str, str] | None = None,
        *,
        request_id: str | None = None,
    ) -> object:
        """Send an authenticated request without retaining raw payloads."""
        headers = {"Authorization": f"Bearer {self._api_token}"}
        if additional_headers is not None:
            headers.update(additional_headers)
        headers[REQUEST_ID_HEADER] = self._request_id(request_id)
        request = self._session.get if method == "get" else self._session.post
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                async with request(
                    f"{self._base_url}{path}",
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    if response.status in {401, 403}:
                        raise PriceWatchAuthenticationError(response.status)
                    if response.status != 200:
                        raise await self._async_api_response_error(response)
                    try:
                        payload = await _read_bounded_json(response)
                    except (aiohttp.ClientError, TypeError, ValueError) as err:
                        raise PriceWatchInvalidResponseError(
                            "non-JSON response"
                        ) from err
        except TimeoutError as err:
            raise PriceWatchTimeoutError from err
        except aiohttp.ClientError as err:
            raise PriceWatchTransportError from err

        return payload

    @staticmethod
    def _idempotency_key(value: str | None) -> str:
        """Reuse a caller key or create one once at the action boundary."""
        if value is None:
            return str(uuid4())
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9._: -]{1,128}", value
        ):
            raise ValueError("idempotency key must be 1-128 safe characters")
        return value

    @staticmethod
    def _request_id(value: str | None) -> str:
        if value is None:
            return str(uuid4())
        try:
            parsed = UUID(value)
        except (AttributeError, ValueError) as err:
            raise ValueError("request ID must be a UUID") from err
        if str(parsed) != value:
            raise ValueError("request ID must be a canonical UUID")
        return value

    @staticmethod
    async def _async_api_response_error(
        response: aiohttp.ClientResponse,
    ) -> PriceWatchApiResponseError:
        """Extract only a safe machine-readable error code from a response."""
        service_code: str | None = None
        try:
            payload = await _read_bounded_json(response)
        except (
            aiohttp.ClientError,
            TypeError,
            ValueError,
            PriceWatchInvalidResponseError,
        ):
            payload = None
        if isinstance(payload, dict):
            service_code = _trusted_service_code(
                payload.get("code")
            ) or _trusted_service_code(
                payload.get("error", {}).get("code")
                if isinstance(payload.get("error"), dict)
                else None
            )
        return PriceWatchApiResponseError(response.status, service_code)

    def _parse_watch(self, value: object) -> PriceWatchWatch:
        if not isinstance(value, dict):
            raise PriceWatchInvalidResponseError
        required_strings = ("id", "retailer_id", "product_url", "title")
        if any(not isinstance(value.get(key), str) or not value[key] for key in required_strings):
            raise PriceWatchInvalidResponseError
        self._parse_product_url(value["product_url"])
        if not isinstance(value.get("enabled"), bool):
            raise PriceWatchInvalidResponseError
        variant = value.get("variant")
        if not isinstance(variant, dict):
            raise PriceWatchInvalidResponseError
        retailer_variant_id = variant.get("retailer_variant_id")
        options = variant.get("options")
        if retailer_variant_id is not None and (
            not isinstance(retailer_variant_id, str) or not retailer_variant_id
        ):
            raise PriceWatchInvalidResponseError
        if options is not None and (
            not isinstance(options, dict)
            or not options
            or not all(
                isinstance(key, str)
                and key
                and isinstance(option, str)
                and option
                for key, option in options.items()
            )
        ):
            raise PriceWatchInvalidResponseError
        if retailer_variant_id is None and options is None:
            raise PriceWatchInvalidResponseError
        interval = value.get("check_interval_minutes")
        if not isinstance(interval, int) or interval <= 0:
            raise PriceWatchInvalidResponseError
        target = value.get("target_price_cents")
        if target is not None and (not isinstance(target, int) or target < 0):
            raise PriceWatchInvalidResponseError
        optional_strings = (
            "current_observation_id",
            "last_successful_check_at",
            "last_attempt_at",
            "last_attempt_error_code",
        )
        if any(
            value.get(key) is not None and not isinstance(value.get(key), str)
            for key in optional_strings
        ):
            raise PriceWatchInvalidResponseError
        if "current_observation" not in value:
            raise PriceWatchInvalidResponseError
        product_image_url = self._parse_product_image_url(
            value.get("product_image_url")
        )
        last_attempt_status = value.get("last_attempt_status")
        if last_attempt_status is not None and last_attempt_status not in _WATCH_STATUSES:
            raise PriceWatchInvalidResponseError
        return PriceWatchWatch(
            id=value["id"],
            retailer_id=value["retailer_id"],
            product_url=value["product_url"],
            title=value["title"],
            variant=PriceWatchVariant(
                retailer_variant_id=retailer_variant_id,
                options=options,
            ),
            enabled=value["enabled"],
            target_price_cents=target,
            check_interval_minutes=interval,
            current_observation_id=value.get("current_observation_id"),
            current_observation=PriceWatchApiClient._parse_current_observation(
                value["current_observation"]
            ),
            last_successful_check_at=value.get("last_successful_check_at"),
            last_attempt_at=value.get("last_attempt_at"),
            last_attempt_status=last_attempt_status,
            last_attempt_error_code=value.get("last_attempt_error_code"),
            product_image_url=product_image_url,
        )

    @staticmethod
    def _parse_product_url(value: object) -> str:
        """Accept only absolute HTTPS product URLs without credentials."""
        if not isinstance(value, str):
            raise PriceWatchInvalidResponseError
        try:
            parsed = urlsplit(value)
        except ValueError as err:
            raise PriceWatchInvalidResponseError from err
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise PriceWatchInvalidResponseError
        return value

    def _parse_product_image_url(self, value: object) -> str | None:
        """Accept only a Price Watch image endpoint, never a retailer URL.

        The App returns its image capability as a relative URL. Resolve that
        narrow endpoint against the configured service base URL so the
        integration can fetch it internally.
        """
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise PriceWatchInvalidResponseError

        try:
            image_url = urlsplit(value)
            base_url = urlsplit(self._base_url)
            if not image_url.scheme and not image_url.netloc:
                return self._parse_relative_product_image_url(image_url, base_url)
            valid_url = (
                image_url.scheme == base_url.scheme
                and bool(image_url.netloc)
                and not image_url.username
                and not image_url.password
                and not image_url.fragment
                and image_url.hostname == base_url.hostname
                and image_url.port == base_url.port
                and image_url.scheme == base_url.scheme
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise PriceWatchInvalidResponseError

        if image_url.query:
            return self._parse_relative_product_image_url(
                urlsplit(
                    urlunsplit(("", "", image_url.path, image_url.query, ""))
                ),
                base_url,
            )

        base_path = base_url.path.rstrip("/")
        allowed_path_prefix = f"{base_path}/v1/" if base_path else "/v1/"
        if not image_url.path.startswith(allowed_path_prefix):
            raise PriceWatchInvalidResponseError
        return urlunsplit(
            (
                image_url.scheme,
                image_url.netloc,
                image_url.path,
                "",
                "",
            )
        )

    @staticmethod
    def _parse_relative_product_image_url(image_url, base_url) -> str:
        """Resolve the App's one supported relative image capability route."""
        if image_url.fragment or not re.fullmatch(
            r"/v1/watches/[A-Za-z0-9_-]+/image", image_url.path
        ):
            raise PriceWatchInvalidResponseError
        try:
            query = parse_qsl(
                image_url.query, keep_blank_values=True, strict_parsing=True
            )
        except ValueError as err:
            raise PriceWatchInvalidResponseError from err
        if len(query) != 1 or query[0][0] != "token" or not re.fullmatch(
            r"[A-Za-z0-9_-]{43}", query[0][1]
        ):
            raise PriceWatchInvalidResponseError
        base_path = base_url.path.rstrip("/")
        return urlunsplit(
            (
                base_url.scheme,
                base_url.netloc,
                f"{base_path}{image_url.path}",
                image_url.query,
                "",
            )
        )

    @staticmethod
    def _parse_current_observation(
        value: object,
    ) -> PriceWatchCurrentObservation | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise PriceWatchInvalidResponseError
        required_strings = ("id", "checked_at", "status", "currency")
        if any(
            not isinstance(value.get(key), str) or not value[key]
            for key in required_strings
        ):
            raise PriceWatchInvalidResponseError
        if value["status"] not in {"available", "out_of_stock"}:
            raise PriceWatchInvalidResponseError
        optional_money = ("price_cents", "compare_at_price_cents")
        if any(key not in value for key in optional_money):
            raise PriceWatchInvalidResponseError
        if any(
            value.get(key) is not None
            and (not isinstance(value[key], int) or value[key] < 0)
            for key in optional_money
        ):
            raise PriceWatchInvalidResponseError
        optional_strings = ("selected_variant_label", "error_code")
        if any(
            value.get(key) is not None and not isinstance(value[key], str)
            for key in optional_strings
        ):
            raise PriceWatchInvalidResponseError
        if value["status"] == "out_of_stock" and (
            value.get("price_cents") is not None
            or value.get("compare_at_price_cents") is not None
        ):
            raise PriceWatchInvalidResponseError
        return PriceWatchCurrentObservation(
            id=value["id"],
            checked_at=value["checked_at"],
            status=value["status"],
            price_cents=value.get("price_cents"),
            compare_at_price_cents=value.get("compare_at_price_cents"),
            currency=value["currency"],
            selected_variant_label=value.get("selected_variant_label"),
            error_code=value.get("error_code"),
        )

    @staticmethod
    def _parse_event(value: object) -> PriceWatchEvent:
        if not isinstance(value, dict):
            raise PriceWatchInvalidResponseError
        required_strings = (
            "event_id",
            "watch_id",
            "type",
            "occurred_at",
            "deduplication_key",
        )
        if any(
            not isinstance(value.get(key), str) or not value[key]
            for key in required_strings
        ):
            raise PriceWatchInvalidResponseError
        observation_id = value.get("observation_id")
        if observation_id is not None and not isinstance(observation_id, str):
            raise PriceWatchInvalidResponseError
        data = value.get("data")
        if not isinstance(data, dict) or len(data) > 4:
            raise PriceWatchInvalidResponseError
        for key, item in data.items():
            if key not in {"target_price_cents", "from", "to", "error_code"}:
                raise PriceWatchInvalidResponseError
            if key == "error_code":
                if (
                    not isinstance(item, str)
                    or not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", item)
                ):
                    raise PriceWatchInvalidResponseError
            elif (
                not isinstance(item, int)
                or isinstance(item, bool)
                or item < 0
            ):
                raise PriceWatchInvalidResponseError
        return PriceWatchEvent(
            id=value["event_id"],
            watch_id=value["watch_id"],
            observation_id=observation_id,
            type=value["type"],
            occurred_at=value["occurred_at"],
            deduplication_key=value["deduplication_key"],
            data=data,
        )

    @staticmethod
    def _parse_check_result(value: object) -> PriceWatchCheckResult:
        if not isinstance(value, dict):
            raise PriceWatchInvalidResponseError
        required_strings = ("id", "watch_id", "checked_at", "status", "currency")
        if any(
            not isinstance(value.get(key), str) or not value[key]
            for key in required_strings
        ):
            raise PriceWatchInvalidResponseError
        if value["status"] not in _WATCH_STATUSES:
            raise PriceWatchInvalidResponseError
        return PriceWatchCheckResult(
            id=value["id"],
            watch_id=value["watch_id"],
            checked_at=value["checked_at"],
            status=value["status"],
        )

    @staticmethod
    def _parse_retailer_attempt(value: object) -> PriceWatchRetailerAttempt | None:
        """Parse one bounded attempt summary; absent means never attempted."""
        if value is None:
            return None
        if not isinstance(value, dict):
            raise PriceWatchInvalidResponseError
        acquisition_method = value.get("acquisition_method")
        if acquisition_method not in _RETAILER_ACQUISITION_METHODS:
            raise PriceWatchInvalidResponseError
        occurred_at = value.get("occurred_at")
        if not isinstance(occurred_at, str) or not occurred_at:
            raise PriceWatchInvalidResponseError
        failure_classification = value.get("failure_classification")
        if failure_classification is not None and (
            not isinstance(failure_classification, str) or not failure_classification
        ):
            raise PriceWatchInvalidResponseError
        return PriceWatchRetailerAttempt(
            acquisition_method=acquisition_method,
            occurred_at=occurred_at,
            failure_classification=failure_classification,
        )

    @staticmethod
    def _parse_retailer_metric(value: object) -> PriceWatchRetailerMetric:
        """Parse one bounded per-method retailer strategy metric."""
        if not isinstance(value, dict):
            raise PriceWatchInvalidResponseError
        acquisition_method = value.get("acquisition_method")
        if acquisition_method not in _RETAILER_ACQUISITION_METHODS:
            raise PriceWatchInvalidResponseError
        int_fields = ("attempts", "successes", "blocked_count", "rate_limited_count")
        if any(
            not isinstance(value.get(key), int)
            or isinstance(value.get(key), bool)
            or value[key] < 0
            for key in int_fields
        ):
            raise PriceWatchInvalidResponseError
        float_fields = (
            "success_rate_percent",
            "median_acquisition_duration_ms",
            "blocked_rate_percent",
            "rate_limited_rate_percent",
        )
        if any(
            not isinstance(value.get(key), (int, float))
            or isinstance(value.get(key), bool)
            or value[key] < 0
            for key in float_fields
        ):
            raise PriceWatchInvalidResponseError
        last_success_at = value.get("last_success_at")
        if last_success_at is not None and (
            not isinstance(last_success_at, str) or not last_success_at
        ):
            raise PriceWatchInvalidResponseError
        failure_counts = value.get("failure_counts")
        if not isinstance(failure_counts, dict) or not all(
            isinstance(key, str)
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count >= 0
            for key, count in failure_counts.items()
        ):
            raise PriceWatchInvalidResponseError
        return PriceWatchRetailerMetric(
            acquisition_method=acquisition_method,
            attempts=value["attempts"],
            successes=value["successes"],
            success_rate_percent=value["success_rate_percent"],
            median_acquisition_duration_ms=value["median_acquisition_duration_ms"],
            blocked_count=value["blocked_count"],
            blocked_rate_percent=value["blocked_rate_percent"],
            rate_limited_count=value["rate_limited_count"],
            rate_limited_rate_percent=value["rate_limited_rate_percent"],
            last_success_at=last_success_at,
            failure_counts=dict(failure_counts),
        )

    @staticmethod
    def _parse_retailer_watch_impact(value: object) -> PriceWatchRetailerWatchImpact:
        """Parse the bounded active/affected watch-impact counts."""
        if not isinstance(value, dict):
            raise PriceWatchInvalidResponseError
        counts = ("active_watch_count", "affected_watch_count")
        if any(
            not isinstance(value.get(key), int)
            or isinstance(value.get(key), bool)
            or value[key] < 0
            for key in counts
        ):
            raise PriceWatchInvalidResponseError
        return PriceWatchRetailerWatchImpact(
            active_watch_count=value["active_watch_count"],
            affected_watch_count=value["affected_watch_count"],
        )

    def _parse_retailer(self, value: object) -> PriceWatchRetailer:
        """Parse one F10 Phase 6a bounded retailer operational summary."""
        if not isinstance(value, dict):
            raise PriceWatchInvalidResponseError
        retailer_id = value.get("retailer_id")
        if not isinstance(retailer_id, str) or not retailer_id:
            raise PriceWatchInvalidResponseError
        if not isinstance(value.get("enabled"), bool):
            raise PriceWatchInvalidResponseError
        acquisition_methods = value.get("acquisition_methods")
        if (
            not isinstance(acquisition_methods, list)
            or not acquisition_methods
            or not all(
                isinstance(method, str) and method in _RETAILER_ACQUISITION_METHODS
                for method in acquisition_methods
            )
        ):
            raise PriceWatchInvalidResponseError
        interpretation_mode = value.get("interpretation_mode")
        if interpretation_mode not in _RETAILER_INTERPRETATION_MODES:
            raise PriceWatchInvalidResponseError
        status = value.get("status")
        if status not in _RETAILER_STATUSES:
            raise PriceWatchInvalidResponseError
        preferred_strategy = value.get("preferred_strategy")
        if preferred_strategy not in _RETAILER_PREFERRED_STRATEGIES:
            raise PriceWatchInvalidResponseError
        active_strategy = value.get("active_strategy")
        if active_strategy not in _RETAILER_ACQUISITION_METHODS:
            raise PriceWatchInvalidResponseError
        effective_strategy_reason = value.get("effective_strategy_reason")
        if effective_strategy_reason not in _RETAILER_EFFECTIVE_STRATEGY_REASONS:
            raise PriceWatchInvalidResponseError
        cooldown_until = value.get("cooldown_until")
        if cooldown_until is not None and (
            not isinstance(cooldown_until, str) or not cooldown_until
        ):
            raise PriceWatchInvalidResponseError
        metrics = value.get("metrics")
        if not isinstance(metrics, list):
            raise PriceWatchInvalidResponseError
        return PriceWatchRetailer(
            retailer_id=retailer_id,
            enabled=value["enabled"],
            acquisition_methods=tuple(acquisition_methods),
            interpretation_mode=interpretation_mode,
            status=status,
            preferred_strategy=preferred_strategy,
            active_strategy=active_strategy,
            effective_strategy_reason=effective_strategy_reason,
            watch_impact=self._parse_retailer_watch_impact(value.get("watch_impact")),
            metrics=tuple(
                self._parse_retailer_metric(item) for item in metrics
            ),
            cooldown_until=cooldown_until,
            last_success=self._parse_retailer_attempt(value.get("last_success")),
            last_failure=self._parse_retailer_attempt(value.get("last_failure")),
        )

    @staticmethod
    def _parse_retailer_diagnostic_result(
        value: object,
    ) -> PriceWatchRetailerDiagnosticResult:
        """Parse a bounded diagnostic outcome; never price/product data."""
        if not isinstance(value, dict):
            raise PriceWatchInvalidResponseError
        required_strings = ("retailer_id", "watch_id", "tested_at")
        if any(
            not isinstance(value.get(key), str) or not value[key]
            for key in required_strings
        ):
            raise PriceWatchInvalidResponseError
        outcome = value.get("outcome")
        if outcome not in _RETAILER_DIAGNOSTIC_OUTCOMES:
            raise PriceWatchInvalidResponseError
        acquisition_method = value.get("acquisition_method")
        if (
            acquisition_method is not None
            and acquisition_method not in _RETAILER_ACQUISITION_METHODS
        ):
            raise PriceWatchInvalidResponseError
        classification = value.get("classification")
        if classification is not None and (
            not isinstance(classification, str) or not classification
        ):
            raise PriceWatchInvalidResponseError
        return PriceWatchRetailerDiagnosticResult(
            retailer_id=value["retailer_id"],
            watch_id=value["watch_id"],
            outcome=outcome,
            tested_at=value["tested_at"],
            acquisition_method=acquisition_method,
            classification=classification,
        )
