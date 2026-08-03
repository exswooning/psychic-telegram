"""
tests/test_perf_changes.py
==========================
The throughput and durability changes, pinned by the property each one was
supposed to establish rather than by its implementation.

Every one of these was a silent cost: nothing failed, the migration just took
longer or used more memory than it needed to, which is exactly the class of
defect a test suite built around correctness does not catch on its own.
"""

from __future__ import annotations

import socket
import sqlite3
import threading

import pytest

from db import MigrationDB


class TestConnectionPragmas:
    """
    `synchronous` and `foreign_keys` are per-connection and are NOT persisted
    in the database file, unlike `journal_mode`. Setting them in SCHEMA only
    ever configured the connection that ran the schema, so every worker thread
    was committing at synchronous=FULL -- an fsync per commit, twice per
    migrated item -- with foreign keys switched off.
    """

    def _pragma(self, db: MigrationDB, name: str):
        return db.conn.execute(f"PRAGMA {name}").fetchone()[0]

    def test_worker_connections_are_not_at_full_sync(self, tmp_path):
        db = MigrationDB(str(tmp_path / "m.db"))
        db.init_schema()
        got = {}

        def worker():
            got["sync"] = self._pragma(db, "synchronous")
            got["fk"] = self._pragma(db, "foreign_keys")
            got["journal"] = self._pragma(db, "journal_mode")

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert got["sync"] == 1, "synchronous=NORMAL is 1; 2 means FULL"
        assert got["fk"] == 1, "foreign keys were off on worker connections"
        # NORMAL is only safe because the journal is WAL. If this ever stops
        # being WAL, NORMAL stops being a safe default and this must be revisited.
        assert got["journal"].lower() == "wal"

    def test_the_main_thread_agrees_with_its_workers(self, tmp_path):
        """A pragma that differs by thread is worse than one that is wrong
        everywhere, because it only misbehaves under concurrency."""
        db = MigrationDB(str(tmp_path / "m.db"))
        db.init_schema()
        main = self._pragma(db, "synchronous")
        got = {}
        t = threading.Thread(target=lambda: got.setdefault(
            "sync", db.conn.execute("PRAGMA synchronous").fetchone()[0]))
        t.start()
        t.join()
        assert main == got["sync"]


class TestTransportRetries:
    """
    A multi-hour migration reliably sees connections reset and sockets time
    out. None of those are HttpError, so each one permanently failed an item
    and cost a re-run.
    """

    def _decorated(self, seq):
        from resilience import retry_on_google_error

        calls = {"n": 0}

        @retry_on_google_error(max_retries=4, base_delay=0.001, max_delay=0.002)
        def fn():
            i = calls["n"]
            calls["n"] += 1
            if i < len(seq):
                raise seq[i]
            return "ok"

        return fn, calls

    @pytest.mark.parametrize("exc", [
        ConnectionResetError("reset by peer"),
        BrokenPipeError("broken pipe"),
        socket.timeout("timed out"),
        socket.gaierror("name resolution failed"),
    ])
    def test_transient_transport_errors_are_retried(self, exc):
        fn, calls = self._decorated([exc])
        assert fn() == "ok"
        assert calls["n"] == 2, "the call was not retried"

    def test_ssl_errors_are_retried(self):
        import ssl

        fn, calls = self._decorated([ssl.SSLError("handshake failure")])
        assert fn() == "ok"

    def test_incomplete_read_is_retried(self):
        import http.client

        fn, calls = self._decorated([http.client.IncompleteRead(b"partial")])
        assert fn() == "ok"

    def test_a_transport_error_still_gives_up_eventually(self):
        """Retrying forever would hang a migration on a dead network."""
        fn, _ = self._decorated([ConnectionResetError()] * 20)
        with pytest.raises(RuntimeError, match="exhausted"):
            fn()

    def test_an_unrelated_exception_is_not_swallowed(self):
        """Widening the retry set must not turn a bug into a slow retry loop."""
        fn, calls = self._decorated([ValueError("a real bug")])
        with pytest.raises(ValueError):
            fn()
        assert calls["n"] == 1


