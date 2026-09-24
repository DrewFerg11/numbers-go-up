#!/usr/bin/env bash
# Poll a container's own HEALTHCHECK status (Dockerfile ships one) until it
# reports "healthy", instead of curl-ing a host port that a caller may not
# have published with -p -- a container started without a published port
# always fails that curl, so the loop silently burns its full timeout and
# gates nothing. `docker inspect`/`docker exec` work over the Docker socket
# regardless of published ports, so this is a real readiness signal for any
# container, published or not.
set -euo pipefail

container="$1"
timeout="${2:-60}"
elapsed=0

while [ "$elapsed" -lt "$timeout" ]; do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$container" 2>/dev/null || echo "starting")
    if [ "$status" = "healthy" ]; then
        exit 0
    fi
    if [ "$status" = "unhealthy" ]; then
        echo "::error::$container reported unhealthy" >&2
        docker logs "$container" || true
        exit 1
    fi
    sleep 2
    elapsed=$((elapsed + 2))
done

echo "::error::$container never became healthy within ${timeout}s" >&2
docker logs "$container" || true
exit 1
