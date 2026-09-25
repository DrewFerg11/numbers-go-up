import logging
import os
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi_offline import FastAPIOffline

from numbers_go_up import (
    __version__,
    api,
    dashboard,
    http,
    migrate,
    milestones,
    mqtt,
    netfs,
    scheduler,
    storage,
)
from numbers_go_up.api import HealthResponse
from numbers_go_up.config import load_config
from numbers_go_up.plugins import LoadedPlugin, discover

ENV_LOG_LEVEL = "NGU_LOG_LEVEL"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

logger = logging.getLogger(__name__)


class PackageLogHandler(logging.StreamHandler):
    """The handler configure_logging() attaches to the ``numbers_go_up``
    logger -- its own type, so a second call can tell it's already there."""


class HealthCheckAccessFilter(logging.Filter):
    """Drop uvicorn access-log lines for ``/health`` and ``/health/*``.

    An uptime monitor polling every minute would otherwise bury every other
    line in the log. Nothing is lost: plugin failures and recoveries are
    logged by the scheduler when they happen.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn's access record args are
        # (client_addr, method, full_path, http_version, status_code).
        args = record.args
        if not isinstance(args, tuple) or len(args) < 3 or not isinstance(args[2], str):
            return True
        path = args[2].split("?", 1)[0]
        return path != "/health" and not path.startswith("/health/")


def configure_logging(env: Mapping[str, str] | None = None) -> None:
    """Give this package's loggers a handler, and quiet health-check access lines.

    uvicorn only configures its own loggers. Without this, every
    ``numbers_go_up`` log call below WARNING was dropped, and warnings went
    through logging's bare last-resort handler with no timestamp or logger
    name. ``NGU_LOG_LEVEL`` (default INFO) sets the level; DEBUG also logs
    every repeat of an ongoing plugin failure. Safe to call more than once.
    """
    env = os.environ if env is None else env

    package_logger = logging.getLogger("numbers_go_up")
    if not any(isinstance(h, PackageLogHandler) for h in package_logger.handlers):
        handler = PackageLogHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        package_logger.addHandler(handler)

    requested = env.get(ENV_LOG_LEVEL, "INFO").strip().upper()
    level = logging.getLevelNamesMapping().get(requested)
    package_logger.setLevel(logging.INFO if level is None else level)
    if level is None:
        logger.warning(
            "%s=%r is not a logging level; using INFO",
            ENV_LOG_LEVEL,
            env[ENV_LOG_LEVEL],
        )

    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, HealthCheckAccessFilter) for f in access_logger.filters):
        access_logger.addFilter(HealthCheckAccessFilter())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Here rather than at import: uvicorn configures its loggers before it
    # imports the app, so this runs after that and nothing overwrites it.
    configure_logging()
    config = load_config()
    netfs.check_not_network_filesystem(config["storage"]["path"])
    migrate.run_migrations(config["storage"]["path"])
    # A docker stop or crash mid-poll leaves a plugin_runs row with
    # finished_at IS NULL -- close it as interrupted before anything reads
    # plugin state, so a restart never reports a stale "polling" plugin or
    # counts the orphaned row as a failure.
    storage.close_interrupted_runs(config["storage"]["path"], int(time.time()))

    app.state.config = config
    # One discovery pass for the whole process: every consumer below
    # (plugin_intervals/plugin_metrics, plugin_names, the scheduler, MQTT,
    # milestones) derives from this same list instead of calling discover()
    # again -- each call re-imports every plugin file (module-level code,
    # including user plugins from NGU_PLUGIN_DIR) from scratch, which used
    # to mean three separate module objects per plugin (one per call site),
    # splitting a plugin's own module-level state across them and logging
    # every contract-violation warning three times.
    all_plugins = discover(config)
    enabled_plugins = [
        LoadedPlugin(
            name=p.name,
            module=p.module,
            metrics=p.metrics,
            interval_seconds=p.interval_seconds,
            config=p.config,
            source=p.source,
        )
        for p in all_plugins
        if p.enabled
    ]
    # Read by the API routes rather than recomputed per request --
    # enabled_plugins covers exactly the ones the stale rule needs an
    # interval for.
    app.state.plugin_intervals = {
        plugin.name: plugin.interval_seconds for plugin in enabled_plugins
    }
    # The dashboard overview needs each plugin's METRICS to tell a pattern
    # series (e.g. per-model) from a static one.
    app.state.plugin_metrics = {
        plugin.name: plugin.metrics for plugin in enabled_plugins
    }
    # /api/plugins reports on every discovered plugin, enabled or not.
    app.state.plugin_names = [p.name for p in all_plugins]

    # A plugin disabled in config or whose file was removed since the last
    # run leaves its series stranded active forever otherwise -- frozen on
    # the dashboard, still in /api/stats/latest, and MQTT re-announcing
    # their discovery on every reconnect (#133). Retiring only ever flips
    # `active`; history is kept, and get_or_create_series reactivates a
    # series the moment its plugin polls again (re-enabled or reinstalled).
    retired = storage.retire_series_not_in(
        config["storage"]["path"], (p.name for p in enabled_plugins)
    )
    for plugin_name, count in retired.items():
        logger.info(
            "Retired %d series for disabled/removed plugin %s", count, plugin_name
        )

    shared_http_client = http.build_client()

    # Built whether or not config["mqtt"] is set -- a NoopPublisher when
    # it's absent, so nothing downstream ever branches on "is MQTT on". Not
    # started yet: see the comment below start() for why.
    mqtt_publisher = mqtt.build_publisher(config, app.state.plugin_intervals)
    app.state.mqtt_publisher = mqtt_publisher

    # Built (and validated) whether or not config["milestones"] is set -- a
    # NoopEvaluator when it's absent, so nothing downstream ever branches on
    # "are milestones on". A bad milestones *config* (a missing webhook env
    # var, an invalid rule, an unmatched pattern) fails startup here; an
    # unreachable *webhook* is only ever discovered later, on delivery.
    #
    # Validated before mqtt_publisher.start(), not after: milestones config
    # needs enabled_plugins to check pattern rules, so it can't be validated
    # as early as the mqtt block. Starting the MQTT client first and then
    # failing here would leave a retained "online" message and a running
    # paho network thread behind on a startup that never actually succeeds.
    milestone_evaluator = milestones.build_evaluator(
        config, enabled_plugins, shared_http_client
    )
    app.state.milestone_evaluator = milestone_evaluator

    # A broker that's unreachable must never fail startup: the client just
    # keeps retrying on its own network thread.
    mqtt_publisher.start()

    job_scheduler = scheduler.build_scheduler(
        config,
        http=shared_http_client,
        publisher=mqtt_publisher,
        milestone_evaluator=milestone_evaluator,
        enabled_plugins=enabled_plugins,
    )
    app.state.scheduler = job_scheduler
    job_scheduler.start()
    try:
        yield
    finally:
        # wait=False: shutdown must not hang on an in-flight collect() --
        # Python can't safely kill a thread, so we just stop waiting on it.
        job_scheduler.shutdown(wait=False)
        mqtt_publisher.stop()
        shared_http_client.close()


# FastAPIOffline (not FastAPI directly) serves /docs and /redoc from its
# own bundled Swagger UI / ReDoc assets instead of a CDN, so both render
# with the container's network fully blocked (#92) -- pulled via pip at
# build time like every other dependency, not vendored in this repo.
app = FastAPIOffline(
    title="numbers-go-up",
    version=__version__,
    description=(
        "Self-hosted, plugin-based tracker for the counters you care about, "
        "with history, rate-of-change, and a Home Assistant integration. "
        "This is the REST API a running instance exposes; see the project's "
        "[README](https://github.com/DrewFerg11/numbers-go-up) for setup."
    ),
    contact={
        "name": "numbers-go-up",
        "url": "https://github.com/DrewFerg11/numbers-go-up",
    },
    license_info={
        "name": "MIT",
        "url": "https://github.com/DrewFerg11/numbers-go-up/blob/main/LICENSE",
    },
    lifespan=lifespan,
)
app.include_router(api.router)
app.include_router(api.health_router)
app.include_router(dashboard.router)
app.mount("/static", StaticFiles(directory=str(dashboard.STATIC_DIR)), name="static")


@app.get(
    "/health",
    tags=["health"],
    summary="Liveness probe",
    response_model=HealthResponse,
)
def health() -> dict[str, str]:
    """Liveness only: 200 while the process serves requests. Deliberately
    ignores plugin state -- a source being down is not a reason to restart
    the container. Monitor plugin polling with ``/health/plugins``."""
    return {"status": "ok", "version": __version__}
