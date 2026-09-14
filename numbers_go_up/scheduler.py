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

import httpx
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from numbers_go_up import plugins, storage
from numbers_go_up.config import ConfigError
from numbers_go_up.http import Blocked, RateLimited
from numbers_go_up.plugins import LoadedPlugin, discover_plugins

logger = logging.getLogger(__name__)

# APScheduler's own jitter only ever delays a run (0..N seconds), so a
# ±20% "jitter_fraction" is implemented here as a plain interval jitter in
# seconds, matching the "delay-only" behaviour Responsible Use #3 accepts.
DEFAULT_JITTER_FRACTION = 0.2
FIRST_RUN_MAX_DELAY_SECONDS = 60

# 429 without Retry-After, and 403, back off exponentially, capped --
# Responsible Use #3's exceptions to "no backoff storms" (Failure Handling #4).
MAX_BACKOFF_SECONDS = 86400


@dataclasses.dataclass(frozen=True)
class RunResult:
    run_id: int
    status: str  # "ok" or "error"
    samples_written: int
    error: str | None
    # The exception behind an "error" run, when there was one (a contract
    # violation has none) -- kept so the scheduler can log what failed.
    exception: BaseException | None = None


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
    except RateLimited as exc:
        error = f"rate limited (retry_after={exc.retry_after})"
        storage.finish_run(
            db_path, run_id, "error", error, samples_written=0, finished_at=now
        )
        # Re-raised so the scheduler can apply the 429 backoff rule -- an
        # exception to "no backoff storms" -- to this plugin's next run.
        # Ordinary exceptions don't get this treatment; they're handled
        # below and never change the interval.
        raise
    except Blocked as exc:
        # Same as a 429: a 403 means the source is refusing this client,
        # so re-raise for the scheduler's backoff. The error text starts
        # with BLOCKED_ERROR_PREFIX, which /api/plugins reads back as
        # status "blocked".
        storage.finish_run(
            db_path, run_id, "error", str(exc), samples_written=0, finished_at=now
        )
        raise
    except Exception as exc:
        error = storage.error_tail(traceback.format_exc())
        storage.finish_run(
            db_path, run_id, "error", error, samples_written=0, finished_at=now
        )
        return RunResult(
            run_id=run_id, status="error", samples_written=0, error=error, exception=exc
        )

    samples_written = 0
    violations: list[str] = []
    returned_keys: set[str] = set()

    try:
        items = list(result.items())

        # Cardinality guard: count *before* writing anything, so a runaway
        # pattern match (a paging bug, a source dumping its whole database)
        # never creates a single extra series this run rather than being
        # truncated partway through. Exact keys are never counted or
        # capped -- only pattern-matched keys can grow unboundedly.
        pattern_match_count = sum(
            1
            for key, _ in items
            if (match := plugins.resolve_metric(key, plugin.metrics)) is not None
            and plugins.is_pattern_key(match[0])
        )
        cardinality_ok = pattern_match_count <= plugins.MAX_PATTERN_KEYS_PER_RUN
        if not cardinality_ok:
            violations.append(
                f"plugin returned {pattern_match_count} pattern-matched keys, "
                f"exceeding the cardinality cap of "
                f"{plugins.MAX_PATTERN_KEYS_PER_RUN}"
            )

        for key, raw_value in items:
            match = plugins.resolve_metric(key, plugin.metrics)
            if match is None:
                violations.append(f"{key!r} is not declared in this plugin's METRICS")
                continue
            declared_key, meta = match
            is_pattern = plugins.is_pattern_key(declared_key)
            if is_pattern and not cardinality_ok:
                # Already reported once, above; don't create any of the
                # series that pushed this run over the cap.
                continue

            label = meta.get("label")
            unit = meta.get("unit")
            icon = meta.get("icon")
            attrs: dict[str, Any] | None = None

            if isinstance(raw_value, dict):
                if "kind" in raw_value or "unit" in raw_value:
                    violations.append(
                        f"{key!r}: 'kind'/'unit' cannot be set per poll "
                        "(fixed by the plugin's METRICS pattern)"
                    )
                    continue
                if "value" not in raw_value:
                    violations.append(f"{key!r}: metadata dict is missing 'value'")
                    continue
                value = raw_value["value"]
                if "label" in raw_value:
                    label = raw_value["label"]
                if "attrs" in raw_value:
                    attrs = raw_value["attrs"]
            else:
                value = raw_value

            if isinstance(value, bool) or not isinstance(value, int | float):
                violations.append(f"{key!r} value {value!r} is not int or float")
                continue
            if math.isnan(value) or math.isinf(value):
                violations.append(f"{key!r} value {value!r} is not finite")
                continue

            series_id = storage.get_or_create_series(
                db_path,
                key,
                plugin.name,
                meta["kind"],
                label,
                unit,
                icon,
                now,
                attrs=attrs,
            )
            returned_keys.add(key)
            if storage.record_sample(db_path, series_id, now, value, heartbeat_seconds):
                samples_written += 1
    except Exception as exc:
        # collect() succeeded but validate/store blew up (a non-dict
        # result, sqlite contention, a full disk...). Finish the run and
        # swallow, exactly like the collect() guard above: a plugin that
        # breaks after collect() still never stops another plugin's poll
        # or crashes the service. samples_written stays honest -- rows
        # written before the crash are counted.
        error = storage.error_tail(traceback.format_exc())
        storage.finish_run(
            db_path,
            run_id,
            "error",
            error,
            samples_written=samples_written,
            finished_at=now,
        )
        return RunResult(
            run_id=run_id,
            status="error",
            samples_written=samples_written,
            error=error,
            exception=exc,
        )

    if violations:
        # Write the valid metrics and report the offenders: a status of
        # error with samples_written > 0 is honest -- data arrived and the
        # plugin broke its contract, but good data isn't thrown away.
        error = storage.error_tail("; ".join(violations))
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

    _reconcile_pattern_series(db_path, plugin, returned_keys)

    storage.finish_run(
        db_path, run_id, "ok", None, samples_written=samples_written, finished_at=now
    )
    return RunResult(
        run_id=run_id, status="ok", samples_written=samples_written, error=None
    )


