from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from numbers_go_up import __version__, migrate, scheduler
from numbers_go_up.config import load_config


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = load_config()
    migrate.run_migrations(config["storage"]["path"])

    job_scheduler = scheduler.build_scheduler(config)
    job_scheduler.start()
    try:
        yield
    finally:
        # wait=False: shutdown must not hang on an in-flight collect() --
        # Python can't safely kill a thread, so we just stop waiting on it.
        job_scheduler.shutdown(wait=False)


app = FastAPI(title="numbers-go-up", version=__version__, lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
