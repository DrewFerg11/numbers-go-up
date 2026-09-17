# numbers-go-up

Self-hosted, plugin-based tracker for the counters you care about — with
history, rate-of-change, a REST API, and Home Assistant integration.

> **Status: early.** Phase 0 (scaffold + container) is landing. There's no
> plugin, storage, or dashboard yet — just a service that builds, runs, and
> answers `/health`. This README will grow into real setup instructions as the
> phases land.

## The idea

Counters you care about live scattered across a dozen sites, each of which
shows you a number today and no memory of what it was last month. This runs on
your own hardware, checks those numbers on a schedule, and keeps the history —
so you can ask what changed, how fast, and since when.

- **One container.** FastAPI + APScheduler + SQLite. No time-series database,
  no Grafana, no message broker to babysit.
- **Plugins are the extension point.** A source is one Python file. Drop it in
  a folder and it gets scheduled.
- **Small on disk.** The storage layer records a sample only when a value
  actually changes, with a daily heartbeat so gaps stay meaningful. Measured at
  roughly 0.4 MB per year for twenty series.
- **Your data stays yours.** It's a file on your disk. Back it up by copying it.
- **Home Assistant native.** MQTT discovery creates the entities for you, with
  the right `state_class`, so you get real long-term statistics without writing
  YAML.

## Planned sources

| Source | Status |
|---|---|
| MakerWorld | Shipped |
| GitHub | Shipped |
| YouTube | Planned |
| TikTok | Planned |
| Anything else | Write a plugin — that's the point |

**Every plugin is opt-in and inert by default.** A fresh install makes zero
outbound requests to any platform until you configure one. There are no default
user IDs or handles baked into the image.

## Running it

Everything persistent lives outside the image — the container itself is
disposable.

```sh
mkdir -p data config user-plugins
chown -R 1000:1000 data config user-plugins   # match the container's non-root user
docker compose up -d
curl localhost:8080/health
```

`/health` only says the process is up. To be alerted when a source stops
polling, point an uptime monitor (e.g. Uptime Kuma, HTTP type) at
`/health/plugins`. It returns 503 when an enabled plugin is blocked (HTTP 403)
or has failed 3 polls in a row. Tune that with `?failures=N`.

Logs go to the container's stderr. A failing plugin logs one warning when it
starts failing and one line when it recovers, not one per poll. Set
`NGU_LOG_LEVEL=DEBUG` to see every repeat.

> **Heads-up:** if you skip the `chown` step, Docker creates `./data`,
> `./config`, and `./user-plugins` as root-owned, and the container (running as
> UID 1000) can't write to them — you'll hit permission errors once
> storage/config land.

- `./data` — the SQLite DB and migration backups. **Must be local disk, never
  an NFS/SMB share** — SQLite's WAL locking is unreliable over network
  filesystems.
- `./config` — drop a `config.yaml` here (see
  [`config.yaml.example`](config.yaml.example)). If it's missing, the service
  writes a commented example next to where it looked and starts with defaults
  and zero plugins enabled.
- `./user-plugins` — optional plugins of your own. Named `user-plugins` on the
  host (not `plugins`, which is the repo's own built-in-plugin source
  directory) so a bind mount from a clone doesn't shadow the built-ins.

`docker rm` the container any time — none of the above lives inside it.

### Timezone

`TZ` is **not set by default** — the container falls back to UTC. Set it to
your own zone in `docker-compose.yml` (e.g. `TZ=America/New_York`), because it
affects timestamp correctness in the SQLite history and the daily heartbeat
boundary.

## Roadmap

MVP is a service that builds and runs in Docker, collects from one real source,
and exposes a REST API to inspect what it collected. After that: the remaining
plugins, a small dashboard, Home Assistant, and hardening.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) first — it covers the plugin contract,
the rules a plugin has to meet, and the licensing constraint on contributed
code.

Found a source that stopped working? That's expected and routine — most of
these are unofficial endpoints that change without notice. Open an issue with
the "Broken data source" template.

## Security

No authentication, by design — this is built to run on a trusted LAN. If you
want access away from home, use a VPN rather than exposing it. See
[SECURITY.md](SECURITY.md) before reporting anything.

## A note on what this is

