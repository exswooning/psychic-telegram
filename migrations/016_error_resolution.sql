-- Control-plane telemetry only; no migration mappings or tenant data are changed.
CREATE TABLE IF NOT EXISTS error_log_cursors (
 source_key TEXT PRIMARY KEY, identity TEXT NOT NULL, generation INTEGER NOT NULL,
 offset INTEGER NOT NULL, anchor TEXT NOT NULL, context TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS error_log_sources (
 source_key TEXT PRIMARY KEY, account_id INTEGER, job TEXT NOT NULL, path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS error_cases (
 id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
 fingerprint TEXT NOT NULL, category TEXT NOT NULL, service TEXT NOT NULL,
 reason TEXT NOT NULL, title TEXT NOT NULL, state TEXT NOT NULL,
 occurrences INTEGER NOT NULL DEFAULT 1, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
 version TEXT NOT NULL, next_action TEXT NOT NULL, candidate_commit TEXT,
 evidence TEXT NOT NULL DEFAULT '', UNIQUE(account_id,fingerprint)
);
CREATE INDEX IF NOT EXISTS error_cases_state ON error_cases(state,last_seen);
CREATE TABLE IF NOT EXISTS error_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT NOT NULL, generation INTEGER NOT NULL,
 position INTEGER NOT NULL, account_id INTEGER NOT NULL, run_id TEXT NOT NULL,
 job TEXT NOT NULL, kind TEXT NOT NULL, source_user TEXT NOT NULL,
 service TEXT NOT NULL, item_id TEXT NOT NULL, operation TEXT NOT NULL,
 message TEXT NOT NULL, metrics TEXT NOT NULL, version TEXT NOT NULL,
 recorded_at TEXT NOT NULL, case_id INTEGER REFERENCES error_cases(id),
 UNIQUE(source_key,generation,position)
);
CREATE INDEX IF NOT EXISTS error_events_case ON error_events(case_id,id);
CREATE TABLE IF NOT EXISTS error_affected_items (
 case_id INTEGER NOT NULL REFERENCES error_cases(id), source_user TEXT NOT NULL,
 item_id TEXT NOT NULL, last_seen TEXT NOT NULL,
 PRIMARY KEY(case_id,source_user,item_id)
);
CREATE TABLE IF NOT EXISTS error_case_history (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES error_cases(id),
 state TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS error_runner_tokens (
 id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, label TEXT NOT NULL,
 token_hash TEXT UNIQUE NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, last_seen TEXT, detail TEXT NOT NULL DEFAULT 'Not connected'
);
CREATE TABLE IF NOT EXISTS error_fix_attempts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES error_cases(id),
 runner_id INTEGER NOT NULL REFERENCES error_runner_tokens(id), lease_hash TEXT NOT NULL,
 lease_until REAL NOT NULL, state TEXT NOT NULL, started_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, finished_at TEXT, source_commit TEXT,
 candidate_commit TEXT, detail TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS error_resolution_policy (
 id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 1,
 daily_limit INTEGER NOT NULL DEFAULT 3 CHECK(daily_limit BETWEEN 1 AND 20)
);
INSERT OR IGNORE INTO error_resolution_policy(id) VALUES(1);
CREATE TABLE IF NOT EXISTS error_rechecks (
 case_id INTEGER PRIMARY KEY REFERENCES error_cases(id), requested_at TEXT NOT NULL,
 started_at TEXT, detail TEXT NOT NULL DEFAULT ''
);
