-- Throwaway migration used only by tests. Never ships to production.
CREATE TABLE test_marker (
    id    INTEGER PRIMARY KEY,
    label TEXT NOT NULL
);
