-- 003_active_jobs.sql
-- ============================================================================
-- Cross-account resource admission. See job_admission.py.
--
-- webui.py's Job (seed/reset target) and api_server.py's detached _spawn()
-- (migrate/full-setup) are two separate OS processes -- an in-memory
-- Semaphore in one cannot see a job the other just launched. SQLite's own
-- locking is what actually coordinates them: a row inserted here by either
-- process is immediately visible to a COUNT(*) from the other, no shared
-- memory required. Same reasoning as every other table in this file living
-- in one shared migration.db rather than per-process state.
--
-- One row per currently-running heavy job (seed, reset target, migrate,
-- full-setup -- see job_admission.py for exactly which launch sites use
-- this; the lighter endpoints, benchmark/coverage/provision-users/etc.,
-- deliberately do not). Deleted the moment that job finishes -- this is a
-- live admission ledger, not a history (operator_actions_log already is).
-- ============================================================================
CREATE TABLE IF NOT EXISTS active_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER,
    job_name    TEXT NOT NULL,
    pid         INTEGER,
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
