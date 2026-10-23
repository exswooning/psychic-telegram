-- Once configured as a source, a domain cannot be made wipeable by an
-- operator revocation, account deletion, config removal or role change.
CREATE TABLE IF NOT EXISTS source_domain_protections (
    domain TEXT PRIMARY KEY NOT NULL,
    protected_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (length(domain) > 0)
);
INSERT OR IGNORE INTO source_domain_protections(domain)
    SELECT lower(rtrim(trim(domain), '.')) FROM tenant_configs
    WHERE side='source' AND length(trim(domain)) > 0;
INSERT OR IGNORE INTO source_domain_protections(domain)
    SELECT lower(rtrim(trim(domain), '.')) FROM superseded_configs
    WHERE side='source' AND length(trim(domain)) > 0;
CREATE TRIGGER IF NOT EXISTS protect_source_on_insert AFTER INSERT ON tenant_configs
WHEN NEW.side='source' AND length(trim(NEW.domain)) > 0
BEGIN
    INSERT OR IGNORE INTO source_domain_protections(domain)
    VALUES(lower(rtrim(trim(NEW.domain), '.')));
END;
CREATE TRIGGER IF NOT EXISTS protect_source_on_update AFTER UPDATE ON tenant_configs
BEGIN
    INSERT OR IGNORE INTO source_domain_protections(domain)
    SELECT lower(rtrim(trim(OLD.domain), '.'))
    WHERE OLD.side='source' AND length(trim(OLD.domain)) > 0;
    INSERT OR IGNORE INTO source_domain_protections(domain)
    SELECT lower(rtrim(trim(NEW.domain), '.'))
    WHERE NEW.side='source' AND length(trim(NEW.domain)) > 0;
END;
CREATE TRIGGER IF NOT EXISTS protect_source_on_delete BEFORE DELETE ON tenant_configs
WHEN OLD.side='source' AND length(trim(OLD.domain)) > 0
BEGIN
    INSERT OR IGNORE INTO source_domain_protections(domain)
    VALUES(lower(rtrim(trim(OLD.domain), '.')));
END;
CREATE TRIGGER IF NOT EXISTS source_protection_no_delete BEFORE DELETE ON source_domain_protections
BEGIN
    SELECT RAISE(ABORT, 'Source domain protection cannot be removed');
END;
CREATE TRIGGER IF NOT EXISTS source_protection_no_update BEFORE UPDATE ON source_domain_protections
BEGIN
    SELECT RAISE(ABORT, 'Source domain protection cannot be changed');
END;
