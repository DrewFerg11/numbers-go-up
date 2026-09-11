from fastapi import FastAPI

from numbers_go_up import __version__

app = FastAPI(title="numbers-go-up", version=__version__)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
