-- 002_accounts.sql
-- ============================================================================
-- SaaS accounts: real customers, real sessions, and where each customer's
-- own tenant config/keys/database live. Purely additive, same rules as
-- 001_control_plane.sql -- CREATE ... IF NOT EXISTS only, safe to apply
-- against a live migration.db.
--
-- Two-tier isolation, deliberately: these three tables are the ONLY new
-- schema. A customer's actual migration data (identity_map, id_mapping,
-- audit_log, upload_ledger...) is isolated by FILE, not by an account_id
-- column -- each account gets its own migration.db under
-- data/accounts/{id}/migration.db (see accounts_auth.py, config.py's
-- Settings(account_id=...)). That keeps db.py, main.py and every engine
-- script untouched: they already take a db path from Settings, so pointing
-- them at a different file per account requires no schema or query changes
-- in code that has spent months getting the same real migration right.
--
-- Applied by control_plane_db.apply_migrations(), which is idempotent and
-- runs on api_server.py startup.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- accounts -- one row per paying (or trialing) customer.
--
-- password_hash is "pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>" --
-- stdlib hashlib.pbkdf2_hmac, no external dependency, self-describing so the
-- iteration count can be raised later without invalidating existing hashes
-- (accounts_auth.verify_password reads the stored iteration count, not a
-- hardcoded one).
--
-- `plan` exists now so Pricing.tsx has something real to write to; there is
-- no billing enforcement in this pass -- see accounts_auth.py's module
-- docstring for exactly what "trial" means today (nothing, yet).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS accounts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    name           TEXT NOT NULL,
    plan           TEXT NOT NULL DEFAULT 'trial',
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------------------------------------------------------------------------
-- sessions -- opaque bearer tokens, not JWT. A stolen JWT is valid until it
-- expires no matter what the server does; a stolen session row can be
-- deleted the moment it is noticed. For a product this size, "can revoke
-- instantly" beats "one fewer database read per request".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    expires_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_account ON sessions(account_id);

-- ---------------------------------------------------------------------------
-- tenant_configs -- the per-account replacement for the single global
-- env.sh. One row per (account, side). db_path/sa_key_path point at files
-- under that account's own data/accounts/{id}/ and keys/{id}/ directories,
-- set once at account creation (accounts_auth.create_account) and otherwise
-- updated by that account's own Quick Setup run (full_setup.py), never by
-- another account's.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenant_configs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    side         TEXT NOT NULL CHECK (side IN ('source','target')),
    domain       TEXT,
    admin_email  TEXT,
    sa_key_path  TEXT,
    db_path      TEXT,
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(account_id, side)
);
