# Price Watch for Home Assistant

A Home Assistant custom integration for a self-hosted **Price Watch** service. It exposes the service's watch state in Home Assistant and provides actions for refreshing and managing watches.

> [!WARNING]
> This is an early release. Run a Price Watch service on your own private network before adding this integration.

## Public package, private runtime

This is a public distribution repository for the Home Assistant integration only.
It contains the HACS package and safe installation documentation. It does **not**
contain the Price Watch service source, retailer adapters or fixtures, test data,
private deployment configuration, your watch data, or API tokens.

The integration connects from Home Assistant to a Price Watch API URL that you
configure. Keep that API on your trusted network; installing this public HACS
package does not publish your service or watchlist. If you use the Home
Assistant App distribution, install it separately from
[`carl09/price-watch-addons`](https://github.com/carl09/price-watch-addons).

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
- Latest immutable target-event entity for notification automations
- Refresh and enable/disable watch actions
- Shopping List support when that Home Assistant capability is available

The integration only calls the configured Price Watch API. It does not scrape retailer sites, access the service database, or expose the service token to a dashboard card.

## Target-event notifications

`sensor.price_watch_latest_target_event` exposes the immutable ID of the most
recent service-produced `target_reached` event. It is `unknown` when no such
event has been received by Home Assistant, `none` when the service has no such
event, and unavailable while Home Assistant cannot refresh Price Watch.

Its safe attributes are `watch_id`, `occurred_at`, `deduplication_key`,
`event_type`, and (when supplied by the service) `target_price_cents`. Use a
state-change automation against the event ID to present notifications; do not
recalculate price or target conditions in YAML. The entity never exposes the
API token, raw response body, service configuration, or retailer data.

## Schedule checks in Home Assistant

`price_watch.check_all` runs a check when you invoke it. To run it once daily,
create a Home Assistant automation such as:

```yaml
id: price_watch_daily_check
alias: Price Watch — daily check
description: Runs all Price Watch checks every day at 8:00am.
mode: single
triggers:
  - trigger: time
    at: "08:00:00"
conditions: []
actions:
  - action: price_watch.check_all
```

This automation contains no API token; the integration keeps the configured
token in its config entry.

## Support

Please use the [issue tracker](https://github.com/carl09/price-watch-integration/issues) for bugs or feature requests. Do not include API tokens, private service URLs, product links, or watch data in issues.

## Licence

MIT. See [LICENSE](LICENSE).
