"""Config flow for the Price Watch integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    PriceWatchApiClient,
    PriceWatchAuthenticationError,
    PriceWatchInvalidResponseError,
    PriceWatchTimeoutError,
    PriceWatchTransportError,
    normalise_base_url,
)
from .const import CONF_API_TOKEN, CONF_BASE_URL, DEFAULT_TITLE, DOMAIN


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle Price Watch configuration."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Configure the single supported Price Watch service."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is None:
            return self._show_user_form()

        errors = await self._async_validate_input(user_input)
        if errors:
            return self._show_user_form(errors)
        return self.async_create_entry(title=DEFAULT_TITLE, data=user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Update the service URL or token for the existing entry."""
        entry = self._get_reconfigure_entry()
        if user_input is None:
            return self._show_reconfigure_form(entry.data)

        errors = await self._async_validate_input(user_input)
        if errors:
            return self._show_reconfigure_form(user_input, errors)
        self.hass.config_entries.async_update_entry(entry, data=user_input)
        return self.async_abort(reason="reconfigure_successful")

    async def _async_validate_input(
        self, user_input: dict[str, Any]
    ) -> dict[str, str]:
        """Normalise and validate credentials without exposing them."""
        try:
            user_input[CONF_BASE_URL] = normalise_base_url(
                str(user_input[CONF_BASE_URL])
            )
        except (KeyError, ValueError):
            return {"base": "invalid_url"}

        token = str(user_input.get(CONF_API_TOKEN, "")).strip()
        if not token:
            return {"base": "invalid_auth"}
        user_input[CONF_API_TOKEN] = token

        client = PriceWatchApiClient(
            user_input[CONF_BASE_URL],
            token,
            async_get_clientsession(self.hass),
        )
        try:
            await client.async_get_health()
        except PriceWatchAuthenticationError:
            return {"base": "invalid_auth"}
        except PriceWatchTransportError:
            return {"base": "cannot_connect"}
        except PriceWatchTimeoutError:
            return {"base": "timeout"}
        except PriceWatchInvalidResponseError:
            return {"base": "invalid_response"}
        return {}

    def _show_user_form(
        self, errors: dict[str, str] | None = None
    ) -> config_entries.ConfigFlowResult:
        return self.async_show_form(
            step_id="user",
            data_schema=self._show_user_form_schema(),
            errors=errors or {},
        )

    def _show_reconfigure_form(
        self,
        defaults: dict[str, Any],
        errors: dict[str, str] | None = None,
    ) -> config_entries.ConfigFlowResult:
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                self._show_user_form_schema(), defaults
            ),
            errors=errors or {},
        )

    @staticmethod
    def _show_user_form_schema() -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_BASE_URL): str,
                vol.Required(CONF_API_TOKEN): str,
            }
        )
