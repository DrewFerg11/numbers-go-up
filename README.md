<p align="center">
  <img src="https://raw.githubusercontent.com/DrewFerg11/numbers-go-up/main/docs/assets/icon.png" width="160" height="160" alt="numbers-go-up icon" />
</p>

<h1 align="center">numbers-go-up</h1>

<p align="center">
  <a href="https://github.com/DrewFerg11/numbers-go-up/releases/latest"><img alt="Latest stable release" src="https://img.shields.io/github/v/release/DrewFerg11/numbers-go-up?label=stable" /></a>
  <a href="https://github.com/DrewFerg11/numbers-go-up/releases"><img alt="Latest RC" src="https://img.shields.io/github/v/release/DrewFerg11/numbers-go-up?include_prereleases&sort=semver&filter=*-rc.*&label=rc" /></a>
  <a href="https://github.com/DrewFerg11/numbers-go-up/actions/workflows/ci-test.yml"><img alt="CI / Test" src="https://img.shields.io/github/actions/workflow/status/DrewFerg11/numbers-go-up/ci-test.yml?branch=main&label=test" /></a>
  <a href="https://github.com/DrewFerg11/numbers-go-up/actions/workflows/ci-build.yml"><img alt="CI / Build" src="https://img.shields.io/github/actions/workflow/status/DrewFerg11/numbers-go-up/ci-build.yml?branch=main&label=build" /></a>
  <a href="https://github.com/DrewFerg11/numbers-go-up/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://img.shields.io/github/actions/workflow/status/DrewFerg11/numbers-go-up/codeql.yml?branch=main&label=codeql" /></a>
  <a href="https://github.com/DrewFerg11/numbers-go-up/actions/workflows/sources-canary.yml"><img alt="Sources canary" src="https://img.shields.io/github/actions/workflow/status/DrewFerg11/numbers-go-up/sources-canary.yml?label=sources%20canary" /></a>
</p>

<p align="center">
  <a href="https://github.com/DrewFerg11/numbers-go-up/commits/main"><img alt="Last commit" src="https://img.shields.io/github/last-commit/DrewFerg11/numbers-go-up" /></a>
  <a href="https://github.com/DrewFerg11/numbers-go-up/blob/main/LICENSE"><img alt="License: MIT" src="https://img.shields.io/github/license/DrewFerg11/numbers-go-up" /></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-blue" />
  <a href="https://hub.docker.com/r/drewferg11/numbers-go-up"><img alt="Docker pulls" src="https://img.shields.io/docker/pulls/drewferg11/numbers-go-up" /></a>
  <a href="https://hub.docker.com/r/drewferg11/numbers-go-up"><img alt="Docker image size" src="https://img.shields.io/docker/image-size/drewferg11/numbers-go-up?sort=semver" /></a>
  <img alt="Multi-arch: amd64, arm64, armv7" src="https://img.shields.io/badge/arch-amd64%20%7C%20arm64%20%7C%20armv7-informational" />
</p>

---

Self-hosted, plugin-based tracker for the counters you care about, with
history, rate-of-change, a REST API, and Home Assistant integration.

Counters live scattered across a dozen sites, each of which shows you a number
today and no memory of what it was last month. numbers-go-up runs on your own
hardware, checks those numbers on a schedule, and keeps the history, so you can
ask what changed, how fast, and since when.

> **Status: usable.** Storage, scheduler, all five plugins, the REST API, the
> dashboard, Home Assistant via MQTT discovery, milestone alerts, scheduled
> maintenance and multi-arch release images have landed. No `v1.0.0` tag yet.

