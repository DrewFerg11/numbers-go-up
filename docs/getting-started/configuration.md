# Configuration

Settings come from two places:

- **`config.yaml`**, in the folder mounted at `/config`, holds everything
  that isn't a secret. The file is optional; every key has a default.
- **Environment variables** hold file paths and **every credential**. Set
  them under `environment:` in `docker-compose.yml`.

Edit `config.yaml` on the host and `docker compose restart` to apply it; no
rebuild is needed. All intervals are in seconds.

!!! warning "Secrets go in environment variables, never in `config.yaml`"
    The MQTT password, the milestone webhook URL, and the optional API
    tokens are read **only** from environment variables. `config.yaml` is
    meant to be safe to copy, share and back up. The full list is
    [below](#environment-variables).

## Validation

An unknown key in `poll`, `storage`, `server` or `dashboard`, or an unknown
top-level section, **fails startup** with the offending key named (and a
"did you mean" suggestion for a misspelled section). So does a value of the
wrong type, such as a quoted number. A typo like `mqqt:` stops the
container instead of silently doing nothing.

`NGU_CONFIG_STRICT=0` downgrades that failure to a warning and starts
anyway. It's an escape hatch for getting a broken config booting while you
fix it, not a setting to leave on, because it accepts exactly the typos the
check exists to catch.

## `poll`

Scheduling defaults for every plugin.

| Key | Default | Meaning |
|---|---|---|
| `default_interval` | `1800` | Poll interval for a plugin that sets neither its own `poll_interval` nor a built-in default. Positive integer. |
| `jitter_fraction` | `0.2` | Each poll is delayed by a random 0 to `jitter_fraction × interval` seconds, so polls don't line up. Never early. A number in `[0, 1)`. |

## `storage`

| Key | Default | Meaning |
|---|---|---|
| `heartbeat_seconds` | `86400` | A sample is stored only when a value changes, plus one heartbeat sample per this interval even if it hasn't, so a flat line is distinguishable from missing data. Positive integer. |
| `plugin_runs_retention_days` | `30` | How long poll-run records (successes, errors) are kept. Each plugin's newest finished run is always kept. `0` or more. |
| `backups.keep_daily` | `7` | Number of daily database backups to keep in `./data/backups/`. `0` disables them. |

The database always lives at `stats.db` in the data folder.

## `server`

| Key | Default | Meaning |
|---|---|---|
| `external_url` | none | The URL you reach the service on, e.g. `http://192.168.1.50:8080`. Optional. Used as the Home Assistant device's "Visit" link. Must be an `http(s)` URL with a host. |

The container always listens on `0.0.0.0:8080`. To change the outside port,
change the port mapping in compose (`"9000:8080"`), not this file.
`server.host` and `server.port` are deprecated; they're accepted with a
warning and have no effect.

## `dashboard`

| Key | Default | Meaning |
|---|---|---|
| `pinned` | `[]` | Up to 6 metric keys shown in the index strip at the top of the dashboard. Empty uses the first 4 cumulative metrics. |

## `plugins`

One block per plugin, keyed by plugin name. **Every plugin is off until
its block sets `enabled: true`**, and a plugin that isn't enabled makes no
requests at all.

Two keys are common to every plugin:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turns the plugin on. |
| `poll_interval` | the plugin's own default (`1800` for every built-in) | Seconds between polls. Values below **300** (5 minutes) are raised to 300 with a warning. |

Everything else is plugin-specific, such as which account, repos, channels
or counters to read. See each plugin's page under [Plugins](../plugins/index.md),
and the fully commented
[`config.yaml.example`](https://github.com/DrewFerg11/numbers-go-up/blob/main/config.yaml.example).

```yaml
plugins:
  github:
    enabled: true
    repos: ["owner/name"]
```

## `mqtt` and `milestones`

Both are optional, and both are off when their block is absent: no block
means no MQTT connection and no webhook calls. Their keys are covered on
the [Home Assistant](../home-assistant/index.md) page, alongside the
Home Assistant setup they feed. Until that page is written, the fully
commented
[`config.yaml.example`](https://github.com/DrewFerg11/numbers-go-up/blob/main/config.yaml.example)
shows every key.

## Environment variables

**Paths.** `docker-compose.yml` already sets these to the bind mounts, so
leave them alone unless you change the mounts.

| Variable | Default | Meaning |
|---|---|---|
| `NGU_DATA_DIR` | `/data` | Folder holding the database and backups |
| `NGU_CONFIG_FILE` | `/config/config.yaml` | Path to the config file |
| `NGU_PLUGIN_DIR` | `/plugins` | Folder scanned for user plugins |

**Secrets.** Set only the ones you use.

| Variable | Used by |
|---|---|
| `NGU_MQTT_PASSWORD` | MQTT broker password |
| `NGU_MILESTONE_WEBHOOK_URL` | Milestone webhook URL. The variable's name can be changed with `milestones.webhook_url_env`. |
| `NGU_GITHUB_TOKEN` | Optional. Raises the GitHub plugin's rate limit. |
| `NGU_YOUTUBE_API_KEY` | Required by the YouTube plugin |

**Behaviour.**

| Variable | Default | Meaning |
|---|---|---|
| `PUID` / `PGID` | `1000` | Host user and group that own `./data` and `./config`. See [Install](install.md#3-file-ownership). |
| `TZ` | unset (UTC) | Container timezone |
| `NGU_LOG_LEVEL` | `INFO` | Log verbosity. `DEBUG` shows every repeated plugin failure, not just the first of a streak. |
| `NGU_CONFIG_STRICT` | `1` | `0` downgrades config validation failures to a warning. See [Validation](#validation). |
| `NGU_ALLOW_NETWORK_FS` | unset | `1` lets the database run on a network filesystem despite the risk. See [Install](install.md). |
