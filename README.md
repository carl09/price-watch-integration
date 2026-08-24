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
- Summary, target-status, and dynamic watch devices/entities
- Latest immutable target-event entity for notification automations
- Refresh and enable/disable watch actions
- Shopping List support when that Home Assistant capability is available

The integration only calls the configured Price Watch API. It does not scrape retailer sites, access the service database, or expose the service token to a dashboard card.

## Watch devices and entities

Each Price Watch is represented by one Home Assistant Device, identified
permanently by its service `watch_id`. Changing a title, URL, retailer display
name, or selected variant cannot create a second device.

The existing current-price sensor remains the primary entity with its current
entity and unique IDs. The device also groups target price, target match,
current status, and last-observation timestamp entities. Selected variant,
retailer variant ID, product URL, raw error code, and related low-level facts
remain safe attributes of the primary price sensor. Enabled remains a primary
sensor attribute; use the existing `price_watch.set_enabled` action to change
it.

Removed watches become unavailable rather than being deleted and recreated.
This preserves Home Assistant history and device registry identity while
avoiding presentation of an old price as current. No manual migration is
required; devices are created from the next successful coordinator refresh.

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

## Monitor health and failure notifications

`binary_sensor.price_watch_monitor_health` is `on` only when the Price Watch
service summary reports zero stale and zero failed enabled watches. It is `off`
when either count is non-zero and unavailable when Home Assistant cannot
refresh the service. Its safe attributes are the service stale/failed counts,
enabled-watch count, and latest-check timestamp.

`sensor.price_watch_latest_failure_event` exposes the immutable ID of the most
recent service-produced `check_failed` event. It is `none` when the service has
no failure event and unavailable when the coordinator cannot refresh. Its safe
attributes are the event watch ID, occurrence time, deduplication key, type,
and a trusted failure error code when present.

Use the immutable event ID and health transition directly in a persistent
notification automation. This does not recalculate monitoring logic or include
tokens, retailer URLs, or raw error data:

```yaml
id: price_watch_monitor_notifications
alias: Price Watch monitoring notifications
mode: single
triggers:
  - trigger: state
    entity_id: sensor.price_watch_latest_failure_event
    id: failure
  - trigger: state
    entity_id: binary_sensor.price_watch_monitor_health
    from: "off"
    to: "on"
    id: recovery
conditions: []
actions:
  - choose:
      - conditions:
          - condition: trigger
            id: failure
          - condition: template
            value_template: >-
              {{ trigger.from_state is not none
                 and trigger.to_state is not none
                 and trigger.from_state.state not in ['unknown', 'unavailable']
                 and trigger.to_state.state not in ['unknown', 'unavailable', 'none'] }}
        sequence:
          - action: persistent_notification.create
            data:
              title: Price Watch monitoring failure
              message: >-
                Price Watch recorded monitoring failure event
                {{ trigger.to_state.state }}.
      - conditions:
          - condition: trigger
            id: recovery
        sequence:
          - action: persistent_notification.create
            data:
              title: Price Watch monitoring recovered
              message: Price Watch monitor health recovered.
```

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
