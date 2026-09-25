<p align="center">
  <img src="docs/assets/icon.png" width="160" height="160" alt="numbers-go-up icon" />
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
  <a href="./LICENSE"><img alt="License: MIT" src="https://img.shields.io/github/license/DrewFerg11/numbers-go-up" /></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-blue" />
  <a href="https://hub.docker.com/r/drewferg11/numbers-go-up"><img alt="Docker pulls" src="https://img.shields.io/docker/pulls/drewferg11/numbers-go-up" /></a>
  <a href="https://hub.docker.com/r/drewferg11/numbers-go-up"><img alt="Docker image size" src="https://img.shields.io/docker/image-size/drewferg11/numbers-go-up?sort=semver" /></a>
  <img alt="Multi-arch: amd64, arm64, armv7" src="https://img.shields.io/badge/arch-amd64%20%7C%20arm64%20%7C%20armv7-informational" />
</p>

---

Self-hosted, plugin-based tracker for the counters you care about — with
history, rate-of-change, a REST API, and Home Assistant integration.

> **Status: usable.** Everything through hardening has landed — storage,
> scheduler, the MakerWorld/GitHub/YouTube plugins, the REST API, the
> dashboard, Home Assistant via MQTT discovery, milestone alerts, scheduled
> maintenance, and multi-arch release images. No `v1.0.0` tag yet.

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

## Sources

