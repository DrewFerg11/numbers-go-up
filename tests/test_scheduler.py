import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

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


def _scheduler_config(
    tmp_path, plugins_config=None, jitter_fraction=0.2, db_path=None
):
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
        late_module.METRICS = {"late_plugin.x": {"kind": "gauge", "label": "X", "unit": ""}}

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
        for job_plugin, run_time in ((slow_plugin, 0.0), (late_plugin, 0.5)):
            job_scheduler.add_job(
                scheduler._run_scheduled_plugin,
                trigger="interval",
                seconds=3600,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=None,
                next_run_time=datetime.now(UTC) + timedelta(seconds=run_time),
                id=f"plugin:{job_plugin.name}",
                args=[db_path, job_plugin, None, 86400],
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
    job_scheduler.add_job(
        scheduler._run_scheduled_plugin,
        trigger="interval",
        seconds=300,
        max_instances=1,
        coalesce=True,
        args=[db_path, plugin, None, 86400],
    )
    job_scheduler.start()
    time.sleep(0.2)  # let the job actually start running

    started = time.perf_counter()
    job_scheduler.shutdown(wait=False)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0
