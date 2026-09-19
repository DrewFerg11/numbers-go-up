import logging
import os
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
)
from numbers_go_up.api import HealthResponse
from numbers_go_up.config import load_config
from numbers_go_up.plugins import discover_plugin_names, discover_plugins

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

    app.state.config = config
    # Computed once at startup and read by the API routes rather than
    # recomputed per request -- discover_plugins() only returns *enabled*
    # plugins, so this covers exactly the ones the stale rule needs an
    # interval for. build_scheduler() below discovers again to build its
    # jobs; the duplicate work happens once at startup, not per request.
    enabled_plugins = discover_plugins(config)
    app.state.plugin_intervals = {
        plugin.name: plugin.interval_seconds for plugin in enabled_plugins
    }
    # The dashboard overview needs each plugin's METRICS to tell a pattern
    # series (e.g. per-model) from a static one -- same one-time discovery,
    # no extra module execution per request.
    app.state.plugin_metrics = {
        plugin.name: plugin.metrics for plugin in enabled_plugins
    }
    # /api/plugins reports on every discovered plugin, enabled or not, but
    # must not discover per request: discovery executes every plugin module
    # (module-level code, including user plugins from NGU_PLUGIN_DIR), and
    # a mid-flight file edit would otherwise let the endpoint diverge from
    # the scheduler's startup snapshot. Same one-time scan, read by the
    # route off app.state.
    app.state.plugin_names = discover_plugin_names(config)

    shared_http_client = http.build_client()

    # Built (and started) whether or not config["mqtt"] is set -- a
    # NoopPublisher when it's absent, so nothing downstream ever branches on
    # "is MQTT on". A broker that's unreachable never fails startup: the
    # client just keeps retrying on its own network thread.
    mqtt_publisher = mqtt.build_publisher(config, app.state.plugin_intervals)
    app.state.mqtt_publisher = mqtt_publisher
    mqtt_publisher.start()

    # Built (and validated) whether or not config["milestones"] is set -- a
    # NoopEvaluator when it's absent, so nothing downstream ever branches on
    # "are milestones on". A bad milestones *config* (a missing webhook env
    # var, an invalid rule) fails startup here; an unreachable *webhook* is
    # only ever discovered later, on delivery.
    milestone_evaluator = milestones.build_evaluator(
        config, enabled_plugins, shared_http_client
    )
    app.state.milestone_evaluator = milestone_evaluator

    job_scheduler = scheduler.build_scheduler(
        config,
        http=shared_http_client,
        publisher=mqtt_publisher,
        milestone_evaluator=milestone_evaluator,
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
