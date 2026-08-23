"""Minimal authenticated client for the Price Watch service."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit
from uuid import UUID, uuid4

import aiohttp

from .const import (
    CHECKS_PATH,
    EVENTS_PATH,
    HEALTH_PATH,
    REQUEST_ID_HEADER,
    REQUEST_TIMEOUT_SECONDS,
    SUMMARY_PATH,
    WATCHES_PATH,
)


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
    status: str
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


_TRUSTED_SERVICE_CODES = frozenset(
    {
        "idempotency_conflict",
        "idempotency_key_required",
        "internal_error",
        "not_found",
        "unauthorized",
        "unsupported_product_host",
        "unsupported_retailer",
        "validation_error",
    }
)


def _trusted_service_code(value: object) -> str | None:
    """Return only expected machine-readable service codes."""
    return value if isinstance(value, str) and value in _TRUSTED_SERVICE_CODES else None


def normalise_base_url(value: str) -> str:
    """Return a canonical HTTP(S) service URL without a trailing slash."""
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base URL must be an HTTP(S) URL without credentials")
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
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
        """Return validated watches from the first service page."""
        payload = await self._async_get_json(WATCHES_PATH, request_id=request_id)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise PriceWatchInvalidResponseError
        next_cursor = payload.get("next_cursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise PriceWatchInvalidResponseError
        return tuple(self._parse_watch(item) for item in payload["data"])

    async def async_get_events(
        self, *, request_id: str | None = None
    ) -> tuple[PriceWatchEvent, ...]:
        """Return validated immutable events."""
        payload = await self._async_get_json(EVENTS_PATH, request_id=request_id)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise PriceWatchInvalidResponseError
        return tuple(self._parse_event(item) for item in payload["data"])

    async def async_check_all(self, *, request_id: str | None = None) -> None:
        """Run due watches with a new idempotency key.

        The Home Assistant action only needs the request to complete; the
        coordinator refresh immediately afterwards fetches the authoritative
        watch state. Do not reject a successful check because its per-watch
        response body is not needed by the action.
        """
        await self._async_post_json(
            CHECKS_PATH,
            str(uuid4()),
            request_id=request_id,
        )

    async def async_check_watch(
        self, watch_id: str, *, request_id: str | None = None
    ) -> PriceWatchCheckResult:
        """Run one watch with a new idempotency key."""
        if not isinstance(watch_id, str) or not watch_id:
            raise ValueError("watch ID is required")
        payload = await self._async_post_json(
            f"{WATCHES_PATH}/{quote(watch_id, safe='')}/check",
            str(uuid4()),
            request_id=request_id,
        )
        return self._parse_check_result(payload)

    async def async_set_enabled(
        self, watch_id: str, enabled: bool, *, request_id: str | None = None
    ) -> PriceWatchEnabledResult:
        """Set one watch's enabled state with a new idempotency key."""
        if not isinstance(watch_id, str) or not watch_id:
            raise ValueError("watch ID is required")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        payload = await self._async_patch_json(
            f"{WATCHES_PATH}/{quote(watch_id, safe='')}",
            str(uuid4()),
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

    async def _async_get_json(
        self, path: str, *, request_id: str | None = None
    ) -> object:
        """Fetch one authenticated JSON response without retaining raw payloads."""
        return await self._async_request_json("get", path, request_id=request_id)

    async def _async_post_json(
        self,
        path: str,
        idempotency_key: str,
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
                    json={},
                ) as response:
                    if response.status in {401, 403}:
                        raise PriceWatchAuthenticationError(response.status)
                    if response.status != 200:
                        raise await self._async_api_response_error(response)
                    try:
                        return await response.json(content_type=None)
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
        payload: dict[str, bool],
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
                ) as response:
                    if response.status in {401, 403}:
                        raise PriceWatchAuthenticationError(response.status)
                    if response.status != 200:
                        raise await self._async_api_response_error(response)
                    try:
                        return await response.json(content_type=None)
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
                ) as response:
                    if response.status in {401, 403}:
                        raise PriceWatchAuthenticationError(response.status)
                    if response.status != 200:
                        raise await self._async_api_response_error(response)
                    try:
                        payload = await response.json(content_type=None)
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
            payload = await response.json(content_type=None)
        except (aiohttp.ClientError, TypeError, ValueError):
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
            product_image_url=product_image_url,
        )

    def _parse_product_image_url(self, value: object) -> str | None:
        """Accept only a Price Watch image endpoint, never a retailer URL.

        The App returns its image capability as a relative URL so its internal
        hostname and token never need to be persisted by the service. Resolve
        that narrow, authenticated endpoint against the configured service
        base URL before exposing it to Home Assistant.
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
                image_url.scheme in {"http", "https"}
                and bool(image_url.netloc)
                and not image_url.username
                and not image_url.password
                and not image_url.query
                and not image_url.fragment
                and image_url.hostname == base_url.hostname
                and image_url.port == base_url.port
                and image_url.scheme == base_url.scheme
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise PriceWatchInvalidResponseError

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
        if value["status"] not in {
            "available",
            "out_of_stock",
            "unknown",
            "check_failed",
        }:
            raise PriceWatchInvalidResponseError
        optional_money = ("price_cents", "compare_at_price_cents")
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
            "id",
            "watch_id",
            "type",
            "occurred_at",
            "deduplication_key",
            "data_json",
        )
        if any(not isinstance(value.get(key), str) or not value[key] for key in required_strings):
            raise PriceWatchInvalidResponseError
        observation_id = value.get("observation_id")
        if observation_id is not None and not isinstance(observation_id, str):
            raise PriceWatchInvalidResponseError
        try:
            data = json.loads(value["data_json"])
        except (TypeError, ValueError) as err:
            raise PriceWatchInvalidResponseError from err
        if not isinstance(data, dict):
            raise PriceWatchInvalidResponseError
        return PriceWatchEvent(
            id=value["id"],
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
        if value["status"] not in {
            "available",
            "out_of_stock",
            "unknown",
            "check_failed",
        }:
            raise PriceWatchInvalidResponseError
        return PriceWatchCheckResult(
            id=value["id"],
            watch_id=value["watch_id"],
            checked_at=value["checked_at"],
            status=value["status"],
        )
