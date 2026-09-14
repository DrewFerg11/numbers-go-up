import json
import logging
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import httpx
import pytest
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from numbers_go_up import migrate, plugins, scheduler, storage
from numbers_go_up.http import BLOCKED_ERROR_PREFIX, Blocked, RateLimited

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


def _scheduler_config(tmp_path, plugins_config=None, jitter_fraction=0.2, db_path=None):
    db_path = db_path or (tmp_path / "stats.db")
    migrate.run_migrations(db_path)
    return {
        "storage": {"path": str(db_path), "heartbeat_seconds": 86400},
        "poll": {"default_interval": 1800, "jitter_fraction": jitter_fraction},
        "plugins": plugins_config or {},
        "plugin_dir": str(FIXTURES_DIR),
    }


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

    def test_error_is_capped_however_long_the_traceback_is(self, db_path):
        # A real traceback's length depends on the filesystem path it was
        # raised from: a long Windows temp path blows past the cap where
        # CI's short Linux paths don't, which is how #42 stayed hidden.
        # Force the issue instead of trusting the environment.
        module = ModuleType("raises_long")
        module.POLL_INTERVAL_SECONDS = 300
        module.METRICS = {}

        def collect(config, http):
            raise RuntimeError("head " + "x" * 5000 + " END_OF_MESSAGE")

        module.collect = collect
        plugin = _plugin_from_module("raises_long", module, {})

        result = scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
        )

        assert result.status == "error"
        assert len(result.error) <= 500
        # the tail is kept, so the exception itself survives the cap
        assert result.error.rstrip().endswith("END_OF_MESSAGE")

        conn = sqlite3.connect(str(db_path))
        try:
            stored = conn.execute(
                "SELECT error FROM plugin_runs WHERE id = ?", (result.run_id,)
            ).fetchone()[0]
        finally:
            conn.close()

        # what the caller keeps and what the database holds are the same text
        assert stored == result.error

    def test_contract_violation_error_is_also_capped(self, db_path):
        module = ModuleType("many_violations")
        module.POLL_INTERVAL_SECONDS = 300
        module.METRICS = {}

        def collect(config, http):
            return {f"undeclared.key.number_{i}": 1 for i in range(200)}

        module.collect = collect
        plugin = _plugin_from_module("many_violations", module, {})

        result = scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
        )

        assert result.status == "error"
        assert len(result.error) <= 500

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

    def test_resets_to_zero_after_a_success(self, db_path):
        # The reset branch of consecutive_failures ("else: break" on the
        # newest 'ok' row) is not exercised anywhere else in this PR:
        # every run in the test above is an error for fake_raises, so the
        # break never executes and a regression counting ALL errors
        # instead of consecutive ones would pass silently. Seed an 'ok'
        # run directly and prove the reset.
        run_id = storage.start_run(db_path, "fake_raises", 1600)
        storage.finish_run(
            db_path, run_id, "ok", None, samples_written=1, finished_at=1600
        )

        assert storage.consecutive_failures(db_path, "fake_raises") == 0

    def test_unknown_plugin_has_zero_consecutive_failures(self, db_path):
        assert storage.consecutive_failures(db_path, "never-ran") == 0


