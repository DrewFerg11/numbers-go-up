import time

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from numbers_go_up import dashboard, migrate, storage

DAY = 86400
HOUR = 3600


def make_app(
    db_path,
    plugin_intervals=None,
    plugin_metrics=None,
    plugins_config=None,
    dashboard_config=None,
    default_interval=1800,
):
    app = FastAPI()
    app.include_router(dashboard.router)
    app.mount(
        "/static", StaticFiles(directory=str(dashboard.STATIC_DIR)), name="static"
    )
    app.state.config = {
        "storage": {"path": str(db_path)},
        "poll": {"default_interval": default_interval},
        "plugins": plugins_config or {},
        "dashboard": dashboard_config or {},
    }
    app.state.plugin_intervals = plugin_intervals or {}
    app.state.plugin_metrics = plugin_metrics or {}
    app.state.plugin_names = list((plugins_config or {}).keys())
    return app


def db(tmp_path):
    path = tmp_path / "stats.db"
    migrate.run_migrations(str(path))
    return str(path)


def client_for(tmp_path, **kwargs):
    db_path = db(tmp_path)
    app = make_app(db_path, **kwargs)
    return TestClient(app), db_path


def seed_series(
    db_path, metric_key, plugin_name="acme", kind="cumulative", now=None, **kwargs
):
    now = now if now is not None else int(time.time())
    return storage.get_or_create_series(
        db_path, metric_key, plugin_name, kind, "Label", "unit", "icon", now, **kwargs
    )


# --- GET / --------------------------------------------------------------


def test_index_renders_empty_state_with_no_plugins(tmp_path):
    client, _ = client_for(tmp_path, plugins_config={})

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_index_renders_with_healthy_sample_data(tmp_path):
    client, db_path = client_for(tmp_path, plugins_config={"acme": {"enabled": True}})
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now)
    storage.record_sample(db_path, series_id, now, 10, DAY)

    response = client.get("/")

    assert response.status_code == 200


def test_index_exposes_the_unhealthy_failure_threshold_to_the_client(tmp_path):
    # dashboard.js reads this off the .app div rather than hardcoding its
    # own copy of api.DEFAULT_UNHEALTHY_FAILURES, so the status line can't
    # silently drift from /health/plugins' threshold.
    from numbers_go_up.api import DEFAULT_UNHEALTHY_FAILURES

    client, _ = client_for(tmp_path)

    response = client.get("/")

    assert f'data-unhealthy-threshold="{DEFAULT_UNHEALTHY_FAILURES}"' in response.text


def test_index_exposes_the_blocked_error_prefix_to_the_client(tmp_path):
    # dashboard.js checks last_error against this prefix (matching
    # api._unhealthy_reason's own check) rather than trusting `status`
    # alone -- a blocked plugin stays scheduled and retried, so its newest
    # run can read "polling" (in flight) while last_error still carries
    # the prior blocked result. Checking status alone would let the dot
    # go green/grey exactly when /health/plugins reports it unhealthy.
    from numbers_go_up.http import BLOCKED_ERROR_PREFIX

    client, _ = client_for(tmp_path)

    response = client.get("/")

    assert f'data-blocked-prefix="{BLOCKED_ERROR_PREFIX}"' in response.text


def test_index_tile_has_no_href_but_row_label_links_to_detail_page(tmp_path):
    # The detail page (/m/{key}) now exists (this PR). Per spec, the
    # watchlist row's metric name is a link to it; the index tile stays a
    # pure selector (clicking a pinned tile shouldn't navigate away from
    # the overview). This can't watch the client-side DOM directly (no JS
    # test runner in this repo), so it pins the static asset's source --
    # it fails loudly if a future edit drops the row link or adds a tile
    # link back.
    js = (dashboard.STATIC_DIR / "js" / "dashboard.js").read_text(encoding="utf-8")

    assert 'node.removeAttribute("href")' in js
    assert 'label.href = "/m/' in js


def test_status_class_checks_last_error_not_just_status(tmp_path):
    # No JS test runner in this repo, so this pins the source: statusClass
    # must key off `last_error` (matching api._unhealthy_reason's own
    # blocked check), not `plugin.status === "blocked"` alone -- a blocked
    # plugin stays scheduled, so a retry in flight reports status
    # "polling" while last_error still says blocked. Fails loudly if a
    # future edit reverts to the status-only check.
    js = (dashboard.STATIC_DIR / "js" / "dashboard.js").read_text(encoding="utf-8")

    assert "plugin.last_error" in js
    assert "BLOCKED_PREFIX" in js