[Try the demo](https://drewferg11.github.io/numbers-go-up/demo/) · [Read the documentation](https://drewferg11.github.io/numbers-go-up/) · [Check source status](https://drewferg11.github.io/numbers-go-up/status/)

[![The numbers-go-up dashboard: pinned metrics, a watchlist with sparklines, and plugin health](https://raw.githubusercontent.com/DrewFerg11/numbers-go-up/main/docs/assets/dashboard.png)](https://drewferg11.github.io/numbers-go-up/demo/)

- **One container.** FastAPI, APScheduler and SQLite. No time-series database,
  Grafana or message broker to babysit.
- **Plugins are the extension point.** A source is one Python file; drop it in
  `./user-plugins` and it gets scheduled.
- **Small, meaningful history.** A sample is stored only when a value changes,
  with a daily heartbeat so gaps stay meaningful.
- **Your data stays yours.** It is a SQLite file on your disk; back it up by
  copying it.
- **Home Assistant native.** MQTT discovery creates the sensors for you, with
  the right `state_class` for long-term statistics, and milestone alerts can
  notify your phone.

## Sources

| Source | What it tracks | Data |
|---|---|---|
| [MakerWorld](https://drewferg11.github.io/numbers-go-up/plugins/makerworld/) | Profile counters, optionally per-model | Unofficial |
| [GitHub](https://drewferg11.github.io/numbers-go-up/plugins/github/) | Repository and release counters | Official API |
| [YouTube](https://drewferg11.github.io/numbers-go-up/plugins/youtube/) | Channel and video counters | Official API |
| [TikTok](https://drewferg11.github.io/numbers-go-up/plugins/tiktok/) | Profile and video counters | Unofficial |
| [Abacus](https://drewferg11.github.io/numbers-go-up/plugins/abacus/) | Any public Abacus namespace/key counter | Official API |
| Anything else | [Write a plugin](https://drewferg11.github.io/numbers-go-up/plugins/writing-a-plugin/) | |

**Every plugin is opt-in and inert by default.** A fresh install makes zero
outbound requests to any platform until you configure one, and no default user
IDs or handles are baked into the image.

## Quickstart

```sh
mkdir -p data config user-plugins
docker compose pull && docker compose up -d
curl localhost:8080/health
```

`./data` and `./config` are bind mounts, and `./data` must be local disk, not
NFS or SMB. The [install guide](https://drewferg11.github.io/numbers-go-up/getting-started/install/) has the full
walkthrough, including the compose file, file ownership and first run.

## Documentation

- [Install](https://drewferg11.github.io/numbers-go-up/getting-started/install/) and [configuration](https://drewferg11.github.io/numbers-go-up/getting-started/configuration/)
- [Plugins](https://drewferg11.github.io/numbers-go-up/plugins/) and the [metric catalogue](https://drewferg11.github.io/numbers-go-up/plugins/metrics/)
- [Home Assistant](https://drewferg11.github.io/numbers-go-up/home-assistant/), including milestone alerts
- [Operations](https://drewferg11.github.io/numbers-go-up/operations/) (backups, updates, hardening, logs) and [troubleshooting](https://drewferg11.github.io/numbers-go-up/troubleshooting/)
- [REST API reference](https://drewferg11.github.io/numbers-go-up/api/)
- [Writing a plugin](https://drewferg11.github.io/numbers-go-up/plugins/writing-a-plugin/)
- [Privacy](https://drewferg11.github.io/numbers-go-up/privacy/)

## Responsible use

This is a general-purpose counter tracker with a plugin folder. It is not
affiliated with, endorsed by, or connected to any of the platforms its plugins
can read. Plugins read **public** data about **your own** accounts, never
require your credentials, and poll politely: conservative intervals and real
backoff when asked to slow down. You are responsible for complying with the
terms of any service you point this at. Unofficial sources can break or be
removed without notice. See [NOTICE.md](https://github.com/DrewFerg11/numbers-go-up/blob/main/NOTICE.md) for the full statement.

## Contributing

Read [CONTRIBUTING.md](https://github.com/DrewFerg11/numbers-go-up/blob/main/CONTRIBUTING.md) first; it covers the plugin
contract, the rules a plugin has to meet, and the licensing constraint on
contributed code. A source that stopped working is routine: open an issue with
the "Broken data source" template.

## Security

No authentication, by design. This is built to run on a trusted LAN; for access
away from home, use a VPN rather than exposing it. See
[SECURITY.md](https://github.com/DrewFerg11/numbers-go-up/blob/main/SECURITY.md) before reporting anything.

## License

MIT. See [LICENSE](https://github.com/DrewFerg11/numbers-go-up/blob/main/LICENSE).