This is a general-purpose counter tracker with a plugin folder. It is not
affiliated with, endorsed by, or connected to any of the platforms its plugins
can read. Those plugins read **public** data about **your own** accounts, they
never require your credentials, and they're written to be polite about it:
honest User-Agent, conservative intervals, one request per poll, and real
backoff when asked to slow down.

You are responsible for complying with the terms of any service you point this
at. See [NOTICE.md](NOTICE.md) for the full statement.

**Documented exception:** MakerWorld can optionally track your own published
models' counters, next to its eight profile counters
(`plugins.makerworld.models` in [`config.yaml.example`](config.yaml.example)).
It's opt-in and adds one paged listing request per poll — still public data
about your own account, still no auth — on top of the one profile request.

**Documented exception:** GitHub makes one request per configured repo plus
paged release listings, against the official, documented API — the only
source in this project with that status. Every other source here is
unofficial.

### GitHub

Tracks stars, forks, watchers, and open issues (which include open pull
requests) per repo, plus optional release-asset download totals, via the
official REST API. Configure `plugins.github.repos` with one or more
`"owner/name"` strings (see
[`config.yaml.example`](config.yaml.example)).

- **Token (optional):** set the `NGU_GITHUB_TOKEN` environment variable,
  never in `config.yaml`, to raise the unauthenticated rate limit (60
  requests/hour) to 5,000/hour. It's sent only to `api.github.com`.
- **Renamed or transferred repos:** GitHub redirects the old `owner/name` to
  the new one and this plugin follows it. Series are keyed by the repo's
  permanent numeric ID, not by name, so history survives a rename.
- **Known limitation:** `release_downloads` sums `download_count` across
  every release. Deleting a release lowers that sum. Storage doesn't reject
  a falling cumulative series, and Home Assistant will read the drop as a
  counter reset — this is expected, not a bug.

## Home Assistant

MQTT discovery is the supported way in: add an `mqtt` block to
`config.yaml` and every active series shows up as a Home Assistant sensor
automatically, with no YAML on the HA side, and stays in HA's long-term
statistics with the right `state_class`.

**Prerequisite:** an MQTT broker HA can also reach. If you run HA OS or
Supervised, the Mosquitto add-on is the easy path; any broker works
otherwise.

```yaml
mqtt:
  host: "192.168.1.x"
  port: 1883
  username: "ngu"                 # optional
  tls: false                      # true = TLS with system CAs (usually port 8883)
  discovery_prefix: "homeassistant"
  topic_prefix: "numbers-go-up"
```

Set the broker password via the `NGU_MQTT_PASSWORD` environment variable —
never in `config.yaml`. Omit the whole `mqtt` block to keep MQTT off; that's
zero connections, exactly like a plugin that's never configured.

Every series becomes one sensor entity, all grouped under a single
**numbers-go-up** device (manufacturer "numbers-go-up", model "stats
poller") so they show up together in HA's device list instead of scattered
across "MQTT" entities. `cumulative` metrics get `state_class:
total_increasing`; `gauge` metrics get `state_class: measurement`.

- **`expire_after`** is set to 3x that plugin's poll interval — the same
  staleness rule `/api/plugins` and `/api/stats/latest` already use. If a
  plugin stops polling, its entities go "unavailable" in HA instead of
  silently freezing on the last value.
- **Entity IDs are permanent.** An entity's `object_id`/`unique_id` is
  derived once from its metric key (`makerworld.profile.design_downloads` →
  `ngu_makerworld_profile_design_downloads`) and never changes, even if the
  series' label changes later. Two different metric keys that would collide
  on the same object_id (a `.`/`_` clash) are detected: the second one is
  skipped with a logged error rather than silently overwriting the first.
- **To remove everything:** disable the plugins (or delete the `mqtt` block
  and restart), then in Home Assistant go to Settings → Devices & Services →
  MQTT and delete the "numbers-go-up" device. Deactivating one series (e.g. a
  MakerWorld model that's no longer published) removes just that entity —
  its discovery config and retained state are cleared automatically — while
  the rest of the device stays intact.

**Without MQTT:** there's currently no REST-polling fallback for Home
Assistant (a plain `rest` sensor reading `/api/stats/latest` is a possible
future addition — not implemented here); MQTT discovery is the only
supported integration path today.

## License

MIT — see [LICENSE](LICENSE).
