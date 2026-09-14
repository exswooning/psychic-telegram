-- A domain set up in a slot that already held a DIFFERENT domain used to
-- vanish: tenant_configs keeps exactly one (account, side) row, so the new
-- setup overwrote the old and the previous domain was simply gone from every
-- view. This keeps the old one instead -- domain, admin, and a backup of its
-- key -- so nothing a person set up ever disappears, and it can be seen (and
-- later re-linked) rather than silently lost.
CREATE TABLE IF NOT EXISTS superseded_configs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    side          TEXT NOT NULL CHECK (side IN ('source','target')),
    domain        TEXT NOT NULL,
    admin_email   TEXT,
    key_path      TEXT,               -- a timestamped backup of the old key
    replaced_by   TEXT,               -- the domain that took the slot
    superseded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_superseded_account
    ON superseded_configs(account_id, side);
