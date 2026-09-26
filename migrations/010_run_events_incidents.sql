-- What happened to each run, and what went wrong with it.
--
-- run_events is the watcher's memory. A run is "started" when the watcher first
-- sees its process in the admission table and "finished" when it stops being
-- there; storing both means a restart of the watcher loses nothing, because the
-- open runs (started, never finished) are read back from here rather than kept
-- in memory. rc is the exit code where one was observed and NULL where it was
-- not -- and NULL is kept as NULL: an unobserved exit must never read as 0.
CREATE TABLE IF NOT EXISTS run_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    account_id  INTEGER,
    job_name    TEXT NOT NULL,
    event       TEXT NOT NULL CHECK (event IN ('started','finished')),
    pid         INTEGER,
    rc          INTEGER,
    started_at  TEXT,
    detail      TEXT NOT NULL DEFAULT '',
    handled_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_events_open ON run_events(event, handled_at, id);

-- An incident is a problem that wants a person or Claude. The same problem
-- recurring is one incident with a count, not a hundred rows: the fingerprint
-- says "same problem", and an open or acknowledged incident with the same one
-- inside the dedupe window is bumped instead of duplicated.
CREATE TABLE IF NOT EXISTS incidents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    account_id   INTEGER,
    job_name     TEXT,
    kind         TEXT NOT NULL,      -- crashed | verdict_fail | failing | stalled | traceback
    severity     TEXT NOT NULL DEFAULT 'error' CHECK (severity IN ('info','warn','error')),
    fingerprint  TEXT NOT NULL,
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL DEFAULT '',
    run_id       TEXT,               -- the report that documents it
    brief_path   TEXT,               -- the hand-off for whoever fixes it
    occurrences  INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','acknowledged','resolved')),
    resolved_at  TEXT,
    note         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_incidents_fp ON incidents(fingerprint, status);