| Source | Status |
|---|---|
| [MakerWorld](https://drewferg11.github.io/numbers-go-up/plugins/makerworld/) | Shipped |
| [GitHub](https://drewferg11.github.io/numbers-go-up/plugins/github/) | Shipped |
| [YouTube](https://drewferg11.github.io/numbers-go-up/plugins/youtube/) | Shipped |
| TikTok | Shipped |
| Anything else | Write a plugin — that's the point |

**Every plugin is opt-in and inert by default.** A fresh install makes zero
outbound requests to any platform until you configure one. There are no default
user IDs or handles baked into the image.

## Running it

Everything persistent lives outside the image — the container itself is
disposable.

```sh
git clone https://github.com/DrewFerg11/numbers-go-up.git && cd numbers-go-up
mkdir -p data config user-plugins
docker compose up -d
curl localhost:8080/health
```

The full walkthrough, covering the bind mounts, file ownership, the
local-disk requirement for `./data`, and what happens on first run, is the
[Install guide](https://drewferg11.github.io/numbers-go-up/getting-started/install/).
Every `config.yaml` key and environment variable is in the
[Configuration reference](https://drewferg11.github.io/numbers-go-up/getting-started/configuration/).

**`./data` must be local disk, never an NFS/SMB share.** SQLite's locking is
unreliable over network filesystems, and the container refuses to start on
one (see the [Install guide](https://drewferg11.github.io/numbers-go-up/getting-started/install/)).

Point an uptime monitor at `/health/plugins`, not `/health`: it returns 503
when a source stops polling, while `/health` only says the process is up.

Backups, the restore drill, updating, PUID/PGID, container hardening, log
rotation and where the images are published are on the
[Operations page](https://drewferg11.github.io/numbers-go-up/operations/). Symptoms and fixes are under
[Troubleshooting](https://drewferg11.github.io/numbers-go-up/troubleshooting/).

## Dashboard

`/` is a lightweight overview page — a stock-watchlist view of your own
counters: an index strip of pinned metrics, a watchlist grouped by plugin
with sparklines, and a status line showing each plugin's polling health.
It's server-rendered (no build step, no CDN — everything, including fonts,
is served from the container) and refreshes itself every 60 seconds.

![Dashboard overview](docs/assets/dashboard.png)

Pin up to 6 metrics to the index strip with `dashboard.pinned` in
`config.yaml` (see [`config.yaml.example`](config.yaml.example)); leave it
empty and the first 4 cumulative metrics are used instead.

## API

The dashboard's topbar links to `/docs`, a self-contained Swagger UI (no
CDN — it renders with the container's network fully blocked) for the REST
API; the raw schema is at `/openapi.json`.

## Roadmap

The MVP — a service that builds and runs in Docker, collects from one real
source, and exposes a REST API to inspect what it collected — shipped, and so
did everything planned after it: the GitHub and YouTube plugins, the
dashboard, Home Assistant via MQTT discovery, milestone alerts, and the
hardening pass (scheduled maintenance, backups, PUID/PGID, multi-arch
releases).

What's left is tracked in
[issues](https://github.com/DrewFerg11/numbers-go-up/issues): the TikTok
plugin, and a `v1.0.0` tag once it has run long enough to earn one.

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

**YouTube's unofficial path was evaluated and rejected (#62):** the
originally-planned `unofficial-livecounts-api` library doesn't just need a
browser-style User-Agent — every request it makes is signed with three
custom headers derived from the current timestamp via a private hashing
scheme, which this project would have to reimplement to pass its bot check.
That's a materially different, higher-risk exercise than TikTok's one fixed
User-Agent exception, so it was dropped before any code shipped. YouTube
tracks official-API numbers only, accepting the rounding described below.

### GitHub

Stars, forks, watchers, open issues and release downloads per repo, via the
official REST API. Setup, metrics, the optional `NGU_GITHUB_TOKEN`, and
known limitations are on the
[GitHub plugin page](https://drewferg11.github.io/numbers-go-up/plugins/github/).

### YouTube

Subscribers, views and video count per channel, plus views and likes per
video, via the official Data API v3 (needs `NGU_YOUTUBE_API_KEY`). Finding
your channel ID, quota, and the subscriber-rounding caveat are on the
[YouTube plugin page](https://drewferg11.github.io/numbers-go-up/plugins/youtube/).

### Abacus

Tracks generic counters (any Abacus `namespace`/key pair) via the public,
unauthenticated [Abacus API](https://abacus.jasoncameron.dev). Not tied to
any specific site — it's config-driven, so it can track any static page's
Abacus-backed counter, such as the "boards flashed" badge on the
Split-Flap-Display web flasher. Configure `plugins.abacus.counters` with
one or more `{key, namespace, name, label, unit}` entries (see
[`config.yaml.example`](config.yaml.example)).

- **This is a feel-good badge, not an audit trail.** The count is
  client-side and unauthenticated — anyone who knows (or guesses) the
  namespace and key can hit Abacus's `/hit/` endpoint directly and inflate
  it. Nothing about this plugin, or the source it reads, proves the number
  reflects real user activity.
- **Counters expire after ~6 months of no *increments*.** Reads alone don't
  refresh the TTL (verified live, see #96) — only an actual `/hit/` does.
  A counter that goes quiet for that long is garbage-collected upstream and
  a later read comes back 404/`exists: false`, which this plugin treats as
  a **failed poll**, never a silent drop to 0 in a `cumulative` series.
- **`key` is a permanent slug**, separate from the real Abacus `namespace`/
  `name` (which may contain characters — dots, mixed case — outside the
  `[a-z0-9_-]+` metric-key charset). Same convention as the YouTube
  plugin's channel `key`. Pick it once; it can't be renamed without losing
  history.
- **`unit` is config-accepted but not the entity's actual unit.** Every
  configured counter shares one `METRICS` pattern
  (`abacus.counter.{key}.value`), and this project's plugin contract fixes
  `kind`/`unit`/`icon` per pattern at declare time — they can never vary
  per poll (only `label`/`attrs` can). So `unit` from config can't become
  each series' real `unit_of_measurement`; it's carried through into that
  series' `attrs.unit` instead, so it's still visible via the API/
  dashboard, rather than silently discarded.
- **Known limitation — the "value went backwards" check is best-effort
  and in-process only.** `collect()` has no access to storage (no plugin
  does), and storage itself doesn't reject a falling cumulative series
  (same known limitation the GitHub plugin documents above). This plugin
  keeps its own last-seen value per counter in memory for the life of the
  process and fails the poll if a new read comes in lower — catching a
  `get`/`hit` typo or an expired-and-reset counter *within* a run of the
  service. It is **not persisted**: a restart forgets every counter's
  last-seen value, so a regression that happens to land right after a
  restart is not caught. Treat it as a helpful guard, not a guarantee.

### TikTok

Tracks followers, following, and video count for your own handle(s), plus
views and likes for individual videos, by reading the same
`__UNIVERSAL_DATA_FOR_REHYDRATION__` JSON blob your browser does when it
loads a profile or video page — no library, no login, no cookies.

- **Finding your handle:** it's the part after the `@` in your profile URL
  (`tiktok.com/@yourhandle`) — a leading `@` in config is accepted and
  stripped, but not required. Configure `plugins.tiktok.handles` (see
  [`config.yaml.example`](config.yaml.example)); the `key` is a permanent
  slug, same convention as YouTube's `channels[].key`, since a handle may
  contain `.` or uppercase letters that aren't Home-Assistant-entity-id-safe.
- **Per-video views and likes:** configure `plugins.tiktok.videos` with the
  numeric `id` from a video's URL (`tiktok.com/@handle/video/{id}`) — no
  handle needed alongside it, and independent of `handles` entirely (track
  videos with no account configured, or vice versa, or both). There's no way
  to discover a handle's videos here: doing that means TikTok's own
  item-list API, which is gated behind request signing that a plain HTTP
  client can't replicate (confirmed live during #113 — an unsigned call
  returns `HTTP 200` with an empty body). So video ids are always supplied
  directly, same as handles.
- **Documented exception:** this is the one plugin here that doesn't send the
  project's honest User-Agent. The profile/video pages only serve the data
  blob to a browser-shaped request, so this plugin sends one fixed,
  documented `User-Agent` + `Accept-Language` — no rotation, no cookies, no
  session reuse, no proxies. Still reads only public data about your own
  account(s) and videos.
- **Account-level likes are not tracked.** TikTok's `heartCount` (total
  likes received) overflows a signed int32 for large accounts (observed as
  a negative number in the wild) — it will never appear in this plugin's
  metrics. A single video's like count doesn't share this problem and is
  tracked normally.
- **Display rounding above ~1M followers/likes**, same caveat as YouTube's
  official API: the page itself rounds large numbers, so day-to-day change
  on a big account may show as a flat line with an occasional step.
- A handle/video id that resolves to something else (renamed, redirected, or
  TikTok serving a mismatched page) fails the poll instead of writing that
  other account's or video's numbers into your series. Missing or
  non-numeric stats also fail the poll rather than writing a bogus value,
  and a deleted/private/unavailable video fails distinctly.
- **Zero followers fails the poll by default** — a real, tracked account is
  almost never at exactly zero, so this is treated as a broken scrape. If
  you're genuinely tracking a fresh account that has 0 followers, set
  `allow_zero_followers: true` on that handle's entry to opt out of the
  guard. (Zero views/likes on a video is not guarded the same way — a
  freshly posted video legitimately starts at 0.)

## Home Assistant

MQTT discovery is the supported way in: add an `mqtt` block to
`config.yaml`, put the broker password in `NGU_MQTT_PASSWORD`, and every
active series shows up as a Home Assistant sensor, grouped under one device,
with the right `state_class` for long-term statistics. No YAML on the Home
Assistant side. The walkthrough, availability behaviour, filtering and
removal are on the [Home Assistant page](https://drewferg11.github.io/numbers-go-up/home-assistant/).

## Milestone alerts

Get a phone notification when a counter crosses a milestone ("Design
Downloads passed 500") through a Home Assistant webhook, whether or not MQTT
is configured. Each threshold fires once, ever. Setup and the exact
semantics are under
[Home Assistant → Milestone alerts](https://drewferg11.github.io/numbers-go-up/home-assistant/#milestone-alerts).

## License

MIT — see [LICENSE](LICENSE).
