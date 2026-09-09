-- 006_job_queue.sql
-- ============================================================================
-- Work waiting for a slot, so a refusal becomes a place in line.
--
-- job_admission caps the box at MAX_CONCURRENT_TENANT_JOBS heavy jobs, and
-- until now a request over that cap was simply refused: "capacity is full,
-- try again shortly". On a single-operator box that is fine -- you try again.
-- With several accounts on one deployment it means whoever retries at the
-- right moment wins, and everyone else discovers the refusal by watching a
-- page that says nothing is running. A queue makes "try again shortly" the
-- system's job rather than the operator's.
--
-- Why a table and not an in-memory list
-- -------------------------------------
-- The thing that starts these jobs is webui.py, and webui.py restarts on
-- every deploy -- several times an hour on a working day. An in-memory queue
-- would lose every waiting request each time, silently, and the accounts
-- that were waiting would simply never run. The same reasoning that put
-- active_jobs (003) and user_claims (004) in SQLite rather than in a process.
--
-- Why the payload is JSON
-- -----------------------
-- What a queued job needs is exactly what its endpoint already builds: an
-- argv and an env overlay. Modelling those as columns would mean a migration
-- every time a flag is added -- --groups and --big-file-mb both landed this
-- week. The queue does not interpret the payload; it hands it back to the
-- same code that would have run it immediately.
--
-- Status, and why finished rows stay
-- ----------------------------------
-- queued -> running -> done | failed | cancelled. Finished rows are kept so
-- "why did my job not run" has an answer after the fact: a queue that
-- deletes its history can only ever say what is waiting now.
CREATE TABLE IF NOT EXISTS job_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER,
    job_name     TEXT NOT NULL,
    payload      TEXT NOT NULL,          -- JSON: {argv, env, cwd, label}
    requested_by TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'queued',
    queued_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    started_at   TEXT,
    finished_at  TEXT,
    detail       TEXT NOT NULL DEFAULT ''
);

-- The dispatcher's only hot query: the oldest queued row. Without this it is
-- a table scan every few seconds, forever.
CREATE INDEX IF NOT EXISTS idx_job_queue_waiting
    ON job_queue (status, id);

-- One account cannot fill the queue with the same job over and over. A
-- double-clicked button is the common case, and it must not become two runs
-- of a five-hour seed against one tenant.
CREATE UNIQUE INDEX IF NOT EXISTS idx_job_queue_one_pending
    ON job_queue (account_id, job_name)
    WHERE status = 'queued';
