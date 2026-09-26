# Troubleshooting

Start with the symptom you're seeing.

## A plugin's dot is amber or red

The status line at the bottom of the dashboard has one dot per enabled
plugin. **Hover the dot** to see the last error.

- **Amber:** the last one or two polls failed. Often transient, such as a
  network blip or a source hiccup, and it clears on the next good poll.
- **Red:** three or more polls in a row failed, or the source answered
  `403` (see the next section).

The same information is in `GET /api/plugins`: `status`, `last_error`,
`consecutive_failures`, and `next_poll`. The most common causes:

| `last_error` mentions | Likely cause |
|---|---|
| `... is not configured` / `must be configured` | The plugin is enabled but has no account, repo, channel or video set. It makes no request until you add one. |
| `NGU_..._API_KEY is not set` | A required secret is missing from the container's environment |
| `missing expected field`, `not valid JSON` | The source changed its response or served a maintenance page. For an unofficial source, open a "Broken data source" issue. |
| `exceeds ... max` | More items are configured or selected than the plugin's `max` safety cap. Raise the cap on purpose, or track fewer. |
| a `404` | An ID, repo or handle is wrong, or no longer exists |

Each plugin page has a **When it fails** section with its specifics.

## A source shows `blocked`

`blocked` means the source answered **`403 Forbidden`**: it's refusing
this client. That's a different problem from a broken plugin, so it gets
its own status, and it turns the dot red and `/health/plugins` unhealthy on
the **first** occurrence. The plugin keeps its schedule and backs off.

Common reasons:

- **An API quota or rate limit ran out.** YouTube's daily quota and
  GitHub's hourly limit both answer `403`. Look for `quotaExceeded` or a
  rate-limit message in `last_error`, then poll less often or track fewer
  items. For GitHub, add `NGU_GITHUB_TOKEN`.
- **The source is refusing your network.** Unofficial sources sometimes
  block whole IP ranges, such as cloud or VPN addresses, and not others.
  Check whether the same page loads from your browser on the same
  connection. It can be network-dependent: a 403 from one place may not
  reproduce from another.

## A Home Assistant entity is unavailable

Entities go **unavailable** on purpose when their data stops arriving,
rather than freezing on a stale number:

- **Its plugin hasn't polled successfully for 3× its interval** (90 minutes
  at the default). Check that plugin's dot or `/api/plugins`. The entity
  comes back on the next successful poll.
- **Every entity went unavailable at once:** the service stopped, or lost
  its broker connection, and its MQTT last-will fired. Check
  `docker compose ps` and the `mqtt` section of `GET /api/integrations`.
- **You excluded it** with `mqtt.exclude` (or left it out of `include`). It
  stops updating and expires. Delete it in Home Assistant if you don't want
  it.

## "database is locked"

This almost always means **`./data` is on a network share** (NFS, SMB),
where SQLite's locking doesn't work reliably. The container refuses to
start on the common network filesystems, so seeing this usually means the
check was overridden with `NGU_ALLOW_NETWORK_FS=1`, or the share is a type
it can't recognise.

Move `./data` to local disk: stop the container, copy the folder, update
the bind mount, start it again. See
[Install](getting-started/install.md#2-create-the-folders).

Also make sure only **one** container uses that `./data` folder.

## The container won't start

Run `docker compose logs numbers-go-up`. The startup error names the
problem:

- **A config error** names the bad key, often with a "did you mean"
  suggestion. See [Configuration → Validation](getting-started/configuration.md#validation).
- **A network-filesystem error** means `./data` is on a share. See the
  previous section.
- **`PUID must not be 0`**: the app never runs as root. Set `PUID`/`PGID`
  to a real user.
- **A database newer than this build** means an older image is running
  against a database that a newer one already migrated. See
  [Operations → Updating](operations.md#updating).

## Where to look

**Logs.** `docker compose logs -f numbers-go-up`. Plugin failures are
logged when their state changes, not on every poll, so the log stays
readable during a long outage:

- **WARNING** on the first failure of a streak, and again if the kind of
  failure changes
- **INFO** when the plugin recovers
- **DEBUG** for every repeat in between. Set `NGU_LOG_LEVEL=DEBUG` to see
  those.

**Health endpoints.** There are two, for two different jobs:

| Endpoint | Answers | Point at it |
|---|---|---|
| `/health` | Is the process up? Stays `200` even if every plugin is failing. | Docker's own `HEALTHCHECK` already uses it, so `docker ps` and Portainer show healthy/unhealthy. |
| `/health/plugins` | Are the sources actually polling? Returns `503` when an enabled plugin is blocked, or has failed 3 polls in a row. Tune that with `?failures=N`. | **Your uptime monitor** (Uptime Kuma or similar, HTTP type) |

**API.** `GET /api/plugins` for per-plugin status, and
`GET /api/integrations` for the maintenance job, MQTT and milestone
deliveries. The container serves interactive API docs at `/docs`.
