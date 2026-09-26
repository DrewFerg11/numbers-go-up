"""Offline smoke tests for scripts/demo/{seed,export}.py (#160).

Uses a tiny ``days`` window (not the full :data:`seed.DAYS_OF_HISTORY`) so
a full seed-and-export round trip stays fast in CI.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path

from scripts.demo import export, seed

DAYS = 5

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DASHBOARD_JS = _REPO_ROOT / "numbers_go_up" / "static" / "js" / "dashboard.js"
_SHIM_JS = _REPO_ROOT / "scripts" / "demo" / "shim.js"

# Only tags that actually *load* a resource -- <link href>, <script src>,
# <img src> -- never a plain <a href>, which can legitimately point at an
# external URL (detail.html's "Open on {{ metric.plugin }}" link) or an
# app-internal route ("/docs", "/?range=..."). Those are content, not the
# offline-loading guarantee this test is checking.
_LOADED_RESOURCE_RE = re.compile(
    r'<link\b[^>]*\bhref="([^"]+)"|<(?:script|img)\b[^>]*\bsrc="([^"]+)"'
)


def _loaded_resource_urls(html: str) -> list[str]:
    return [a or b for a, b in _LOADED_RESOURCE_RE.findall(html)]


def test_seed_produces_an_up_to_date_migrated_database(tmp_path):
    db_path = tmp_path / "seed.db"

    seed.seed(db_path, now=1_800_000_000, days=DAYS)

    from numbers_go_up import migrate

    # run_migrations is idempotent -- calling it again on an
    # already-current database must not raise or change anything.
    migrate.run_migrations(db_path)

    conn = sqlite3.connect(db_path)
    series = conn.execute("SELECT plugin_name, active FROM metric_series").fetchall()
    conn.close()

    by_plugin = {}
    for plugin_name, active in series:
        by_plugin.setdefault(plugin_name, set()).add(bool(active))

    # The retired plugin's series are all inactive; every other plugin's
    # are all active -- retirement only ever touched the one plugin.
    assert by_plugin[seed.RETIRED_PLUGIN] == {False}
    for plugin_name, active_flags in by_plugin.items():
        if plugin_name != seed.RETIRED_PLUGIN:
            assert active_flags == {True}, plugin_name


def test_seed_gives_the_flaky_plugin_only_failed_runs(tmp_path):
    db_path = tmp_path / "seed.db"
    seed.seed(db_path, now=1_800_000_000, days=DAYS)

    conn = sqlite3.connect(db_path)
    statuses = {
        row[0]
        for row in conn.execute(
            "SELECT status FROM plugin_runs WHERE plugin_name = ?",
            (seed.FLAKY_PLUGIN,),
        )
    }
    conn.close()

    # No successful run at all -- storage.last_ok_runs() has no entry for
    # this plugin, so queries.is_stale() reads every one of its metrics as
    # stale regardless of interval.
    assert statuses == {"error"}


def test_seed_covers_both_kinds(tmp_path):
    db_path = tmp_path / "seed.db"
    seed.seed(db_path, now=1_800_000_000, days=DAYS)

    conn = sqlite3.connect(db_path)
    kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM metric_series")}
    conn.close()

    assert kinds == {"gauge", "cumulative"}


def test_export_produces_every_range_and_no_third_party_urls(tmp_path):
    db_path = tmp_path / "seed.db"
    output = tmp_path / "dist"
    seed.seed(db_path, now=1_800_000_000, days=DAYS)

    asyncio.run(export.export(db_path, output))

    from numbers_go_up.queries import VALID_RANGES

    assert (output / "index.html").exists()
    for range_key in VALID_RANGES:
        assert (output / "api" / f"overview-{range_key}.json").exists()

    metric_dirs = sorted(p.name for p in (output / "m").iterdir())
    assert "counter.orders" in metric_dirs
    # The retired plugin's series is inactive but its history is kept
    # (#133) -- /m/<key> still resolves for it.
    assert "legacy.widget_count" in metric_dirs

    for metric_dir in metric_dirs:
        for range_key in VALID_RANGES:
            assert (output / "api" / f"history-{metric_dir}-{range_key}.json").exists()

    index_html = (output / "index.html").read_text()
    detail_html = (output / "m" / "counter.orders" / "index.html").read_text()

    # index.html sits at the export root, m/<key>/index.html two levels
    # below it -- each page's loaded resources must be relative at exactly
    # that depth, and never absolute (a CDN, or a root-absolute /static/...
    # that 404s once the export isn't served from a domain root).
    for html, expected_prefix in (
        (index_html, "static/"),
        (detail_html, "../../static/"),
    ):
        urls = _loaded_resource_urls(html)
        assert urls, "expected at least one loaded resource"
        for url in urls:
            assert not re.match(r"https?://", url), f"third-party resource URL: {url}"
        static_urls = [u for u in urls if "static/" in u]
        assert static_urls, "expected at least one static/ asset"
        for url in static_urls:
            assert url.startswith(expected_prefix), url
            assert not url.startswith("/static/"), url

    assert not (output / "static" / "img" / "icon-512.png").exists()
    assert (output / "static" / "css" / "dashboard.css").exists()


def test_export_injects_the_shim_and_banner_at_the_right_depth(tmp_path):
    db_path = tmp_path / "seed.db"
    output = tmp_path / "dist"
    seed.seed(db_path, now=1_800_000_000, days=DAYS)

    asyncio.run(export.export(db_path, output))

    assert (output / "demo-shim.js").exists()

    index_html = (output / "index.html").read_text()
    assert 'src="demo-shim.js"' in index_html
    assert index_html.index('src="demo-shim.js"') < index_html.index(
        "static/js/dashboard.js"
    )
    assert 'id="demo-snapshot-banner"' in index_html

    detail_html = (output / "m" / "counter.orders" / "index.html").read_text()
    assert 'src="../../demo-shim.js"' in detail_html
    assert detail_html.index('src="../../demo-shim.js"') < detail_html.index(
        "static/js/detail.js"
    )
    assert 'id="demo-snapshot-banner"' in detail_html


def test_seed_gauge_values_stay_within_their_declared_bounds():
    # The full shipped DAYS_OF_HISTORY, not the short DAYS used elsewhere
    # in this file -- this is exactly what ships to the public demo once
    # #161 lands, and the random walk's drift compounds with step count.
    rng = seed.random.Random(20260925)  # seed.seed()'s own default seed_value
    steps = seed.DAYS_OF_HISTORY * seed.STEPS_PER_DAY
    for series_defs in seed.PLUGINS.values():
        for metric_key, kind, _label, _unit, _icon, bounds in series_defs:
            if bounds is None:
                continue
            start = rng.uniform(*bounds) if kind == "gauge" else rng.uniform(0, 50)
            values = seed._walk(
                rng, start, steps, cumulative=kind == "cumulative", bounds=bounds
            )
            lo, hi = bounds
            assert all(lo <= v <= hi for v in values), (
                f"{metric_key} left its declared bounds {bounds}: "
                f"min={min(values)} max={max(values)}"
            )


def test_shim_refresh_delay_matches_dashboard_js_refresh_ms():
    # demo-shim.js refuses to schedule dashboard.js's auto-refresh by
    # matching on the hardcoded literal 60000 -- if dashboard.js's own
    # REFRESH_MS ever drifts from that, the shim silently stops
    # recognising it and the demo gets a live auto-refresh loop against
    # static files (a permanent, console-spamming 404/rejection every
    # interval). Pin both sides so drift is a red test, not a live bug.
    dashboard_js = _DASHBOARD_JS.read_text()
    match = re.search(r"var REFRESH_MS = (\d+);", dashboard_js)
    assert match is not None, "REFRESH_MS constant not found in dashboard.js"
    refresh_ms = int(match.group(1))

    shim_js = _SHIM_JS.read_text()
    assert f"if (delay === {refresh_ms}) return -1;" in shim_js
