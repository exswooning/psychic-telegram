-- The mirror's per-pair settings (mirror_scheduler.py). One row per account, which is
-- one tenant pair. The cycles themselves, their changes, held deletions and conflicts
-- live in the account's own ledger (mirror_* tables in db.py); this is only what the
-- scheduler needs to decide when the next cycle is due, readable without opening any
-- ledger.
CREATE TABLE IF NOT EXISTS mirror_settings (
    account_id       INTEGER PRIMARY KEY,
    enabled          INTEGER NOT NULL DEFAULT 0,
    interval_min     INTEGER NOT NULL DEFAULT 15,
    deletion_mode    TEXT NOT NULL DEFAULT 'mirror' CHECK (deletion_mode IN ('mirror','keep')),
    cap_pct          REAL NOT NULL DEFAULT 2.0,
    deletions_paused INTEGER NOT NULL DEFAULT 0,
    enabled_at       TEXT,
    last_started_at  TEXT,
    last_tally_on    TEXT,
    updated_at       TEXT,
    updated_by       TEXT
);
