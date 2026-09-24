-- The *unsafe* rebuild ordering: rename the live table out of the way
-- first. Since SQLite 3.26, renaming a parent table rewrites the child's
-- REFERENCES to follow it, so samples.series_id ends up pointing at
-- metric_series_old -- which this then drops.
ALTER TABLE metric_series RENAME TO metric_series_old;

CREATE TABLE metric_series (
    id          INTEGER PRIMARY KEY,
    metric_key  TEXT    NOT NULL UNIQUE,
    plugin_name TEXT    NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'gauge'
                        CHECK (kind IN ('gauge', 'cumulative')),
    label       TEXT,
    unit        TEXT,
    icon        TEXT,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER,
    last_value  REAL,
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    attrs       TEXT    NOT NULL DEFAULT '{}'
);

INSERT INTO metric_series SELECT * FROM metric_series_old;
DROP TABLE metric_series_old;

CREATE INDEX idx_series_plugin ON metric_series(plugin_name);
