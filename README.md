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
| MakerWorld | Shipped |
| GitHub | Shipped |
| YouTube | Shipped |
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
docker compose pull && docker compose up -d
curl localhost:8080/health
```

The full walkthrough, covering the bind mounts, file ownership, the
local-disk requirement for `./data`, and what happens on first run, is the
[Install guide](https://drewferg11.github.io/numbers-go-up/getting-started/install/).
Every `config.yaml` key and environment variable is in the
[Configuration reference](https://drewferg11.github.io/numbers-go-up/getting-started/configuration/).

**`./data` must be local disk, never an NFS/SMB share.** SQLite's locking is
unreliable over network filesystems, and the container refuses to start on
one; see "Network filesystems" below.

`/health` only says the process is up. To be alerted when a source stops
polling, point an uptime monitor (e.g. Uptime Kuma, HTTP type) at
`/health/plugins`. It returns 503 when an enabled plugin is blocked (HTTP 403)
or has failed 3 polls in a row. Tune that with `?failures=N`.

Logs go to the container's stderr. A failing plugin logs one warning when it
starts failing and one line when it recovers, not one per poll. Set
`NGU_LOG_LEVEL=DEBUG` to see every repeat.

Docker also knows when the app is unhealthy: the image ships a `HEALTHCHECK`
against `/health`, so `docker ps`, `docker inspect`, and Portainer all show a
`healthy`/`unhealthy` status without any extra config.

### File ownership (PUID/PGID)

NAS distros rarely use UID 1000 for the first user — Synology's is usually
1026, Unraid uses `99:100`. Set `PUID`/`PGID` in `docker-compose.yml` to match
whoever should own `./data` and `./config` on the host:

```yaml
environment:
  - PUID=1026
  - PGID=100
```

On startup, if the container is running as root (the default — no `user:` in
compose), the entrypoint `chown`s `./data` and `./config` to `PUID:PGID` (only
when the top-level owner doesn't already match, so this is a no-op on every
boot after the first) and then drops privileges to that user before running
anything. `PUID=0`/`PGID=0` are refused outright — the app never runs as
root.

Already setting `user: "1000:1000"` in compose? Nothing changes — the
entrypoint detects it's already non-root and skips straight to running the
app. Existing deployments keep working unchanged.

### Hardened runtime

For a locked-down compose config, add:

```yaml
read_only: true
tmpfs: [/tmp]
security_opt: ["no-new-privileges:true"]
cap_drop: [ALL]
```

With `user: "1000:1000"` set (non-root mode), all four work as-is. In
PUID/PGID mode (started as root, the default), the entrypoint needs to
`chown` and switch users, so use this instead:

```yaml
read_only: true
tmpfs: [/tmp]
cap_drop: [ALL]
cap_add: [CHOWN, SETUID, SETGID]
# no security_opt: no-new-privileges would block setpriv from switching users.
```

Either way, nothing the app writes lives outside `/data`, `/config`, and
`/tmp`.

### Network filesystems

`./data` holds the SQLite database, and SQLite's WAL locking is unreliable
over NFS/SMB — the one deployment mistake that can silently corrupt it. At
startup the container inspects `./data`'s mount and refuses to start with a
clear error if it's `nfs`, `nfs4`, `cifs`, `smb3`, `smbfs`, `fuse.sshfs`, or
`9p`. If you understand the risk and want to proceed anyway, set
`NGU_ALLOW_NETWORK_FS=1` — it still logs a WARNING on every start.

### Log rotation

`docker-compose.yml` already sets:

```yaml
logging:
  driver: json-file
  options: { max-size: "10m", max-file: "3" }
