"""Schema sizing benchmark: how big is the database, and how fast are reads?

A manual tool (issue #101, part 1). It rebuilds the four storage candidates
the "SQLite is the right choice" decision was based on, fills them with the
same simulated history, VACUUMs, and prints a Markdown table of on-disk size
and query timings that can be pasted straight into the docs.

    original schema (``metrics`` + secondary index)   x  every poll / on change
    current schema  (clustered ``samples``)           x  every poll / on change

The current schema is built by running the real migrations through
``numbers_go_up.migrate.run_migrations`` and filled with the real
``storage.store_poll``; its queries are the real ``storage`` functions, and
the ``/api/stats/latest`` row calls the real route handler. The original
schema no longer exists in the repo (it only ever lived in a design note), so
it is declared here, reconstructed from that note's description: one row per
sample carrying the metric key as text, with a secondary index on
(metric, ts). Its queries are the straightforward SQL equivalents.

Everything happens in a temporary directory; nothing touches ``./data``.
Sizes and row counts are deterministic for a given set of flags (the RNG is
seeded); timings are wall-clock medians and vary a little run to run.

Usage:
    python benchmarks/schema_sizing.py
    python benchmarks/schema_sizing.py --days 90 --series 100 --change-rate 0.02

Not imported by the package, not collected by pytest, not run in CI.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Make `python benchmarks/schema_sizing.py` work from a plain checkout, with
# or without the package pip-installed.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from numbers_go_up import api, migrate, storage  # noqa: E402

# Simulated history starts at a fixed instant (2026-01-01 UTC), so timestamps
# -- and therefore varint sizes -- do not depend on when the script is run.
START_TS = 1_767_225_600
DAY = 86_400
# The daily heartbeat from the README: a flat series still writes one sample
# per day under store-on-change.
HEARTBEAT_SECONDS = DAY
DEFAULT_INTERVAL = 1800

# The pre-clustering layout: a rowid table with the metric key repeated as
# text on every row, plus the secondary index that makes it queryable.
ORIGINAL_DDL = """
CREATE TABLE metrics (
    id     INTEGER PRIMARY KEY,
    metric TEXT    NOT NULL,
    ts     INTEGER NOT NULL,
    value  REAL    NOT NULL
);
CREATE INDEX idx_metrics_metric_ts ON metrics(metric, ts);
"""

QUERY_LABELS = [
    ("latest", "latest, 1 series"),
    ("asof", "as-of 24h"),
    ("hist7d", "history 7d"),
    ("hist1y", "history 1y"),
    ("stats", "/api/stats/latest"),
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--days", type=int, default=365, help="days of history")
    parser.add_argument("--polls-per-day", type=int, default=48, help="polls per day")
    parser.add_argument("--series", type=int, default=20, help="number of series")
    parser.add_argument(
        "--change-rate",
        type=float,
        default=0.08,
        help="fraction of polls where a series' value actually changes",
    )
    parser.add_argument("--seed", type=int, default=1, help="RNG seed")
    parser.add_argument(
        "--repeats",
        type=int,
        default=200,
        help="timed runs per quick query (the 1y history uses a tenth)",
    )
    args = parser.parse_args(argv)
    if args.days < 1 or args.polls_per_day < 1 or args.series < 1:
        parser.error("--days, --polls-per-day and --series must be >= 1")
    if not 0 <= args.change_rate <= 1:
        parser.error("--change-rate must be between 0 and 1")
    if args.repeats < 1:
        parser.error("--repeats must be >= 1")
    return args


def metric_key(n: int) -> str:
    return f"benchmark.profile.metric_{n:03d}"


def simulate(args: argparse.Namespace) -> tuple[list[int], list[list[float]]]:
    """Poll timestamps, and per-series values for every poll.

    Each series is a cumulative counter that steps up by 1-5 on a poll with
    probability ``--change-rate`` and otherwise stays put.
    """
    rng = random.Random(args.seed)
    n_polls = args.days * args.polls_per_day
    step = DAY / args.polls_per_day
    timestamps = [START_TS + int(i * step) for i in range(n_polls)]
    values = []
    for _ in range(args.series):
        current = float(rng.randint(100, 100_000))
        column = []
        for _ in range(n_polls):
            if rng.random() < args.change_rate:
                current += rng.randint(1, 5)
            column.append(current)
        values.append(column)
    return timestamps, values


def keep_on_change(timestamps: list[int], column: list[float]) -> list[int]:
    """Indexes of the polls store-on-change writes: the first, every change,
    and a heartbeat when a flat series has gone ``HEARTBEAT_SECONDS``."""
    kept = []
    last_value = last_ts = None
    for i, (ts, value) in enumerate(zip(timestamps, column, strict=True)):
        if (
            last_value is None
            or value != last_value
            or ts - last_ts >= HEARTBEAT_SECONDS
        ):
            kept.append(i)
            last_value, last_ts = value, ts
    return kept


def build_original(
    db_path: Path, timestamps: list[int], values: list[list[float]], every_poll: bool
) -> int:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(ORIGINAL_DDL)
        rows = []
        for n, column in enumerate(values):
            indexes = (
                range(len(timestamps))
                if every_poll
                else keep_on_change(timestamps, column)
            )
            key = metric_key(n)
            rows.extend((key, timestamps[i], column[i]) for i in indexes)
        conn.executemany("INSERT INTO metrics (metric, ts, value) VALUES (?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def build_current(
    db_path: Path, timestamps: list[int], values: list[list[float]], every_poll: bool
) -> int:
    """Run the real migrations, then feed polls through ``storage.store_poll``.

    A zero heartbeat makes ``store_poll`` write on every poll; the production
    heartbeat gives store-on-change.
    """
    migrate.run_migrations(db_path)
    heartbeat = 0 if every_poll else HEARTBEAT_SECONDS
    written = 0
    for i, ts in enumerate(timestamps):
        items = [
            (metric_key(n), column[i], "cumulative", None, None, None, None)
            for n, column in enumerate(values)
        ]
        written += storage.store_poll(db_path, "benchmark", ts, heartbeat, items)
    return written


def vacuum_and_size(db_path: Path) -> int:
    """Checkpoint the WAL into the main file, VACUUM, and return its size."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    finally:
        conn.close()
    return os.path.getsize(db_path)


