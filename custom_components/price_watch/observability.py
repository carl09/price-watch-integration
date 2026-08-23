"""Safe structured logging for Price Watch integration operations."""

from __future__ import annotations

import logging
import re

from .api import (
    PriceWatchApiResponseError,
    PriceWatchAuthenticationError,
    PriceWatchInvalidResponseError,
    PriceWatchTimeoutError,
    PriceWatchTransportError,
)

_LOGGER = logging.getLogger(__package__)
_SAFE_SERVICE_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def log_success(
    operation: str, route: str, *, watch_id: str | None = None
) -> None:
    """Log a successful operation at debug level without sensitive values."""
    _log(
        logging.DEBUG,
        "succeeded",
        operation,
        route,
        watch_id=watch_id,
    )


def log_failure(
    operation: str,
    route: str,
    error: Exception,
    *,
    watch_id: str | None = None,
    service_code: str | None = None,
) -> None:
    """Log a classified API failure without exception text or response bodies."""
    category, http_status, error_service_code = _classify_error(error)
    _log(
        logging.WARNING,
        "failed",
        operation,
        route,
        failure=category,
        watch_id=watch_id,
        http_status=http_status,
        service_code=service_code or error_service_code,
    )


def log_service_failure(
    operation: str,
    route: str,
    *,
    failure: str,
    watch_id: str | None = None,
    service_code: str | None = None,
) -> None:
    """Log a safe local Home Assistant service failure."""
    _log(
        logging.WARNING,
        "failed",
        operation,
        route,
        failure=failure,
        watch_id=watch_id,
        service_code=service_code,
    )


def _classify_error(error: Exception) -> tuple[str, int | None, str | None]:
    if isinstance(error, PriceWatchAuthenticationError):
        return "authentication", error.http_status, None
    if isinstance(error, PriceWatchTimeoutError):
        return "timeout", None, None
    if isinstance(error, PriceWatchTransportError):
        return "transport", None, None
    if isinstance(error, PriceWatchApiResponseError):
        return "api_response", error.http_status, error.service_code
    if isinstance(error, PriceWatchInvalidResponseError):
        return "invalid_response", None, None
    return "api_response", None, None


def _log(
    level: int,
    outcome: str,
    operation: str,
    route: str,
    *,
    failure: str | None = None,
    watch_id: str | None = None,
    http_status: int | None = None,
    service_code: str | None = None,
) -> None:
    fields = [
        f"operation={operation}",
        f"route={route}",
        f"outcome={outcome}",
    ]
    if failure is not None:
        fields.append(f"failure={failure}")
    if watch_id is not None:
        fields.append(f"watch_id={_safe_identifier(watch_id)}")
    if http_status is not None and 100 <= http_status <= 599:
        fields.append(f"http_status={http_status}")
    if service_code is not None and _SAFE_SERVICE_CODE.fullmatch(service_code):
        fields.append(f"service_code={service_code}")
    _LOGGER.log(level, "Price Watch integration %s", " ".join(fields))


def _safe_identifier(value: str) -> str:
    if (
        len(value) > 128
        or "://" in value
        or "?" in value
        or "bearer" in value.lower()
        or "token" in value.lower()
    ):
        return "<redacted>"
    return value
