# Install

numbers-go-up ships as one multi-arch container image (`amd64`, `arm64`,
`armv7`) and is run with Docker Compose. Everything it keeps lives in two
folders on the host, so the container itself is disposable.

## What you need

- Docker with the Compose plugin (`docker compose version` works)
- A folder on **local disk** for the database. Read the warning below
  before pointing it at a NAS share.

## 1. Get the compose file

```sh
git clone https://github.com/DrewFerg11/numbers-go-up.git
cd numbers-go-up
```

The [`docker-compose.yml`](https://github.com/DrewFerg11/numbers-go-up/blob/main/docker-compose.yml)
at the repo root is the supported way to run it. It runs the published
`ghcr.io/drewferg11/numbers-go-up:latest` image (its `build: .` also lets
you build from the checkout), publishes port `8080`, rotates logs, and sets
up the bind mounts below.

## 2. Create the folders

```sh
mkdir -p data config user-plugins
```

| Host folder | In the container | What lives there |
|---|---|---|
| `./data` | `/data` | The SQLite database and its backups. **Local disk only.** |
| `./config` | `/config` | `config.yaml`, which you edit on the host. No rebuild needed. |
| `./user-plugins` | `/plugins` | Optional plugins of your own. Leave it empty if you have none. |

`docker rm` the container any time; none of this lives inside it.

!!! danger "Keep `./data` off NFS and SMB shares"
    SQLite's locking is unreliable over network filesystems, and this is the
    one deployment mistake that can **silently corrupt the database**. The
    container checks where `./data` is mounted and refuses to start if it is
    `nfs`, `nfs4`, `cifs`, `smb3`, `smbfs`, `fuse.sshfs`, or `9p`.

    On a NAS, run the container on the NAS itself with `./data` on its local
    volume, not on another machine with the share mounted.
    `NGU_ALLOW_NETWORK_FS=1` overrides the check if you accept the risk,
    and a warning is still logged on every start.

## 3. File ownership

You don't need to `chown` anything by hand. The container starts as root,
changes ownership of `./data` and `./config` to `PUID:PGID`, then drops to
that user before the app runs. Both default to `1000`.

If your host user isn't `1000`, which is common on NAS distros (Synology's
first user is usually `1026`, Unraid uses `99:100`), set both in
`docker-compose.yml`:

```yaml
environment:
  - PUID=1026
  - PGID=100
```

`PUID=0`/`PGID=0` are refused: the app never runs as root.

If you'd rather manage ownership yourself, set `user: "1000:1000"` on the
service instead. The container then skips the ownership step entirely, and
you run `chown -R 1000:1000 data config` on the host once.

## 4. Set your timezone

`TZ` isn't set by default, so the container runs in UTC. Add your zone
under `environment:`. It decides where the daily heartbeat boundary falls.

```yaml
  - TZ=America/New_York
```

## 5. Start it

```sh
docker compose up -d
curl localhost:8080/health
```

Then open `http://<host>:8080` for the dashboard. The container listens on
`8080` inside; to use a different port outside, change the mapping in
compose (`"9000:8080"`).

## What happens on first run

With no `config.yaml` in `./config`, the service writes a fully commented
`config.yaml.example` next to where it looked, then starts on built-in
defaults with **zero plugins enabled**.

!!! success "Nothing is tracked until you ask"
    A fresh install makes **zero outbound requests** to any platform until
    you enable a plugin, and no user IDs, handles or repos are baked into the
    image. The dashboard stays empty until you configure a source.

Copy the example to `config.yaml`, enable a plugin, and restart:

```sh
cp config/config.yaml.example config/config.yaml
# edit config/config.yaml
docker compose restart
```

## Next

- [Configuration](configuration.md): every section of `config.yaml`, and
  which settings go in environment variables instead
- [Plugins](../plugins/index.md): what each source collects and how to set
  it up
- [Home Assistant](../home-assistant/index.md): entities via MQTT
  discovery, with no YAML on the Home Assistant side
