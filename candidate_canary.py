"""Bounded Gmail canary for the operator-authorized s. -> t. sandbox pair.

The coding worker receives only a success/status observation. Mail content,
message IDs, headers, Google credentials and customer tenants never leave the
server. The candidate requests fixed slots in exactly two sandbox mailboxes.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import candidate_scope
import control_plane_db as cpdb
import error_resolution as errors

SOURCE_DOMAIN = "s.sarafgloabalexim.com"
TARGET_DOMAIN = "t.sarafgloabalexim.com"
SOURCE_USER = "info@" + SOURCE_DOMAIN
TARGET_USER = "nest@" + TARGET_DOMAIN
SANDBOX_ACCOUNT = 3
MAX_READS_PER_SIDE = 4


class CanaryRefused(ValueError):
    pass


def _installed_release(root):
    try:
        return (Path(root) / "DEPLOYED_COMMIT").read_text().strip()
    except OSError:
        raise CanaryRefused("Installed release identity is unavailable") from None


def reserve(runner, attempt_id, lease, commit, side, slot, proof, root):
    if side not in {"source", "target"} or type(slot) is not int or slot not in {0, 1}:
        raise CanaryRefused(
            "Only the two configured sandbox metadata slots are allowed"
        )
    with cpdb.rw() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT a.*,c.service,c.account_id FROM error_fix_attempts a "
            "JOIN error_cases c ON c.id=a.case_id WHERE a.id=? AND a.runner_id=? "
            "AND a.lease_hash=?",
            (attempt_id, runner["id"], errors.digest(lease)),
        ).fetchone()
        if (
            not row
            or row["finished_at"]
            or row["lease_until"] < time.time()
            or row["state"] != "TESTING"
            or row["service"] != "gmail"
            or row["candidate_commit"] != commit
            or (
                runner["account_id"] != -1 and runner["account_id"] != row["account_id"]
            )
        ):
            raise CanaryRefused("A live, scoped Gmail candidate lease is required")
        snapshot = db.execute(
            "SELECT s.case_seen_at,c.last_seen FROM candidate_case_snapshots s "
            "JOIN error_cases c ON c.id=? WHERE s.attempt_id=?",
            (row["case_id"], attempt_id),
        ).fetchone()
        if not snapshot or snapshot["case_seen_at"] != snapshot["last_seen"]:
            raise CanaryRefused("The case changed since this candidate was claimed")
        base = _installed_release(root)
        if row["source_commit"] != base:
            raise CanaryRefused("Candidate parent does not match the installed release")
        try:
            artifact_hash = candidate_scope.validate_proof(proof, commit, base)
        except (ValueError, OSError) as exc:
            raise CanaryRefused(str(exc)) from None
        stored = db.execute(
            "SELECT candidate_commit,base_commit,artifact_hash FROM candidate_canary_artifacts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if stored and tuple(stored) != (commit, base, artifact_hash):
            raise CanaryRefused("Candidate artifact changed during qualification")
        db.execute(
            "INSERT OR IGNORE INTO candidate_canary_artifacts VALUES(?,?,?,?)",
            (attempt_id, commit, base, artifact_hash),
        )
        current = db.execute(
            "SELECT reads FROM candidate_canary_reads WHERE attempt_id=? AND side=?",
            (attempt_id, side),
        ).fetchone()
        if current and current["reads"] >= MAX_READS_PER_SIDE:
            raise CanaryRefused("Sandbox read budget exhausted for this candidate")
        db.execute(
            "INSERT INTO candidate_canary_reads(attempt_id,side,reads) VALUES(?,?,1) "
            "ON CONFLICT(attempt_id,side) DO UPDATE SET reads=reads+1",
            (attempt_id, side),
        )


def _sandbox_settings():
    from config import Settings

    settings = Settings(account_id=SANDBOX_ACCOUNT)
    if (settings.source_domain.lower(), settings.target_domain.lower()) != (
        SOURCE_DOMAIN,
        TARGET_DOMAIN,
    ):
        raise CanaryRefused(
            "Configured sandbox domains no longer match the authorized pair"
        )
    if settings.auth_mode != "key":
        raise CanaryRefused(
            "The sandbox canary requires configured service-account keys"
        )
    with sqlite3.connect("file:" + settings.db_path + "?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT target_email FROM identity_map WHERE lower(source_email)=?",
            (SOURCE_USER,),
        ).fetchone()
    if not row or row[0].lower() != TARGET_USER:
        raise CanaryRefused("The authorized sandbox user mapping has changed")
    return settings


def gmail_read(side, slot):
    """Perform only list/get metadata reads, returning no provider response data."""
    import google_auth_httplib2
    import httplib2
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    settings = _sandbox_settings()
    user = SOURCE_USER if side == "source" else TARGET_USER
    key = settings.source_sa_key if side == "source" else settings.target_sa_key
    # Source token is structurally read-only. Target's existing delegated grant
    # includes gmail.modify; only list/get are called and that token stays here.
    scope = (
        "https://www.googleapis.com/auth/gmail.readonly"
        if side == "source"
        else "https://www.googleapis.com/auth/gmail.modify"
    )
    creds = service_account.Credentials.from_service_account_file(
        key, scopes=[scope]
    ).with_subject(user)
    http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=15))
    service = build(
        "gmail", "v1", http=http, cache_discovery=False, static_discovery=True
    )
    try:
        ids = (
            service.users()
            .messages()
            .list(userId="me", maxResults=2)
            .execute()
            .get("messages", [])
        )
        if len(ids) <= slot:
            raise CanaryRefused(
                "The configured sandbox mailbox lacks enough messages for the canary"
            )
        service.users().messages().get(
            userId="me", id=ids[slot]["id"], format="minimal", fields="id"
        ).execute()
        return {"status": 200}
    finally:
        service.close()


def probe(runner, attempt_id, lease, commit, side, slot, proof, root):
    if not errors.policy()["enabled"]:
        raise CanaryRefused("Automatic coding is paused")
    if not errors.globally_idle(root):
        raise CanaryRefused("A migration, verification or repair has priority")
    reserve(runner, attempt_id, lease, commit, side, slot, proof, root)
    if not errors.globally_idle(root):
        raise CanaryRefused("A migration, verification or repair has priority")
    try:
        result = gmail_read(side, slot)
        if result != {"status": 200}:
            raise CanaryRefused("Sandbox Gmail metadata read did not succeed")
        with cpdb.rw() as db:
            cur = db.execute(
                "UPDATE candidate_canary_reads SET successful_slots=successful_slots | ? "
                "WHERE attempt_id=? AND side=? AND EXISTS "
                "(SELECT 1 FROM error_fix_attempts a WHERE a.id=? AND a.runner_id=? "
                "AND a.lease_hash=? AND a.lease_until>=? AND a.finished_at IS NULL "
                "AND a.state='TESTING' AND a.candidate_commit=?)",
                (
                    1 << slot,
                    attempt_id,
                    side,
                    attempt_id,
                    runner["id"],
                    errors.digest(lease),
                    time.time(),
                    commit,
                ),
            )
            if not cur.rowcount:
                raise CanaryRefused("Candidate lease expired during the sandbox read")
        return result
    except CanaryRefused:
        raise
    except Exception as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        raise CanaryRefused(
            "Sandbox Gmail metadata read failed"
            + (f" (HTTP {status})" if isinstance(status, int) else "")
        ) from None
