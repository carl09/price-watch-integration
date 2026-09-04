"""Focused tests for obsolete per-watch button registry reconciliation."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.price_watch import (
    _async_reconcile_watch_button_registry,
    async_setup_entry,
)
from custom_components.price_watch.const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    DATA_CLIENTS,
    DOMAIN,
)

pytestmark = pytest.mark.asyncio


def _entry(*, data: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data=data
        or {
            CONF_BASE_URL: "http://price-watch.test:8787",
            CONF_API_TOKEN: "redacted-test-token",
        },
    )


def _register(
    registry,
    entry,
    *,
    domain="button",
    platform=DOMAIN,
    unique_id,
    **kwargs,
):
    return registry.async_get_or_create(
        domain,
        platform,
        unique_id,
        config_entry=entry,
        **kwargs,
    )


async def test_reconciliation_removes_only_exact_active_watch_buttons(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    other_entry = _entry()
    other_entry.add_to_hass(hass)

    removed = [
        _register(registry, entry, unique_id="watch_watch-one_check"),
        _register(registry, entry, unique_id="watch_watch-two_retry_image"),
    ]
    renamed = _register(
        registry,
        entry,
        unique_id="watch_watch-three_check",
        original_name="Renamed obsolete action",
    )
    disabled = _register(
        registry,
        entry,
        unique_id="watch_watch-four_retry_image",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    unavailable = _register(
        registry, entry, unique_id="watch_watch-five_check"
    )
    hass.states.async_set(unavailable.entity_id, "unavailable")
    removed.extend((renamed, disabled, unavailable))
    retained = (
        _register(registry, entry, unique_id="watch__check"),
        _register(registry, entry, unique_id="watch_watch-six_check_extra"),
        _register(registry, entry, unique_id="watch_watch-seven_retry_image_old"),
        _register(registry, entry, unique_id="watch_watch-eight/check"),
        _register(registry, entry, unique_id="watch_watch-nine check"),
        _register(registry, entry, unique_id="not_watch_watch-ten_check"),
        _register(registry, entry, unique_id="watch_watch-eleven_check", domain="sensor"),
        _register(registry, entry, unique_id="watch_watch-twelve_check", platform="other"),
        _register(registry, other_entry, unique_id="watch_watch-thirteen_check"),
    )

    await _async_reconcile_watch_button_registry(hass, entry)

    for registry_entry in removed:
        assert registry.async_get(registry_entry.entity_id) is None
    for registry_entry in retained:
        assert registry.async_get(registry_entry.entity_id) is not None
    assert entry.data["watch_button_registry_reconciled"] == 1
    assert entry.data[CONF_BASE_URL] == "http://price-watch.test:8787"


async def test_valid_marker_still_scans_without_rewriting_entry_data(hass):
    entry = _entry(data={"keep": "value", "watch_button_registry_reconciled": 1})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    obsolete = _register(registry, entry, unique_id="watch_watch-one_check")

    with patch.object(hass.config_entries, "async_update_entry") as update:
        await _async_reconcile_watch_button_registry(hass, entry)

    assert registry.async_get(obsolete.entity_id) is None
    update.assert_not_called()
    assert entry.data == {"keep": "value", "watch_button_registry_reconciled": 1}


@pytest.mark.parametrize("marker", [True, False, "1", 1.0, -1, object()])
async def test_malformed_marker_fails_without_registry_mutation(hass, marker):
    entry = _entry(
        data={"keep": "value", "watch_button_registry_reconciled": marker}
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    obsolete = _register(registry, entry, unique_id="watch_watch-one_check")

    with pytest.raises(ConfigEntryNotReady):
        await _async_reconcile_watch_button_registry(hass, entry)

    assert registry.async_get(obsolete.entity_id) is not None
    assert entry.data == {
        "keep": "value",
        "watch_button_registry_reconciled": marker,
    }


@pytest.mark.parametrize("marker", [1, 2])
async def test_valid_marker_is_not_downgraded_or_rewritten(hass, marker):
    entry = _entry(data={"keep": "value", "watch_button_registry_reconciled": marker})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    obsolete = _register(registry, entry, unique_id="watch_watch-one_check")

    with patch.object(hass.config_entries, "async_update_entry") as update:
        await _async_reconcile_watch_button_registry(hass, entry)

    assert registry.async_get(obsolete.entity_id) is None
    update.assert_not_called()
    assert entry.data == {"keep": "value", "watch_button_registry_reconciled": marker}


async def test_partial_removal_failure_keeps_successful_removal_and_marker(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    first = _register(registry, entry, unique_id="watch_watch-one_check")
    second = _register(registry, entry, unique_id="watch_watch-two_check")
    original_remove = registry.async_remove

    def remove(entity_id):
        if entity_id == second.entity_id:
            raise RuntimeError("registry write failed")
        return original_remove(entity_id)

    with patch.object(registry, "async_remove", side_effect=remove):
        with pytest.raises(ConfigEntryNotReady):
            await _async_reconcile_watch_button_registry(hass, entry)

    assert registry.async_get(first.entity_id) is None
    assert registry.async_get(second.entity_id) is not None
    assert "watch_button_registry_reconciled" not in entry.data


async def test_marker_update_failure_blocks_setup_and_preserves_marker(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    obsolete = _register(registry, entry, unique_id="watch_watch-one_check")

    with patch.object(
        hass.config_entries,
        "async_update_entry",
        side_effect=RuntimeError("entry write failed"),
    ):
        with pytest.raises(ConfigEntryNotReady):
            await _async_reconcile_watch_button_registry(hass, entry)

    assert registry.async_get(obsolete.entity_id) is None
    assert "watch_button_registry_reconciled" not in entry.data


async def test_marker_readback_failure_blocks_setup_and_keeps_marker_pending(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    obsolete = _register(registry, entry, unique_id="watch_watch-one_check")

    with patch.object(hass.config_entries, "async_update_entry"):
        with pytest.raises(ConfigEntryNotReady):
            await _async_reconcile_watch_button_registry(hass, entry)

    assert registry.async_get(obsolete.entity_id) is None
    assert "watch_button_registry_reconciled" not in entry.data


async def test_repeated_setup_scans_again_without_marker_rewrite(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)

    await _async_reconcile_watch_button_registry(hass, entry)
    late_entry = _register(registry, entry, unique_id="watch_watch-late_check")
    with patch.object(hass.config_entries, "async_update_entry") as update:
        await _async_reconcile_watch_button_registry(hass, entry)

    assert registry.async_get(late_entry.entity_id) is None
    update.assert_not_called()
    assert entry.data["watch_button_registry_reconciled"] == 1


async def test_setup_does_not_forward_or_create_runtime_data_when_reconciliation_fails(
    hass,
):
    entry = _entry()
    entry.add_to_hass(hass)
    forwarded = AsyncMock()

    with patch(
        "custom_components.price_watch._async_reconcile_watch_button_registry",
        new=AsyncMock(side_effect=ConfigEntryNotReady("retry")),
    ), patch.object(hass.config_entries, "async_forward_entry_setups", forwarded):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)

    forwarded.assert_not_awaited()
    assert DOMAIN not in hass.data or entry.entry_id not in hass.data[DOMAIN].get(
        DATA_CLIENTS, {}
    )
