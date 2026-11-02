-- A migration start an operator set for a particular hour. The payload is the
-- StartMigration body exactly as the Start dialog would have sent it.
CREATE TABLE IF NOT EXISTS scheduled_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 account_id INTEGER NOT NULL,
 run_at TEXT NOT NULL,                    -- UTC, 'YYYY-MM-DDTHH:MM:SSZ'
 payload TEXT NOT NULL,
 requested_by TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'PENDING'
   CHECK(status IN ('PENDING','STARTING','STARTED','FAILED','MISSED','CANCELLED')),
 detail TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_scheduled_runs_due ON scheduled_runs (status, run_at);
