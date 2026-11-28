-- Users on a shared tenant that belong to ONE account's migration (domain_guard.protected_subject):
-- a. holds the synthetic campaign (account 5) and, as d-*@a., a government tenant's real users
-- migrated by account 8. Every other account never sees them; nothing deletes them. Append-only.
CREATE TABLE IF NOT EXISTS protected_subjects (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    domain           TEXT NOT NULL CHECK (length(domain) > 0),
    localpart_prefix TEXT,
    address          TEXT,
    owner_account_id INTEGER NOT NULL,
    reason           TEXT NOT NULL CHECK (length(reason) > 0),
    recorded_by      TEXT NOT NULL,
    recorded_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK ((localpart_prefix IS NULL) != (address IS NULL)),
    CHECK (localpart_prefix IS NULL OR length(localpart_prefix) > 0)
);
CREATE TRIGGER IF NOT EXISTS protected_subjects_no_delete BEFORE DELETE ON protected_subjects
BEGIN
    SELECT RAISE(ABORT, 'A protected subject cannot be removed');
END;
CREATE TRIGGER IF NOT EXISTS protected_subjects_no_update BEFORE UPDATE ON protected_subjects
BEGIN
    SELECT RAISE(ABORT, 'A protected subject cannot be changed');
END;
