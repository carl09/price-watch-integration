"""Canonical content and retained-contract checks for IAC-03."""

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "price_watch"


def test_retained_service_metadata_keeps_check_actions_and_strict_fields():
    """The public service declaration retains both check actions unchanged."""
    services = yaml.safe_load((COMPONENT / "services.yaml").read_text())

    assert set(("check_all", "check_watch")) <= services.keys()
    assert services["check_all"]["fields"]["idempotency_key"]["required"] is False
    assert services["check_watch"]["fields"]["watch_id"]["required"] is True
    assert services["check_watch"]["fields"]["idempotency_key"]["required"] is False
    assert "watch_check" not in services
    assert "watch_retry_image" not in services


def test_translations_and_source_contain_no_obsolete_watch_button_contract():
    """Only the two obsolete per-watch button wrappers are absent."""
    strings = json.loads((COMPONENT / "strings.json").read_text())
    translations = json.loads((COMPONENT / "translations" / "en.json").read_text())
    button_source = (COMPONENT / "button.py").read_text()
    init_source = (COMPONENT / "__init__.py").read_text()
    const_source = (COMPONENT / "const.py").read_text()
    readme = (ROOT / "README.md").read_text()

    for document in (strings, translations):
        assert "entity" not in document or "button" not in document["entity"]
    for source in (button_source, init_source, const_source, readme):
        assert "DATA_WATCH_BUTTON_MANAGERS" not in source
        assert "PriceWatchWatchButtonManager" not in source
        assert "PriceWatchWatchCheckButton" not in source
        assert "PriceWatchWatchImageRetryButton" not in source
        assert "watch_check" not in source
        assert "watch_retry_image" not in source
        assert "Check product" not in source
        assert "Retry product image" not in source

    assert "PriceWatchRetailerTestButton" in button_source
    assert "PriceWatchRetailerResetButton" in button_source
    assert 'SERVICE_CHECK_ALL = "check_all"' in const_source
    assert 'SERVICE_CHECK_WATCH = "check_watch"' in const_source
    assert "obsolete per-watch action buttons are removed" in readme
    assert "entity-specific customizations" in readme
    assert "price_watch.check_watch" in readme
    assert "price_watch.check_all" in readme


def test_retained_routes_and_client_operations_remain_declared():
    """The cleanup does not remove retained client/API route methods."""
    api_source = (COMPONENT / "api.py").read_text()
    services_source = (COMPONENT / "services.py").read_text()
    image_source = (COMPONENT / "image_proxy.py").read_text()

    for source, terms in (
        (
            api_source,
            (
                "async_check_all",
                "async_check_watch",
                "async_retry_product_image",
                "Idempotency-Key",
                "CHECKS_PATH",
                "/image/retry",
            ),
        ),
        (
            services_source,
            (
                "SERVICE_CHECK_ALL",
                "SERVICE_CHECK_WATCH",
                "async_check_all",
                "async_check_watch",
            ),
        ),
        (image_source, ("PriceWatchImageCache", "async_get_image")),
    ):
        for term in terms:
            assert term in source
