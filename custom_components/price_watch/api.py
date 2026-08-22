"""Minimal authenticated client for the Price Watch service."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import aiohttp

from .const import (
    CHECKS_PATH,
    EVENTS_PATH,
    HEALTH_PATH,
    REQUEST_TIMEOUT_SECONDS,
    SUMMARY_PATH,
    WATCHES_PATH,
)


class PriceWatchApiError(Exception):
    """Base error raised by the Price Watch API client."""


class PriceWatchAuthenticationError(PriceWatchApiError):
    """The configured token was rejected by the service."""


class PriceWatchTransportError(PriceWatchApiError):
    """The service could not be reached."""


class PriceWatchTimeoutError(PriceWatchApiError):
    """The service did not respond before the request deadline."""


class PriceWatchInvalidResponseError(PriceWatchApiError):
    """The service returned an unexpected health response."""


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

    async def async_get_health(self) -> PriceWatchHealth:
        """Validate the authenticated service health response."""
        payload = await self._async_get_json(HEALTH_PATH)
        if not isinstance(payload, dict):
            raise PriceWatchInvalidResponseError
        if payload.get("status") != "healthy" or payload.get("database") != "healthy":
            raise PriceWatchInvalidResponseError
        version = payload.get("version")
        if not isinstance(version, str) or not version:
            raise PriceWatchInvalidResponseError
        return PriceWatchHealth(version=version)

    async def async_get_summary(self) -> PriceWatchSummary:
        """Return a validated service summary."""
        payload = await self._async_get_json(SUMMARY_PATH)
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

    async def async_get_watches(self) -> tuple[PriceWatchWatch, ...]:
        """Return validated watches from the first service page."""
        payload = await self._async_get_json(WATCHES_PATH)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise PriceWatchInvalidResponseError
        next_cursor = payload.get("next_cursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise PriceWatchInvalidResponseError
        return tuple(self._parse_watch(item) for item in payload["data"])

    async def async_get_events(self) -> tuple[PriceWatchEvent, ...]:
        """Return validated immutable events."""
        payload = await self._async_get_json(EVENTS_PATH)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise PriceWatchInvalidResponseError
        return tuple(self._parse_event(item) for item in payload["data"])

    async def async_check_all(self) -> None:
        """Run due watches with a new idempotency key.

        The Home Assistant action only needs the request to complete; the
        coordinator refresh immediately afterwards fetches the authoritative
        watch state. Do not reject a successful check because its per-watch
        response body is not needed by the action.
        """
        await self._async_post_json(CHECKS_PATH, str(uuid4()))

    async def async_check_watch(self, watch_id: str) -> PriceWatchCheckResult:
        """Run one watch with a new idempotency key."""
        if not isinstance(watch_id, str) or not watch_id:
            raise ValueError("watch ID is required")
        payload = await self._async_post_json(
            f"{WATCHES_PATH}/{quote(watch_id, safe='')}/check",
            str(uuid4()),
        )
        return self._parse_check_result(payload)

    async def async_set_enabled(
        self, watch_id: str, enabled: bool
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
        )
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("id"), str)
            or not payload["id"]
            or not isinstance(payload.get("enabled"), bool)
        ):
            raise PriceWatchInvalidResponseError
        return PriceWatchEnabledResult(id=payload["id"], enabled=payload["enabled"])

    async def _async_get_json(self, path: str) -> object:
        """Fetch one authenticated JSON response without retaining raw payloads."""
        return await self._async_request_json("get", path)

    async def _async_post_json(self, path: str, idempotency_key: str) -> object:
        """Post one authenticated JSON response without retaining raw payloads."""
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                async with self._session.post(
                    f"{self._base_url}{path}",
                    headers={
                        "Authorization": f"Bearer {self._api_token}",
                        "Idempotency-Key": idempotency_key,
                    },
                    json={},
                ) as response:
                    if response.status in {401, 403}:
                        raise PriceWatchAuthenticationError
                    if response.status != 200:
                        raise PriceWatchInvalidResponseError(
                            f"HTTP {response.status}"
                        )
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
    ) -> object:
        """Patch one authenticated JSON response without retaining raw payloads."""
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                async with self._session.patch(
                    f"{self._base_url}{path}",
                    headers={
                        "Authorization": f"Bearer {self._api_token}",
                        "Idempotency-Key": idempotency_key,
                    },
                    json=payload,
                ) as response:
                    if response.status in {401, 403}:
                        raise PriceWatchAuthenticationError
                    if response.status != 200:
                        raise PriceWatchInvalidResponseError(
                            f"HTTP {response.status}"
                        )
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
    ) -> object:
        """Send an authenticated request without retaining raw payloads."""
        headers = {"Authorization": f"Bearer {self._api_token}"}
        if additional_headers is not None:
            headers.update(additional_headers)
        request = self._session.get if method == "get" else self._session.post
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                async with request(
                    f"{self._base_url}{path}",
                    headers={"Authorization": f"Bearer {self._api_token}"},
                ) as response:
                    if response.status in {401, 403}:
                        raise PriceWatchAuthenticationError
                    if response.status != 200:
                        raise PriceWatchInvalidResponseError(
                            f"HTTP {response.status}"
                        )
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
    def _parse_watch(value: object) -> PriceWatchWatch:
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