def median_ms(fn, repeats: int) -> float:
    fn()  # warm the page cache and statement cache
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def original_queries(db_path: Path, key: str, end: int, n_series: int) -> dict:
    def scalar_as_of(conn, metric: str, ts: int):
        return conn.execute(
            "SELECT value FROM metrics WHERE metric = ? AND ts <= ? "
            "ORDER BY ts DESC LIMIT 1",
            (metric, ts),
        ).fetchone()

    def latest():
        with contextlib.closing(storage.connect(db_path)) as conn:
            return scalar_as_of(conn, key, end)

    def asof():
        with contextlib.closing(storage.connect(db_path)) as conn:
            return scalar_as_of(conn, key, end - DAY)

    def history(window: int):
        with contextlib.closing(storage.connect(db_path)) as conn:
            anchor = scalar_as_of(conn, key, end - window)
            rows = conn.execute(
                "SELECT ts, value FROM metrics WHERE metric = ? "
                "AND ts > ? AND ts <= ? ORDER BY ts",
                (key, end - window, end),
            ).fetchall()
        return anchor, rows

    def stats():
        # The same shape as the handler: current value plus the 1h and 24h
        # as-of values for every series, on one connection. The original
        # schema had no cached last_value, so "current" is a seek too.
        with contextlib.closing(storage.connect(db_path)) as conn:
            return [
                [scalar_as_of(conn, metric_key(n), ts) for ts in (end, end - 3600)]
                + [scalar_as_of(conn, metric_key(n), end - DAY)]
                for n in range(n_series)
            ]

    return {
        "latest": latest,
        "asof": asof,
        "hist7d": lambda: history(7 * DAY),
        "hist1y": lambda: history(365 * DAY),
        "stats": stats,
    }


def current_queries(db_path: Path, end: int) -> dict:
    series_id = storage.get_series_by_key(db_path, metric_key(0))["id"]
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config={
                    "storage": {"path": str(db_path)},
                    "poll": {"default_interval": DEFAULT_INTERVAL},
                }
            )
        )
    )

    def stats():
        # The handler reads the wall clock; pin it to the end of the
        # simulated history so the as-of windows land on the data.
        with mock.patch.object(api.time, "time", return_value=end):
            return api.stats_latest(request)

    return {
        "latest": lambda: storage.latest(db_path, [series_id]),
        "asof": lambda: storage.value_as_of(db_path, series_id, end - DAY),
        "hist7d": lambda: storage.history(db_path, series_id, end - 7 * DAY, end),
        "hist1y": lambda: storage.history(db_path, series_id, end - 365 * DAY, end),
        "stats": stats,
    }


def time_queries(queries: dict, repeats: int) -> dict[str, float]:
    return {
        name: median_ms(
            queries[name], max(1, repeats // 10) if name == "hist1y" else repeats
        )
        for name, _ in QUERY_LABELS
    }


def format_ms(ms: float) -> str:
    return f"{ms:.3f} ms" if ms < 10 else f"{ms:.1f} ms"


def render_table(results: list[dict]) -> str:
    header = ["Candidate", "Sample rows", "Size", "Bytes/row"] + [
        label for _, label in QUERY_LABELS
    ]
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(["---"] + ["---:"] * (len(header) - 1)) + "|",
    ]
    for r in results:
        cells = [
            r["name"],
            f"{r['rows']:,}",
            f"{r['size'] / 1_000_000:.1f} MB",
            f"{r['size'] / r['rows']:.0f}" if r["rows"] else "n/a",
        ] + [format_ms(r["timings"][name]) for name, _ in QUERY_LABELS]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> str:
    timestamps, values = simulate(args)
    end = timestamps[-1]
    candidates = [
        ("Original schema, every poll", build_original, True),
        ("Original schema, on change", build_original, False),
        ("Clustered schema, every poll", build_current, True),
        ("Clustered schema, on change", build_current, False),
    ]
    results = []
    with tempfile.TemporaryDirectory(prefix="schema-sizing-") as tmp:
        for i, (name, build, every_poll) in enumerate(candidates):
            print(f"building {name} ...", file=sys.stderr, flush=True)
            db_path = Path(tmp) / f"candidate{i}.db"
            rows = build(db_path, timestamps, values, every_poll)
            size = vacuum_and_size(db_path)
            if build is build_original:
                queries = original_queries(db_path, metric_key(0), end, args.series)
            else:
                queries = current_queries(db_path, end)
            print(f"timing {name} ...", file=sys.stderr, flush=True)
            results.append(
                {
                    "name": name,
                    "rows": rows,
                    "size": size,
                    "timings": time_queries(queries, args.repeats),
                }
            )
    params = (
        f"{args.days} days x {args.polls_per_day} polls/day x {args.series} series, "
        f"{args.change_rate:.0%} of polls change, seed {args.seed}"
    )
    return (
        f"Simulated: {params}. Size is the VACUUMed file; timings are the "
        f"median of {args.repeats} runs ({max(1, args.repeats // 10)} for "
        "history 1y), including a fresh connection per call.\n\n"
        + render_table(results)
    )


def main(argv: list[str] | None = None) -> None:
    print(run(parse_args(argv)))


if __name__ == "__main__":
    main()
