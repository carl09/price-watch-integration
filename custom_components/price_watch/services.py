"""Manual Price Watch action registration."""

from __future__ import annotations

from functools import partial
from uuid import uuid4

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .api import (
    PriceWatchApiClient,
    PriceWatchApiResponseError,
    PriceWatchAuthenticationError,
    PriceWatchInvalidResponseError,
    PriceWatchTimeoutError,
    PriceWatchTransportError,
)
from .const import (
    ATTR_ENABLED,
    ATTR_WATCH_ID,
    IDEMPOTENCY_KEY,
    DATA_CLIENTS,
    DATA_COORDINATORS,
    DOMAIN,
    SERVICE_CHECK_ALL,
    SERVICE_CHECK_WATCH,
    SERVICE_ADD_TO_SHOPPING_LIST,
    SERVICE_SET_ENABLED,
    SHOPPING_LIST_ADD_ITEM,
    SHOPPING_LIST_DOMAIN,
    SHOPPING_LIST_ITEM_NAME,
)
from .coordinator import PriceWatchCoordinator
from .observability import log_failure, log_service_failure, log_success

_IDEMPOTENCY_KEY_SCHEMA = vol.All(
    str, vol.Match(r"^[A-Za-z0-9._: -]{1,128}$")
)


def _action_schema(fields: dict[object, object]) -> vol.Schema:
    """Allow callers to preserve one action idempotency key."""
    return vol.Schema(
        {
            **fields,
            vol.Optional(IDEMPOTENCY_KEY): _IDEMPOTENCY_KEY_SCHEMA,
        }
    )


def async_register_services(hass: HomeAssistant) -> None:
    """Register manual-check actions once for the integration domain."""
    if not hass.services.has_service(DOMAIN, SERVICE_CHECK_ALL):
        hass.services.async_register(
            DOMAIN,
            SERVICE_CHECK_ALL,
            partial(_async_check_all, hass),
            schema=_action_schema({}),
        )
    if not hass.services.has_service(DOMAIN, SERVICE_CHECK_WATCH):
        hass.services.async_register(
            DOMAIN,
            SERVICE_CHECK_WATCH,
            partial(_async_check_watch, hass),
            schema=_action_schema(
                {vol.Required(ATTR_WATCH_ID): vol.All(str, vol.Length(min=1))}
            ),
        )
    if not hass.services.has_service(DOMAIN, SERVICE_SET_ENABLED):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_ENABLED,
            partial(_async_set_enabled, hass),
            schema=_action_schema(
                {
                    vol.Required(ATTR_WATCH_ID): vol.All(str, vol.Length(min=1)),
                    vol.Required(ATTR_ENABLED): bool,
                }
            ),
        )
    if (
        hass.services.has_service(SHOPPING_LIST_DOMAIN, SHOPPING_LIST_ADD_ITEM)
        and not hass.services.has_service(DOMAIN, SERVICE_ADD_TO_SHOPPING_LIST)
    ):
        hass.services.async_register(
            DOMAIN,
            SERVICE_ADD_TO_SHOPPING_LIST,
            partial(_async_add_to_shopping_list, hass),
            schema=_action_schema(
                {vol.Required(ATTR_WATCH_ID): vol.All(str, vol.Length(min=1))}
            ),
        )