```

Equivalent flags for a bare `docker run`:

```sh
docker run --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 ...
```

Portainer stacks honor the compose `logging:` block directly. The app only
ever logs to stdout/stderr — there are no log files inside the container to
worry about.

### Where to pull the image

GHCR is the primary, canonical registry and has no pull-rate limit for
public images:

```
ghcr.io/drewferg11/numbers-go-up:<version>
```

Tagged releases are also mirrored to Docker Hub, byte-identical (same
digest, same architectures), for anyone who prefers to pull from there:

```
docker.io/drewferg11/numbers-go-up:<version>
```

Docker Hub's anonymous pulls are capped (100 pulls/6 h per IP as of this
writing), which matters if you run Watchtower or another auto-updater —
GHCR doesn't have that limit for public images, so it's the better default
for unattended pulls.

### Timezone

`TZ` is **not set by default** — the container falls back to UTC. Set it to
your own zone in `docker-compose.yml` (e.g. `TZ=America/New_York`), because it
affects timestamp correctness in the SQLite history and the daily heartbeat
boundary.

### Backup & restore

The database looks after itself: a scheduled `maintenance` job runs once a
day (first run 10 minutes after startup) and does four things, each isolated
so one failing step doesn't stop the rest:

1. Prunes `plugin_runs` older than `storage.plugin_runs_retention_days`
   (default 30) — always keeping each plugin's newest finished run
   regardless of age, so a disabled plugin still reports its last status.
2. Takes a daily backup to `./data/backups/stats-daily-YYYYMMDD.db` via
   SQLite's own online backup API (safe under WAL, no downtime), verifies it
   with `PRAGMA quick_check`, and keeps the newest `storage.backups.keep_daily`
   files (default 7; `0` disables backups).
3. Runs `PRAGMA optimize`.
4. Checkpoints and truncates the WAL file.

Check `GET /api/integrations` for the last run's summary (rows pruned,
backup file/size, any errors) and when the next run is scheduled.

**Backups sit on the same disk as the live database.** They protect against
a bad migration or accidental data loss, not a dead drive — `./data` still
belongs in your NAS's own snapshot/backup job.

**To restore:**

```sh
docker compose stop
mv data/stats.db data/stats.db.bak    # keep the current one aside
# the mv above doesn't rename these -- remove the stale sidecars so
# a crashed instance's WAL doesn't get replayed over the restored db:
rm -f data/stats.db-wal data/stats.db-shm
cp data/backups/stats-daily-<date>.db data/stats.db
docker compose start
curl localhost:8080/api/metrics        # confirm it looks right
```

Restoring a **pre-migration** backup (`stats-pre-v*.db`, made automatically
before a schema migration) needs the **older image tag** it came from — the
downgrade guard refuses to start against a database newer than the running
build understands, by design.

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

### YouTube

Tracks subscribers, views, and video count for your own channel(s), plus
views and likes for individual videos, via the official Data API v3 — the
only source this plugin ships (see the spike note above on why the
unofficial `livecounts` path was dropped before it shipped).

- **Finding your channel ID:** it's the `UC...` string (24 characters,
  case-sensitive) in your channel's Advanced Settings on YouTube Studio, or
  in a channel URL of the form `youtube.com/channel/UCxxxx...`. A handle
  (`@yourname`) or custom URL is **not** accepted — resolving one to an ID
  costs an extra request and can be ambiguous, so paste the ID directly.
- **API key (required):** create one in Google Cloud Console (YouTube Data
  API v3 enabled) and set it via the `NGU_YOUTUBE_API_KEY` environment
  variable — never in `config.yaml`. It costs 1 quota unit per poll against
  a free 10,000/day quota, whether the request is against `channels` or
  `videos`.
- **Exact vs. rounded:** the official API rounds `subscriberCount` to about
  3 significant figures once a channel gets reasonably large, so day-to-day
  changes on a bigger channel may show as a flat line most days with an
  occasional step. `views`/`videos` are commonly understood to be exact.
  Switching `source` is a deliberate, one-time user action — a series never
  switches sources automatically, since that would write a fake jump in its
  history — and the chart will show one visible step if you ever change it.
- A channel that hides its public subscriber count, or whose subscriber
  count reads exactly 0, fails the poll rather than writing a bogus 0.
- Series labels use the channel's current YouTube title (falling back to
  its `key` if unavailable), so a renamed channel's label drifts to match
  on its next poll — the same trade-off the GitHub plugin makes with
  `full_name`.
- **Per-video views and likes (#115):** configure `plugins.youtube.videos`
  with the 11-character `id` from a video's URL
  (`youtube.com/watch?v={id}`) — no channel needed alongside it, and
  independent of `channels` entirely (track videos with no channel
  configured, or vice versa, or both). Uses the same `videos` endpoint and
  API key as `channels`, so no scraping is involved. A video's `viewCount`
  has no zero-value guard (a freshly uploaded video legitimately starts at
  0), but `likeCount` is simply omitted from that poll's series when the
  uploader has hidden it, rather than failing the whole poll.

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
  include: []                     # glob patterns on metric_key; empty = everything
  exclude: []                     # glob patterns on metric_key; checked after include
```