def test_chart_js_implements_the_stale_dashed_tail(tmp_path):
    # No JS test runner in this repo. The overview and detail acceptance
    # criteria both require a stale series to draw a grey dashed
    # continuation after its last good poll, not just a uniformly grey
    # line -- pin that render() actually branches on staleSinceTs (a
    # documented-but-unimplemented opts field was the exact regression a
    # prior review caught) and that callers pass a real per-point
    # direction rather than collapsing it to a flat "stale" color.
    chart_js = (dashboard.STATIC_DIR / "js" / "chart.js").read_text(encoding="utf-8")
    dashboard_js = (dashboard.STATIC_DIR / "js" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    assert "opts.staleSinceTs" in chart_js
    assert "direction: directionOf(metric)" in dashboard_js
    assert 'direction: metric.stale ? "stale"' not in dashboard_js

    # A stale series' history stops at its last good poll -- there's no
    # stored point at "now" for the dashed tail to extend to, so render()
    # must synthesize one. Caught only by manually rendering the chart in
    # a browser: without this, staleSinceTs equals the last real point's
    # timestamp, splitIdx lands on the final index, and no tail is drawn
    # at all.
    assert "Date.now()" in chart_js


def test_theme_toggle_reloads_the_selected_chart(tmp_path):
    # chart.js resolves colors from CSS custom properties once, at render
    # time -- toggling the theme without re-rendering leaves the big
    # chart and change bars showing the previous theme's colors until the
    # next 60s auto-refresh happens to fire.
    js = (dashboard.STATIC_DIR / "js" / "dashboard.js").read_text(encoding="utf-8")
    toggle_start = js.index('themeToggle.addEventListener("click"')
    toggle_body = js[toggle_start : toggle_start + 800]

    assert "loadChartFor(" in toggle_body


def test_change_bar_domain_extends_to_now_for_a_stale_series(tmp_path):
    # chart.render() synthesizes a (now, lastValue) tail point for a
    # stale series, extending the *line's* x-domain to now -- but the
    # bar strip's domain must match, or every bar maps too far right
    # (worst at the last one, which ends up drawn under the dashed "no
    # data" tail instead of at its own timestamp). Both the overview
    # (dashboard.js) and detail page (detail.js) share this bug shape.
    dashboard_js = (dashboard.STATIC_DIR / "js" / "dashboard.js").read_text(
        encoding="utf-8"
    )
    detail_js = (dashboard.STATIC_DIR / "js" / "detail.js").read_text(encoding="utf-8")

    assert "staleSinceTs != null ? Date.now()" in dashboard_js
    assert "staleSinceTs != null ? Date.now()" in detail_js


def test_load_chart_for_guards_against_out_of_order_responses(tmp_path):
    # Every selection change and every 60s auto-refresh fires a fetch;
    # without a sequencing guard, a slower response for an earlier
    # selection (a stale metric's history is exactly the expensive case)
    # can land after a faster response for a later one and render under
    # the wrong metric's header/stats/highlighted row.
    js = (dashboard.STATIC_DIR / "js" / "dashboard.js").read_text(encoding="utf-8")

    assert "chartRequestId" in js
    assert "requestId !== state.chartRequestId" in js


def test_change_bars_positioned_by_timestamp_not_array_index(tmp_path):
    # _bucket_changes omits empty buckets, so under real store-on-change
    # data (gaps between changes are the norm) evenly spacing N bars
    # across the strip by index would put a bar under the wrong point in
    # time relative to the line above it, which is drawn on a real time
    # axis. renderBars must derive each bar's x from bar.ts against a
    # domain, and both chart callers must pass one.
    chart_js = (dashboard.STATIC_DIR / "js" / "chart.js").read_text(encoding="utf-8")
    dashboard_js = (dashboard.STATIC_DIR / "js" / "dashboard.js").read_text(
        encoding="utf-8"
    )
    detail_js = (dashboard.STATIC_DIR / "js" / "detail.js").read_text(encoding="utf-8")

    assert "bar.ts - start" in chart_js
    assert "i * barWidth" not in chart_js
    assert "renderChangeBars(" in dashboard_js and "data.bars, {" in dashboard_js
    assert "renderChangeBars(" in detail_js and "data.bars, {" in detail_js


def test_detail_range_switch_does_a_full_reload(tmp_path):
    # detail.js only ever fetched /api/stats/history on a range switch,
    # which refreshes the chart and headline but never the server-rendered
    # stats row (OPEN/HIGH/AVG-DAY/BEST DAY) or the recorded-changes
    # table -- both are range-dependent and computed only in
    # dashboard.metric_detail, so after clicking e.g. 1D they'd silently
    # keep describing the page's original range. A full navigation keeps
    # the whole page consistent by construction instead of duplicating
    # that computation in JS a second time.
    js = (dashboard.STATIC_DIR / "js" / "detail.js").read_text(encoding="utf-8")
    set_range_start = js.index("function setRange(range)")
    set_range_body = js[set_range_start : set_range_start + 900]

    assert "window.location.href" in set_range_body


def test_index_renders_with_every_plugin_failing(tmp_path):
    client, db_path = client_for(tmp_path, plugins_config={"acme": {"enabled": True}})
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now - 2 * DAY)
    storage.record_sample(db_path, series_id, now - 2 * DAY, 10, DAY)
    run_id = storage.start_run(db_path, "acme", now)
    storage.finish_run(
        db_path, run_id, "error", "boom", samples_written=0, finished_at=now
    )

    response = client.get("/")

    assert response.status_code == 200


