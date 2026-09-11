-- ---------------------------------------------------------------
-- 0001_initial.sql
-- ---------------------------------------------------------------

-- One row per metric series, ever. The catalogue.
CREATE TABLE metric_series (
    id          INTEGER PRIMARY KEY,
    metric_key  TEXT    NOT NULL UNIQUE,   -- "makerworld.profile.design_downloads"
    plugin_name TEXT    NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'gauge'
                        CHECK (kind IN ('gauge', 'cumulative')),
    label       TEXT,                      -- "MakerWorld Downloads"
    unit        TEXT,                      -- "downloads"
    icon        TEXT,                      -- display hint
    first_seen  INTEGER NOT NULL,          -- unix seconds, UTC
    last_seen   INTEGER,                   -- newest sample ts
    last_value  REAL,                      -- newest sample value
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    attrs       TEXT    NOT NULL DEFAULT '{}'   -- JSON escape hatch
);

CREATE INDEX idx_series_plugin ON metric_series(plugin_name);

-- The time series itself. Clustered on (series_id, ts): samples for one
-- series are physically adjacent, and there is no secondary index to
-- maintain. This is what buys the 8.4x size reduction.
CREATE TABLE samples (
    series_id INTEGER NOT NULL REFERENCES metric_series(id) ON DELETE CASCADE,
    ts        INTEGER NOT NULL,            -- unix seconds, UTC
    value     REAL    NOT NULL,
    PRIMARY KEY (series_id, ts)
) WITHOUT ROWID;

-- One row per plugin invocation, success or failure. This is the liveness
-- record — it is what lets a flat line be distinguished from a dead source
-- even though unchanged values are not written to `samples`.
CREATE TABLE plugin_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plugin_name     TEXT    NOT NULL,
    started_at      INTEGER NOT NULL,
    finished_at     INTEGER,
    status          TEXT    NOT NULL CHECK (status IN ('ok', 'error')),
    error           TEXT,                  -- last ~500 chars of traceback
    duration_ms     INTEGER,
    samples_written INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_runs_plugin_time ON plugin_runs(plugin_name, started_at DESC);

-- Small key-value store: cached channel IDs, milestone fire-once markers.
CREATE TABLE state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Migration ledger.
CREATE TABLE schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    applied_at INTEGER NOT NULL
);
