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
| MakerWorld | First plugin — in progress |
| GitHub | Planned |
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
at.

## License

MIT — see [LICENSE](LICENSE).