def test_index_sets_theme_before_first_paint(tmp_path):
    # The inline theme-setting script must be in <head>, ahead of <body>,
    # so a stored preference applies before the page paints instead of
    # flashing the dark default first.
    client, _ = client_for(tmp_path)

    response = client.get("/")

    head_start = response.text.index("<head>")
    head_end = response.text.index("</head>")
    theme_script_pos = response.text.index("ngu.theme")
    body_pos = response.text.index("<body>")

    assert head_start < theme_script_pos < head_end < body_pos


def test_index_has_no_external_asset_urls(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.get("/")

    assert "http://" not in response.text
    assert "https://github.com" not in response.text.replace(
        'href="https://github.com/DrewFerg11/numbers-go-up#running-it"', ""
    )
    # The SVG namespace URI ("http://www.w3.org/2000/svg") is a required
    # constant, not a fetched asset -- only look for things that would
    # actually cause a network request.
    for static_file in dashboard.STATIC_DIR.rglob("*"):
        if static_file.is_file():
            content = static_file.read_text(encoding="utf-8", errors="ignore")
            assert "@import" not in content
            assert "cdn." not in content
            assert 'fetch("http' not in content
            assert 'src="http' not in content
            assert 'href="http' not in content


def test_index_topbar_links_to_the_api_docs(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.get("/")

    assert '<a class="api-link" href="/docs"' in response.text


def test_index_escapes_a_malicious_label(tmp_path):
    # The page shell never inlines per-request metric data server-side --
    # everything comes from a client-side fetch of /api/stats/overview and
    # is inserted via textContent -- so a label containing <script> can
    # never reach the rendered HTML unescaped in the first place.
    client, db_path = client_for(tmp_path, plugins_config={"acme": {"enabled": True}})
    now = int(time.time())
    storage.get_or_create_series(
        db_path,
        "acme.widgets",
        "acme",
        "cumulative",
        "<script>alert(1)</script>",
        "unit",
        "icon",
        now,
    )

    response = client.get("/")

    assert "<script>alert(1)</script>" not in response.text


# --- /api/stats/overview -------------------------------------------------


def test_overview_invalid_range_422(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.get("/api/stats/overview?range=5Y")

    assert response.status_code == 422


def test_overview_defaults_to_1m(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.get("/api/stats/overview")

    assert response.status_code == 200
    assert response.json()["range"] == "1M"


def test_overview_each_range_bound(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now - 400 * DAY)
    storage.record_sample(db_path, series_id, now - 400 * DAY, 100, DAY)
    storage.record_sample(db_path, series_id, now - 10 * DAY, 150, DAY)
    storage.record_sample(db_path, series_id, now - 8 * HOUR, 160, DAY)
    storage.record_sample(db_path, series_id, now - 3 * HOUR, 170, DAY)
    storage.record_sample(db_path, series_id, now - 20 * 60, 180, DAY)
    storage.record_sample(db_path, series_id, now - 1, 200, DAY)

    # "open" carries forward the latest sample at or before each range's
    # window start, given the samples seeded above.
    expected_open = {
        "1H": 170,
        "6H": 160,
        "12H": 150,
        "1D": 150,
        "1W": 150,
        "1M": 100,
        "3M": 100,
        "1Y": 100,
    }

    for range_key in dashboard.RANGE_HOURS:
        response = client.get(f"/api/stats/overview?range={range_key}")
        assert response.status_code == 200
        metric = response.json()["metrics"][0]
        assert metric["value"] == 200
        assert metric["open"] == expected_open[range_key]

    response = client.get("/api/stats/overview?range=ALL")
    assert response.status_code == 200
    metric = response.json()["metrics"][0]
    assert metric["value"] == 200
    assert metric["open"] == 100


def test_overview_all_starts_at_first_sample(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now - 5 * DAY)
    storage.record_sample(db_path, series_id, now - 5 * DAY, 10, DAY)
    storage.record_sample(db_path, series_id, now, 20, DAY)

    response = client.get("/api/stats/overview?range=ALL")

    metric = response.json()["metrics"][0]
    assert metric["open"] == 10
    assert metric["value"] == 20
    assert metric["change"] == 10


def test_overview_series_younger_than_range_has_non_null_open(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now - HOUR)
    storage.record_sample(db_path, series_id, now - HOUR, 5, DAY)

    response = client.get("/api/stats/overview?range=1M")

    metric = response.json()["metrics"][0]
    assert metric["open"] == 5
    assert metric["change"] == 0


def test_overview_change_pct_null_when_open_is_zero(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now - 2 * DAY, kind="gauge")
    storage.record_sample(db_path, series_id, now - 2 * DAY, 0, DAY)
    storage.record_sample(db_path, series_id, now, 5, DAY)

    response = client.get("/api/stats/overview?range=1M")

    metric = response.json()["metrics"][0]
    assert metric["open"] == 0
    assert metric["change_pct"] is None


def test_overview_high_low(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now - 5 * DAY, kind="gauge")
    storage.record_sample(db_path, series_id, now - 5 * DAY, 10, DAY)
    storage.record_sample(db_path, series_id, now - 4 * DAY, 50, DAY)
    storage.record_sample(db_path, series_id, now - 3 * DAY, 5, DAY)
    storage.record_sample(db_path, series_id, now, 20, DAY)

    response = client.get("/api/stats/overview?range=1M")

    metric = response.json()["metrics"][0]
    assert metric["high"] == 50
    assert metric["low"] == 5


def test_overview_stale_since_is_the_plugins_last_successful_poll(tmp_path):
    # The chart's dashed "no data" tail boundary (#133): stale_since is
    # the plugin's last OK poll, not the series' last written sample --
    # a flat, store-on-change series can go most of a day between writes
    # while still polling successfully every interval.
    client, db_path = client_for(tmp_path, plugin_intervals={"acme": 1800})
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now - 5 * DAY)
    storage.record_sample(db_path, series_id, now - 5 * DAY, 10, DAY)

    run_id = storage.start_run(db_path, "acme", now - 60)
    storage.finish_run(
        db_path, run_id, "ok", None, samples_written=0, finished_at=now - 60
    )

    response = client.get("/api/stats/overview?range=1M")

    metric = response.json()["metrics"][0]
    assert metric["stale"] is False
    assert metric["stale_since"] != metric["updated"]


def test_overview_avg_per_day(tmp_path):
    # Matches the detail page's own avg_per_day formula: change over the
    # range divided by elapsed days, so the overview's stats row and the
    # detail page's stats row can't drift apart. 1M is exactly 30 days,
    # so a +20 change gives 20/30 = 0.666... -> 0.67.
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now - 30 * DAY)
    storage.record_sample(db_path, series_id, now - 30 * DAY, 0, DAY)
    storage.record_sample(db_path, series_id, now, 20, DAY)

    response = client.get("/api/stats/overview?range=1M")

    metric = response.json()["metrics"][0]
    assert metric["avg_per_day"] == 0.67


def test_overview_changes_counts_stored_samples_in_range(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now - 5 * DAY)
    storage.record_sample(db_path, series_id, now - 5 * DAY, 10, DAY)
    storage.record_sample(db_path, series_id, now - 4 * DAY, 20, DAY)
    storage.record_sample(db_path, series_id, now - 3 * DAY, 30, DAY)
    storage.record_sample(db_path, series_id, now, 40, DAY)

    response = client.get("/api/stats/overview?range=1M")

    metric = response.json()["metrics"][0]
    assert metric["changes"] == 3


def test_overview_spark_capped_and_bounds(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now - 90 * DAY)
    value = 1
    for day in range(90, -1, -1):
        storage.record_sample(db_path, series_id, now - day * DAY, value, DAY)
        value += 1

    response = client.get("/api/stats/overview?range=3M")

    metric = response.json()["metrics"][0]
    assert len(metric["spark"]) <= 60
    assert metric["spark"][0][1] == metric["open"]
    assert metric["spark"][-1][1] == metric["value"]


def test_overview_excludes_inactive_series(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now)
    storage.record_sample(db_path, series_id, now, 10, DAY)
    conn = storage.connect(db_path)
    try:
        conn.execute("UPDATE metric_series SET active = 0 WHERE id = ?", (series_id,))
        conn.commit()
    finally:
        conn.close()

    response = client.get("/api/stats/overview")

    assert response.json()["metrics"] == []


def test_overview_pattern_populated_for_pattern_keys(tmp_path):
    metrics = {
        "acme.model.{id}.downloads": {
            "kind": "cumulative",
            "label": "Downloads",
            "unit": "downloads",
        }
    }
    client, db_path = client_for(
        tmp_path,
        plugins_config={"acme": {"enabled": True}},
        plugin_metrics={"acme": metrics},
    )
    now = int(time.time())
    series_id = seed_series(db_path, "acme.model.42.downloads", now=now)
    storage.record_sample(db_path, series_id, now, 5, DAY)

    response = client.get("/api/stats/overview")

    metric = response.json()["metrics"][0]
    assert metric["pattern"] == "acme.model.{id}.downloads"


def test_overview_pattern_null_for_static_keys(tmp_path):
    metrics = {"acme.widgets": {"kind": "cumulative", "label": "Widgets", "unit": "u"}}
    client, db_path = client_for(
        tmp_path,
        plugins_config={"acme": {"enabled": True}},
        plugin_metrics={"acme": metrics},
    )
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now)
    storage.record_sample(db_path, series_id, now, 5, DAY)

    response = client.get("/api/stats/overview")

    metric = response.json()["metrics"][0]
    assert metric["pattern"] is None


def test_overview_invalid_range_returns_422_exact(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.get("/api/stats/overview?range=bogus")

    assert response.status_code == 422


# --- pinned ---------------------------------------------------------------


def test_overview_pinned_honoured_in_order_and_capped(tmp_path):
    keys = [f"acme.m{i}" for i in range(8)]
    client, db_path = client_for(
        tmp_path, dashboard_config={"pinned": list(reversed(keys))[:7]}
    )
    now = int(time.time())
    for key in keys:
        series_id = seed_series(db_path, key, now=now)
        storage.record_sample(db_path, series_id, now, 1, DAY)

    response = client.get("/api/stats/overview")

    pinned = response.json()["pinned"]
    assert pinned == list(reversed(keys))[:6]


def test_overview_pinned_unknown_key_skipped(tmp_path):
    client, db_path = client_for(
        tmp_path, dashboard_config={"pinned": ["acme.widgets", "ghost.nope"]}
    )
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now)
    storage.record_sample(db_path, series_id, now, 1, DAY)

    response = client.get("/api/stats/overview")

    assert response.json()["pinned"] == ["acme.widgets"]


def test_overview_pinned_empty_falls_back_to_default(tmp_path):
    client, db_path = client_for(tmp_path, dashboard_config={"pinned": []})
    now = int(time.time())
    for i in range(6):
        kind = "cumulative" if i < 5 else "gauge"
        series_id = seed_series(db_path, f"acme.m{i}", kind=kind, now=now)
        storage.record_sample(db_path, series_id, now, 1, DAY)

    response = client.get("/api/stats/overview")

    pinned = response.json()["pinned"]
    assert len(pinned) == 4
    assert all(key.startswith("acme.m") for key in pinned)


# --- folding: pattern field is what the client uses to fold -------------


def test_overview_mixed_static_and_pattern_series_have_distinct_pattern_field(tmp_path):
    metrics = {
        "acme.profile.downloads": {
            "kind": "cumulative",
            "label": "Downloads",
            "unit": "u",
        },
        "acme.model.{id}.downloads": {
            "kind": "cumulative",
            "label": "Model Downloads",
            "unit": "u",
        },
    }
    client, db_path = client_for(
        tmp_path,
        plugins_config={"acme": {"enabled": True}},
        plugin_metrics={"acme": metrics},
    )
    now = int(time.time())
    static_id = seed_series(db_path, "acme.profile.downloads", now=now)
    storage.record_sample(db_path, static_id, now, 5, DAY)
    pattern_id = seed_series(db_path, "acme.model.1.downloads", now=now)
    storage.record_sample(db_path, pattern_id, now, 5, DAY)

    response = client.get("/api/stats/overview")

    by_key = {m["key"]: m for m in response.json()["metrics"]}
    assert by_key["acme.profile.downloads"]["pattern"] is None
    assert by_key["acme.model.1.downloads"]["pattern"] == "acme.model.{id}.downloads"


# --- status dots -----------------------------------------------------------


def test_overview_plugins_status_matches_health_thresholds(tmp_path):
    client, db_path = client_for(tmp_path, plugins_config={"acme": {"enabled": True}})
    now = int(time.time())
    run_id = storage.start_run(db_path, "acme", now)
    storage.finish_run(db_path, run_id, "ok", None, samples_written=1, finished_at=now)

    response = client.get("/api/stats/overview")

    plugin = next(p for p in response.json()["plugins"] if p["name"] == "acme")
    assert plugin["status"] == "ok"
    assert plugin["consecutive_failures"] == 0


# --- recent_changes on the overview --------------------------------------


def test_overview_recent_changes_excludes_zero_change_heartbeats(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now - DAY)
    storage.record_sample(db_path, series_id, now - DAY, 5, HOUR)
    storage.record_sample(db_path, series_id, now - HOUR, 5, HOUR)
    storage.record_sample(db_path, series_id, now, 9, HOUR)

    response = client.get("/api/stats/overview")

    changes = response.json()["recent_changes"]
    assert len(changes) == 1
    assert changes[0]["key"] == "acme.widgets"
    assert changes[0]["change"] == 4


def test_overview_recent_changes_respects_limit_and_order(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now - 20 * HOUR)
    for i in range(15):
        storage.record_sample(db_path, series_id, now - (14 - i) * HOUR, i + 1, 1)

    response = client.get("/api/stats/overview")

    changes = response.json()["recent_changes"]
    assert len(changes) == 10
    assert changes == sorted(changes, key=lambda c: c["ts"], reverse=True)


# --- /m/{metric_key} -------------------------------------------------------


def test_detail_unknown_key_404(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.get("/m/nope.does.not.exist")

    assert response.status_code == 404


def test_detail_known_key_renders(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now - 5 * DAY)
    storage.record_sample(db_path, series_id, now - 5 * DAY, 10, DAY)
    storage.record_sample(db_path, series_id, now, 20, DAY)

    response = client.get("/m/acme.widgets")

    assert response.status_code == 200
    assert "acme.widgets" in response.text


def test_detail_topbar_links_to_the_api_docs(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now)
    storage.record_sample(db_path, series_id, now, 10, DAY)

    response = client.get("/m/acme.widgets")

    assert '<a class="api-link" href="/docs"' in response.text


def test_detail_inactive_series_renders_with_chip(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.retired", now=now - DAY)
    storage.record_sample(db_path, series_id, now - DAY, 5, DAY)
    conn = storage.connect(db_path)
    try:
        conn.execute("UPDATE metric_series SET active = 0 WHERE id = ?", (series_id,))
        conn.commit()
    finally:
        conn.close()

    response = client.get("/m/acme.retired")

    assert response.status_code == 200
    assert "INACTIVE" in response.text


def test_detail_exposes_stale_and_updated_to_the_client(tmp_path):
    # detail.js reads these to draw the big chart's dashed grey
    # continuation after the series' last good poll (chart.js
    # staleSinceTs) -- without them the client has no way to know when a
    # stale series' data actually stopped being current.
    client, db_path = client_for(tmp_path, plugins_config={"acme": {"enabled": True}})
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now - 2 * DAY)
    storage.record_sample(db_path, series_id, now - 2 * DAY, 10, DAY)
    run_id = storage.start_run(db_path, "acme", now)
    storage.finish_run(
        db_path, run_id, "error", "boom", samples_written=0, finished_at=now
    )

    response = client.get("/m/acme.widgets")

    assert 'data-stale="true"' in response.text
    assert 'data-updated="' in response.text
    # No successful poll at all -> stale_since is empty, distinct from the
    # sample-age-based `updated` the old rule used for the chart tail.
    assert 'data-stale-since=""' in response.text


def test_detail_stale_since_is_the_plugins_last_successful_poll(tmp_path):
    client, db_path = client_for(tmp_path, plugin_intervals={"acme": 1800})
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now - 2 * DAY)
    storage.record_sample(db_path, series_id, now - 2 * DAY, 10, DAY)
    run_id = storage.start_run(db_path, "acme", now - 60)
    storage.finish_run(
        db_path, run_id, "ok", None, samples_written=0, finished_at=now - 60
    )

    response = client.get("/m/acme.widgets")

    assert 'data-stale="false"' in response.text
    assert 'data-stale-since="' in response.text
    assert 'data-stale-since=""' not in response.text


def test_detail_recorded_change_renders_full_precision(tmp_path):
    # Jinja's `|format("%d")` on a float truncates it (1.37 -> "1"), so a
    # fractional recorded change used to lose its fractional part on the
    # page even though the value column next to it kept full precision.
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", kind="gauge", now=now - DAY)
    storage.record_sample(db_path, series_id, now - DAY, 1.5, DAY)
    storage.record_sample(db_path, series_id, now, 2.87, DAY)

    response = client.get("/m/acme.widgets")

    assert "+1.37" in response.text
    assert "+1<" not in response.text


def test_detail_recorded_change_rounds_away_float_noise(tmp_path):
    # value - LAG(value) over floats can produce IEEE-754 noise for a
    # value that's really a round number: 10.1 - 10.0 == 0.09999999999999964
    # in Python. That must not print verbatim (it printed fine for the
    # value column, which is stored/read directly, but the *change* is
    # itself a float subtraction and had no rounding pass).
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", kind="gauge", now=now - DAY)
    storage.record_sample(db_path, series_id, now - DAY, 10.0, DAY)
    storage.record_sample(db_path, series_id, now, 10.1, DAY)

    response = client.get("/m/acme.widgets")

    assert "+0.1" in response.text
    assert "0.09999999999999964" not in response.text


def test_detail_https_url_rendered_as_link(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    storage.get_or_create_series(
        db_path,
        "acme.widgets",
        "acme",
        "cumulative",
        "Widgets",
        "u",
        "i",
        now,
        attrs={"url": "https://example.com/widgets"},
    )
    storage.record_sample(
        db_path,
        storage.get_series_by_key(db_path, "acme.widgets")["id"],
        now,
        5,
        DAY,
    )

    response = client.get("/m/acme.widgets")

    assert 'href="https://example.com/widgets"' in response.text


def test_detail_javascript_url_not_rendered_as_link(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    storage.get_or_create_series(
        db_path,
        "acme.widgets",
        "acme",
        "cumulative",
        "Widgets",
        "u",
        "i",
        now,
        attrs={"url": "javascript:alert(1)"},
    )
    storage.record_sample(
        db_path,
        storage.get_series_by_key(db_path, "acme.widgets")["id"],
        now,
        5,
        DAY,
    )

    response = client.get("/m/acme.widgets")

    assert "javascript:" not in response.text


def test_detail_http_url_not_rendered_as_link(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    storage.get_or_create_series(
        db_path,
        "acme.widgets",
        "acme",
        "cumulative",
        "Widgets",
        "u",
        "i",
        now,
        attrs={"url": "http://example.com/widgets"},
    )
    storage.record_sample(
        db_path,
        storage.get_series_by_key(db_path, "acme.widgets")["id"],
        now,
        5,
        DAY,
    )

    response = client.get("/m/acme.widgets")

    assert 'href="http://example.com/widgets"' not in response.text


def test_detail_breadcrumb_group_for_pattern_series(tmp_path):
    metrics = {
        "acme.model.{id}.downloads": {
            "kind": "cumulative",
            "label": "Downloads",
            "unit": "u",
        }
    }
    client, db_path = client_for(
        tmp_path,
        plugins_config={"acme": {"enabled": True}},
        plugin_metrics={"acme": metrics},
    )
    now = int(time.time())
    series_id = seed_series(db_path, "acme.model.1.downloads", now=now)
    storage.record_sample(db_path, series_id, now, 5, DAY)

    response = client.get("/m/acme.model.1.downloads")

    assert response.status_code == 200
    assert "Models" in response.text