def _reconcile_pattern_series(
    db_path: str | Path, plugin: LoadedPlugin, returned_keys: set[str]
) -> None:
    """Deactivate/reactivate pattern-matched series after a fully successful run.

    Only called once a run is known to be status "ok" with zero contract
    violations (Failure Handling: a Cloudflare 403 must never retire every
    subject at once). A series is only touched here if its key currently
    matches one of the plugin's *pattern* METRICS entries -- exact keys are
    never auto-deactivated, and a series whose pattern was removed from the
    plugin entirely (a code change, not a poll) is left alone too.
    """
    for row in storage.series_for_plugin(db_path, plugin.name):
        key = row["metric_key"]
        match = plugins.resolve_metric(key, plugin.metrics)
        if match is None or not plugins.is_pattern_key(match[0]):
            continue

        should_be_active = key in returned_keys
        if bool(row["active"]) != should_be_active:
            storage.set_series_active(db_path, row["id"], should_be_active)


def compute_backoff_delay_seconds(
    interval_seconds: int,
    retry_after: float | None,
    consecutive_429s: int,
    cap_seconds: int = MAX_BACKOFF_SECONDS,
) -> float:
    """The backoff table (Responsible Use #3), all four rows:

    - Success / ordinary error: not this function's job -- the caller
      simply doesn't reschedule, so the job's normal interval applies.
    - 429 with Retry-After: ``max(Retry-After, interval)``, capped — the
      header is attacker-adjacent (any mirror/CDN/anti-bot layer in front
      of the origin can set it), so an absurd value is trusted only up to
      the cap; beyond that the plugin returns weekly instead of never.
    - 429 without Retry-After, or 403 (``retry_after=None``):
      ``interval * 2**consecutive_429s``, capped. The counter covers any
      unbroken 429/403 streak.
    """
    if retry_after is not None:
        return min(max(retry_after, interval_seconds), cap_seconds)
    return min(interval_seconds * (2**consecutive_429s), cap_seconds)


# Response headers worth logging on a failed poll: who answered and why.
# cf-ray is the id Cloudflare support asks for. Never auth-bearing headers.
_LOGGED_RESPONSE_HEADERS = (
    "server",
    "cf-mitigated",
    "cf-ray",
    "retry-after",
    "content-type",
)
_LOGGED_BODY_CHARS = 200


def describe_failure(exc: BaseException) -> tuple[str, str]:
    """Summarise a failed poll's exception for the log as ``(signature, detail)``.

    ``signature`` is deliberately coarse -- the exception type, plus the
    HTTP status when there is one -- so a streak of the same failure logs
    once and a *different* failure (403 -> 500) logs again. ``detail`` is
    one line: the exception's first line and, for HTTP failures, the status,
    the identifying response headers, and the start of the body.
    """
    lines = str(exc).splitlines()
    signature = type(exc).__name__
    detail = f"{signature}: {lines[0] if lines else ''}"

    response = getattr(exc, "response", None)
    if not isinstance(response, httpx.Response):
        return signature, detail

    signature = f"{signature}/{response.status_code}"
    parts = [detail, f"status={response.status_code}"]
    for name in _LOGGED_RESPONSE_HEADERS:
        value = response.headers.get(name)
        if value is not None:
            parts.append(f"{name}={value}")
    try:
        body = " ".join(response.text.split())[:_LOGGED_BODY_CHARS]
    except httpx.ResponseNotRead:
        body = ""
    if body:
        parts.append(f"body={body!r}")
    return signature, " ".join(parts)


