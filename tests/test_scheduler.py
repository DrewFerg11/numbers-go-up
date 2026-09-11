import sqlite3
from pathlib import Path
from types import ModuleType

import pytest

from numbers_go_up import migrate, plugins, scheduler, storage

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "plugins"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "stats.db"
    migrate.run_migrations(path)
    return path


def _sample_count(db_path, series_id):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM samples WHERE series_id = ?", (series_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _load_fixture_plugin(
    filename: str, config: dict | None = None
) -> plugins.LoadedPlugin:
    module = plugins.load_plugin_from_path(FIXTURES_DIR / filename)
    return plugins.LoadedPlugin(
        name=Path(filename).stem.lstrip("_"),
        module=module,
        metrics=module.METRICS,
        interval_seconds=module.POLL_INTERVAL_SECONDS,
        config=config or {},
        source="user",
    )


def _plugin_from_module(
    name: str, module: ModuleType, metrics: dict
) -> plugins.LoadedPlugin:
    return plugins.LoadedPlugin(
        name=name,
        module=module,
        metrics=metrics,
        interval_seconds=300,
        config={},
        source="user",
    )


class TestRunPluginOnceHappyPath:
    def test_fake_incrementing_plugin_produces_expected_sample_count_after_n_ticks(
        self, db_path
    ):
        plugin = _load_fixture_plugin("_fake_incrementing.py")

        for tick in range(5):
            result = scheduler.run_plugin_once(
                db_path,
                plugin,
                http=None,
                now=1000 + tick * 300,
                heartbeat_seconds=86400,
            )
            assert result.status == "ok"

        series_id = storage.get_or_create_series(
            db_path,
            "fake_incrementing.demo.count",
            "fake_incrementing",
            "cumulative",
            "",
            "",
            "",
            0,
        )
        assert _sample_count(db_path, series_id) == 5

    def test_fake_constant_plugin_produces_one_sample_not_n(self, db_path):
        plugin = _load_fixture_plugin("_fake_constant.py")

        for tick in range(5):
            scheduler.run_plugin_once(
                db_path,
                plugin,
                http=None,
                now=1000 + tick * 300,
                heartbeat_seconds=86400,
            )

        series_id = storage.get_or_create_series(
            db_path, "fake_constant.demo.value", "fake_constant", "gauge", "", "", "", 0
        )
        assert _sample_count(db_path, series_id) == 1

    def test_constant_plugin_writes_again_once_heartbeat_is_crossed(self, db_path):
        plugin = _load_fixture_plugin("_fake_constant.py")
        heartbeat_seconds = 86400

        scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=heartbeat_seconds
        )
        result = scheduler.run_plugin_once(
            db_path,
            plugin,
            http=None,
            now=1000 + heartbeat_seconds,
            heartbeat_seconds=heartbeat_seconds,
        )

        assert result.samples_written == 1

    def test_samples_written_matches_rows_actually_written(self, db_path):
        plugin = _load_fixture_plugin("_fake_incrementing.py")

        result = scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
        )

        assert result.samples_written == 1


