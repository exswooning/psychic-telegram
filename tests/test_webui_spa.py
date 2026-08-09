"""
tests/test_webui_spa.py
========================
Real data for migration-webui, the React dashboard that shipped wired to
nothing: `useMigration.ts` called Math.random() on a timer, and there was
not a single `fetch(` in its source tree. webui_spa.py replaces that with
reads from the actual ledger -- these tests pin the mapping and, more
importantly, the places where an honest "we don't know" beats a fabricated
number.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

import webui_spa
from db import MigrationDB


@pytest.fixture
def ledger(tmp_path):
    db = MigrationDB(str(tmp_path / "m.db"))
    db.init_schema()
    return db


@pytest.fixture
def reader(ledger):
    """A plain read connection, exactly what _db_conn() hands the real
    payload builders -- not the ledger's own write connection."""
    conn = sqlite3.connect(ledger.path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _seed_identity(ledger, source, target, status="DONE"):
    with ledger.write() as c:
        c.execute(
            "INSERT INTO identity_map (source_email, target_email, status) "
            "VALUES (?,?,?)", (source, target, status))


def _audit(ledger, user, item_type, status, n=1, error=None):
    with ledger.write() as c:
        for i in range(n):
            c.execute(
                "INSERT INTO audit_log (source_user, item_id, item_type, "
                "status, error_message) VALUES (?,?,?,?,?)",
                (user, f"{item_type}-{i}", item_type, status, error))


class TestServiceProgress:
    def test_an_explicit_total_governs_the_percentage(self):
        got = webui_spa._service_progress(5, 0, 10)
        assert got["progress"] == 50
        assert got["itemsTotal"] == 10

    def test_no_total_falls_back_to_attempted_as_a_floor(self):
        """The honest case: contacts/tasks/chat/permissions have no discovery
        pass, so there is no real 'total' to report. Reporting one anyway
        would be inventing a target that does not exist. With nothing
        outstanding, attempted-so-far and done coincide, so it reads
        'completed' -- not because a target was met, but because there is
        nothing left queued against the only total this schema can offer."""
        got = webui_spa._service_progress(3, 0, None)
        assert got["itemsTotal"] == 3   # attempted, not a guessed total
        assert got["status"] == "completed"

    def test_a_pending_failure_against_the_floor_is_in_progress_not_done(self):
        """2 of the 5 attempted are still FAILED, so 'completed' would be
        wrong even though the floor (done+failed) has technically been met."""
        got = webui_spa._service_progress(3, 2, None)
        assert got["itemsTotal"] == 5
        assert got["status"] == "in_progress"

    def test_zero_attempted_and_no_total_is_not_started(self):
        got = webui_spa._service_progress(0, 0, None)
        assert got["status"] == "not_started"

    def test_all_failed_is_failed_not_in_progress(self):
        got = webui_spa._service_progress(0, 3, None)
        assert got["status"] == "failed"


class TestUsersPayload:
    def test_status_mapping(self, ledger, reader):
        _seed_identity(ledger, "alice@src.com", "alice@tgt.com", status="RUNNING")
        _seed_identity(ledger, "bob@src.com", "bob@tgt.com", status="DONE")
        _seed_identity(ledger, "carol@src.com", "carol@tgt.com", status="PAUSED_QUOTA")

        users = webui_spa.users_payload(reader, cap_bytes=750 * 1024**3)
        by_email = {u["email"]: u for u in users}

        assert by_email["alice@src.com"]["status"] == "in_progress"
        assert by_email["bob@src.com"]["status"] == "completed"
        assert by_email["carol@src.com"]["status"] == "needs_attention"

    def test_contacts_tasks_chat_are_populated_from_the_extra_query(
            self, ledger, reader):
        """tui.UserRow does not carry these -- the whole reason this module's
        extra query exists rather than reusing tui.collect_snapshot() alone."""
        _seed_identity(ledger, "alice@src.com", "alice@tgt.com")
        _audit(ledger, "alice@src.com", "contact", "SUCCESS", n=4)
        _audit(ledger, "alice@src.com", "task", "SUCCESS", n=2)
        _audit(ledger, "alice@src.com", "chat_message", "SUCCESS", n=7)

        users = webui_spa.users_payload(reader, cap_bytes=750 * 1024**3)
        details = users[0]["details"]

        assert details["contacts"]["itemsCompleted"] == 4
        assert details["tasks"]["itemsCompleted"] == 2
        assert details["chat"]["itemsCompleted"] == 7

    def test_permissions_is_separate_from_drive(self, ledger, reader):
        """A user can finish every file and still have ACL grants
        outstanding; conflating the two buckets would hide that."""
        _seed_identity(ledger, "alice@src.com", "alice@tgt.com")
        _audit(ledger, "alice@src.com", "file", "SUCCESS", n=10)
        _audit(ledger, "alice@src.com", "acl", "FAILED", n=2)

        details = webui_spa.users_payload(reader, cap_bytes=750 * 1024**3)[0]["details"]

        assert details["drive"]["itemsCompleted"] == 10
        assert details["permissions"]["itemsCompleted"] == 0
        assert details["permissions"]["itemsTotal"] == 2  # attempted, all failed

    def test_retries_and_warnings_are_never_fabricated(self, ledger, reader):
        """This ledger cannot distinguish 'succeeded on retry' from 'succeeded
        first try', and SKIPPED almost always means 'already migrated', not a
        warning. Reporting anything but 0 here would be inventing a signal
        this schema does not carry."""
        _seed_identity(ledger, "alice@src.com", "alice@tgt.com")
        _audit(ledger, "alice@src.com", "file", "SKIPPED_ALREADY_DONE", n=50)

        u = webui_spa.users_payload(reader, cap_bytes=750 * 1024**3)[0]
        assert u["retries"] == 0
        assert u["warnings"] == 0

    def test_display_name_is_derived_not_hardcoded(self, ledger, reader):
        """The store this replaces had 'Alice Johnson' hardcoded for
        alice@... -- a real tenant will not have that surname."""
        _seed_identity(ledger, "j.smith@src.com", "j.smith@tgt.com")
        u = webui_spa.users_payload(reader, cap_bytes=750 * 1024**3)[0]
        assert u["name"] == "J Smith"
        assert "Johnson" not in u["name"]


class TestActivityPayload:
    def test_ordered_by_id_not_by_timestamp_text(self, ledger, reader):
        """audit_log has no index on timestamp; sorting by it on every SPA
        poll would be a full-table sort on a busy dashboard. `id` is the
        primary key and increases with insertion order, so this constructs
        rows whose STORED timestamp strings are out of order and confirms the
        output still follows insertion (id) order, not a re-sort of the
        timestamp column."""
        with ledger.write() as c:
            c.execute("INSERT INTO audit_log (source_user, item_id, item_type,"
                      " status, timestamp) VALUES (?,?,?,?,?)",
                      ("alice@src.com", "first", "file", "SUCCESS",
                       "2099-01-01T00:00:00Z"))   # inserted first, latest-looking timestamp
            c.execute("INSERT INTO audit_log (source_user, item_id, item_type,"
                      " status, timestamp) VALUES (?,?,?,?,?)",
                      ("alice@src.com", "second", "file", "SUCCESS",
                       "2000-01-01T00:00:00Z"))   # inserted second, earliest-looking timestamp

        out = webui_spa.activity_payload(reader)

        # If this sorted by the timestamp text, "second" (2000-01-01) would
        # come last, not first. It comes first, because id DESC put the most
        # recently INSERTED row on top regardless of what timestamp it claims.
        assert "second" in out[0]["id"]

    def test_limit_is_respected(self, ledger, reader):
        _audit(ledger, "alice@src.com", "file", "SUCCESS", n=30)
        assert len(webui_spa.activity_payload(reader, limit=5)) == 5

    def test_failed_rows_map_to_failed_not_completed(self, ledger, reader):
        _audit(ledger, "alice@src.com", "file", "FAILED_QUOTA", n=1,
              error="quota exceeded")
        out = webui_spa.activity_payload(reader)
        assert out[0]["status"] == "failed"
        assert out[0]["details"] == "quota exceeded"


class TestMetricsPayload:
    def test_network_is_zero_not_fabricated(self):
        """No persistent byte-rate counter exists across a run. 0 is the
        honest answer; this is the field the original store filled with
        Math.random() * 2 - 1 forever."""
        from config import Settings

        got = webui_spa.metrics_payload(Settings(), 750 * 1024**3, {})
        assert got["network"] == {"up": 0.0, "down": 0.0}

    def test_workers_max_is_the_real_hard_cap(self):
        import resources
        from config import Settings

        got = webui_spa.metrics_payload(Settings(), 750 * 1024**3, {})
        assert got["workers"]["max"] == resources.HARD_CAP

    def test_no_calls_yet_reads_as_healthy_not_unknown(self):
        """Nothing has failed because nothing has been attempted -- a fresh
        process with no traffic is not the same as a degraded one."""
        import metrics as metrics_mod
        from config import Settings

        metrics_mod.METRICS.reset()
        got = webui_spa.metrics_payload(Settings(), 750 * 1024**3, {})
        assert got["apiHealth"] == "healthy"

    def test_empty_totals_does_not_raise(self):
        """totals is {} whenever the DB is missing or unreadable; every key
        this function reads from it must have a safe default."""
        from config import Settings

        webui_spa.metrics_payload(Settings(), 750 * 1024**3, {})  # must not raise


class TestVerificationPayload:
    def test_drive_verified_when_done_meets_expected(self, ledger, reader):
        from config import Settings

        _seed_identity(ledger, "alice@src.com", "alice@tgt.com")
        _audit(ledger, "alice@src.com", "file", "SUCCESS", n=10)
        with ledger.write() as c:
            c.execute("INSERT INTO discovery (source_user, file_count, "
                      "folder_count) VALUES (?,?,?)", ("alice@src.com", 10, 0))

        out = webui_spa.verification_payload(reader, Settings())
        drive = next(r for r in out if r["type"] == "Drive")
        assert drive["status"] == "verified"

    def test_drive_mismatch_when_short(self, ledger, reader):
        from config import Settings

        _seed_identity(ledger, "alice@src.com", "alice@tgt.com")
        _audit(ledger, "alice@src.com", "file", "SUCCESS", n=7)
        with ledger.write() as c:
            c.execute("INSERT INTO discovery (source_user, file_count, "
                      "folder_count) VALUES (?,?,?)", ("alice@src.com", 10, 0))

        out = webui_spa.verification_payload(reader, Settings())
        drive = next(r for r in out if r["type"] == "Drive")
        assert drive["status"] == "mismatch"
        assert drive["sourceCount"] == 10 and drive["targetCount"] == 7

    def test_share_access_uses_the_real_acl_audit_output_when_present(
            self, ledger, reader, tmp_path, monkeypatch):
        """The one row here that is a genuine independent verification rather
        than a ledger completion proxy -- it must come from acl_audit.py's
        real output, not be derived from audit_log at all."""
        from config import Settings

        audit_file = tmp_path / "acl_audit.json"
        audit_file.write_text(json.dumps(
            {"totals": {"grants_source": 500, "grants_matched": 500}}))
        monkeypatch.setattr(webui_spa, "__file__", str(tmp_path / "webui_spa.py"))

        out = webui_spa.verification_payload(reader, Settings())
        share = next(r for r in out if r["type"] == "Share access")
        assert share["status"] == "verified"
        assert share["sourceCount"] == 500

    def test_share_access_is_not_started_without_a_real_audit_file(
            self, ledger, reader, tmp_path, monkeypatch):
        from config import Settings

        monkeypatch.setattr(webui_spa, "__file__", str(tmp_path / "webui_spa.py"))
        out = webui_spa.verification_payload(reader, Settings())
        share = next(r for r in out if r["type"] == "Share access")
        assert share["status"] == "not_started"
        assert share["ageSeconds"] is None

    def test_share_access_reports_its_own_staleness(
            self, ledger, reader, tmp_path, monkeypatch):
        """acl_audit.py is a standalone script -- nothing during migrate or
        delta ever rewrites acl_audit.json, so a 3-day-old file can sit next
        to a migration that has been running for an hour with no way to
        tell them apart except this. Confirmed live: a "79.4%" card was
        mistaken for current progress when the file was actually 3 days
        old."""
        import os
        from config import Settings

        audit_file = tmp_path / "acl_audit.json"
        audit_file.write_text(json.dumps(
            {"totals": {"grants_source": 500, "grants_matched": 397}}))
        old = time.time() - 3 * 86400
        os.utime(audit_file, (old, old))
        monkeypatch.setattr(webui_spa, "__file__", str(tmp_path / "webui_spa.py"))

        out = webui_spa.verification_payload(reader, Settings())
        share = next(r for r in out if r["type"] == "Share access")
        assert share["ageSeconds"] == pytest.approx(3 * 86400, abs=5)


class TestReportPayload:
    def test_no_job_ever_run_reports_an_honest_dash(self, ledger, reader):
        from config import Settings

        report = webui_spa.report_payload(reader, Settings(), 0.0, 0.0)
        assert report["totalDuration"] == "—"
        assert report["averageThroughput"] == "—"

    def test_a_finished_job_produces_a_real_duration(self, ledger, reader):
        from config import Settings

        started, finished = 1000.0, 1000.0 + 3661   # 1h 1m 1s
        report = webui_spa.report_payload(reader, Settings(), started, finished)
        assert report["totalDuration"] == "1h 1m"

    def test_item_counts_are_exact_from_the_ledger(self, ledger, reader):
        from config import Settings

        _seed_identity(ledger, "alice@src.com", "alice@tgt.com")
        _audit(ledger, "alice@src.com", "message", "SUCCESS", n=42)
        _audit(ledger, "alice@src.com", "file", "SUCCESS", n=13)

        report = webui_spa.report_payload(reader, Settings(), 1000.0, 2000.0)
        assert report["emailsMigrated"] == 42
        assert report["driveFilesMigrated"] == 13


class TestStagesPayload:
    """
    The Dashboard's pipeline widget used to be frozen fake data (Gmail
    permanently at 68%, Drive at 42%) that nothing in the frontend ever
    updated. stages_payload() replaces it -- these tests pin both the real
    rollups (gmail/drive/etc, sourced from users_payload) and the honestly
    unknown ones (user_creation has no ledger table at all).
    """

    def test_no_users_is_all_waiting_not_fabricated_progress(self, ledger, reader):
        from config import Settings

        stages = webui_spa.stages_payload(reader, Settings(), job_finished=0.0)
        by_id = {s["id"] for s in stages}
        assert {"discovery", "gmail", "drive", "user_creation"} <= by_id
        # user_creation has no ledger signal at all and is always
        # "not_started" (never "waiting", which would imply it is queued
        # behind something this engine can actually observe).
        assert all(s["status"] in ("waiting", "not_started") for s in stages)
        assert all(s["progress"] == 0 for s in stages)

    def test_gmail_stage_rolls_up_real_per_user_mailbox_progress(
            self, ledger, reader):
        from config import Settings

        _seed_identity(ledger, "alice@src.com", "alice@tgt.com")
        _seed_identity(ledger, "bob@src.com", "bob@tgt.com")
        with ledger.write() as c:
            c.execute("INSERT INTO discovery (source_user, messages_total) "
                      "VALUES (?,?)", ("alice@src.com", 10))
            c.execute("INSERT INTO discovery (source_user, messages_total) "
                      "VALUES (?,?)", ("bob@src.com", 10))
        _audit(ledger, "alice@src.com", "message", "SUCCESS", n=10)
        _audit(ledger, "bob@src.com", "message", "SUCCESS", n=5)

        stages = webui_spa.stages_payload(reader, Settings(), job_finished=0.0)
        gmail = next(s for s in stages if s["id"] == "gmail")
        assert gmail["progress"] == 75          # (100 + 50) / 2, matches Users page exactly
        assert gmail["usersCompleted"] == 1      # only alice hit 100%
        assert gmail["status"] == "in_progress"

    def test_discovery_stage_reflects_real_table_coverage(self, ledger, reader):
        from config import Settings

        _seed_identity(ledger, "alice@src.com", "alice@tgt.com")
        _seed_identity(ledger, "bob@src.com", "bob@tgt.com")
        with ledger.write() as c:
            c.execute("INSERT INTO discovery (source_user, file_count) "
                      "VALUES (?,?)", ("alice@src.com", 5))

        stages = webui_spa.stages_payload(reader, Settings(), job_finished=0.0)
        discovery = next(s for s in stages if s["id"] == "discovery")
        assert discovery["status"] == "in_progress"
        assert discovery["usersCompleted"] == 1
        assert discovery["usersTotal"] == 2

    def test_user_creation_is_always_reported_unknown_not_guessed(
            self, ledger, reader):
        """provision.ensure_users never writes to audit_log (see
        main.py's cmd_provision_users), so there is no ledger signal for
        this stage at all -- it must never claim completed."""
        from config import Settings

        _seed_identity(ledger, "alice@src.com", "alice@tgt.com", status="DONE")
        _audit(ledger, "alice@src.com", "file", "SUCCESS", n=5)

        stages = webui_spa.stages_payload(reader, Settings(), job_finished=0.0)
        user_creation = next(s for s in stages if s["id"] == "user_creation")
        assert user_creation["status"] == "not_started"

    def test_authentication_is_proxied_by_real_activity_ever_happening(
            self, ledger, reader):
        from config import Settings

        _seed_identity(ledger, "alice@src.com", "alice@tgt.com")
        before = webui_spa.stages_payload(reader, Settings(), job_finished=0.0)
        assert next(s for s in before if s["id"] == "authentication")["status"] == "waiting"

        _audit(ledger, "alice@src.com", "file", "SUCCESS", n=1)
        after = webui_spa.stages_payload(reader, Settings(), job_finished=0.0)
        assert next(s for s in after if s["id"] == "authentication")["status"] == "completed"

    def test_report_stage_completes_only_once_a_job_has_actually_finished(
            self, ledger, reader):
        from config import Settings

        stages = webui_spa.stages_payload(reader, Settings(), job_finished=0.0)
        assert next(s for s in stages if s["id"] == "report")["status"] == "waiting"

        stages = webui_spa.stages_payload(reader, Settings(), job_finished=12345.0)
        assert next(s for s in stages if s["id"] == "report")["status"] == "completed"
