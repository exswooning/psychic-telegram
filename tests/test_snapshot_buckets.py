"""
tests/test_snapshot_buckets.py
==============================
Every item type an engine writes lands in some bucket.

collect_snapshot dispatched on item_type and bucketed only file/folder,
message, event and acl. `shortcut` and `draft` fell through to nothing --
they migrate at FULL fidelity per scope.py, write their own audit rows, and
were counted by no surface at all.

Live, r2-george had 3 shortcuts and 8 drafts copied successfully while the
per-user page read "Drive 3277/3277" and "Mailbox 1575/1575". Both
self-consistent, both eleven items short of what actually moved -- the kind
of wrong number that never looks wrong, because the denominator agrees with
it.
"""

from __future__ import annotations

import sqlite3

import pytest

import tui


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE identity_map (source_email TEXT PRIMARY KEY,
            target_email TEXT, status TEXT DEFAULT 'PENDING');
        CREATE TABLE audit_log (source_user TEXT, item_id TEXT, item_type TEXT,
            status TEXT, bytes_moved INTEGER DEFAULT 0, error_message TEXT,
            timestamp TEXT DEFAULT '2026-01-01T00:00:00Z');
        CREATE TABLE discovery (source_user TEXT, scanned_at TEXT,
            file_count INT, folder_count INT, messages_total INT);
        CREATE TABLE upload_ledger (target_user TEXT, day_utc TEXT, bytes_sent INT);
        INSERT INTO identity_map VALUES ('a@src','a@tgt','DONE');
    """)
    return c


def _add(conn, item_type, n, status="SUCCESS"):
    conn.executemany(
        "INSERT INTO audit_log (source_user,item_id,item_type,status) VALUES (?,?,?,?)",
        [("a@src", f"{item_type}{i}", item_type, status) for i in range(n)])


def _row(conn):
    return tui.collect_snapshot(conn, 750 * 1024**3).users[0]


class TestNothingMigratedIsCountedByNothing:
    def test_shortcuts_count_as_drive(self, conn):
        _add(conn, "file", 5); _add(conn, "folder", 2); _add(conn, "shortcut", 3)
        assert _row(conn).drive_done == 10

    def test_drafts_count_as_mail(self, conn):
        _add(conn, "message", 5); _add(conn, "draft", 8)
        assert _row(conn).mail_done == 13

    def test_the_live_shape_adds_up(self, conn):
        """r2-george exactly: the page said 3277 and 1575."""
        _add(conn, "file", 3041); _add(conn, "folder", 236); _add(conn, "shortcut", 3)
        _add(conn, "message", 1575); _add(conn, "draft", 8)
        u = _row(conn)
        assert u.drive_done == 3280 and u.mail_done == 1583

    def test_failed_shortcuts_and_drafts_are_failures_too(self, conn):
        _add(conn, "shortcut", 2, "FAILED")
        _add(conn, "draft", 3, "FAILED")
        u = _row(conn)
        assert u.drive_failed == 2 and u.mail_failed == 3

    def test_skipped_ones_are_skips(self, conn):
        _add(conn, "shortcut", 1, "SKIPPED_UNEXPORTABLE")
        _add(conn, "draft", 2, "SKIPPED_IS_DRAFT")
        u = _row(conn)
        assert u.drive_skipped == 1 and u.mail_skipped == 2

    def test_unrelated_types_still_do_not_land_in_drive(self, conn):
        """acl and event have their own buckets; widening drive must not
        swallow them."""
        _add(conn, "acl", 9); _add(conn, "event", 4)
        u = _row(conn)
        assert u.drive_done == 0 and u.cal_done == 4
