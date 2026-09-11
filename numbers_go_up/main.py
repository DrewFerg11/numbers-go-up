from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from numbers_go_up import __version__, migrate
from numbers_go_up.config import load_config


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = load_config()
    migrate.run_migrations(config["storage"]["path"])
    yield


app = FastAPI(title="numbers-go-up", version=__version__, lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
