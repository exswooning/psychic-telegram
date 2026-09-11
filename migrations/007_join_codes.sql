-- 007_join_codes.sql
--
-- Adding a machine meant copying a 43-character shared token onto it, by
-- hand, from a page on another computer. The token is also long-lived and
-- the SAME secret for every node, so the thing being carried around on a
-- clipboard is the credential for the whole control plane.
--
-- A join code replaces that for the common case: short enough to read off
-- a screen and type, single-use, and dead in fifteen minutes. It is the
-- model Tailscale auth keys and kubeadm tokens use, for the same reason.
--
-- The CODE ITSELF IS NEVER STORED. Only its SHA-256, exactly as sessions
-- would be if they were written today: a code in the clear in this table
-- is a credential anyone with the database can redeem, and the whole point
-- of the short window is to bound that exposure.
--
-- SHA-256 with no salt and no stretching is right HERE and wrong for a
-- password: these are 40 bits of process-random entropy, not something a
-- human chose, so there is no dictionary to run and nothing for a slow KDF
-- to buy.
CREATE TABLE IF NOT EXISTS join_codes (
    code_hash   TEXT PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    expires_at  TEXT NOT NULL,
    -- Set on redemption. Single use: a code that worked once must not work
    -- again, because the script it serves contains the real node token.
    used_at     TEXT,
    used_from   TEXT
);

-- Redemption is unauthenticated by necessity -- the joining machine has no
-- credential yet, that is what it is collecting -- so it is guessable in
-- principle and must be rate limited in practice. Keyed by source address
-- because there is no account to key on.
CREATE TABLE IF NOT EXISTS join_attempts (
    addr            TEXT PRIMARY KEY,
    failed_count    INTEGER NOT NULL DEFAULT 0,
    first_failed_at TEXT,
    locked_until    TEXT
);
