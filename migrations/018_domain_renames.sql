-- A planned primary-domain rename of one side of a migration (domain_rename.py).
-- Kept for good: a source-side rename whose old domain was a government one makes
-- the new name government too (domain_guard.is_government), so its record must
-- never disappear. Only applied_at and a once-read customer_id may be set later.
CREATE TABLE IF NOT EXISTS domain_renames (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL,
    side        TEXT NOT NULL CHECK (side IN ('source','target')),
    from_domain TEXT NOT NULL CHECK (length(from_domain) > 0),
    to_domain   TEXT NOT NULL CHECK (length(to_domain) > 0),
    admin_local TEXT NOT NULL CHECK (length(admin_local) > 0),
    customer_id TEXT,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    applied_at  TEXT
);
CREATE TRIGGER IF NOT EXISTS domain_renames_no_delete BEFORE DELETE ON domain_renames
BEGIN
    SELECT RAISE(ABORT, 'A domain rename record cannot be removed');
END;
CREATE TRIGGER IF NOT EXISTS domain_renames_fixed BEFORE UPDATE ON domain_renames
WHEN NEW.account_id IS NOT OLD.account_id OR NEW.side IS NOT OLD.side
  OR NEW.from_domain IS NOT OLD.from_domain OR NEW.to_domain IS NOT OLD.to_domain
  OR NEW.admin_local IS NOT OLD.admin_local OR NEW.recorded_by IS NOT OLD.recorded_by
  OR NEW.recorded_at IS NOT OLD.recorded_at
  OR (OLD.customer_id IS NOT NULL AND NEW.customer_id IS NOT OLD.customer_id)
  OR (OLD.applied_at IS NOT NULL AND NEW.applied_at IS NOT OLD.applied_at)
BEGIN
    SELECT RAISE(ABORT, 'A domain rename record cannot be changed');
END;
