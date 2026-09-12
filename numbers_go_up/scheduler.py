"""The plugin run wrapper: runs one plugin once, with per-poll isolation.

Deliberately separate from APScheduler (#20's job): tests drive
``run_plugin_once`` with ``now`` stepped forward by hand, so "N ticks"
takes microseconds, not N poll intervals.

Adjusted from the guide's ``run_plugin_once(plugin, http, now) -> RunResult``
sketch: the storage layer built in Phase 1 takes ``db_path`` as its first
argument (connection-per-operation, see #12), and the heartbeat interval
lives in config rather than being a scheduler constant, so both are
threaded through here too.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import random
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from numbers_go_up import storage
from numbers_go_up.config import ConfigError
from numbers_go_up.plugins import LoadedPlugin, discover_plugins

logger = logging.getLogger(__name__)

# APScheduler's own jitter only ever delays a run (0..N seconds), so a
# ±20% "jitter_fraction" is implemented here as a plain interval jitter in
# seconds, matching the "delay-only" behaviour Responsible Use #3 accepts.
DEFAULT_JITTER_FRACTION = 0.2
FIRST_RUN_MAX_DELAY_SECONDS = 60


@dataclasses.dataclass(frozen=True)
class RunResult:
    run_id: int
    status: str  # "ok" or "error"
    samples_written: int
    error: str | None


def run_plugin_once(
    db_path: str | Path,
    plugin: LoadedPlugin,
    http: Any,
    now: int,
    heartbeat_seconds: int,
) -> RunResult:
    """Run ``plugin.module.collect()`` once, in isolation.

    Every step from Failure Handling #1: start a plugin_runs row, call
    collect() inside try/except (catching Exception only -
    KeyboardInterrupt/SystemExit must propagate so the container stays
    stoppable), validate and store whatever came back, then finish the
    run either way. A raising plugin, or one that returns bad data, never
    stops another plugin's poll and never crashes the service.
    """
    run_id = storage.start_run(db_path, plugin.name, now)

    try:
        result = plugin.module.collect(plugin.config, http)
    except Exception:
        error = traceback.format_exc()
        storage.finish_run(
            db_path, run_id, "error", error, samples_written=0, finished_at=now
        )
        return RunResult(run_id=run_id, status="error", samples_written=0, error=error)

    samples_written = 0
    violations: list[str] = []

    try:
        for key, value in result.items():
            if key not in plugin.metrics:
                violations.append(f"{key!r} is not declared in this plugin's METRICS")
                continue
            if isinstance(value, bool) or not isinstance(value, int | float):
                violations.append(f"{key!r} value {value!r} is not int or float")
                continue
            if math.isnan(value) or math.isinf(value):
                violations.append(f"{key!r} value {value!r} is not finite")
                continue

            meta = plugin.metrics[key]
            series_id = storage.get_or_create_series(
                db_path,
                key,
                plugin.name,
                meta["kind"],
                meta.get("label"),
                meta.get("unit"),
                meta.get("icon"),
                now,
            )
            if storage.record_sample(db_path, series_id, now, value, heartbeat_seconds):
                samples_written += 1
    except Exception:
        # collect() succeeded but validate/store blew up (a non-dict
        # result, sqlite contention, a full disk...). Finish the run and
        # swallow, exactly like the collect() guard above: a plugin that
        # breaks after collect() still never stops another plugin's poll
        # or crashes the service. samples_written stays honest -- rows
        # written before the crash are counted.
        error = traceback.format_exc()
        storage.finish_run(
            db_path,
            run_id,
            "error",
            error,
            samples_written=samples_written,
            finished_at=now,
        )
        return RunResult(
            run_id=run_id, status="error", samples_written=samples_written, error=error
        )

    if violations:
        # Write the valid metrics and report the offenders: a status of
        # error with samples_written > 0 is honest -- data arrived and the
        # plugin broke its contract, but good data isn't thrown away.
        error = "; ".join(violations)
        storage.finish_run(
            db_path,
            run_id,
            "error",
            error,
            samples_written=samples_written,
            finished_at=now,
        )
        return RunResult(
            run_id=run_id, status="error", samples_written=samples_written, error=error
        )

    storage.finish_run(
        db_path, run_id, "ok", None, samples_written=samples_written, finished_at=now
    )
    return RunResult(
        run_id=run_id, status="ok", samples_written=samples_written, error=None
    )


def _run_scheduled_plugin(
    db_path: str | Path, plugin: LoadedPlugin, http: Any, heartbeat_seconds: int
) -> None:
    run_plugin_once(db_path, plugin, http, int(time.time()), heartbeat_seconds)


def _validated_jitter_fraction(value: Any) -> float:
    """Return ``value`` as a float usable as APScheduler's delay-only jitter.

    The fraction is consumed arithmetically inside IntervalTrigger at fire
    time (``interval_seconds * fraction``, then ``random.uniform(0,
    jitter)``), so a bad value must fail startup here rather than kill the
    scheduler's polling loop at runtime. A quoted ``jitter_fraction: "0.2"``
    (the same quoting slip discover_plugins defends against for
    poll_interval) would make ``interval_seconds * "0.2"`` a ~540-character
    repeated string — no TypeError until the first fire, where
    ``random.uniform(0, <str>)`` raises inside the scheduler's main loop and
    every plugin stops polling while /health keeps serving 200. A negative
    fraction fires polls early; a fraction >= 1 lets jitter exceed the
    interval.
    """
    # Bools are ints in Python, but ``jitter_fraction: true`` is a config
    # mistake, not a number -- same convention as plugins._is_valid_interval.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(
            f"poll.jitter_fraction must be a number, got {value!r} "
            f"({type(value).__name__})"
        )
    fraction = float(value)
    # The 0 <= comparison chain also rejects NaN (every comparison with NaN
    # is False); the explicit isnan check just documents it.
    if math.isnan(fraction) or not 0 <= fraction < 1:
        raise ConfigError(
            f"poll.jitter_fraction must be a finite number in [0, 1), got {value!r}"
        )
    return fraction


def build_scheduler(config: dict[str, Any], http: Any = None) -> BackgroundScheduler:
    """Build (but don't start) one interval job per enabled plugin.

    ``http`` is ``None`` here: Phase 2 ships no real plugin yet, and the
    shared client with 429-aware backoff is #21's job. The scheduler wiring
    doesn't change when that lands — only what gets passed as ``http``.

    A single-threaded executor is deliberate: it makes ``max_instances=1``
    meaningful per job (APScheduler enforces it per job regardless, but a
    single worker keeps polls serialized against the one SQLite writer
    rather than relying on ``busy_timeout`` to paper over concurrent
    writes). Every job also sets ``coalesce=True`` so a missed run (e.g.
    the container was asleep) doesn't fire a pile of catch-up runs, and
    ``misfire_grace_time=None`` so a run submitted late to a backed-up
    executor executes late instead of being silently discarded (which
    would leave a poll missing from ``plugin_runs``).

    ``jitter_fraction`` is validated up front (float in ``[0, 1)``): a bad
    value raises ``ConfigError`` here and fails startup, instead of
    killing the scheduler's polling loop at the first fire (see
    :func:`_validated_jitter_fraction`).

    One uvicorn worker, always: multiple workers would mean multiple
    schedulers polling the same sources and writing the same SQLite file
    from separate processes. The Dockerfile's CMD has no ``--workers``
    flag — never add one.
    """
    db_path = config["storage"]["path"]
    heartbeat_seconds = config["storage"]["heartbeat_seconds"]
    jitter_fraction = _validated_jitter_fraction(
        config.get("poll", {}).get("jitter_fraction", DEFAULT_JITTER_FRACTION)
    )

    scheduler = BackgroundScheduler(executors={"default": ThreadPoolExecutor(10)})

    for plugin in discover_plugins(config):
        first_run_delay = random.uniform(0, FIRST_RUN_MAX_DELAY_SECONDS)
        scheduler.add_job(
            _run_scheduled_plugin,
            trigger="interval",
            seconds=plugin.interval_seconds,
            jitter=plugin.interval_seconds * jitter_fraction,
            next_run_time=datetime.now() + timedelta(seconds=first_run_delay),
            max_instances=1,
            coalesce=True,
            misfire_grace_time=None,
            id=f"plugin:{plugin.name}",
            args=[db_path, plugin, http, heartbeat_seconds],
        )
        logger.info(
            "Scheduled plugin %s every %ss (source=%s)",
            plugin.name,
            plugin.interval_seconds,
            plugin.source,
        )

    return scheduler
