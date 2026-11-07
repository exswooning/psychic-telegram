-- Per-attempt request budget; no mailbox identifiers or message data are stored.
CREATE TABLE IF NOT EXISTS candidate_canary_reads (
    attempt_id INTEGER NOT NULL REFERENCES error_fix_attempts(id),
    side TEXT NOT NULL CHECK(side IN ('source','target')),
    reads INTEGER NOT NULL DEFAULT 0,
    successful_slots INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(attempt_id, side)
);
CREATE TABLE IF NOT EXISTS candidate_canary_artifacts (
    attempt_id INTEGER PRIMARY KEY REFERENCES error_fix_attempts(id),
    candidate_commit TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    artifact_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_case_snapshots (
    attempt_id INTEGER PRIMARY KEY REFERENCES error_fix_attempts(id),
    case_seen_at TEXT NOT NULL
);
