# Home Assistant

numbers-go-up publishes every counter to Home Assistant through **MQTT
discovery**. Add one block to `config.yaml` and each series appears as a
sensor, grouped under a single device, with the right `state_class` for
long-term statistics. There's no YAML to write on the Home Assistant side.

## What you need

An MQTT broker that both numbers-go-up and Home Assistant can reach, with
Home Assistant's **MQTT integration** set up against it. On Home Assistant
OS or Supervised, the Mosquitto broker add-on is the easy path; any broker
works.

## 1. Point numbers-go-up at the broker

Add an `mqtt` block to `config.yaml`:

```yaml
mqtt:
  host: "192.168.1.20"            # your broker
  # port: 1883
  # username: "ngu"
  # tls: false
```

Put the broker password in the **`NGU_MQTT_PASSWORD`** environment variable
in `docker-compose.yml`, **never in `config.yaml`**:

```yaml
environment:
  - NGU_MQTT_PASSWORD=your-broker-password
```

Then `docker compose restart`.

| Key | Default | Meaning |
|---|---|---|
| `host` | none (required) | Broker hostname or IP |
| `port` | `1883` | Broker port. Usually `8883` with TLS. |
| `username` | none | Broker username, if it needs one |
| `tls` | `false` | Connect with TLS, verified against the system's CA certificates |
| `discovery_prefix` | `homeassistant` | Must match Home Assistant's MQTT discovery prefix. Only change it if you changed it there. |
| `topic_prefix` | `numbers-go-up` | Prefix for the state, attribute and status topics |
| `include` | `[]` | Glob patterns on metric keys. If set, only matching series are published. |
| `exclude` | `[]` | Glob patterns on metric keys. Matching series are never published; applied after `include`. |

**No `mqtt` block means no MQTT at all**: zero connections, exactly like a
plugin that's never configured. An unreachable broker never stops the
service or its polling. The service reconnects on its own, and republishes
everything from the database when it does.

## 2. Entities appear on their own

Within a poll interval, open **Settings → Devices & services → MQTT** in
Home Assistant. A device called **numbers-go-up** (model "stats poller")
holds one sensor per series.

- **Entity IDs are permanent.** Each is derived once from the metric key:
  `makerworld.profile.design_downloads` becomes
  `sensor.ngu_makerworld_profile_design_downloads`. It never changes, even
  if the series' label does, so automations and dashboards keep working.
- **Names, units and icons** come from the plugin. Extra details, such as
  the repo or model URL, are exposed as entity attributes.