Set the broker password via the `NGU_MQTT_PASSWORD` environment variable —
never in `config.yaml`. Omit the whole `mqtt` block to keep MQTT off; that's
zero connections, exactly like a plugin that's never configured.

**Choosing which series get published:** by default every active series is
published. `include`/`exclude` are lists of glob patterns
([`fnmatch`](https://docs.python.org/3/library/fnmatch.html) syntax) matched
against the series' `metric_key`, which already encodes plugin and entity
(`github.repo.12345.stars`, `makerworld.model.987.downloads`,
`youtube.channel.main.views`) — so one mechanism covers filtering by whole
plugin (`youtube.*`), by one entity within a plugin
(`makerworld.model.987.*`), or by metric across everything
(`*.comments`). If `include` is non-empty, only series matching at least one
of its patterns are eligible; `exclude` is then applied on top and always
wins. Leave both empty for the previous all-active-series behavior.
Changing `include`/`exclude` doesn't retract a series HA already learned
about — a series that becomes excluded stops getting new state, and goes
"unavailable" once its `expire_after` elapses, but its discovery config and
last retained state stay in HA until you remove it manually (delete the
entity in HA, or temporarily deactivate the series) — filtering is a
publish-time decision, not the same as the deactivation lifecycle.

Every series becomes one sensor entity, all grouped under a single
**numbers-go-up** device (manufacturer "numbers-go-up", model "stats
poller") so they show up together in HA's device list instead of scattered
across "MQTT" entities. `cumulative` metrics get `state_class:
total_increasing`; `gauge` metrics get `state_class: measurement`.

- **`expire_after`** is set to 3x that plugin's poll interval — exactly the
  `stale` rule `/api/stats/latest` and the dashboard use: no successful poll
  of the series' plugin within that window. If a plugin stops polling, its
  entities go "unavailable" in HA at the same moment the dashboard and API
  start showing it as stale, instead of silently freezing on the last value.
- **Entity IDs are permanent.** An entity's `object_id`/`unique_id` is
  derived once from its metric key (`makerworld.profile.design_downloads` →
  `ngu_makerworld_profile_design_downloads`) and never changes, even if the
  series' label changes later. Two different metric keys that would collide
  on the same object_id (a `.`/`_` clash) are detected: the second one is
  skipped with a logged error rather than silently overwriting the first.
- **Disabling a plugin retires its series** at the next restart: they leave
  the dashboard, `/api/stats/latest`, and (via the same deactivation
  lifecycle as a MakerWorld model that stops being returned) Home Assistant,
  with their discovery config and retained state cleared automatically.
  History is kept — `/api/metrics` still lists them with `active: false`,
  and their detail page and history are still reachable. Re-enabling the
  plugin brings a series back, with its full history, as soon as it polls
  successfully again.
- **To remove everything:** disable the plugins and restart — that retires
  every one of their series, which removes their HA entities as above.
  (Deleting the `mqtt` block instead only stops future publishing: with no
  `MqttPublisher` running, nothing sends the `offline` status or clears
  retained discovery configs, so already-published entities linger in HA
  until removed there.) To also remove the now-empty "numbers-go-up" device
  itself, go to Settings → Devices & Services → MQTT and delete it.

**Without MQTT:** there's currently no REST-polling fallback for Home
Assistant (a plain `rest` sensor reading `/api/stats/latest` is a possible
future addition — not implemented here); MQTT discovery is the only
supported integration path today.

## Milestone alerts

Get a phone notification when a counter crosses a milestone — "Design
Downloads passed 500" — instead of watching a dashboard for it. Independent
of MQTT: this works whether or not the `mqtt` block above is configured.

Add a `milestones` block to `config.yaml`:

```yaml
milestones:
  webhook_url_env: NGU_MILESTONE_WEBHOOK_URL   # env var holding the full HA webhook URL
  rules:
    - metric: makerworld.profile.design_downloads
      every: 500                               # fires at 500, 1000, 1500, ...
    - metric: youtube.channel.main.subscribers
      at: [1000, 2500, 5000, 10000]            # fires only at these thresholds
    - metric: makerworld.model.{id}.downloads  # a plugin's METRICS pattern --
      every: 100                               # applies to every matching series
```

Set the webhook URL via the `NGU_MILESTONE_WEBHOOK_URL` environment
variable (or whatever name `webhook_url_env` points at) — never in
`config.yaml`; it's a credential, exactly like `NGU_MQTT_PASSWORD`.

**In Home Assistant:** Settings → Automations → create a new automation
with a **Webhook** trigger, and copy the webhook URL it gives you into that
environment variable. Add a **Notify → Mobile App** action reading the
payload's `message` field, e.g.:

```yaml
trigger:
  - platform: webhook
    webhook_id: your-webhook-id
    allowed_methods: [POST]
    local_only: true
action:
  - service: notify.mobile_app_your_phone
    data:
      message: "{{ trigger.json.message }}"
      title: "numbers-go-up"
```

HA webhooks are `local_only: true` by default — fine if the service and HA
are on the same network; turn it off only if numbers-go-up reaches HA from
outside it.

**A rule** needs `metric` (an exact metric key, or one of a plugin's
`METRICS` pattern templates, applying to every series that pattern
matches — even ones that don't exist yet) plus `every` and/or `at`:
`every: 500` fires at every multiple of 500; `at: [1000, 2500]` fires only
at those exact thresholds; both together fire at the union of both, still
once per threshold.

**Fires once per threshold, ever:**

- Never on startup, and never for a brand new series' first sample — there's
  no "previous" value to have crossed anything yet.
- Adding a rule to a series that's already past some of its thresholds
  doesn't fire a backlog of "missed" milestones — it just quietly starts
  tracking from the current value.
- If a gauge crosses a threshold, dips back below it, and climbs past it
  again, that's still just one notification, not two.
- A burst that jumps past several thresholds at once (e.g. 480 → 1020 with
  `every: 500`) still sends exactly one notification, for the highest one
  (1000).

**Delivery:** a failed POST (Home Assistant down, a network blip) is
retried on the plugin's next successful poll, and survives a service
restart. After 24 hours of failed retries it's dropped (logged once) rather
than retried forever, and the milestone counts as delivered so a webhook
that comes back later doesn't get flooded with a backlog. One exception: if
a still-failing delivery is superseded by a newer, higher crossing on the
same series before it succeeds, it's not retried at all -- the higher
threshold's notification covers it, since fire-once lives in the marker,
not in whichever pending delivery happens to survive.

Check `GET /api/integrations` for `milestones.pending` (deliveries still
retrying), `milestones.last_sent`, and `milestones.last_error` — the
webhook URL itself is never included anywhere in that response, or in the
logs.

## License

MIT — see [LICENSE](LICENSE).
