"""Home Assistant test fixtures for the public Price Watch integration."""

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow the isolated test instance to load the custom integration."""
    yield