class TestReviewFixes:
    def test_nan_value_is_rejected_and_run_marked_error(self, db_path):
        # A NaN passes isinstance(value, int | float), and sqlite3 binds it
        # as NULL, which violates samples.value REAL NOT NULL.
        module = ModuleType("nan_plugin")
        module.METRICS = {
            "nan_plugin.value": {"kind": "gauge", "label": "Value", "unit": ""}
        }

        def collect(config, http):
            return {"nan_plugin.value": float("nan")}

        module.collect = collect
        plugin = _plugin_from_module("nan_plugin", module, module.METRICS)

        result = scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
        )

        assert result.status == "error"
        assert result.samples_written == 0
        assert "nan_plugin.value" in result.error

    def test_infinite_value_is_rejected_and_run_marked_error(self, db_path):
        module = ModuleType("inf_plugin")
        module.METRICS = {
            "inf_plugin.value": {"kind": "gauge", "label": "Value", "unit": ""}
        }

        def collect(config, http):
            return {"inf_plugin.value": float("inf")}

        module.collect = collect
        plugin = _plugin_from_module("inf_plugin", module, module.METRICS)

        result = scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
        )

        assert result.status == "error"
        assert result.samples_written == 0
        assert "inf_plugin.value" in result.error

    def test_non_dict_result_does_not_crash_the_service(self, db_path):
        module = ModuleType("list_plugin")
        module.METRICS = {
            "list_plugin.value": {"kind": "gauge", "label": "Value", "unit": ""}
        }

        def collect(config, http):
            return ["not", "a", "dict"]

        module.collect = collect
        plugin = _plugin_from_module("list_plugin", module, module.METRICS)

        result = scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
        )

        assert result.status == "error"
        assert result.samples_written == 0
        assert result.error is not None
        assert "AttributeError" in result.error

    def test_none_result_does_not_crash_the_service(self, db_path):
        module = ModuleType("none_plugin")
        module.METRICS = {
            "none_plugin.value": {"kind": "gauge", "label": "Value", "unit": ""}
        }

        def collect(config, http):
            return None

        module.collect = collect
        plugin = _plugin_from_module("none_plugin", module, module.METRICS)

        result = scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
        )

        assert result.status == "error"
        assert result.samples_written == 0

    def test_storage_failure_mid_loop_is_contained_and_samples_stay_honest(
        self, db_path
    ):
        module = ModuleType("flaky_storage")
        module.METRICS = {
            "flaky_storage.a": {"kind": "gauge", "label": "A", "unit": ""},
            "flaky_storage.b": {"kind": "gauge", "label": "B", "unit": ""},
        }
        order = iter(["a", "b"])

        real_get_or_create = storage.get_or_create_series
        real_record_sample = storage.record_sample

        def flaky_record_sample(db, series_id, ts, value, heartbeat_seconds):
            # The first write succeeds; the second blows up, simulating
            # sqlite contention (busy_timeout expiry) or a full disk.
            if next(order) == "b":
                raise sqlite3.OperationalError("database is locked")
            return real_record_sample(db, series_id, ts, value, heartbeat_seconds)

        def collect(config, http):
            return {"flaky_storage.a": 1, "flaky_storage.b": 2}

        module.collect = collect
        plugin = _plugin_from_module("flaky_storage", module, module.METRICS)

        with (
            patch.object(storage, "record_sample", flaky_record_sample),
            patch.object(storage, "get_or_create_series", real_get_or_create),
        ):
            result = scheduler.run_plugin_once(
                db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
            )

        # The run is finished and marked error...
        assert result.status == "error"
        assert "database is locked" in result.error
        # ...and samples_written is honest: the row written before the
        # crash is still counted.
        assert result.samples_written == 1

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT status, error, samples_written, finished_at "
                "FROM plugin_runs WHERE id = ?",
                (result.run_id,),
            ).fetchone()
            samples = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        finally:
            conn.close()

        assert row[0] == "error"
        assert row[1] is not None
        assert row[2] == 1
        assert row[3] == 1000  # finished_at
        assert samples == 1

    def test_dict_like_result_is_iterated_normally(self, db_path):
        class Mapping:
            def items(self):
                return iter({"dictlike.value": 7}.items())

        module = ModuleType("dictlike_plugin")
        module.METRICS = {
            "dictlike.value": {"kind": "gauge", "label": "Value", "unit": ""}
        }

        def collect(config, http):
            return Mapping()

        module.collect = collect
        plugin = _plugin_from_module("dictlike", module, module.METRICS)

        result = scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
        )

        # No isinstance(result, dict) check was added: anything iterable
        # via .items() still works.
        assert result.status == "ok"
        assert result.samples_written == 1


class TestBuildScheduler:
    def _config(self, tmp_path, plugins_config=None, jitter_fraction=0.2):
        db_path = tmp_path / "stats.db"
        migrate.run_migrations(db_path)
        return {
            "storage": {"path": str(db_path), "heartbeat_seconds": 86400},
            "poll": {"default_interval": 1800, "jitter_fraction": jitter_fraction},
            "plugins": plugins_config or {},
            "plugin_dir": str(FIXTURES_DIR),
        }

    def test_zero_plugins_enabled_has_no_jobs(self, tmp_path):
        job_scheduler = scheduler.build_scheduler(self._config(tmp_path))

        assert job_scheduler.get_jobs() == []

    def test_only_enabled_plugins_get_jobs(self, tmp_path):
        config = self._config(tmp_path, plugins_config={"valid": {"enabled": True}})

        job_scheduler = scheduler.build_scheduler(config)

        assert [job.id for job in job_scheduler.get_jobs()] == ["plugin:valid"]

    def test_job_has_max_instances_1_and_coalesce_true(self, tmp_path):
        config = self._config(tmp_path, plugins_config={"valid": {"enabled": True}})

        job_scheduler = scheduler.build_scheduler(config)

        job = job_scheduler.get_job("plugin:valid")
        assert job.max_instances == 1
        assert job.coalesce is True

    def test_job_interval_matches_the_plugins_resolved_seconds(self, tmp_path):
        config = self._config(
            tmp_path, plugins_config={"valid": {"enabled": True, "poll_interval": 900}}
        )

        job_scheduler = scheduler.build_scheduler(config)

        job = job_scheduler.get_job("plugin:valid")
        assert job.trigger.interval.total_seconds() == 900

    def test_first_run_happens_within_the_random_delay_window(self, tmp_path):
        config = self._config(tmp_path, plugins_config={"valid": {"enabled": True}})
        before = datetime.now(UTC)

        job_scheduler = scheduler.build_scheduler(config)

        after = datetime.now(UTC)
        job = job_scheduler.get_job("plugin:valid")
        next_run = job.next_run_time.astimezone(UTC)

        assert (
            before
            <= next_run
            <= after + timedelta(seconds=scheduler.FIRST_RUN_MAX_DELAY_SECONDS)
        )