class TestGmailDoesNotRoundTripBase64:
    """
    `raw` arrives base64url and `body['raw']` wants base64url. Decoding it only
    to re-encode identical bytes doubled peak memory per message and burned CPU
    on every one.
    """

    def test_the_raw_string_is_passed_through_untouched(self, auth, db, settings,
                                                        identity):
        import gmail_engine
        from tests.conftest import SRC_USER, TGT_USER

        src = auth.source_gmail(SRC_USER)
        src.add_message(b"hello there", labels=["INBOX"])

        gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()

        sent = auth.target_gmail(TGT_USER).calls_to("messages.insert")
        assert sent, "nothing was inserted"
        posted = sent[0]["body"]["raw"]
        original = next(iter(src.messages.values()))["raw"]
        assert posted == original, "raw was re-encoded rather than passed through"

    def test_byte_accounting_still_measures_the_decoded_size(self, auth, db,
                                                             settings, identity):
        """The 750 GB/day guard reads this number. Switching to the encoded
        length would over-count by a third and stop a run early."""
        import base64

        import gmail_engine
        from tests.conftest import SRC_USER, TGT_USER

        src = auth.source_gmail(SRC_USER)
        body = b"x" * 3000
        src.add_message(body, labels=["INBOX"])

        gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()

        row = db.conn.execute(
            "SELECT bytes_moved FROM audit_log WHERE item_type='message' "
            "AND status='SUCCESS'").fetchone()
        raw = next(iter(src.messages.values()))["raw"]
        decoded = len(base64.urlsafe_b64decode(raw))
        assert abs(row["bytes_moved"] - decoded) <= 2


class TestDownloadChunking:
    def test_the_chunk_size_is_explicit_not_the_library_default(self):
        """The library default is 100 MB. With N workers that is the worst-case
        resident set, on a codebase whose resources.py exists because a laptop
        swap-stalled into socket timeouts."""
        from config import Settings

        assert Settings().download_chunk_bytes <= 32 * 1024 * 1024

    def test_the_rate_limiter_is_charged_once_per_call_not_per_chunk(
            self, migrator, monkeypatch):
        """
        The bucket is sized for API requests per second. A large file draining
        through it spent those tokens on byte transfer, throttling every other
        call the same user had to make.

        Asserted behaviourally: a download that takes five chunks must still
        cost exactly one token. The earlier version of this test compared
        source text either side of the `while` loop, which broke on any
        refactor and passed for a wrong implementation phrased differently.
        """
        class CountingLimiter:
            def __init__(self):
                self.acquired = 0

            def acquire(self):
                self.acquired += 1

        class ChunkedDownloader:
            """Five chunks, like MediaIoBaseDownload on a file 5x the chunk size."""

            def __init__(self, fh, request, chunksize=None):
                self.fh = fh
                self.chunksize = chunksize
                self.remaining = 5

            def next_chunk(self):
                self.remaining -= 1
                self.fh.write(b"x" * 16)
                return None, self.remaining == 0

        limiter = CountingLimiter()
        migrator.limiter = limiter
        monkeypatch.setattr("drive_engine.MediaIoBaseDownload", ChunkedDownloader)

        path, size = migrator._download_via(lambda: object())

        assert size == 5 * 16, "the fake downloader did not run to completion"
        assert limiter.acquired == 1, (
            f"charged {limiter.acquired} tokens for one download; the request "
            f"limiter must not be paid per chunk")

    def test_the_configured_chunk_size_reaches_the_downloader(
            self, migrator, monkeypatch):
        """Setting it in config is only useful if it is actually passed on --
        the library default of 100 MB applies whenever it is not."""
        seen = {}

        class RecordingDownloader:
            def __init__(self, fh, request, chunksize=None):
                seen["chunksize"] = chunksize

            def next_chunk(self):
                return None, True

        migrator.settings.download_chunk_bytes = 4 * 1024 * 1024
        monkeypatch.setattr("drive_engine.MediaIoBaseDownload", RecordingDownloader)

        migrator._download_via(lambda: object())

        assert seen["chunksize"] == 4 * 1024 * 1024