- **The "Visit" link** on the device points at `server.external_url` if you
  set it (see [Configuration](../getting-started/configuration.md#server)).

## 3. Long-term statistics

Every entity gets the `state_class` that matches its metric's kind, so Home
Assistant keeps **long-term statistics** for it automatically:

| Metric kind | `state_class` | Home Assistant treats it as |
|---|---|---|
| `cumulative` (downloads, views) | `total_increasing` | A counter. The statistics show how much it grew per hour, day or month. A drop is read as a counter reset. |
| `gauge` (followers, stars) | `measurement` | A level. The statistics keep the min, max and mean per period. |

That's what makes a statistics graph or card work over months of history,
long after Home Assistant's own recorder has purged the raw states.

!!! info "Screenshot coming"
    A long-term statistics graph of a real counter goes here.

## Availability: dead sources go unavailable

A stale number that looks live is worse than no number, so:

- **State is republished after every successful poll**, even when the value
  hasn't changed.
- Every entity has **`expire_after` set to 3× its plugin's poll interval**:
  90 minutes at the 30-minute default. If a source stops polling
  successfully, its entities go **unavailable** in Home Assistant at the
  same moment the dashboard marks them stale.
- When the service stops, its MQTT last-will marks **every** entity
  unavailable at once.

## Choosing what gets published

By default every active series is published. `include` and `exclude` are
lists of shell-style glob patterns matched against the metric key, which
already encodes plugin and item:

```yaml
mqtt:
  host: "192.168.1.20"
  include: ["github.*", "youtube.channel.*"]   # only these...
  exclude: ["*.open_issues"]                   # ...minus these
```

This filters **what gets published from now on**. An entity Home Assistant
already knows about isn't removed: it stops getting updates and goes
unavailable after its `expire_after`. Delete it in Home Assistant if you
want it gone.

## Removing entities

- **Disable a plugin and restart:** its series are retired, and their Home
  Assistant entities are removed automatically. History is kept, and
  re-enabling the plugin brings them back.
- **Remove everything:** disable every plugin and restart, then delete the
  now-empty numbers-go-up device under Settings → Devices & services → MQTT.
- Deleting the `mqtt` block **doesn't** remove entities. It only stops
  publishing, so existing entities linger until you delete them in Home
  Assistant.

## Milestone alerts

Get a phone notification when a counter crosses a milestone ("Design
Downloads passed 500") instead of watching a dashboard. Milestones don't
need MQTT: they call a Home Assistant webhook directly.

**1. In Home Assistant**, create an automation with a **Webhook** trigger,
and a notify action that reads the payload's `message`:

```yaml
triggers:
  - trigger: webhook
    webhook_id: numbers-go-up-milestones
    allowed_methods: [POST]
    local_only: true
actions:
  - action: notify.mobile_app_your_phone
    data:
      title: numbers-go-up
      message: "{{ trigger.json.message }}"
```

The webhook URL is `http://<home-assistant>:8123/api/webhook/<webhook_id>`.
Keep `local_only: true` unless numbers-go-up reaches Home Assistant from
outside your network.

**2. In numbers-go-up**, put that URL in the
**`NGU_MILESTONE_WEBHOOK_URL`** environment variable. It's a credential, so
it never goes in `config.yaml`. Then add rules:

```yaml
milestones:
  webhook_url_env: NGU_MILESTONE_WEBHOOK_URL
  rules:
    - metric: github.repo.12345.stars
      every: 100                        # 100, 200, 300, ...
    - metric: youtube.channel.main.subscribers
      at: [1000, 2500, 5000, 10000]     # only these
    - metric: makerworld.model.{id}.downloads
      every: 500                        # every model, including future ones
```

A rule's `metric` is an exact metric key, or one of a plugin's patterns (as
shown on each [plugin page](../plugins/index.md)) to cover every matching
series. `every` and `at` can be combined.

**Each threshold fires once, ever:**

- **Never on startup**, and never for a new series' first value.
- **No backlog.** Adding a rule to a series that's already past some
  thresholds quietly starts from the current value, rather than sending a
  burst of "missed" alerts.
- **No repeats.** A value that dips below a threshold and climbs back past
  it doesn't alert twice.
- **One alert per jump.** A jump past several thresholds at once (480 →
  1020 with `every: 500`) sends one alert, for the highest (1000).

**Delivery is at-least-once.** A failed call, say while Home Assistant is
restarting, is retried after the plugin's next successful poll, and survives
a restart of numbers-go-up. After **24 hours** of failures it's dropped and
logged once, so a webhook that comes back days later isn't flooded with a
backlog. `GET /api/integrations` shows pending deliveries and the last
error. The webhook URL itself never appears there, or in the logs.

## Fallback: REST sensors, without MQTT

!!! warning "This is the fallback, not the recommended setup"
    Use it only if you can't run an MQTT broker. You write and maintain
    YAML for every sensor, there's no device or automatic entity creation,
    and availability isn't handled for you.

Home Assistant's
[RESTful sensor](https://www.home-assistant.io/integrations/sensor.rest/)
can read the latest values from `GET /api/stats/latest`. That endpoint
returns every active series, keyed by metric key. In
`configuration.yaml`:

```yaml
rest:
  - resource: http://192.168.1.50:8080/api/stats/latest
    scan_interval: 900
    sensor:
      - name: Repo stars
        unique_id: ngu_rest_github_repo_12345_stars
        value_template: "{{ value_json.metrics['github.repo.12345.stars'].value }}"
        state_class: measurement
      - name: Channel views
        unique_id: ngu_rest_youtube_channel_main_views
        value_template: "{{ value_json.metrics['youtube.channel.main.views'].value }}"
        state_class: total_increasing
```

Pick `state_class` from the metric's kind (see the table above). Each entry
in `metrics` also has a `stale` flag you can use in an availability
template.
