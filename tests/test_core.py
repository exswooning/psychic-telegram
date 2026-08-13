"""
tests/test_core.py
==================
Persistence, resilience primitives, discovery, scope manifest, and the TUI's
snapshot collector. These are fast, pure, and have no Google surface at all.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

import scope as scope_mod
import tui
from db import MigrationDB, bulk_seed_identities
from resilience import (
    DailyQuotaGuard,
    PermanentAPIError,
    QuotaExhausted,
    RateLimiter,
    retry_on_google_error,
)
from tests.conftest import SRC_USER, TGT_USER
from tests.fakes import http_error


# ======================================================================
# DB
# ======================================================================
def test_identity_resolution_is_case_insensitive(db):
    bulk_seed_identities(db, [("J.Smith@TenantA.com", "john@tenantb.com")])
    assert db.resolve_identity("j.smith@tenanta.com") == "john@tenantb.com"
    assert db.resolve_identity("J.SMITH@TENANTA.COM") == "john@tenantb.com"
    assert db.resolve_identity("nobody@tenanta.com") is None
    assert db.resolve_identity(None) is None


def test_id_mapping_upserts_rather_than_duplicating(db):
    db.record_mapping(SRC_USER, "s1", "t1", "folder")
    db.record_mapping(SRC_USER, "s1", "t2", "folder")
    assert db.get_target_id(SRC_USER, "s1", "folder") == "t2"
    n = db.conn.execute("SELECT COUNT(*) c FROM id_mapping").fetchone()["c"]
    assert n == 1


def test_id_mapping_is_scoped_per_user(db):
    db.record_mapping("a@x.com", "same-id", "t-a", "file")
    db.record_mapping("b@x.com", "same-id", "t-b", "file")
    assert db.get_target_id("a@x.com", "same-id", "file") == "t-a"
    assert db.get_target_id("b@x.com", "same-id", "file") == "t-b"


def test_audit_upsert_preserves_modified_time_when_omitted(db):
    db.log_audit(SRC_USER, "f1", "file", "SUCCESS",
                 modified_time="2024-01-01T00:00:00Z")
    db.log_audit(SRC_USER, "f1", "file", "FAILED", "later error")
    row = db.get_audit(SRC_USER, "f1", "file")
    assert row["status"] == "FAILED"
    assert row["modified_time"] == "2024-01-01T00:00:00Z"


def test_last_synced_time_only_returns_for_success(db):
    db.log_audit(SRC_USER, "f1", "file", "FAILED", "boom",
                 modified_time="2024-01-01T00:00:00Z")
    assert db.last_synced_modified_time(SRC_USER, "f1", "file") is None
    db.log_audit(SRC_USER, "f1", "file", "SUCCESS",
                 modified_time="2024-01-01T00:00:00Z")
    assert db.last_synced_modified_time(SRC_USER, "f1", "file") == \
        "2024-01-01T00:00:00Z"


def test_schema_upgrade_is_additive(tmp_path):
    """A DB from an older build must gain columns without losing rows."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE discovery (
            source_user TEXT NOT NULL,
            scanned_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            file_count INTEGER NOT NULL DEFAULT 0,
            folder_count INTEGER NOT NULL DEFAULT 0,
            native_count INTEGER NOT NULL DEFAULT 0,
            shortcut_count INTEGER NOT NULL DEFAULT 0,
            max_depth INTEGER NOT NULL DEFAULT 0,
            total_bytes INTEGER NOT NULL DEFAULT 0,
            largest_bytes INTEGER NOT NULL DEFAULT 0,
            oversized_native INTEGER NOT NULL DEFAULT 0,
            est_days REAL NOT NULL DEFAULT 0,
            mime_histogram TEXT,
            PRIMARY KEY (source_user, scanned_at));
        INSERT INTO discovery (source_user, file_count) VALUES ('old@x.com', 42);
    """)
    conn.commit()
    conn.close()

    d = MigrationDB(path)
    cols = {r["name"] for r in d.conn.execute("PRAGMA table_info(discovery)")}
    assert {"messages_total", "threads_total", "user_label_count"} <= cols
    row = d.conn.execute("SELECT * FROM discovery").fetchone()
    assert row["file_count"] == 42, "existing rows must survive the upgrade"
    d.close()


def test_concurrent_writes_do_not_deadlock(db):
    """The engine writes from N worker threads; SQLite must cope."""
    errors: list[Exception] = []

    def worker(n: int):
        try:
            for i in range(40):
                db.log_audit(f"u{n}@x.com", f"item{i}", "file", "SUCCESS",
                             bytes_moved=10)
                db.record_mapping(f"u{n}@x.com", f"s{i}", f"t{i}", "file")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent writes failed: {errors}"
    total = db.conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"]
    assert total == 240


# ======================================================================
# Resilience
# ======================================================================
def test_transient_403_reasons_are_retried():
    for reason in ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"):
        calls = {"n": 0}

        @retry_on_google_error(max_retries=3, base_delay=0.001, max_delay=0.002)
        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise http_error(403, reason)
            return "ok"

        assert flaky() == "ok"
        assert calls["n"] == 3


def test_fatal_403_reasons_fail_immediately():
    for reason in ("insufficientPermissions", "storageQuotaExceeded",
                   "cannotDownloadFile", "domainPolicy"):
        calls = {"n": 0}

        @retry_on_google_error(max_retries=5, base_delay=0.001)
        def fatal():
            calls["n"] += 1
            raise http_error(403, reason)

        with pytest.raises(PermanentAPIError):
            fatal()
        assert calls["n"] == 1, f"{reason} must not be retried"


def test_unknown_403_reason_is_treated_as_permanent():
    calls = {"n": 0}

    @retry_on_google_error(max_retries=5, base_delay=0.001)
    def weird():
        calls["n"] += 1
        raise http_error(403, "someBrandNewReason")

    with pytest.raises(PermanentAPIError):
        weird()
    assert calls["n"] == 1


def test_4xx_client_errors_are_not_retried():
    for status in (400, 401, 404, 409, 412):
        calls = {"n": 0}

        @retry_on_google_error(max_retries=5, base_delay=0.001)
        def bad():
            calls["n"] += 1
            raise http_error(status, "invalid")

        with pytest.raises(PermanentAPIError):
            bad()
        assert calls["n"] == 1, f"HTTP {status} must not be retried"


def test_active_session_invalid_401_is_retried():
    """A freshly created Workspace account is not always immediately ready
    for DWD impersonation -- confirmed live on a brand-new sandbox account,
    which failed its very first Drive call this way despite already being
    created with changePasswordAtNextLogin=False (the other, permanent
    cause of this exact message). Must self-resolve on retry rather than
    killing the whole seeding run for one account."""
    calls = {"n": 0}

    @retry_on_google_error(max_retries=3, base_delay=0.001, max_delay=0.002)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise http_error(401, "authError",
                             "Active session is invalid. Error code: 4")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3


def test_other_401s_still_fail_immediately():
    """The active-session carve-out is scoped to that one literal message --
    a genuinely bad or revoked credential must still fail fast, not retry
    for a minute before reporting what a first attempt already knew."""
    for reason, message in (
        ("authError", "Invalid Credentials"),
        ("required", "Login Required"),
    ):
        calls = {"n": 0}

        @retry_on_google_error(max_retries=5, base_delay=0.001)
        def bad():
            calls["n"] += 1
            raise http_error(401, reason, message)

        with pytest.raises(PermanentAPIError):
            bad()
        assert calls["n"] == 1, f"401 {reason}/{message!r} must not be retried"


def test_5xx_and_429_are_retried_then_give_up():
    for status in (429, 500, 502, 503, 504):
        calls = {"n": 0}

        @retry_on_google_error(max_retries=2, base_delay=0.001, max_delay=0.002)
        def down():
            calls["n"] += 1
            raise http_error(status, "backendError")

        with pytest.raises(RuntimeError):
            down()
        assert calls["n"] == 3, f"HTTP {status} should be attempted 1+2 times"


def test_retry_after_header_is_honoured():
    from tests.fakes import FakeResp
    from googleapiclient.errors import HttpError

    calls = {"n": 0}

    @retry_on_google_error(max_retries=2, base_delay=10.0, max_delay=20.0)
    def slow():
        calls["n"] += 1
        if calls["n"] == 1:
            raise HttpError(
                FakeResp(503, {"retry-after": "0.01"}),
                b'{"error":{"errors":[{"reason":"backendError"}]}}',
            )
        return "ok"

    start = time.monotonic()
    assert slow() == "ok"
    # Retry-After (0.01s) must win over base_delay (10s).
    assert time.monotonic() - start < 2.0


def test_rate_limiter_paces_requests():
    rl = RateLimiter(20, burst=1)
    start = time.monotonic()
    for _ in range(6):
        rl.acquire()
    assert time.monotonic() - start >= 0.2


def test_rate_limiter_is_thread_safe():
    rl = RateLimiter(500, burst=1)
    counts = []

    def worker():
        for _ in range(20):
            rl.acquire()
        counts.append(1)

    ts = [threading.Thread(target=worker) for _ in range(5)]
    start = time.monotonic()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    elapsed = time.monotonic() - start
    assert len(counts) == 5
    # 100 tokens at 500/s cannot complete faster than ~0.2s.
    assert elapsed >= 0.15


def test_quota_guard_refund(db):
    g = DailyQuotaGuard(db, TGT_USER, 1000)
    g.reserve(600)
    assert g.remaining() == 400
    g.refund(600)
    assert g.remaining() == 1000


def test_quota_guard_rejects_oversized_item(db):
    g = DailyQuotaGuard(db, TGT_USER, 1000)
    with pytest.raises(QuotaExhausted):
        g.reserve(1001)
    assert g.remaining() == 1000, "a rejected reservation must not charge"


# ======================================================================
# Discovery
# ======================================================================
def test_discovery_counts_and_depth(auth, db, settings, identity):
    from discovery import scan_user

    src = auth.source_drive(SRC_USER)
    a = src.add_folder("A")
    b = src.add_folder("B", parent=a)
    src.add_binary("f1.pdf", parent=b, data=b"x" * 100)
    src.add_binary("f2.pdf", data=b"y" * 50)
    src.add_native("Doc", parent=a)

    stats = scan_user(auth, db, settings, SRC_USER)
    assert stats["folder_count"] == 2
    assert stats["file_count"] == 3
    assert stats["native_count"] == 1
    assert stats["total_bytes"] == 150
    assert stats["max_depth"] == 2
    assert db.latest_discovery(SRC_USER)["file_count"] == 3


def test_discovery_respects_owned_only(auth, db, settings, identity):
    from discovery import scan_user

    src = auth.source_drive(SRC_USER)
    src.add_binary("mine.pdf")
    shared = src.add_binary("theirs.pdf")
    src.store[shared]["owners"] = [{"emailAddress": "someone@tenanta.com"}]

    settings.owned_only = True
    assert scan_user(auth, db, settings, SRC_USER)["file_count"] == 1
    settings.owned_only = False
    assert scan_user(auth, db, settings, SRC_USER)["file_count"] == 2


# ======================================================================
# Scope manifest
# ======================================================================
def test_scope_matrix_is_well_formed():
    assert len(scope_mod.SCOPE_MATRIX) > 50
    for item in scope_mod.SCOPE_MATRIX:
        assert item.service in scope_mod.SERVICES
        assert item.status in ("FULL", "PARTIAL", "NONE")
        assert item.item
        # Every non-FULL row must explain itself — an unexplained gap is how
        # stakeholders get surprised at cutover.
        if item.status != "FULL":
            assert item.note, f"{item.item} needs a note"


def test_scope_renders_in_every_format():
    assert len(scope_mod.as_text()) > 100
    assert "| Data element |" in scope_mod.as_markdown()
    payload = json.loads(scope_mod.as_json())
    assert payload["counts"]["drive"]["NONE"] > 0
    assert payload["oauth_scopes"]["source"]


def test_source_scopes_are_read_only():
    """A write scope on the source tenant would be a serious mistake."""
    for s in scope_mod.oauth_scopes()["source"]:
        assert s.endswith(".readonly"), f"{s} is not read-only"


def test_default_settings_do_not_widen_the_source_grant():
    """
    Adding a scope the Admin Console hasn't authorised breaks *every* call
    with unauthorized_client, so an optional feature must never widen the
    baseline grant a working deployment depends on.
    """
    from config import Settings, source_scopes, target_scopes

    s = Settings()
    assert source_scopes(s) == scope_mod.oauth_scopes()["source"]
    assert target_scopes(s) == scope_mod.oauth_scopes()["target"]


def test_server_side_mode_swaps_in_the_drive_write_scope():
    from config import Settings, source_scopes

    s = Settings()
    s.transfer_mode = "server_side"
    scopes = source_scopes(s)
    assert "https://www.googleapis.com/auth/drive" in scopes
    assert "https://www.googleapis.com/auth/drive.readonly" not in scopes


def test_gmail_settings_scope_only_when_opted_in():
    from config import GMAIL_SETTINGS_SCOPE, Settings, source_scopes, target_scopes

    s = Settings()
    assert GMAIL_SETTINGS_SCOPE not in source_scopes(s)
    assert GMAIL_SETTINGS_SCOPE not in target_scopes(s)

    s.migrate_gmail_settings = True
    assert GMAIL_SETTINGS_SCOPE in source_scopes(s)
    assert GMAIL_SETTINGS_SCOPE in target_scopes(s)


def test_scope_filters():
    only_none = scope_mod.filter_scope(statuses=["NONE"])
    assert only_none and all(i.status == "NONE" for i in only_none)
    drive_only = scope_mod.filter_scope(services=["drive"])
    assert drive_only and all(i.service == "drive" for i in drive_only)


# ======================================================================
# TUI snapshot
# ======================================================================
def test_snapshot_aggregates_progress(db, settings):
    bulk_seed_identities(db, [(SRC_USER, TGT_USER)])
    db.record_discovery(SRC_USER, file_count=10, folder_count=2,
                        messages_total=8)
    for i in range(6):
        db.log_audit(SRC_USER, f"f{i}", "file", "SUCCESS", bytes_moved=100)
    for i in range(3):
        db.log_audit(SRC_USER, f"m{i}", "message", "SUCCESS", bytes_moved=10)
    db.log_audit(SRC_USER, "bad", "file", "FAILED", "boom")
    db.log_audit(SRC_USER, "skip", "file", "SKIPPED_EXPORT_TOO_LARGE", "big")
    db.add_bytes_sent(TGT_USER, 500)

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    snap = tui.collect_snapshot(conn, cap_bytes=1000)

    u = snap.users[0]
    assert u.drive_done == 6 and u.mail_done == 3
    assert u.failed == 1 and u.drive_skipped == 1
    assert u.expected == 20               # 10 files + 2 folders + 8 messages
    assert snap.totals["items_done"] == 9
    assert snap.totals["items_failed"] == 1
    assert abs(snap.totals["worst_user_quota_frac"] - 0.5) < 1e-9
    conn.close()


def test_snapshot_reports_unknown_progress_without_discovery(db, settings):
    bulk_seed_identities(db, [(SRC_USER, TGT_USER)])
    db.log_audit(SRC_USER, "f1", "file", "SUCCESS")
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    snap = tui.collect_snapshot(conn, cap_bytes=1000)
    # Honest 'unknown' rather than a fabricated percentage.
    assert snap.users[0].fraction is None
    assert snap.totals["fraction"] is None
    conn.close()


def test_tui_connection_cannot_write(db, settings):
    conn = sqlite3.connect(settings.db_path)
    conn.execute("PRAGMA query_only=ON;")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM audit_log")
    conn.close()


@pytest.mark.parametrize("value,expected", [
    (0, "0B"), (1536, "1.5KB"), (1024**3, "1.0GB"), (1024**4, "1.0TB"),
])
def test_human_bytes(value, expected):
    assert tui.human_bytes(value) == expected


def test_bar_and_truncate():
    assert tui.bar(0.0, 10) == "." * 10
    assert tui.bar(1.0, 10) == "#" * 10
    assert tui.bar(0.5, 10) == "#####....."
    assert tui.bar(None, 5) == "?????"      # unknown, not zero
    assert tui.bar(2.0, 4) == "####"        # clamped
    assert tui.truncate("abcdefgh", 5) == "abcd\u2026"
    assert tui.fmt_duration(3725) == "01:02:05"
