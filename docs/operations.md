# Operations

Running numbers-go-up day to day: backups, updates, and hardening the
container.

## Backups

### What runs automatically

A **maintenance job** runs once a day, first 10 minutes after the container
starts. It does four things, each isolated so one failing step doesn't
stop the rest:

1. **Prunes old poll records** older than
   `storage.plugin_runs_retention_days` (default 30). Each plugin's newest
   finished run is always kept, so a disabled plugin still shows its last
   status.
2. **Backs up the database** to `./data/backups/stats-daily-YYYYMMDD.db`,
   using SQLite's online backup API (safe while running, no downtime), and
   checks the copy with `PRAGMA quick_check`. The newest
   `storage.backups.keep_daily` backups (default 7) are kept; `0` turns
   daily backups off.
3. **Runs `PRAGMA optimize`.**
4. **Checkpoints and truncates** the write-ahead log.

**To check it ran**, open `GET /api/integrations`. Its `maintenance`
section shows the last run's time, rows pruned, the backup file and size,
any errors, and when the next run is due.

!!! warning "Backups live on the same disk as the database"
    They protect against a bad migration or an accidental deletion, not a
    dead drive. Include `./data` in your NAS's own snapshot or backup job
    too.

### The restore drill

A backup nobody has restored is a hope, not a backup. Run through this
once, before you need it:

```sh
docker compose stop
mv data/stats.db data/stats.db.bak              # keep the current one aside
rm -f data/stats.db-wal data/stats.db-shm       # don't replay a stale WAL over the restore
cp data/backups/stats-daily-<date>.db data/stats.db
docker compose start
curl localhost:8080/api/metrics                 # confirm the history is there
```

Removing the `-wal` and `-shm` files matters. Otherwise SQLite may replay
the old database's write-ahead log onto the restored copy.

## Updating

```sh
docker compose pull
docker compose up -d
```

**Database migrations apply automatically at startup.** Before applying
one, the service copies the database to
`./data/backups/stats-pre-v<N>-<timestamp>.db`.

**Rolling back** to an older image after a migration needs that
pre-migration backup. An older build refuses to start against a database
newer than it understands, rather than risk damaging it. Restore the
`stats-pre-v…` file using the drill above, and run the image tag it came
from.

## Where the images are published

| Registry | Image | Notes |
|---|---|---|
| GitHub Container Registry | `ghcr.io/drewferg11/numbers-go-up` | **Canonical.** No pull-rate limit for public images. |
| Docker Hub | `docker.io/drewferg11/numbers-go-up` | Mirror of tagged releases, at the **same digest**. Anonymous pulls are rate-limited. |

Prefer GHCR for anything that pulls unattended, such as Watchtower or
another auto-updater. Both are multi-arch: `amd64`, `arm64`, `armv7`.

## Container hardening

### Running as your user

The container starts as root only to change ownership of `./data` and
`./config` to `PUID:PGID`, then drops to that user before the app starts.
See [Install → File ownership](getting-started/install.md#3-file-ownership).
The app itself never runs as root.

### A locked-down runtime

For a read-only, capability-stripped container, add this to the service in
`docker-compose.yml`. With the default `PUID`/`PGID` setup, the entrypoint
needs three capabilities to change ownership and switch users:

```yaml
read_only: true
tmpfs: [/tmp]
cap_drop: [ALL]
cap_add: [CHOWN, SETUID, SETGID]
# no security_opt: no-new-privileges would stop the switch to your user
```

If you run with `user: "1000:1000"` instead, you can drop everything:

```yaml
read_only: true
tmpfs: [/tmp]
security_opt: ["no-new-privileges:true"]
cap_drop: [ALL]
```

Either way, the app writes nothing outside `/data`, `/config` and `/tmp`.

### The network-filesystem guard

At startup the container checks what `./data` is mounted on, and refuses
to start on NFS, SMB/CIFS, SSHFS or 9p. SQLite's locking is unreliable
there, and that can silently corrupt the database. See the warning on the
[Install](getting-started/install.md#2-create-the-folders) page.

## Logs

The app logs only to the container's stdout/stderr; there are no log files
inside it. The shipped `docker-compose.yml` already rotates them:

```yaml
logging:
  driver: json-file
  options: { max-size: "10m", max-file: "3" }
```

For a bare `docker run`, the equivalent is
`--log-driver json-file --log-opt max-size=10m --log-opt max-file=3`.
Portainer stacks honour the compose `logging:` block.

To see more detail, set `NGU_LOG_LEVEL=DEBUG`; see
[Troubleshooting → Where to look](troubleshooting.md#where-to-look).
