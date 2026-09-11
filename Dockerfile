FROM python:3.14.7-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY numbers_go_up ./numbers_go_up

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin ngu \
    && chown -R ngu:ngu /app \
    # /data, /config, /plugins are the default mount points (see
    # docker-compose.yml). Pre-create and own them so the app can write
    # its example config and database even when nothing is bind-mounted
    # over them, e.g. the CI smoke test's bare `docker run`.
    && mkdir -p /data /config /plugins \
    && chown ngu:ngu /data /config /plugins

USER 1000:1000

EXPOSE 8080

CMD ["uvicorn", "numbers_go_up.main:app", "--host", "0.0.0.0", "--port", "8080"]
