-- Textbook SQLite table-rebuild: create the new shape, copy the data
-- across, drop the old table, then rename the new one into place. This
-- is the *safe* ordering -- see migrate.py's module docstring.
CREATE TABLE metric_series_new (
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

INSERT INTO metric_series_new SELECT * FROM metric_series;
DROP TABLE metric_series;
ALTER TABLE metric_series_new RENAME TO metric_series;

CREATE INDEX idx_series_plugin ON metric_series(plugin_name);