def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove Price Watch actions after the final entry unloads."""
    for service in (
        SERVICE_CHECK_ALL,
        SERVICE_CHECK_WATCH,
        SERVICE_SET_ENABLED,
        SERVICE_ADD_TO_SHOPPING_LIST,
    ):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)


def _runtime(hass: HomeAssistant) -> tuple[PriceWatchApiClient, PriceWatchCoordinator]:
    domain_data = hass.data.get(DOMAIN, {})
    clients = domain_data.get(DATA_CLIENTS, {})
    coordinators = domain_data.get(DATA_COORDINATORS, {})
    if len(clients) != 1 or len(coordinators) != 1:
        raise HomeAssistantError("Price Watch service is unavailable")
    entry_id, client = next(iter(clients.items()))
    coordinator = coordinators.get(entry_id)
    if coordinator is None:
        raise HomeAssistantError("Price Watch service is unavailable")
    return client, coordinator


async def _async_check_all(hass: HomeAssistant, call: ServiceCall) -> None:
    """Run due watches, then request an entity refresh."""
    client, coordinator = _runtime(hass)
    operation = SERVICE_CHECK_ALL
    route = "/v1/checks"
    request_id = str(uuid4())
    try:
        await client.async_check_all(
            idempotency_key=call.data.get(IDEMPOTENCY_KEY), request_id=request_id
        )
    except PriceWatchAuthenticationError as err:
        log_failure(operation, route, err, request_id=request_id)
        raise HomeAssistantError("Price Watch authentication failed") from err
    except PriceWatchTimeoutError as err:
        log_failure(operation, route, err, request_id=request_id)
        raise HomeAssistantError("Price Watch service timed out") from err
    except PriceWatchTransportError as err:
        log_failure(operation, route, err, request_id=request_id)
        raise HomeAssistantError("Price Watch service is unavailable") from err
    except PriceWatchInvalidResponseError as err:
        log_failure(operation, route, err, request_id=request_id)
        raise HomeAssistantError("Price Watch service returned invalid data") from err
    except PriceWatchApiResponseError as err:
        log_failure(operation, route, err, request_id=request_id)
        raise HomeAssistantError("Price Watch service rejected the request") from err
    await coordinator.async_request_refresh()
    log_success(operation, route, request_id=request_id)


async def _async_check_watch(hass: HomeAssistant, call: ServiceCall) -> None:
    """Run one watch, then request an entity refresh."""
    client, coordinator = _runtime(hass)
    watch_id = call.data[ATTR_WATCH_ID]
    operation = SERVICE_CHECK_WATCH
    route = "/v1/watches/{watch_id}/check"
    request_id = str(uuid4())
    try:
        await client.async_check_watch(
            watch_id,
            idempotency_key=call.data.get(IDEMPOTENCY_KEY),
            request_id=request_id,
        )
    except PriceWatchAuthenticationError as err:
        log_failure(operation, route, err, watch_id=watch_id, request_id=request_id)
        raise HomeAssistantError("Price Watch authentication failed") from err
    except PriceWatchTimeoutError as err:
        log_failure(operation, route, err, watch_id=watch_id, request_id=request_id)
        raise HomeAssistantError("Price Watch service timed out") from err
    except PriceWatchTransportError as err:
        log_failure(operation, route, err, watch_id=watch_id, request_id=request_id)
        raise HomeAssistantError("Price Watch service is unavailable") from err
    except PriceWatchInvalidResponseError as err:
        log_failure(operation, route, err, watch_id=watch_id, request_id=request_id)
        raise HomeAssistantError("Price Watch service returned invalid data") from err
    except PriceWatchApiResponseError as err:
        log_failure(operation, route, err, watch_id=watch_id, request_id=request_id)
        raise HomeAssistantError("Price Watch service rejected the request") from err
    await coordinator.async_request_refresh()
    log_success(operation, route, watch_id=watch_id, request_id=request_id)


async def _async_set_enabled(hass: HomeAssistant, call: ServiceCall) -> None:
    """Set one watch's enabled state, then request an entity refresh."""
    client, coordinator = _runtime(hass)
    watch_id = call.data[ATTR_WATCH_ID]
    operation = SERVICE_SET_ENABLED
    route = "/v1/watches/{watch_id}"
    request_id = str(uuid4())
    try:
        await client.async_set_enabled(
            watch_id,
            call.data[ATTR_ENABLED],
            idempotency_key=call.data.get(IDEMPOTENCY_KEY),
            request_id=request_id,
        )
    except PriceWatchAuthenticationError as err:
        log_failure(operation, route, err, watch_id=watch_id, request_id=request_id)
        raise HomeAssistantError("Price Watch authentication failed") from err
    except PriceWatchTimeoutError as err:
        log_failure(operation, route, err, watch_id=watch_id, request_id=request_id)
        raise HomeAssistantError("Price Watch service timed out") from err
    except PriceWatchTransportError as err:
        log_failure(operation, route, err, watch_id=watch_id, request_id=request_id)
        raise HomeAssistantError("Price Watch service is unavailable") from err
    except PriceWatchInvalidResponseError as err:
        log_failure(operation, route, err, watch_id=watch_id, request_id=request_id)
        raise HomeAssistantError("Price Watch service returned invalid data") from err
    except PriceWatchApiResponseError as err:
        log_failure(operation, route, err, watch_id=watch_id, request_id=request_id)
        raise HomeAssistantError("Price Watch service rejected the request") from err
    await coordinator.async_request_refresh()
    log_success(operation, route, watch_id=watch_id, request_id=request_id)


async def _async_add_to_shopping_list(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Add a selected coordinator watch to Home Assistant's Shopping List."""
    _, coordinator = _runtime(hass)
    watch_id = call.data[ATTR_WATCH_ID]
    operation = SERVICE_ADD_TO_SHOPPING_LIST
    route = "shopping_list.add_item"
    if not hass.services.has_service(
        SHOPPING_LIST_DOMAIN, SHOPPING_LIST_ADD_ITEM
    ):
        log_service_failure(
            operation,
            route,
            failure="api_response",
            watch_id=watch_id,
            service_code="shopping_list_unavailable",
        )
        raise HomeAssistantError("Shopping List is unavailable")
    if coordinator.data is None:
        log_service_failure(
            operation,
            route,
            failure="invalid_response",
            watch_id=watch_id,
            service_code="coordinator_data_unavailable",
        )
        raise HomeAssistantError("Price Watch data is unavailable")
    watch = next(
        (
            current_watch
            for current_watch in coordinator.data.watches
            if current_watch.id == call.data[ATTR_WATCH_ID]
        ),
        None,
    )
    if watch is None:
        log_service_failure(
            operation,
            route,
            failure="api_response",
            watch_id=watch_id,
            service_code="watch_not_found",
        )
        raise HomeAssistantError("Price Watch watch was not found")

    parts = [f"Buy: {watch.title}"]
    observation = watch.current_observation
    if observation is not None and observation.selected_variant_label:
        parts.append(observation.selected_variant_label)
    if (
        observation is not None
        and observation.status == "available"
        and observation.price_cents is not None
    ):
        parts.append(
            f"${observation.price_cents // 100}.{observation.price_cents % 100:02d}"
        )
    parts.append(watch.product_url)
    try:
        await hass.services.async_call(
            SHOPPING_LIST_DOMAIN,
            SHOPPING_LIST_ADD_ITEM,
            {SHOPPING_LIST_ITEM_NAME: " - ".join(parts)},
            blocking=True,
        )
    except HomeAssistantError as err:
        log_service_failure(
            operation,
            route,
            failure="api_response",
            watch_id=watch_id,
            service_code="shopping_list_add_failed",
        )
        raise HomeAssistantError("Unable to add item to Shopping List") from err
    log_success(operation, route, watch_id=watch_id)