class TestMisfireGrace:
    def test_job_is_created_with_misfire_grace_time_none(self, tmp_path):
        config = _scheduler_config(
            tmp_path, plugins_config={"valid": {"enabled": True}}
        )

        job_scheduler = scheduler.build_scheduler(config)

        job = job_scheduler.get_job("plugin:valid")
        assert job.misfire_grace_time is None

    def test_a_job_submitted_late_to_a_busy_executor_still_runs(self, tmp_path):
        # Without misfire_grace_time=None, APScheduler's 1-second default
        # discards a run submitted late to a backed-up executor: the plugin
        # would silently miss polls with no plugin_runs row. Block the
        # single worker with a slow job, let the other plugin's fire time
        # pass while the worker is busy, and prove collect() still runs.
        module = ModuleType("late_sleeper")
        module.METRICS = {"late_sleeper.x": {"kind": "gauge", "label": "X", "unit": ""}}

        def slow_collect(config, http):
            time.sleep(3)

        module.collect = slow_collect

        late_ran = threading.Event()

        late_module = ModuleType("late_plugin")
        late_module.METRICS = {
            "late_plugin.x": {"kind": "gauge", "label": "X", "unit": ""}
        }

        def late_collect(config, http):
            late_ran.set()
            return {"late_plugin.x": 1}

        late_module.collect = late_collect

        slow_plugin = plugins.LoadedPlugin(
            name="late_sleeper",
            module=module,
            metrics=module.METRICS,
            interval_seconds=3600,
            config={},
            source="user",
        )
        late_plugin = plugins.LoadedPlugin(
            name="late_plugin",
            module=late_module,
            metrics=late_module.METRICS,
            interval_seconds=3600,
            config={},
            source="user",
        )

        config = _scheduler_config(tmp_path)
        db_path = Path(config["storage"]["path"])

        job_scheduler = BackgroundScheduler(
            executors={"default": ThreadPoolExecutor(1)}
        )
        backoff_state: dict[str, int] = {}
        for job_plugin, run_time in ((slow_plugin, 0.0), (late_plugin, 0.5)):
            job_id = f"plugin:{job_plugin.name}"
            job_scheduler.add_job(
                scheduler._run_scheduled_plugin,
                trigger="interval",
                seconds=3600,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=None,
                next_run_time=datetime.now(UTC) + timedelta(seconds=run_time),
                id=job_id,
                args=[
                    job_scheduler,
                    job_id,
                    backoff_state,
                    db_path,
                    job_plugin,
                    None,
                    86400,
                ],
            )
        job_scheduler.start()

        try:
            # late_plugin's poll is submitted while late_sleeper still owns
            # the worker (0.5s into its 3s collect()); with the old default
            # grace time of 1s it would be discarded instead of run late.
            assert late_ran.wait(timeout=5.0), (
                "late-submitted job was discarded instead of running"
            )
            # late_ran fires inside collect(), before finish_run() commits,
            # so wait for the run row to flip from the in-progress failure
            # sentinel to 'ok' before asserting on the ledger.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if storage.consecutive_failures(db_path, "late_plugin") == 0:
                    break
                time.sleep(0.05)
        finally:
            job_scheduler.shutdown(wait=False)

        assert storage.consecutive_failures(db_path, "late_plugin") == 0, (
            "late_plugin's late run should have finished 'ok'"
        )


