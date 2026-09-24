# Writing a migration

Each migration is a `NNNN_name.sql` file, applied once in version order by
`migrate.run_migrations`. See `numbers_go_up/migrate.py`'s module docstring
for the full mechanics; the short version:

- **The runner disables `PRAGMA foreign_keys` for the whole migration
  transaction, and checks `PRAGMA foreign_key_check` before it commits.**
  Runtime connections (`storage.connect()`) keep foreign keys on as always
  -- only the migration connection changes.
- **A table rebuild must go: create the new table -> copy the data across
  -> drop the old table -> rename the new table into place.** Never rename
  the live table out of the way first -- SQLite >= 3.26 rewrites a child
  table's `REFERENCES` to follow a renamed parent, so the child ends up
  pointing at whatever you rename the old table to, and dropping that
  leaves it dangling. The runner's `foreign_key_check` catches this before
  commit, but getting the order right in the first place is one line of
  SQL, not a rollback to debug.
- Every pending file for a run applies inside one transaction. A single
  migration that fails partway rolls the whole run back -- nothing is
  half-applied.
