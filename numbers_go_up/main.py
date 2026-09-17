import logging
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI

from numbers_go_up import __version__, api, http, migrate, netfs, scheduler
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
    app.state.plugin_intervals = {
        plugin.name: plugin.interval_seconds for plugin in discover_plugins(config)
    }
    # /api/plugins reports on every discovered plugin, enabled or not, but
    # must not discover per request: discovery executes every plugin module
    # (module-level code, including user plugins from NGU_PLUGIN_DIR), and
    # a mid-flight file edit would otherwise let the endpoint diverge from
    # the scheduler's startup snapshot. Same one-time scan, read by the
    # route off app.state.
    app.state.plugin_names = discover_plugin_names(config)

    shared_http_client = http.build_client()
    job_scheduler = scheduler.build_scheduler(config, http=shared_http_client)
    app.state.scheduler = job_scheduler
    job_scheduler.start()
    try:
        yield
    finally:
        # wait=False: shutdown must not hang on an in-flight collect() --
        # Python can't safely kill a thread, so we just stop waiting on it.
        job_scheduler.shutdown(wait=False)
        shared_http_client.close()


app = FastAPI(title="numbers-go-up", version=__version__, lifespan=lifespan)
app.include_router(api.router)
app.include_router(api.health_router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness only: 200 while the process serves requests. Deliberately
    ignores plugin state -- a source being down is not a reason to restart
    the container. Monitor plugin polling with ``/health/plugins``."""
    return {"status": "ok", "version": __version__}