class TestJitterFraction:
    def test_quoted_jitter_fraction_raises_config_error_at_build_time(self, tmp_path):
        # A quoted "0.2" passes through as a str and previously reached
        # IntervalTrigger unvalidated: interval_seconds * "0.2" makes a
        # ~540-character string (str * int repetition, no TypeError), and
        # random.uniform(0, <str>) raises inside the scheduler's main loop
        # at the first fire -- every plugin stops polling while /health
        # keeps serving 200. Now it fails startup with ConfigError.
        config = _scheduler_config(
            tmp_path, plugins_config={"valid": {"enabled": True}}, jitter_fraction="0.2"
        )

        with pytest.raises(scheduler.ConfigError):
            scheduler.build_scheduler(config)

    def test_negative_jitter_fraction_raises_config_error(self, tmp_path):
        # Negative jitter would fire polls EARLY (uniform(0, -900)).
        config = _scheduler_config(
            tmp_path, plugins_config={"valid": {"enabled": True}}, jitter_fraction=-0.5
        )

        with pytest.raises(scheduler.ConfigError):
            scheduler.build_scheduler(config)

    def test_jitter_fraction_of_one_raises_config_error(self, tmp_path):
        # 1.0 is excluded: jitter must not reach or exceed the interval.
        config = _scheduler_config(
            tmp_path, plugins_config={"valid": {"enabled": True}}, jitter_fraction=1.0
        )

        with pytest.raises(scheduler.ConfigError):
            scheduler.build_scheduler(config)

    def test_jitter_fraction_above_one_raises_config_error(self, tmp_path):
        config = _scheduler_config(
            tmp_path, plugins_config={"valid": {"enabled": True}}, jitter_fraction=1.5
        )

        with pytest.raises(scheduler.ConfigError):
            scheduler.build_scheduler(config)

    def test_nan_jitter_fraction_raises_config_error(self, tmp_path):
        config = _scheduler_config(
            tmp_path,
            plugins_config={"valid": {"enabled": True}},
            jitter_fraction=float("nan"),
        )

        with pytest.raises(scheduler.ConfigError):
            scheduler.build_scheduler(config)

    def test_infinite_jitter_fraction_raises_config_error(self, tmp_path):
        config = _scheduler_config(
            tmp_path,
            plugins_config={"valid": {"enabled": True}},
            jitter_fraction=float("inf"),
        )

        with pytest.raises(scheduler.ConfigError):
            scheduler.build_scheduler(config)

    def test_jitter_fraction_of_zero_is_valid(self, tmp_path):
        # 0 is the inclusive lower bound: deterministic polling is allowed.
        config = _scheduler_config(
            tmp_path, plugins_config={"valid": {"enabled": True}}, jitter_fraction=0
        )

        job_scheduler = scheduler.build_scheduler(config)

        job = job_scheduler.get_job("plugin:valid")
        assert job.trigger.jitter == 0

    def test_non_bool_numbers_are_coerced_to_float(self, tmp_path):
        # A YAML 0.2 arrives as float, an int 1 arrives as int -- both fine.
        config = _scheduler_config(
            tmp_path, plugins_config={"valid": {"enabled": True}}, jitter_fraction=0.5
        )

        job_scheduler = scheduler.build_scheduler(config)

        job = job_scheduler.get_job("plugin:valid")
        assert job.trigger.jitter == 900  # interval 1800 * 0.5

    def test_bool_jitter_fraction_raises_config_error(self, tmp_path):
        # Bools are ints in Python; poll.jitter_fraction: true is a config
        # mistake, not a number (same convention as
        # plugins._is_valid_interval for poll_interval).
        config = _scheduler_config(
            tmp_path, plugins_config={"valid": {"enabled": True}}, jitter_fraction=True
        )

        with pytest.raises(scheduler.ConfigError):
            scheduler.build_scheduler(config)

    def test_valid_fraction_reaches_the_job_as_interval_times_fraction(self, tmp_path):
        config = _scheduler_config(
            tmp_path, plugins_config={"valid": {"enabled": True}}, jitter_fraction=0.2
        )

        job_scheduler = scheduler.build_scheduler(config)

        job = job_scheduler.get_job("plugin:valid")
        assert job.trigger.jitter == pytest.approx(1800 * 0.2)


def test_shutdown_wait_false_does_not_block_on_a_slow_job(tmp_path):
    db_path = tmp_path / "stats.db"
    migrate.run_migrations(db_path)

    module = ModuleType("slow")
    module.METRICS = {"slow.x": {"kind": "gauge", "label": "X", "unit": ""}}

    def collect(config, http):
        time.sleep(2)
        return {}

    module.collect = collect
    plugin = plugins.LoadedPlugin(
        name="slow",
        module=module,
        metrics=module.METRICS,
        interval_seconds=300,
        config={},
        source="user",
    )

    job_scheduler = BackgroundScheduler(executors={"default": ThreadPoolExecutor(1)})
    job_id = "plugin:slow"
    job_scheduler.add_job(
        scheduler._run_scheduled_plugin,
        trigger="interval",
        seconds=300,
        max_instances=1,
        coalesce=True,
        id=job_id,
        args=[job_scheduler, job_id, {}, db_path, plugin, None, 86400],
    )
    job_scheduler.start()
    time.sleep(0.2)  # let the job actually start running

    started = time.perf_counter()
    job_scheduler.shutdown(wait=False)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0


class TestRunPluginOnceRateLimited:
    def test_429_is_recorded_as_error_naming_rate_limited(self, db_path):
        module = ModuleType("rate_limited_plugin")
        module.METRICS = {
            "rate_limited_plugin.x": {"kind": "gauge", "label": "X", "unit": ""}
        }

        def collect(config, http):
            raise RateLimited(retry_after=30)

        module.collect = collect
        plugin = _plugin_from_module("rate_limited_plugin", module, module.METRICS)

        with pytest.raises(RateLimited):
            scheduler.run_plugin_once(
                db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
            )

        assert storage.consecutive_failures(db_path, "rate_limited_plugin") == 1

        conn = sqlite3.connect(str(db_path))
        try:
            error = conn.execute(
                "SELECT error FROM plugin_runs WHERE plugin_name = ?",
                ("rate_limited_plugin",),
            ).fetchone()[0]
        finally:
            conn.close()
        assert "rate limited" in error


def _blocked() -> Blocked:
    request = httpx.Request("GET", "https://example.invalid/profile")
    return Blocked(
        httpx.Response(403, headers={"cf-mitigated": "challenge"}, request=request)
    )


