# Benchmarks

Manual tools, not part of the package or the test suite: nothing here is
imported by `numbers_go_up`, collected by pytest, run in CI, or shipped in the
image.

## `schema_sizing.py`

Regenerates the database size and query-speed figures quoted in the README
and in [Operations → Database size and speed](../docs/operations.md#database-size-and-speed):
the original schema (`metrics` + secondary index) against the current
clustered one, each storing every poll or only changes.

```bash
python benchmarks/schema_sizing.py                      # 365 days x 48 polls x 20 series, 8% change
python benchmarks/schema_sizing.py --days 90 --series 100 --change-rate 0.02
python benchmarks/schema_sizing.py --help
```

Needs the project's dependencies installed (the same venv the tests use). A
default run takes a few minutes, mostly writing the current-schema databases
one poll at a time through the real `storage.store_poll`. Progress goes to
stderr and the Markdown table to stdout.

What is real and what is modelled:

- The **current schema** is created by running the real migrations in
  `numbers_go_up/migrations/` through `migrate.run_migrations`, so it follows
  the schema automatically. It is filled with `storage.store_poll`, and
  queried with the real `storage.latest`, `value_as_of` and `history`; the
  `/api/stats/latest` column calls the real route handler.
- The **original schema** no longer exists in the repo, so it is declared in
  the script (a rowid table carrying the metric key as text on every row, with
  an index on `(metric, ts)`). It is a reconstruction from the design note's
  description, not a copy of the original DDL. Its queries are the plain SQL
  equivalents; its `/api/stats/latest` figure is the same per-series lookups
  on one connection, not the real handler.
- Row counts and sizes are deterministic for a given set of flags (the RNG is
  seeded with `--seed`). Timings are medians of wall-clock runs, each including
  a fresh connection, so they vary a little between runs and machines.
- Everything is built in a temporary directory that is deleted on exit; it
  never touches `./data`.

The measured table is in
[Operations → Database size and speed](../docs/operations.md#database-size-and-speed).
