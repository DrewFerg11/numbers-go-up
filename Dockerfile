FROM python:3.14.7-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY requirements.txt ./
# util-linux: setpriv, used by docker-entrypoint.sh to drop root privileges.
RUN apt-get update \
    && apt-get install --no-install-recommends -y util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY numbers_go_up ./numbers_go_up
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin ngu \
    && chown -R ngu:ngu /app \
    # /data, /config, /plugins are the default mount points (see
    # docker-compose.yml). Pre-create and own them so the app can write
    # its example config and database even when nothing is bind-mounted
    # over them, e.g. the CI smoke test's bare `docker run`.
    && mkdir -p /data /config /plugins \
    && chown ngu:ngu /data /config /plugins \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)" || exit 1

# Stays root here: docker-entrypoint.sh drops to PUID/PGID (default 1000:1000)
# before exec'ing CMD, unless compose already sets `user:`, in which case it
# skips straight to exec. The app itself never runs as root either way.
USER root

ENTRYPOINT ["docker-entrypoint.sh"]

# No --workers flag: multiple uvicorn workers would mean multiple
# APScheduler instances polling the same sources and writing the same
# SQLite file from separate processes. Never add one.
CMD ["uvicorn", "numbers_go_up.main:app", "--host", "0.0.0.0", "--port", "8080"]