class TestRunPluginOnceBlocked:
    def test_403_is_recorded_as_error_with_the_blocked_prefix(self, db_path):
        module = ModuleType("blocked_plugin")
        module.METRICS = {
            "blocked_plugin.x": {"kind": "gauge", "label": "X", "unit": ""}
        }

        def collect(config, http):
            raise _blocked()

        module.collect = collect
        plugin = _plugin_from_module("blocked_plugin", module, module.METRICS)

        with pytest.raises(Blocked):
            scheduler.run_plugin_once(
                db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
            )

        assert storage.consecutive_failures(db_path, "blocked_plugin") == 1

        conn = sqlite3.connect(str(db_path))
        try:
            error = conn.execute(
                "SELECT error FROM plugin_runs WHERE plugin_name = ?",
                ("blocked_plugin",),
            ).fetchone()[0]
        finally:
            conn.close()
        assert error.startswith(BLOCKED_ERROR_PREFIX)
        assert "cf-mitigated=challenge" in error


class TestComputeBackoffDelaySeconds:
    def test_429_with_retry_after_uses_max_of_retry_after_and_interval(self):
        assert scheduler.compute_backoff_delay_seconds(1800, 5, 1) == 1800
        assert scheduler.compute_backoff_delay_seconds(1800, 3600, 1) == 3600

    def test_429_without_retry_after_backs_off_exponentially(self):
        assert scheduler.compute_backoff_delay_seconds(1800, None, 1) == 3600
        assert scheduler.compute_backoff_delay_seconds(1800, None, 2) == 7200
        assert scheduler.compute_backoff_delay_seconds(1800, None, 3) == 14400

    def test_exponential_backoff_is_capped(self):
        delay = scheduler.compute_backoff_delay_seconds(1800, None, 20)

        assert delay == scheduler.MAX_BACKOFF_SECONDS

        # The Retry-After branch is capped too: a hostile year-long value
        # (RFC 9110 10.6.1.2 suggests receivers discard anything over a
        # year) clamps to the same cap instead of taking the plugin
        # offline until 2036.
        retry_after_delay = scheduler.compute_backoff_delay_seconds(
            1800, 365 * 86400, 1
        )

        assert retry_after_delay == scheduler.MAX_BACKOFF_SECONDS


class _FakeSchedulerStub:
    def __init__(self):
        self.modify_job_calls = []

    def modify_job(self, job_id, next_run_time=None):
        self.modify_job_calls.append((job_id, next_run_time))


class TestRunScheduledPluginBackoff:
    def test_429_with_retry_after_reschedules_using_the_backoff_table(self, db_path):
        module = ModuleType("rl_plugin")
        module.METRICS = {"rl_plugin.x": {"kind": "gauge", "label": "X", "unit": ""}}

        def collect(config, http):
            raise RateLimited(retry_after=5)

        module.collect = collect
        plugin = plugins.LoadedPlugin(
            name="rl_plugin",
            module=module,
            metrics=module.METRICS,
            interval_seconds=1800,
            config={},
            source="user",
        )
        fake_scheduler = _FakeSchedulerStub()
        backoff_state: dict[str, int] = {}

        before = datetime.now()
        scheduler._run_scheduled_plugin(
            fake_scheduler,
            "plugin:rl_plugin",
            backoff_state,
            db_path,
            plugin,
            None,
            86400,
        )
        after = datetime.now()

        assert len(fake_scheduler.modify_job_calls) == 1
        job_id, next_run_time = fake_scheduler.modify_job_calls[0]
        assert job_id == "plugin:rl_plugin"
        # retry_after=5 < interval=1800, so max(5, 1800) = 1800 applies --
        # a short Retry-After never makes the next poll sooner than normal.
        assert before + timedelta(seconds=1800) <= next_run_time
        assert next_run_time <= after + timedelta(seconds=1800)
        assert backoff_state["rl_plugin"] == 1

    def test_429_without_retry_after_increments_the_backoff_counter(self, db_path):
        module = ModuleType("rl_plugin2")
        module.METRICS = {"rl_plugin2.x": {"kind": "gauge", "label": "X", "unit": ""}}

        def collect(config, http):
            raise RateLimited(retry_after=None)

        module.collect = collect
        plugin = plugins.LoadedPlugin(
            name="rl_plugin2",
            module=module,
            metrics=module.METRICS,
            interval_seconds=300,
            config={},
            source="user",
        )
        fake_scheduler = _FakeSchedulerStub()
        backoff_state: dict[str, int] = {}

        scheduler._run_scheduled_plugin(
            fake_scheduler,
            "plugin:rl_plugin2",
            backoff_state,
            db_path,
            plugin,
            None,
            86400,
        )
        scheduler._run_scheduled_plugin(
            fake_scheduler,
            "plugin:rl_plugin2",
            backoff_state,
            db_path,
            plugin,
            None,
            86400,
        )

        assert backoff_state["rl_plugin2"] == 2
        assert len(fake_scheduler.modify_job_calls) == 2

    def test_403_backs_off_exponentially_like_a_429_without_retry_after(self, db_path):
        module = ModuleType("blocked_plugin2")
        module.METRICS = {
            "blocked_plugin2.x": {"kind": "gauge", "label": "X", "unit": ""}
        }

        def collect(config, http):
            raise _blocked()

        module.collect = collect
        plugin = plugins.LoadedPlugin(
            name="blocked_plugin2",
            module=module,
            metrics=module.METRICS,
            interval_seconds=1800,
            config={},
            source="user",
        )
        fake_scheduler = _FakeSchedulerStub()
        backoff_state: dict[str, int] = {}

        before = datetime.now()
        for _ in range(2):
            scheduler._run_scheduled_plugin(
                fake_scheduler,
                "plugin:blocked_plugin2",
                backoff_state,
                db_path,
                plugin,
                None,
                86400,
            )
        after = datetime.now()

        assert backoff_state["blocked_plugin2"] == 2
        assert len(fake_scheduler.modify_job_calls) == 2
        # Second consecutive 403: interval * 2**2.
        _, next_run_time = fake_scheduler.modify_job_calls[1]
        assert before + timedelta(seconds=7200) <= next_run_time
        assert next_run_time <= after + timedelta(seconds=7200)

    def test_success_does_not_reschedule_and_resets_the_backoff_counter(self, db_path):
        plugin = _load_fixture_plugin("_fake_constant.py")
        fake_scheduler = _FakeSchedulerStub()
        backoff_state = {"fake_constant": 3}

        scheduler._run_scheduled_plugin(
            fake_scheduler,
            "plugin:fake_constant",
            backoff_state,
            db_path,
            plugin,
            None,
            86400,
        )

        assert backoff_state["fake_constant"] == 0
        assert fake_scheduler.modify_job_calls == []

    def test_ordinary_error_does_not_reschedule_and_resets_the_backoff_counter(
        self, db_path
    ):
        plugin = _load_fixture_plugin("_fake_raises.py")
        fake_scheduler = _FakeSchedulerStub()
        backoff_state = {"fake_raises": 2}

        scheduler._run_scheduled_plugin(
            fake_scheduler,
            "plugin:fake_raises",
            backoff_state,
            db_path,
            plugin,
            None,
            86400,
        )

        assert backoff_state["fake_raises"] == 0
        assert fake_scheduler.modify_job_calls == []


