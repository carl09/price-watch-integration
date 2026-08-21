# Price Watch for Home Assistant

A Home Assistant custom integration for a self-hosted **Price Watch** service. It exposes the service's watch state in Home Assistant and provides actions for refreshing and managing watches.

> [!WARNING]
> This is an early release. Run a Price Watch service on your own private network before adding this integration.

## Install with HACS

1. In Home Assistant, open **HACS** → **Integrations** → the three-dot menu → **Custom repositories**.
2. Add `https://github.com/carl09/price-watch-integration` and select **Integration**.
3. Install **Price Watch**, then restart Home Assistant.
4. Go to **Settings** → **Devices & services** → **Add integration** → **Price Watch**.
5. Enter the base URL of your Price Watch service and an API token created for Home Assistant.

The token is stored in the Home Assistant config entry. It is not placed in YAML, entity attributes, or browser dashboard code.

## Supported capabilities

- Authenticated setup and reconfiguration
- Summary, target-status, and dynamic watch entities
- Refresh and enable/disable watch actions
- Shopping List support when that Home Assistant capability is available

The integration only calls the configured Price Watch API. It does not scrape retailer sites, access the service database, or expose the service token to a dashboard card.

## Support

Please use the [issue tracker](https://github.com/carl09/price-watch-integration/issues) for bugs or feature requests. Do not include API tokens, private service URLs, product links, or watch data in issues.

## Licence

MIT. See [LICENSE](LICENSE).
