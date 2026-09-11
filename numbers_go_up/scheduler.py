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
import traceback
from pathlib import Path
from typing import Any

from numbers_go_up import storage
from numbers_go_up.plugins import LoadedPlugin


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
    collect() inside try/except (catching Exception only —
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

    for key, value in result.items():
        if key not in plugin.metrics:
            violations.append(f"{key!r} is not declared in this plugin's METRICS")
            continue
        if isinstance(value, bool) or not isinstance(value, int | float):
            violations.append(f"{key!r} value {value!r} is not int or float")
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