def _scripted_plugin(name: str, outcomes: list) -> plugins.LoadedPlugin:
    """A plugin whose collect() plays ``outcomes`` in order: an exception
    instance is raised, anything else is returned as the metric value."""
    module = ModuleType(name)
    metric = f"{name}.x"
    module.METRICS = {metric: {"kind": "gauge", "label": "X", "unit": ""}}
    remaining = list(outcomes)

    def collect(config, http):
        outcome = remaining.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return {metric: outcome}

    module.collect = collect
    return _plugin_from_module(name, module, module.METRICS)


def _http_403(body: str = "", **headers: str) -> Blocked:
    request = httpx.Request("GET", "https://example.invalid/profile")
    return Blocked(httpx.Response(403, headers=headers, text=body, request=request))


class TestDescribeFailure:
    def test_non_http_exception_uses_type_and_first_line(self):
        signature, detail = scheduler.describe_failure(ValueError("bad\nsecond line"))

        assert signature == "ValueError"
        assert detail == "ValueError: bad"

    def test_http_failure_includes_status_headers_and_collapsed_body(self):
        exc = _http_403(
            "<html>\n  <title>Just a moment...</title>\n</html>",
            **{
                "cf-mitigated": "challenge",
                "cf-ray": "abc123-IAD",
                "server": "cloudflare",
            },
        )

        signature, detail = scheduler.describe_failure(exc)

        assert signature == "Blocked/403"
        assert "status=403" in detail
        assert "cf-mitigated=challenge" in detail
        assert "cf-ray=abc123-IAD" in detail
        assert "server=cloudflare" in detail
        assert "body='<html> <title>Just a moment...</title> </html>'" in detail

    def test_body_is_truncated(self):
        _, detail = scheduler.describe_failure(_http_403("x" * 5000))

        assert "x" * 200 in detail
        assert "x" * 201 not in detail


