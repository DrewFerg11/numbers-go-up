import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from numbers_go_up import migrate, plugins, scheduler, storage
from numbers_go_up.http import RateLimited

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