@dataclasses.dataclass
class _FailureStreak:
    signature: str
    count: int
    started: float


def _log_failure(
    streaks: dict[str, _FailureStreak],
    plugin_name: str,
    signature: str,
    detail: str,
    exc: BaseException | None,
) -> None:
    """Log a failed poll without flooding: WARNING on the first failure of a
    streak and whenever the kind of failure changes, DEBUG otherwise.

    The traceback is attached only for failures without an HTTP response
    (a plugin bug, a parse error, a connection error) -- for an HTTP
    failure the status, headers, and body in ``detail`` say more than a
    stack through httpx does.
    """
    exc_info = (
        exc if exc is not None and getattr(exc, "response", None) is None else None
    )
    streak = streaks.get(plugin_name)
    if streak is None:
        streaks[plugin_name] = _FailureStreak(signature, 1, time.time())
        logger.warning(
            "Plugin %s poll failed: %s", plugin_name, detail, exc_info=exc_info
        )
    elif streak.signature != signature:
        logger.warning(
            "Plugin %s failure changed after %d consecutive failures: %s",
            plugin_name,
            streak.count,
            detail,
            exc_info=exc_info,
        )
        streak.signature = signature
        streak.count += 1
    else:
        streak.count += 1
        logger.debug(
            "Plugin %s poll failed again (%d consecutive): %s",
            plugin_name,
            streak.count,
            detail,
        )


def _log_success(streaks: dict[str, _FailureStreak], plugin_name: str) -> None:
    streak = streaks.pop(plugin_name, None)
    if streak is not None:
        logger.info(
            "Plugin %s recovered after %d consecutive failures (failing for %s)",
            plugin_name,
            streak.count,
            timedelta(seconds=int(time.time() - streak.started)),
        )


def _run_scheduled_plugin(
    scheduler: BackgroundScheduler,
    job_id: str,
    backoff_state: dict[str, int],
    db_path: str | Path,
    plugin: LoadedPlugin,
    http: Any,
    heartbeat_seconds: int,
    failure_streaks: dict[str, _FailureStreak] | None = None,
) -> None:
    if failure_streaks is None:
        failure_streaks = {}
    try:
        result = run_plugin_once(
            db_path, plugin, http, int(time.time()), heartbeat_seconds
        )
    except (RateLimited, Blocked) as exc:
        signature, detail = describe_failure(exc)
        _log_failure(failure_streaks, plugin.name, signature, detail, exc)

        consecutive = backoff_state.get(plugin.name, 0) + 1
        backoff_state[plugin.name] = consecutive
        retry_after = exc.retry_after if isinstance(exc, RateLimited) else None
        delay = compute_backoff_delay_seconds(
            plugin.interval_seconds, retry_after, consecutive
        )
        logger.info(
            "Plugin %s backing off (retry_after=%s, consecutive=%d); next run in %.0fs",
            plugin.name,
            retry_after,
            consecutive,
            delay,
        )
        scheduler.modify_job(
            job_id, next_run_time=datetime.now() + timedelta(seconds=delay)
        )
    else:
        # Success or an ordinary error: no backoff (Failure Handling #4),
        # and any 429/403 streak is broken -- reset the counter.
        backoff_state[plugin.name] = 0
        if result.status == "ok":
            _log_success(failure_streaks, plugin.name)
        elif result.exception is not None:
            signature, detail = describe_failure(result.exception)
            _log_failure(
                failure_streaks, plugin.name, signature, detail, result.exception
            )
        else:
            _log_failure(
                failure_streaks,
                plugin.name,
                "contract violation",
                f"contract violation: {result.error}",
                None,
            )


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
    backoff_state: dict[str, int] = {}
    failure_streaks: dict[str, _FailureStreak] = {}

    for plugin in discover_plugins(config):
        job_id = f"plugin:{plugin.name}"
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
            id=job_id,
            args=[
                scheduler,
                job_id,
                backoff_state,
                db_path,
                plugin,
                http,
                heartbeat_seconds,
                failure_streaks,
            ],
        )
        logger.info(
            "Scheduled plugin %s every %ss (source=%s)",
            plugin.name,
            plugin.interval_seconds,
            plugin.source,
        )

    return scheduler
