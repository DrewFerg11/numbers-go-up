from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from numbers_go_up import __version__, api, http, migrate, scheduler
from numbers_go_up.config import load_config
from numbers_go_up.plugins import discover_plugin_names, discover_plugins


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = load_config()
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
