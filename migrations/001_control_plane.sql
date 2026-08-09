-- 001_control_plane.sql
-- ============================================================================
-- Tables backing the Migration Command Center. Purely additive: every
-- statement is CREATE ... IF NOT EXISTS, and nothing here alters, drops or
-- reindexes a table the migration engines write to. It is safe to apply
-- against a migration.db that has a run in flight -- which is exactly how it
-- was first applied (B4 Trial A was ~5h in).
--
-- Applied by control_plane_db.apply_migrations(), which is idempotent and
-- runs on api_server.py startup.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- operator_actions_log -- who did what, why, and what happened.
--
-- The whole safety model of the control plane rests on this table. Every
-- write action the UI can trigger is written here BEFORE it executes, with
-- outcome/finished_at patched in afterwards. That ordering is deliberate: an
-- action that crashes the server mid-flight still leaves a row saying who
-- tried what and why. A log that only records successes is not an audit
-- trail, it is a scoreboard.
--
-- `reason` is NOT NULL with no default on purpose. There is no code path that
-- can write a row without one, so "the UI forgot to ask" fails loudly at the
-- database rather than silently producing unattributed history.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS operator_actions_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at   TEXT,
    actor         TEXT NOT NULL,           -- operator identity (header/session)
    actor_role    TEXT NOT NULL,           -- admin | viewer, as resolved at call time
    action        TEXT NOT NULL,           -- migrate.start | job.stop | acl.revert_public | ...
    target        TEXT,                    -- user email / batch id / node id / NULL for global
    params_json   TEXT,                    -- the exact request body, for reproducibility
    reason        TEXT NOT NULL,           -- operator-supplied Reason Code. Never optional.
    node_id       TEXT,                    -- which VPS this was dispatched to
    outcome       TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING|OK|FAILED|REFUSED
    detail        TEXT                     -- rc, stderr tail, or refusal reason
);
CREATE INDEX IF NOT EXISTS ix_ops_started ON operator_actions_log(started_at DESC);
CREATE INDEX IF NOT EXISTS ix_ops_actor   ON operator_actions_log(actor, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_ops_outcome ON operator_actions_log(outcome)
    WHERE outcome IN ('FAILED', 'REFUSED');

-- ---------------------------------------------------------------------------
-- fleet_nodes -- one row per VPS, last-write-wins from heartbeats.
--
-- Honest scoping note: there is exactly ONE node today. This exists so the
-- second one is a registration rather than a rewrite, and so "which host is
-- this tab actually driving" has a real answer -- a question that has already
-- cost this project time, because a local run and a deployed run both bind
-- 127.0.0.1:8080 and look identical in a browser.
--
-- Heartbeats are last-write-wins rather than append-only: this table answers
-- "what is true now". Historical series belong in metrics, not here, and an
-- append-only heartbeat table on a 30s interval is a slow disk leak.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fleet_nodes (
    node_id        TEXT PRIMARY KEY,       -- stable id, not the IP (IPs move)
    hostname       TEXT,
    location       TEXT,                   -- public IP, or 'Local machine'
    code_commit    TEXT,                   -- git sha the node is running
    last_seen      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    cpu_pct        REAL,
    ram_pct        REAL,
    disk_pct       REAL,
    active_job     TEXT,                   -- job name, or NULL when idle
    job_pid        INTEGER,
    transfer_mode  TEXT,                   -- server_side | download_upload | link_flip
    -- Denormalised counters so the fleet grid renders from one cheap read
    -- instead of a per-node aggregate over audit_log on every heartbeat.
    users_done     INTEGER NOT NULL DEFAULT 0,
    users_running  INTEGER NOT NULL DEFAULT 0,
    users_failed   INTEGER NOT NULL DEFAULT 0,
    error_rate     REAL NOT NULL DEFAULT 0.0,
    notes          TEXT
);
CREATE INDEX IF NOT EXISTS ix_fleet_seen ON fleet_nodes(last_seen DESC);

-- ---------------------------------------------------------------------------
-- public_share_watch -- the security dashboard's memory.
--
-- Motivated by a real incident: link_flip's restore step tried to re-create
-- the `owner` permission, Drive rejects that with 403, the restore failed, and
-- 93 target files were left `anyone:reader`. Nothing in the system noticed --
-- it took a manual acl_audit diff to find them.
--
-- A row is written when a public grant is OBSERVED and cleared when it is
-- confirmed revoked, so "how many public files exist right now" is a single
-- indexed count rather than a live crawl of both tenants.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public_share_watch (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant        TEXT NOT NULL,           -- source | target
    user_email    TEXT NOT NULL,
    file_id       TEXT NOT NULL,
    file_name     TEXT,
    permission_id TEXT,
    grant_type    TEXT,                    -- anyone | domain
    role          TEXT,
    detected_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    revoked_at    TEXT,                    -- NULL = still exposed
    revoked_by    TEXT,                    -- operator_actions_log.id that cleared it
    UNIQUE (tenant, file_id, permission_id)
);
CREATE INDEX IF NOT EXISTS ix_public_open ON public_share_watch(tenant, revoked_at);
