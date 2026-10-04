-- A migration's end of life (lifecycle.py). One row per account, which is one tenant
-- pair: approved complete -- by the operator, or automatically AUTO_APPROVE_DAYS after
-- its last run -- then torn down TEARDOWN_DAYS later.
CREATE TABLE IF NOT EXISTS migration_lifecycle (
    account_id        INTEGER PRIMARY KEY,
    first_seen_at     TEXT NOT NULL,     -- the auto-approval clock never starts before this
    approved_at       TEXT,
    approved_by       TEXT,              -- an operator's name, or 'auto'
    teardown_due_at   TEXT,
    last_attempt_at   TEXT,              -- a failed teardown is retried at most once a day
    torn_down_at      TEXT,
    last_result       TEXT               -- what the last teardown attempt did, JSON
);
