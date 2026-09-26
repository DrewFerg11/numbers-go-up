#!/usr/bin/env python3
"""Build a demo SQLite database with synthetic, plausible history.

Intended to be shared with ``benchmarks/schema_sizing.py`` (#101), which
needs the same thing: a seeded DB with realistic store-on-change history.
Neither had landed as of this module's own PR; whichever lands first owns
this module and the other should import it, so the fixture generator
never forks. This landed first -- #101 should import from here rather
than writing its own generator.

Every series is written through the real storage layer and the real
migration runner, never hand-written SQL, so the fixture cannot describe a
schema the app does not have.

Series names are fake and platform-neutral, per the Responsible Use rule:
this data is published to an indexed Pages site, and it must never look
like it came from -- or discloses numbers for -- a real account on a real
platform.
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from numbers_go_up import migrate, storage  # noqa: E402

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "demo-seed.db"

# One synthetic sample per day, oldest first. Daily resolution (rather
# than something finer) keeps the exported history JSON small: a finer
# cadence multiplies every history-<key>-<range>.json file by the same
# factor, for no benefit to a chart that's already daily-scale at 1Y/ALL.
#
# 60 days doesn't fill 1Y/ALL the way a year-old real instance would --
# rather than trading realism for the ~1 MB export budget one for one,
# see export.py's _UNREFERENCED_STATIC_FILES for where the rest of that
# budget went. The exported total lands at ~1.5 MB: closer to the target
# than a shorter history would justify, and still an honest reflection of
# what the real per-range overview payload (60-point spark per metric,
# times 9 ranges) and the real static assets actually cost.
DAYS_OF_HISTORY = 60
STEPS_PER_DAY = 1
HEARTBEAT_SECONDS = 86400  # matches the step cadence: every tick writes

# (metric_key, kind, label, unit, icon, bounds). bounds is (min, max) for
# a gauge -- _walk reflects the random walk at these instead of letting it
# wander (compounding +-6%/step drift over 60 steps can land anywhere,
# and this is the public demo's face once #161 lands) -- or None for a
# cumulative series, which is unbounded by definition.
_SeriesDef = tuple[str, str, str, str | None, str | None, tuple[float, float] | None]

# Five fake plugins. Two are special: "demo-flaky" never has a successful
# poll (exercises the stale/error status dot), and "demo-legacy" is
# retired entirely after being seeded (exercises the inactive-series path).
PLUGINS: dict[str, list[_SeriesDef]] = {
    "demo-tracker": [
        ("tracker.cpu_pct", "gauge", "CPU usage", "%", "mdi:cpu-64-bit", (0, 100)),
        ("tracker.mem_pct", "gauge", "Memory usage", "%", "mdi:memory", (0, 100)),
        ("tracker.disk_pct", "gauge", "Disk usage", "%", "mdi:harddisk", (0, 100)),
        (
            "tracker.queue_depth",
            "gauge",
            "Queue depth",
            "jobs",
            "mdi:tray-full",
            (0, 200),
        ),
        (
            "tracker.workers_active",
            "gauge",
            "Active workers",
            None,
            "mdi:cog",
            (0, 50),
        ),
    ],
    "demo-counter": [
        ("counter.signups", "cumulative", "Signups", None, "mdi:account-plus", None),
        ("counter.orders", "cumulative", "Orders", None, "mdi:cart", None),
        (
            "counter.messages_sent",
            "cumulative",
            "Messages sent",
            None,
            "mdi:email",
            None,
        ),
        ("counter.pageviews", "cumulative", "Pageviews", None, "mdi:eye", None),
        ("counter.api_calls", "cumulative", "API calls", None, "mdi:api", None),
    ],
    "demo-sensors": [
        (
            "sensors.temp_c",
            "gauge",
            "Temperature",
            "°C",
            "mdi:thermometer",
            (-10, 45),
        ),
        (
            "sensors.humidity_pct",
            "gauge",
            "Humidity",
            "%",
            "mdi:water-percent",
            (0, 100),
        ),
        (
            "sensors.light_lux",
            "gauge",
            "Light level",
            "lux",
            "mdi:brightness-6",
            (0, 2000),
        ),
        ("sensors.battery_pct", "gauge", "Battery", "%", "mdi:battery", (0, 100)),
        ("sensors.noise_db", "gauge", "Noise level", "dB", "mdi:volume-high", (0, 120)),
    ],
    "demo-ledger": [
        ("ledger.deposits", "cumulative", "Deposits", "credits", "mdi:cash-plus", None),
        (
            "ledger.withdrawals",
            "cumulative",
            "Withdrawals",
            "credits",
            "mdi:cash-minus",
            None,
        ),
        (
            "ledger.transfers",
            "cumulative",
            "Transfers",
            "credits",
            "mdi:bank-transfer",
            None,
        ),
    ],
    "demo-flaky": [
        (
            "flaky.heartbeat",
            "gauge",
            "Flaky heartbeat",
            None,
            "mdi:heart-pulse",
            (0, 100),
        ),
    ],
    "demo-legacy": [
        (
            "legacy.widget_count",
            "gauge",
            "Legacy widgets",
            None,
            "mdi:archive",
            (0, 500),
        ),
    ],
}

RETIRED_PLUGIN = "demo-legacy"
FLAKY_PLUGIN = "demo-flaky"


def _walk(
    rng: random.Random,
    start: float,
    steps: int,
    cumulative: bool,
    bounds: tuple[float, float] | None = None,
) -> list[float]:
    """A plausible-looking series: monotonically non-decreasing for a
    cumulative counter, a bounded random walk for a gauge.

    The gauge branch's drift has no mean-reversion term, so compounding
    +-6%/step over many steps can wander arbitrarily far -- reflecting at
    ``bounds`` (required for every gauge series; see ``_SeriesDef``) keeps
    a percentage gauge inside 0-100 and every other gauge inside its own
    plausible range, rather than the raw walk value, which is this data's
    published face once #161 serves it.
    """
    values = [start]
    for _ in range(steps - 1):
        prev = values[-1]
        if cumulative:
            # Occasional flat stretches (store-on-change would otherwise
            # write every tick) plus the occasional bigger jump.
            step = rng.choice([0, 0, 1, 1, 2, 3, 5, 8])
            values.append(prev + step)
        else:
            drift = rng.uniform(-0.06, 0.06) * max(abs(prev), 1)
            new = prev + drift
            if bounds is not None:
                lo, hi = bounds
                if new < lo:
                    new = lo + (lo - new)
                elif new > hi:
                    new = hi - (new - hi)
                new = min(hi, max(lo, new))
            values.append(round(new, 2))
    return values


def seed(
    db_path: Path,
    now: int | None = None,
    seed_value: int = 20260925,
    days: int = DAYS_OF_HISTORY,
) -> None:
    """``days`` defaults to :data:`DAYS_OF_HISTORY`; tests pass a small
    value to keep a full seed-and-export run fast."""
    now = int(time.time()) if now is None else now
    rng = random.Random(seed_value)

    migrate.run_migrations(db_path)

    steps = days * STEPS_PER_DAY
    step_seconds = 86400 // STEPS_PER_DAY
    first_ts = now - steps * step_seconds

    for plugin_name, series_defs in PLUGINS.items():
        # values_by_key[metric_key][tick] -- generated once per series,
        # then replayed one tick at a time so every series on a plugin
        # shares one store_poll call per tick, the same as a real poll
        # that reports several metrics at once (#128).
        values_by_key = {
            metric_key: _walk(
                rng,
                rng.uniform(*bounds) if kind == "gauge" else rng.uniform(0, 50),
                steps,
                cumulative=kind == "cumulative",
                bounds=bounds,
            )
            for metric_key, kind, _label, _unit, _icon, bounds in series_defs
        }

        for tick in range(steps):
            ts = first_ts + tick * step_seconds
            items = [
                (
                    metric_key,
                    values_by_key[metric_key][tick],
                    kind,
                    label,
                    unit,
                    icon,
                    None,
                )
                for metric_key, kind, label, unit, icon, _bounds in series_defs
            ]
            storage.store_poll(db_path, plugin_name, ts, HEARTBEAT_SECONDS, items)

        if plugin_name == FLAKY_PLUGIN:
            # Only ever failed polls -- no row in last_ok_runs, so every
            # metric on this plugin reads as stale/error (queries.is_stale).
            for i in range(3):
                run_id = storage.start_run(db_path, plugin_name, now - (3 - i) * 3600)
                storage.finish_run(
                    db_path,
                    run_id,
                    status="error",
                    error="demo: simulated upstream timeout",
                    samples_written=0,
                    finished_at=now - (3 - i) * 3600 + 5,
                )
        else:
            run_id = storage.start_run(db_path, plugin_name, now - 300)
            storage.finish_run(
                db_path,
                run_id,
                status="ok",
                error=None,
                samples_written=len(series_defs),
                finished_at=now - 295,
            )

    # Retire demo-legacy's series entirely: exercises the inactive-series
    # path (still in /api/metrics and history, gone from the active
    # dashboard list). Every other plugin name is passed through unchanged.
    keep = [name for name in PLUGINS if name != RETIRED_PLUGIN]
    storage.retire_series_not_in(db_path, keep)


def main(argv: list[str]) -> int:
    db_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_DB_PATH
    if db_path.exists():
        db_path.unlink()
    seed(db_path)
    print(f"Seeded demo database at {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
