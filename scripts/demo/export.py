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
_APP_SCRIPT_RE = re.compile(
    r'<script src="[^"]*static/js/(?:dashboard|detail)\.js[^"]*">'
)
_BODY_OPEN_RE = re.compile(r"(<body[^>]*>)")
_REWRITTEN_STATIC_URL_RE = re.compile(r'(?:href|src)="((?:\.\./)*static/[^"]*)"')

# Neither base.html nor index.html/detail.html links these -- they exist
# for a future PWA manifest, not for anything the two crawled page shapes
# render. At ~200 KB combined they are most of the gap to the ~1 MB
# export budget, for zero visible difference in the demo.
_UNREFERENCED_STATIC_FILES = ("icon-512.png", "icon-192.png")

_BANNER_HTML = """
<div id="demo-snapshot-banner" style="position:relative;z-index:1000;
  background:#3a2f00;color:#f4d35e;font:14px/1.4 system-ui,sans-serif;
  padding:.6em 2.2em .6em 1em;text-align:center">
  This is a static snapshot generated at build time -- no live auto-refresh,
  plugin status is frozen as seeded, and "updated" times are build time, not
  now.
  <button onclick="this.parentElement.remove()" aria-label="Dismiss" style="
    position:absolute;right:.4em;top:.4em;background:none;border:0;
    color:inherit;font-size:1.1em;cursor:pointer">&times;</button>
</div>
""".strip()


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


def _postprocess_html(html: str, depth: int) -> str:
    """Rewrite a crawled page for static hosting: relative asset URLs, the
    injected fetch/navigation shim, and the snapshot banner.

    ``depth`` is how many directories the page sits below the export root
    -- 0 for ``index.html``, 2 for ``m/<key>/index.html`` -- and applies to
    both the ``/static/...`` rewrite and where ``demo-shim.js`` (written
    once, at the export root) is loaded from.
    """
    prefix = "../" * depth
    html = _STATIC_URL_RE.sub(lambda m: f'{m.group(1)}{prefix}{m.group(2)[1:]}"', html)
    # Inserted right before dashboard.js/detail.js so its window.fetch and
    # window.setInterval overrides are in place before either ever calls
    # the real ones (see shim.js's own docstring for why both matter).
    html = _APP_SCRIPT_RE.sub(
        f'<script src="{prefix}demo-shim.js"></script>\n\\g<0>', html, count=1
    )
    html = _BODY_OPEN_RE.sub(f"\\1\n{_BANNER_HTML}", html, count=1)
    return html


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


def _verify_static_references(output: Path) -> None:
    """Turn ``_UNREFERENCED_STATIC_FILES``' "nothing links these" claim,
    and the rewrite depth generally, from a reviewed-today assumption into
    a checked one: every ``static/...`` URL any exported page actually
    references must resolve to a real file in the copied tree. A template
    that starts referencing a dropped icon, or a page whose relative depth
    is wrong, fails the export loudly instead of shipping a 404 to the
    live site.
    """
    for html_path in output.rglob("*.html"):
        html = html_path.read_text()
        for url in _REWRITTEN_STATIC_URL_RE.findall(html):
            asset_path = (html_path.parent / url.split("?", 1)[0]).resolve()
            if not asset_path.is_file():
                raise RuntimeError(
                    f"{html_path.relative_to(output)} references missing "
                    f"static asset {url!r} (resolved to {asset_path})"
                )


async def _crawl(client: httpx.AsyncClient, output: Path) -> None:
    from numbers_go_up.queries import VALID_RANGES

    index = await client.get("/")
    index.raise_for_status()
    (output / "index.html").write_text(_postprocess_html(index.text, depth=0))

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
        (page_dir / "index.html").write_text(_postprocess_html(detail.text, depth=2))

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
    shutil.copy(Path(__file__).parent / "shim.js", output / "demo-shim.js")
    _verify_static_references(output)


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