class TestFailureLogging:
    @staticmethod
    def _run(plugin, streaks, db_path):
        scheduler._run_scheduled_plugin(
            _FakeSchedulerStub(),
            f"plugin:{plugin.name}",
            {},
            db_path,
            plugin,
            None,
            86400,
            streaks,
        )

    @staticmethod
    def _records(caplog, level):
        return [
            r
            for r in caplog.records
            if r.name == "numbers_go_up.scheduler" and r.levelno == level
        ]

    def test_first_failure_warns_once_with_traceback_repeats_stay_quiet(
        self, db_path, caplog
    ):
        caplog.set_level(logging.DEBUG, logger="numbers_go_up.scheduler")
        plugin = _scripted_plugin("flaky", [RuntimeError("boom")] * 3)
        streaks: dict = {}

        for _ in range(3):
            self._run(plugin, streaks, db_path)

        warnings = self._records(caplog, logging.WARNING)
        assert len(warnings) == 1
        assert (
            "Plugin flaky poll failed: RuntimeError: boom" in warnings[0].getMessage()
        )
        assert warnings[0].exc_info is not None
        assert len(self._records(caplog, logging.DEBUG)) == 2
        assert streaks["flaky"].count == 3

    def test_change_of_failure_warns_again(self, db_path, caplog):
        caplog.set_level(logging.INFO, logger="numbers_go_up.scheduler")
        plugin = _scripted_plugin(
            "shifting", [RuntimeError("a"), ValueError("b"), ValueError("b")]
        )
        streaks: dict = {}

        for _ in range(3):
            self._run(plugin, streaks, db_path)

        warnings = self._records(caplog, logging.WARNING)
        assert len(warnings) == 2
        assert "changed after 1 consecutive failures: ValueError: b" in (
            warnings[1].getMessage()
        )

    def test_recovery_logs_info_once_and_clears_the_streak(self, db_path, caplog):
        caplog.set_level(logging.INFO, logger="numbers_go_up.scheduler")
        plugin = _scripted_plugin(
            "recovering", [RuntimeError("a"), RuntimeError("a"), 1, 2]
        )
        streaks: dict = {}

        for _ in range(4):
            self._run(plugin, streaks, db_path)

        infos = self._records(caplog, logging.INFO)
        assert len(infos) == 1
        assert "recovered after 2 consecutive failures" in infos[0].getMessage()
        assert streaks == {}

    def test_healthy_polls_log_nothing(self, db_path, caplog):
        caplog.set_level(logging.DEBUG, logger="numbers_go_up.scheduler")
        plugin = _scripted_plugin("healthy", [1, 2, 3])

        for _ in range(3):
            self._run(plugin, {}, db_path)

        assert [r for r in caplog.records if r.name == "numbers_go_up.scheduler"] == []

    def test_blocked_warning_carries_cloudflare_details_without_traceback(
        self, db_path, caplog
    ):
        caplog.set_level(logging.INFO, logger="numbers_go_up.scheduler")
        blocked = _http_403(
            "<title>Just a moment...</title>",
            **{"cf-mitigated": "challenge", "cf-ray": "abc123-IAD"},
        )
        plugin = _scripted_plugin("walled", [blocked, blocked])
        streaks: dict = {}

        for _ in range(2):
            self._run(plugin, streaks, db_path)

        warnings = self._records(caplog, logging.WARNING)
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "cf-mitigated=challenge" in message
        assert "cf-ray=abc123-IAD" in message
        assert "Just a moment" in message
        assert warnings[0].exc_info is None
        # The backoff reschedule is still reported each time, at INFO.
        assert len(self._records(caplog, logging.INFO)) == 2

    def test_contract_violation_is_logged_as_a_failure(self, db_path, caplog):
        caplog.set_level(logging.INFO, logger="numbers_go_up.scheduler")
        module = ModuleType("liar")
        module.METRICS = {"liar.x": {"kind": "gauge", "label": "X", "unit": ""}}
        module.collect = lambda config, http: {"liar.undeclared": 1}
        plugin = _plugin_from_module("liar", module, module.METRICS)

        self._run(plugin, {}, db_path)

        warnings = self._records(caplog, logging.WARNING)
        assert len(warnings) == 1
        assert "contract violation" in warnings[0].getMessage()


class TestPatternMetrics:
    def test_pattern_key_is_accepted_and_creates_a_series(self, db_path):
        plugin = _load_fixture_plugin("_fake_dynamic.py", config={"items": {"42": 7}})

        result = scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
        )

        assert result.status == "ok"
        series = storage.get_series_by_key(db_path, "fake_dynamic.item.42.value")
        assert series is not None
        assert series["kind"] == "cumulative"
        assert series["unit"] == "things"

    def test_value_dict_overrides_label_and_sets_attrs(self, db_path):
        module = ModuleType("dyn")
        module.METRICS = {
            "dyn.item.{id}.value": {
                "kind": "gauge",
                "label": "Default Label",
                "unit": "things",
            }
        }

        def collect(config, http):
            return {
                "dyn.item.42.value": {
                    "value": 7,
                    "label": "Widget 42",
                    "attrs": {"model_id": 42, "url": "https://example.invalid/42"},
                }
            }

        module.collect = collect
        plugin = _plugin_from_module("dyn", module, module.METRICS)

        result = scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
        )

        assert result.status == "ok"
        series = storage.get_series_by_key(db_path, "dyn.item.42.value")
        assert series["label"] == "Widget 42"

        conn = sqlite3.connect(str(db_path))
        try:
            attrs_json = conn.execute(
                "SELECT attrs FROM metric_series WHERE id = ?", (series["id"],)
            ).fetchone()[0]
        finally:
            conn.close()

        assert json.loads(attrs_json) == {
            "model_id": 42,
            "url": "https://example.invalid/42",
        }

    def test_kind_in_value_dict_is_a_contract_violation(self, db_path):
        module = ModuleType("dyn2")
        module.METRICS = {
            "dyn2.item.{id}.value": {
                "kind": "gauge",
                "label": "Item",
                "unit": "things",
            }
        }

        def collect(config, http):
            return {"dyn2.item.1.value": {"value": 1, "kind": "cumulative"}}

        module.collect = collect
        plugin = _plugin_from_module("dyn2", module, module.METRICS)

        result = scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
        )

        assert result.status == "error"
        assert result.samples_written == 0
        assert "kind" in result.error

    def test_unit_in_value_dict_is_a_contract_violation(self, db_path):
        module = ModuleType("dyn3")
        module.METRICS = {
            "dyn3.item.{id}.value": {
                "kind": "gauge",
                "label": "Item",
                "unit": "things",
            }
        }

        def collect(config, http):
            return {"dyn3.item.1.value": {"value": 1, "unit": "widgets"}}

        module.collect = collect
        plugin = _plugin_from_module("dyn3", module, module.METRICS)

        result = scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
        )

        assert result.status == "error"
        assert result.samples_written == 0

    def test_cardinality_cap_is_enforced(self, db_path, monkeypatch):
        monkeypatch.setattr(plugins, "MAX_PATTERN_KEYS_PER_RUN", 2)
        module = ModuleType("many")
        module.METRICS = {
            "many.item.{id}.value": {"kind": "gauge", "label": "Item", "unit": ""}
        }

        def collect(config, http):
            return {f"many.item.{i}.value": i for i in range(5)}

        module.collect = collect
        plugin = _plugin_from_module("many", module, module.METRICS)

        result = scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
        )

        assert result.status == "error"
        assert result.samples_written == 0
        assert "cardinality" in result.error or "exceeding the cardinality" in (
            result.error or ""
        )
        assert storage.get_series_by_key(db_path, "many.item.0.value") is None


