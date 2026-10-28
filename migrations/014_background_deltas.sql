-- Opt-in background follow-up. Existing mirrors keep their existing policy.
CREATE TABLE IF NOT EXISTS background_deltas (
 account_id INTEGER PRIMARY KEY,
 background INTEGER NOT NULL DEFAULT 0,
 follow_domains INTEGER NOT NULL DEFAULT 0,
 recovery_admins TEXT NOT NULL DEFAULT '{}',
 state TEXT NOT NULL DEFAULT 'OFF',
 detail TEXT,
 observation TEXT,
 quiet_since TEXT,
 completed_at TEXT,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tenant_follow_bindings (
 account_id INTEGER NOT NULL,
 side TEXT NOT NULL CHECK(side IN ('source','target')),
 customer_id TEXT NOT NULL,
 admin_id TEXT NOT NULL,
 client_id TEXT NOT NULL,
 original_domain TEXT NOT NULL,
 current_domain TEXT NOT NULL,
 admin_email TEXT NOT NULL,
 ledger_source TEXT NOT NULL,
 ledger_target TEXT NOT NULL,
 PRIMARY KEY(account_id,side)
);
CREATE TABLE IF NOT EXISTS tenant_follow_users (
 account_id INTEGER NOT NULL,
 side TEXT NOT NULL,
 logical_email TEXT NOT NULL,
 user_id TEXT NOT NULL,
 PRIMARY KEY(account_id,side,logical_email),
 UNIQUE(account_id,side,user_id)
);
