import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from numbers_go_up import migrate, queries, storage


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "stats.db"
    migrate.run_migrations(path)
    return str(path)


# --- iso -------------------------------------------------------------------


def test_iso_formats_utc_with_z_suffix():
    assert queries.iso(0) == "1970-01-01T00:00:00Z"


def test_iso_none_is_none():
    assert queries.iso(None) is None


# --- parse_attrs -------------------------------------------------------------


def test_parse_attrs_empty_or_none_is_empty_dict():
    assert queries.parse_attrs(None) == {}
    assert queries.parse_attrs("") == {}


def test_parse_attrs_valid_object():
    assert queries.parse_attrs('{"url": "https://x"}') == {"url": "https://x"}


def test_parse_attrs_invalid_json_degrades_to_empty_dict():
    assert queries.parse_attrs("{not json") == {}


def test_parse_attrs_valid_json_non_object_degrades_to_empty_dict():
    assert queries.parse_attrs("[1, 2]") == {}
    assert queries.parse_attrs("5") == {}


# --- is_stale (#133) ---------------------------------------------------------


def test_is_stale_uses_shared_last_ok_runs_dict(db_path):
    """is_stale takes storage.last_ok_runs()'s result directly rather than
    querying per call -- one grouped query shared across every series in a
    request (stats_latest/build_overview), not one per series.
    """
    now = int(time.time())

    run_id = storage.start_run(db_path, "acme", now)
    storage.finish_run(db_path, run_id, "ok", None, samples_written=0, finished_at=now)

    last_ok = storage.last_ok_runs(db_path)
    assert last_ok == {"acme": now}

    # Recent success -> not stale; a plugin absent from the dict (never
    # succeeded) -> stale; the same dict answers both without a query.
    assert queries.is_stale("acme", 1800, now, last_ok) is False
    assert queries.is_stale("other", 1800, now, last_ok) is True

    # Well past 3x the interval since that same last success -> stale.
    far_future = now + 4 * 1800 + 10
    assert queries.is_stale("acme", 1800, far_future, last_ok) is True


# --- range_start ---------------------------------------------------------


def test_range_start_named_range_subtracts_hours():
    now = 1_000_000
    assert queries.range_start("1D", now, earliest=999) == now - 24 * 3600


def test_range_start_all_uses_given_earliest():
    assert queries.range_start("ALL", 1_000_000, earliest=42) == 42


def test_range_start_all_with_no_earliest_falls_back_to_now():
    assert queries.range_start("ALL", 1_000_000) == 1_000_000


def test_range_start_all_earliest_zero_is_honoured_not_treated_as_falsy():
    # 0 is a legitimate epoch timestamp, not "no earliest given" -- only
    # None should trigger the now-fallback.
    assert queries.range_start("ALL", 1_000_000, earliest=0) == 0


# --- summarize ---------------------------------------------------------


def test_summarize_basic_change_and_avg_per_day():
    result = queries.summarize(
        open_value=0, value=20, high=20, low=0, start=0, now=30 * 86400
    )
    assert result["change"] == 20
    assert result["change_pct"] is None  # open_value == 0
    assert result["high"] == 20
    assert result["low"] == 0
    assert result["avg_per_day"] == pytest.approx(20 / 30, abs=0.01)


def test_summarize_none_open_falls_back_to_value():
    result = queries.summarize(
        open_value=None, value=5, high=None, low=None, start=0, now=86400
    )
    assert result["change"] == 0
    assert result["high"] == 5
    assert result["low"] == 5


def test_summarize_high_low_clipped_against_current_value():
    # A series whose current value is outside the window's own high/low
    # (e.g. it just jumped) still reports a high/low that includes it.
    result = queries.summarize(
        open_value=10, value=100, high=50, low=5, start=0, now=86400
    )
    assert result["high"] == 100
    assert result["low"] == 5


def test_summarize_change_pct_rounds_to_two_places():
    result = queries.summarize(open_value=3, value=4, high=4, low=3, start=0, now=86400)
    assert result["change_pct"] == round((1 / 3) * 100, 2)


def test_summarize_span_days_floors_at_one():
    # A window under a day must not inflate avg_per_day by dividing by a
    # fraction of a day.
    result = queries.summarize(
        open_value=0, value=12, high=12, low=0, start=0, now=3600
    )
    assert result["avg_per_day"] == 12


# --- plugin_statuses ---------------------------------------------------------


def make_app(db_path, plugin_names=None, plugins_config=None):
    app = FastAPI()
    app.state.config = {
        "storage": {"path": db_path},
        "plugins": plugins_config or {},
    }
    app.state.plugin_names = plugin_names or []

    @app.get("/probe")
    def probe(request: Request):
        return {"plugins": queries.plugin_statuses(request)}

    return app


def test_plugin_statuses_disabled_plugin(db_path):
    app = make_app(db_path, plugin_names=["acme"], plugins_config={})
    client = TestClient(app)

    response = client.get("/probe")

    assert response.json()["plugins"] == [
        {
            "name": "acme",
            "status": "disabled",
            "enabled": False,
            "last_poll": None,
            "next_poll": None,
            "consecutive_failures": 0,
            "last_error": None,
            "metrics": [],
        }
    ]


def test_plugin_statuses_pending_when_never_polled(db_path):
    app = make_app(
        db_path,
        plugin_names=["acme"],
        plugins_config={"acme": {"enabled": True}},
    )
    client = TestClient(app)

    response = client.get("/probe")

    assert response.json()["plugins"][0]["status"] == "pending"
