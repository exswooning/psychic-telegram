-- 004_user_claims.sql
-- ============================================================================
-- Which node owns which user, for a migration spread across several machines.
-- See user_claims.py.
--
-- Why a claim table and not a work queue
-- --------------------------------------
-- run_batch fans out across users inside ONE process on ONE machine. Adding a
-- second machine means the two must agree on who migrates whom, and they must
-- agree atomically: two nodes both starting user U inserts every one of U's
-- messages twice, and nothing downstream would notice -- the per-item ledger
-- that makes a re-run idempotent is LOCAL to each node, so neither can see the
-- other's work. SQLite's BEGIN IMMEDIATE gives that atomicity here exactly as
-- it already does for active_jobs (003).
--
-- Leases, and why a dead node's users are NOT auto-stolen
-- ------------------------------------------------------
-- A claim carries an expiry that the owning node renews while it works. An
-- expired lease means the node died or was killed -- but it does NOT mean
-- another node may pick the user up, and this is the important part:
--
--   * resume is driven by the local ledger -- gmail_engine skips a message
--     when `db.get_target_id(user, mid, "message")` returns a row;
--   * that row lives in the dead node's own migration.db;
--   * so a different node re-running the user starts from an empty ledger and
--     re-inserts everything it already delivered.
--
-- The only general duplicate guard (_find_by_message_id, which adopts a
-- message already in the target) runs as `before_retry` -- after an ambiguous
-- failure, not on the common path -- so it does not cover this.
--
-- Therefore an expired lease becomes reclaimable by the SAME node (a restart,
-- whose ledger is intact) and requires an explicit force by any OTHER node.
-- Forcing is a real operator decision with a real cost, so it is recorded
-- rather than silently allowed.
-- ============================================================================
CREATE TABLE IF NOT EXISTS user_claims (
    account_id    INTEGER,
    source_user   TEXT    NOT NULL,
    node_id       TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'CLAIMED',  -- CLAIMED|DONE|FAILED
    services      TEXT    NOT NULL DEFAULT '',
    claimed_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    renewed_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    lease_expires TEXT    NOT NULL,
    forced_from   TEXT    NOT NULL DEFAULT '',
    detail        TEXT    NOT NULL DEFAULT '',
    -- One claim per (account, user). The PK is what makes the race
    -- impossible rather than merely unlikely: a second node's INSERT for a
    -- user already claimed fails on the constraint instead of racing a
    -- SELECT that said "free".
    PRIMARY KEY (account_id, source_user)
);

CREATE INDEX IF NOT EXISTS ix_claims_node   ON user_claims(node_id, status);
CREATE INDEX IF NOT EXISTS ix_claims_lease  ON user_claims(lease_expires);