class TestRunPluginOnceFailureIsolation:
    def test_fake_raises_records_error_with_bounded_traceback_tail(self, db_path):
        plugin = _load_fixture_plugin("_fake_raises.py")

        result = scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
        )

        assert result.status == "error"
        assert len(result.error) <= 500
        assert "simulated plugin failure" in result.error

    def test_a_raising_plugin_does_not_block_another_plugins_run(self, db_path):
        raising = _load_fixture_plugin("_fake_raises.py")
        other = _load_fixture_plugin("_fake_constant.py")

        scheduler.run_plugin_once(
            db_path, raising, http=None, now=1000, heartbeat_seconds=86400
        )
        result = scheduler.run_plugin_once(
            db_path, other, http=None, now=1000, heartbeat_seconds=86400
        )

        assert result.status == "ok"

    def test_keyboardinterrupt_is_not_swallowed(self, db_path):
        module = ModuleType("raises_signal")
        module.METRICS = {
            "raises_signal.x": {"kind": "gauge", "label": "X", "unit": ""}
        }

        def collect(config, http):
            raise KeyboardInterrupt

        module.collect = collect
        plugin = _plugin_from_module("raises_signal", module, module.METRICS)

        with pytest.raises(KeyboardInterrupt):
            scheduler.run_plugin_once(
                db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
            )

        # The in-progress run row is never finished, so it still reads as
        # a failure -- per #14's convention, not a new one.
        assert storage.consecutive_failures(db_path, "raises_signal") == 1

    def test_systemexit_is_not_swallowed(self, db_path):
        module = ModuleType("raises_signal")
        module.METRICS = {
            "raises_signal.x": {"kind": "gauge", "label": "X", "unit": ""}
        }

        def collect(config, http):
            raise SystemExit

        module.collect = collect
        plugin = _plugin_from_module("raises_signal", module, module.METRICS)

        with pytest.raises(SystemExit):
            scheduler.run_plugin_once(
                db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
            )


class TestRunPluginOnceValidation:
    def test_bool_value_is_rejected(self, db_path):
        module = ModuleType("bool_plugin")
        module.METRICS = {
            "bool_plugin.flag": {"kind": "gauge", "label": "Flag", "unit": ""}
        }

        def collect(config, http):
            return {"bool_plugin.flag": True}

        module.collect = collect
        plugin = _plugin_from_module("bool_plugin", module, module.METRICS)

        result = scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
        )

        assert result.status == "error"
        assert result.samples_written == 0
        assert "bool_plugin.flag" in result.error

    def test_non_numeric_value_is_rejected(self, db_path):
        module = ModuleType("string_plugin")
        module.METRICS = {
            "string_plugin.value": {"kind": "gauge", "label": "Value", "unit": ""}
        }

        def collect(config, http):
            return {"string_plugin.value": "not a number"}

        module.collect = collect
        plugin = _plugin_from_module("string_plugin", module, module.METRICS)

        result = scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
        )

        assert result.status == "error"
        assert result.samples_written == 0

    def test_partial_bad_result_writes_valid_metrics_and_marks_run_error(self, db_path):
        module = ModuleType("partial")
        module.METRICS = {
            "partial.good.count": {"kind": "gauge", "label": "Good", "unit": ""},
            "partial.bad.count": {"kind": "gauge", "label": "Bad", "unit": ""},
        }

        def collect(config, http):
            return {
                "partial.good.count": 5,
                "partial.bad.count": "not a number",
                "partial.undeclared.count": 1,
            }

        module.collect = collect
        plugin = _plugin_from_module("partial", module, module.METRICS)

        result = scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
        )

        assert result.status == "error"
        assert result.samples_written == 1
        assert "partial.bad.count" in result.error
        assert "partial.undeclared.count" in result.error


class TestConsecutiveFailures:
    def test_increments_across_failures_and_resets_on_success(self, db_path):
        raising = _load_fixture_plugin("_fake_raises.py")
        ok = _load_fixture_plugin("_fake_constant.py")

        scheduler.run_plugin_once(
            db_path, raising, http=None, now=1000, heartbeat_seconds=86400
        )
        assert storage.consecutive_failures(db_path, "fake_raises") == 1

        scheduler.run_plugin_once(
            db_path, raising, http=None, now=1300, heartbeat_seconds=86400
        )
        assert storage.consecutive_failures(db_path, "fake_raises") == 2

        # A different plugin's runs must not affect this count.
        scheduler.run_plugin_once(
            db_path, ok, http=None, now=1000, heartbeat_seconds=86400
        )
        assert storage.consecutive_failures(db_path, "fake_raises") == 2

    def test_unknown_plugin_has_zero_consecutive_failures(self, db_path):
        assert storage.consecutive_failures(db_path, "never-ran") == 0