class TestPatternSeriesLifecycle:
    def test_a_pattern_key_missing_from_a_successful_run_is_deactivated(self, db_path):
        plugin = _load_fixture_plugin(
            "_fake_dynamic.py", config={"items": {"1": 10, "2": 20}}
        )
        scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
        )
        assert (
            storage.get_series_by_key(db_path, "fake_dynamic.item.1.value") is not None
        )
        assert (
            storage.get_series_by_key(db_path, "fake_dynamic.item.2.value") is not None
        )

        # Model "2" is no longer returned -- a successful run must retire it.
        plugin2 = _load_fixture_plugin("_fake_dynamic.py", config={"items": {"1": 11}})
        result = scheduler.run_plugin_once(
            db_path, plugin2, http=None, now=1300, heartbeat_seconds=86400
        )

        assert result.status == "ok"
        assert (
            storage.get_series_by_key(db_path, "fake_dynamic.item.1.value") is not None
        )
        assert storage.get_series_by_key(db_path, "fake_dynamic.item.2.value") is None

        # History is kept, not deleted.
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT active FROM metric_series WHERE metric_key = ?",
                ("fake_dynamic.item.2.value",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == 0

    def test_a_reactivated_key_becomes_active_again(self, db_path):
        plugin_both = _load_fixture_plugin(
            "_fake_dynamic.py", config={"items": {"1": 10, "2": 20}}
        )
        plugin_only_one = _load_fixture_plugin(
            "_fake_dynamic.py", config={"items": {"1": 11}}
        )

        scheduler.run_plugin_once(
            db_path, plugin_both, http=None, now=1000, heartbeat_seconds=86400
        )
        scheduler.run_plugin_once(
            db_path, plugin_only_one, http=None, now=1300, heartbeat_seconds=86400
        )
        assert storage.get_series_by_key(db_path, "fake_dynamic.item.2.value") is None

        result = scheduler.run_plugin_once(
            db_path, plugin_both, http=None, now=1600, heartbeat_seconds=86400
        )

        assert result.status == "ok"
        assert (
            storage.get_series_by_key(db_path, "fake_dynamic.item.2.value") is not None
        )

    def test_no_deactivation_after_a_failed_run(self, db_path):
        plugin_both = _load_fixture_plugin(
            "_fake_dynamic.py", config={"items": {"1": 10, "2": 20}}
        )
        scheduler.run_plugin_once(
            db_path, plugin_both, http=None, now=1000, heartbeat_seconds=86400
        )

        module = ModuleType("fake_dynamic")
        module.METRICS = plugin_both.metrics

        def collect(config, http):
            raise RuntimeError("source is down")

        module.collect = collect
        failing_plugin = _plugin_from_module(
            "fake_dynamic", module, plugin_both.metrics
        )

        result = scheduler.run_plugin_once(
            db_path, failing_plugin, http=None, now=1300, heartbeat_seconds=86400
        )

        assert result.status == "error"
        assert (
            storage.get_series_by_key(db_path, "fake_dynamic.item.1.value") is not None
        )
        assert (
            storage.get_series_by_key(db_path, "fake_dynamic.item.2.value") is not None
        )

    def test_exact_keys_are_never_auto_deactivated(self, db_path):
        # fake_constant.demo.value is an exact (non-pattern) METRICS key.
        plugin = _load_fixture_plugin("_fake_constant.py")
        scheduler.run_plugin_once(
            db_path, plugin, http=None, now=1000, heartbeat_seconds=86400
        )

        # A run that returns nothing must not retire the exact-key series.
        module = ModuleType("fake_constant")
        module.METRICS = plugin.metrics
        module.collect = lambda config, http: {}
        empty_plugin = _plugin_from_module("fake_constant", module, plugin.metrics)

        result = scheduler.run_plugin_once(
            db_path, empty_plugin, http=None, now=1300, heartbeat_seconds=86400
        )

        assert result.status == "ok"
        assert (
            storage.get_series_by_key(db_path, "fake_constant.demo.value") is not None
        )
