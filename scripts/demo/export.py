#!/usr/bin/env python3
"""Crawl a seeded instance of the real app to a static, self-contained
directory: the demo Pages site is a rehost of the actual dashboard, not a
fork of it.

Runs the app in-process against a seeded SQLite database via
``httpx.ASGITransport`` -- no uvicorn, no port to poll. Zero plugins are
ever enabled (defaults, see ``numbers_go_up.config.load_config``), so the
scheduler never starts a job and no plugin ever runs: the only way this
script could reach the network. That is asserted, not assumed -- see
``_no_sockets`` below.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import socket
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import httpx  # noqa: E402

from scripts.demo.seed import DEFAULT_DB_PATH, RETIRED_PLUGIN  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "demo-dist"

_STATIC_URL_RE = re.compile(r'((?:href|src)=")(/static/[^"]*)"')

# Neither base.html nor index.html/detail.html links these -- they exist
# for a future PWA manifest, not for anything the two crawled page shapes
# render. At ~200 KB combined they are most of the gap to the ~1 MB
# export budget, for zero visible difference in the demo.
_UNREFERENCED_STATIC_FILES = ("icon-512.png", "icon-192.png")


@contextmanager
def _no_sockets():
    """Make ``socket.socket()`` raise for the duration of the crawl.

    Belt and suspenders: the app is only ever driven over
    ``httpx.ASGITransport``, which calls the ASGI app directly in-process
    and never opens a real socket, and zero plugins are enabled so the
    scheduler has nothing to poll. This turns "the export makes no
    outbound network requests" from an assumption about the app's current
    behavior into something that fails loudly the moment it stops being
    true.
    """
    real_socket = socket.socket

    def _blocked(*args, **kwargs):
        raise RuntimeError("scripts/demo/export.py must not open sockets")

    socket.socket = _blocked
    try:
        yield
    finally:
        socket.socket = real_socket


def _rewrite_static_urls(html: str, depth: int) -> str:
    """Rewrite ``/static/...`` (with its cache-busting ``?v=`` query kept
    intact) to a path relative to a page ``depth`` directories below the
    export root -- 0 for ``index.html``, 2 for ``m/<key>/index.html``."""
    prefix = "../" * depth
    return _STATIC_URL_RE.sub(lambda m: f'{m.group(1)}{prefix}{m.group(2)[1:]}"', html)


def _reactivate_demo_series(db_path: Path) -> None:
    """Undo ``main.lifespan``'s own startup housekeeping.

    With zero plugins enabled (required so the scheduler never starts a
    job -- see the module docstring), ``retire_series_not_in`` runs at
    every boot and deactivates every series whose plugin isn't currently
    enabled, which on a fresh checkout is *every* demo series: none of the
    synthetic plugin names in ``scripts/demo/seed.py`` are, or should be, a
    real installed plugin. This restores exactly the active/inactive split
    ``seed()`` set up, without touching ``main.py`` -- the retirement is
    real app behavior working exactly as designed; it just assumes a
    normal deployment, not a demo with plugins that are deliberately never
    "enabled".
    """
    from numbers_go_up import storage

    rows = storage.list_all_series(db_path)
    activate = [r["id"] for r in rows if r["plugin_name"] != RETIRED_PLUGIN]
    deactivate = [r["id"] for r in rows if r["plugin_name"] == RETIRED_PLUGIN]
    storage.set_series_active_bulk(db_path, activate, deactivate)


async def _crawl(client: httpx.AsyncClient, output: Path) -> None:
    from numbers_go_up.queries import VALID_RANGES

    index = await client.get("/")
    index.raise_for_status()
    (output / "index.html").write_text(_rewrite_static_urls(index.text, depth=0))

    metrics_resp = await client.get("/api/metrics")
    metrics_resp.raise_for_status()
    keys = [m["key"] for m in metrics_resp.json()["metrics"]]

    api_dir = output / "api"
    api_dir.mkdir(parents=True, exist_ok=True)
    m_dir = output / "m"

    for range_key in VALID_RANGES:
        overview = await client.get(f"/api/stats/overview?range={range_key}")
        overview.raise_for_status()
        (api_dir / f"overview-{range_key}.json").write_text(overview.text)

    for key in keys:
        detail = await client.get(f"/m/{key}")
        detail.raise_for_status()
        page_dir = m_dir / key
        page_dir.mkdir(parents=True, exist_ok=True)
        # output/m/<key>/index.html is two directories below the export
        # root (m/, then <key>/), so it needs "../../static/...".
        (page_dir / "index.html").write_text(_rewrite_static_urls(detail.text, depth=2))

        for range_key in VALID_RANGES:
            history = await client.get(
                f"/api/stats/history?metric={key}&range={range_key}"
            )
            history.raise_for_status()
            (api_dir / f"history-{key}-{range_key}.json").write_text(history.text)


async def export(seed_db: Path, output: Path) -> None:
    from numbers_go_up import config, main

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    with tempfile.TemporaryDirectory() as data_dir:
        data_path = Path(data_dir)
        shutil.copy(seed_db, data_path / "stats.db")

        env_overrides = {
            config.ENV_DATA_DIR: str(data_path),
            # A nonexistent path: load_config() writes a commented example
            # beside it and falls back to defaults -- zero plugins enabled,
            # the same "no config file" path a fresh install takes.
            config.ENV_CONFIG_FILE: str(data_path / "config.yaml"),
        }
        previous = {key: os.environ.get(key) for key in env_overrides}
        os.environ.update(env_overrides)
        try:
            with _no_sockets():
                transport = httpx.ASGITransport(app=main.app)
                async with (
                    main.app.router.lifespan_context(main.app),
                    httpx.AsyncClient(
                        transport=transport, base_url="http://demo.invalid"
                    ) as client,
                ):
                    _reactivate_demo_series(data_path / "stats.db")
                    await _crawl(client, output)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    shutil.copytree(
        REPO_ROOT / "numbers_go_up" / "static",
        output / "static",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(*_UNREFERENCED_STATIC_FILES),
    )


def main_cli(argv: list[str]) -> int:
    seed_db = Path(argv[1]) if len(argv) > 1 else DEFAULT_DB_PATH
    output = Path(argv[2]) if len(argv) > 2 else DEFAULT_OUTPUT
    if not seed_db.exists():
        print(f"Seed database not found at {seed_db}; run scripts/demo/seed.py first")
        return 1
    asyncio.run(export(seed_db, output))
    print(f"Exported demo to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main_cli(sys.argv))
