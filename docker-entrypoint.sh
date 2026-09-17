#!/bin/sh
# Drops root privileges before exec'ing the real command, so the container
# can be started as root (compose default, no `user:`) and still have the
# app run as an unprivileged UID -- matching whatever UID/GID owns the NAS
# bind mounts, without a hand-run `chown` on the host.
#
# Started non-root already (compose `user: "1000:1000"`, the pre-PUID/PGID
# way of running this image): skip everything below and exec directly.
# Existing deployments keep working unchanged.
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" != "0" ]; then
    exec "$@"
fi

if [ "$PUID" = "0" ] || [ "$PGID" = "0" ]; then
    echo "ERROR: PUID/PGID must not be 0 -- this image never runs the app as root." >&2
    exit 1
fi

if ! getent group "$PGID" >/dev/null 2>&1; then
    groupmod -o -g "$PGID" ngu
fi
if ! id -u "$PUID" >/dev/null 2>&1; then
    usermod -o -u "$PUID" ngu
fi

# Only the top-level owner is checked, not a full tree walk, so a
# already-correct bind mount doesn't pay a recursive chown on every boot.
for dir in /data /config; do
    owner="$(stat -c '%u:%g' "$dir")"
    if [ "$owner" != "${PUID}:${PGID}" ]; then
        chown -R "${PUID}:${PGID}" "$dir"
    fi
done

exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
