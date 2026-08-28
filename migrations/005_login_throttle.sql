-- 005_login_throttle.sql
--
-- There was no limit on login attempts. None: no counter, no lockout, no
-- delay. PBKDF2 at 200,000 iterations makes each guess expensive, which
-- helps against offline cracking and hurts here -- unlimited attempts
-- against a 2-core box is a CPU-exhaustion vector as well as a
-- credential-guessing one.
--
-- Keyed by email rather than by IP. An attacker distributes IPs trivially,
-- and the thing being protected is an account, not a network location.
-- The cost is that someone can lock a known user out for the window; that
-- is the accepted trade, and the window is deliberately short.
CREATE TABLE IF NOT EXISTS login_attempts (
    email           TEXT PRIMARY KEY,
    failed_count    INTEGER NOT NULL DEFAULT 0,
    first_failed_at TEXT,
    locked_until    TEXT
);
