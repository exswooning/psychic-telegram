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


class TestGmailInsertIsSafeToRetry:
    """
    Widening the retry set to transport errors bought reliability at a cost,
    and the cost is not uniform. A retried files.create leaves a Drive orphan
    that verify sees as a surplus; events.import is idempotent through
    iCalUID; messages.insert produces a second copy of an email a user will
    actually see -- and the retry's fresh id then gets written to id_mapping
    as canonical, so the ledger records the duplicate as the real thing.
    """

    RAW_HEADERS = (b"Message-ID: <abc123@source.example>\r\n"
                   b"From: a@source.example\r\n"
                   b"Subject: hello\r\n\r\nbody text")

    def _encoded(self):
        import base64

        return base64.urlsafe_b64encode(self.RAW_HEADERS).decode()

    def _migrator(self, auth, db, settings):
        import gmail_engine
        from tests.conftest import SRC_USER, TGT_USER

        return gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER)

    def test_the_message_id_is_read_out_of_the_raw_payload(self, auth, db,
                                                            settings, identity):
        """format='raw' returns no parsed headers, so reading payload.headers
        would return None for every message and silently disable the guard."""
        m = self._migrator(auth, db, settings)
        assert m._message_id_header(self._encoded()) == "<abc123@source.example>"

    def test_only_a_prefix_of_a_huge_message_is_decoded(self, auth, db,
                                                        settings, identity):
        """This runs on a failure path; decoding a 25 MB attachment to read one
        header would be a poor trade."""
        import base64

        big = self.RAW_HEADERS + b"x" * (2 * 1024 * 1024)
        m = self._migrator(auth, db, settings)
        got = m._message_id_header(base64.urlsafe_b64encode(big).decode())
        assert got == "<abc123@source.example>"

    def test_a_message_with_no_message_id_returns_none(self, auth, db, settings,
                                                       identity):
        import base64

        m = self._migrator(auth, db, settings)
        raw = base64.urlsafe_b64encode(b"Subject: no id\r\n\r\nbody").decode()
        assert m._message_id_header(raw) is None

    def test_a_lost_response_does_not_produce_a_second_copy(
            self, auth, db, settings, identity, monkeypatch):
        """
        The case that actually duplicates, and the one the first version of
        this guard could not see.

        Attempt 1 lands server-side and the response is lost. Attempt 2 would
        insert a second copy and return 200 -- nothing raises, so a guard
        wrapped around the retry loop never runs. The check has to happen
        between attempts.
        """
        m = self._migrator(auth, db, settings)
        state = {"inserts": 0, "delivered": False}

        def flaky_insert():
            state["inserts"] += 1
            if state["inserts"] == 1:
                state["delivered"] = True      # it landed...
                raise ConnectionResetError("response lost")   # ...we never heard
            return {"id": f"tgt-duplicate-{state['inserts']}"}

        monkeypatch.setattr(m, "_find_by_message_id",
                            lambda msgid: "tgt-first" if state["delivered"] else None)
        monkeypatch.setattr(m.settings, "base_backoff", 0.001)
        monkeypatch.setattr(m.settings, "max_backoff", 0.002)
        monkeypatch.setattr(
            m, "tgt",
            type("T", (), {"users": lambda self=None: type("U", (), {
                "messages": lambda self=None: type("M", (), {
                    "insert": lambda self=None, **kw: type("R", (), {
                        "execute": lambda self=None: flaky_insert()})()})()})()})())

        result = m._insert_once({"raw": self._encoded()}, None, self._encoded())

        assert result["id"] == "tgt-first", "adopted the wrong message"
        assert result.get("adopted") is True
        assert state["inserts"] == 1, (
            f"inserted {state['inserts']} times; the second call is the "
            f"duplicate a user would see in their mailbox")

    def test_a_genuinely_failed_insert_is_still_retried(
            self, auth, db, settings, identity, monkeypatch):
        """The guard must not stop legitimate retries: if nothing landed,
        attempt 2 has to actually run."""
        m = self._migrator(auth, db, settings)
        state = {"inserts": 0}

        def flaky_insert():
            state["inserts"] += 1
            if state["inserts"] == 1:
                raise ConnectionResetError("never arrived")
            return {"id": "tgt-second-attempt"}

        monkeypatch.setattr(m, "_find_by_message_id", lambda msgid: None)
        monkeypatch.setattr(m.settings, "base_backoff", 0.001)
        monkeypatch.setattr(m.settings, "max_backoff", 0.002)
        monkeypatch.setattr(
            m, "tgt",
            type("T", (), {"users": lambda self=None: type("U", (), {
                "messages": lambda self=None: type("M", (), {
                    "insert": lambda self=None, **kw: type("R", (), {
                        "execute": lambda self=None: flaky_insert()})()})()})()})())

        result = m._insert_once({"raw": self._encoded()}, None, self._encoded())

        assert result["id"] == "tgt-second-attempt"
        assert state["inserts"] == 2

    def test_a_rate_limit_does_not_trigger_a_lookup(
            self, auth, db, settings, identity, monkeypatch):
        """A 429 was rejected before it was processed, so there is nothing to
        adopt and the lookup would just spend quota confirming it."""
        from tests.fakes import http_error

        m = self._migrator(auth, db, settings)
        looked = {"n": 0}
        state = {"inserts": 0}

        def flaky_insert():
            state["inserts"] += 1
            if state["inserts"] == 1:
                raise http_error(429, "rateLimitExceeded")
            return {"id": "tgt-1"}

        def counting_lookup(msgid):
            looked["n"] += 1
            return None

        monkeypatch.setattr(m, "_find_by_message_id", counting_lookup)
        monkeypatch.setattr(m.settings, "base_backoff", 0.001)
        monkeypatch.setattr(m.settings, "max_backoff", 0.002)
        monkeypatch.setattr(
            m, "tgt",
            type("T", (), {"users": lambda self=None: type("U", (), {
                "messages": lambda self=None: type("M", (), {
                    "insert": lambda self=None, **kw: type("R", (), {
                        "execute": lambda self=None: flaky_insert()})()})()})()})())

        m._insert_once({"raw": self._encoded()}, None, self._encoded())

        assert looked["n"] == 0, "spent a lookup on a request that never landed"

    def test_the_happy_path_costs_no_extra_call(self, auth, db, settings,
                                                identity, monkeypatch):
        """The guard must be free when nothing goes wrong."""
        m = self._migrator(auth, db, settings)
        looked = {"n": 0}

        def counting_lookup(msgid):
            looked["n"] += 1
            return None

        monkeypatch.setattr(m, "_find_by_message_id", counting_lookup)
        monkeypatch.setattr(
            m, "tgt",
            type("T", (), {"users": lambda self=None: type("U", (), {
                "messages": lambda self=None: type("M", (), {
                    "insert": lambda self=None, **kw: type("R", (), {
                        "execute": lambda self=None: {"id": "tgt-1"}})()})()})()})())

        result = m._insert_once({"raw": self._encoded()}, None, self._encoded())

        assert result["id"] == "tgt-1"
        assert looked["n"] == 0


class TestTransportExhaustedIsDistinguishable:
    def test_it_is_still_a_runtime_error(self):
        """Every existing `except RuntimeError` must keep working."""
        from resilience import TransportExhausted

        assert issubclass(TransportExhausted, RuntimeError)

    def test_transport_failures_raise_it_and_api_failures_do_not(self):
        from googleapiclient.errors import HttpError

        from resilience import TransportExhausted, retry_on_google_error
        from tests.fakes import http_error

        @retry_on_google_error(max_retries=1, base_delay=0.001, max_delay=0.002)
        def transport():
            raise ConnectionResetError("reset")

        @retry_on_google_error(max_retries=1, base_delay=0.001, max_delay=0.002)
        def api():
            raise http_error(500, "internalError")

        with pytest.raises(TransportExhausted):
            transport()
        with pytest.raises(RuntimeError) as caught:
            api()
        assert not isinstance(caught.value, TransportExhausted)


class TestUnsharedFilesSkipTheAclCall:
    """
    Measured against a live tenant, not assumed -- which is the point, because
    a fake cannot answer a question about the real API's behaviour.

      * `shared` is populated on 504/504 files
      * 372 of those (74%) are unshared, and their permission list holds only
        the owner, which _sync_acls skips anyway
      * so the call can only ever return nothing to do

    The saving is ONE round trip per unshared file, not two. The modifiedTime
    restore already short-circuited on writes_applied == 0, so an unshared
    file never paid for it -- the first version of this docstring claimed two
    and a test that "proved" it passed identically with the skip removed.

    On the measured corpus that is ~372 of ~1,982 Drive calls: 55 list, 1,008
    transfer (get_media + create), 504 permissions.list, 284
    permissions.create, 132 restores. About 19% of Drive requests, which is
    worth shipping and is not the same claim as "two round trips on three
    files in four".

    Deliberately NOT adopting inline permissions, which Drive does return and
    which do match permissions.list exactly: it does not populate
    permissionDetails there even when asked. Verified on a folder-inherited
    share -- inline reported 0 inherited, permissions.list reported 2 -- and
    _sync_acls reads that flag to honour recreate_inherited_acls, so the
    inline list would silently ignore the setting rather than fail.
    """

    def test_the_skip_does_not_apply_inside_a_shared_drive(self, migrator):
        """Every measurement behind this was taken over 'me' in owners -- pure
        My Drive. A shared drive grants access through membership, so whether
        `shared` means the same thing there is unverified, and guessing would
        drop real per-file grants silently."""
        migrator.shared_drive = "drv-1"
        migrator._sync_acls("src-1", "tgt-1", False)
        assert migrator.src.call_count("permissions.list") == 1

    def test_an_unshared_file_costs_no_permissions_list(self, migrator, auth, db):
        src = auth.source_drive(migrator.source_user)
        src.add_binary("private.pdf")

        migrator.run()

        assert src.call_count("permissions.list") == 0, (
            "listed permissions on a file Drive already said was unshared")

    def test_a_shared_file_still_gets_its_acls(self, migrator, auth, db):
        """The optimisation must not cost a single grant."""
        from db import bulk_seed_identities

        bulk_seed_identities(db, [("bob@tenanta.com", "bob@tenantb.com")])
        src = auth.source_drive(migrator.source_user)
        fid = src.add_binary("shared.pdf")
        src.perms[fid].append({"id": "p1", "type": "user", "role": "writer",
                               "emailAddress": "bob@tenanta.com"})

        migrator.run()

        assert src.call_count("permissions.list") == 1
        tgt = auth.target_drive(migrator.target_user)
        assert tgt.call_count("permissions.create") == 1

    def test_the_restore_was_already_conditional_so_it_is_not_a_second_saving(
            self, migrator, auth):
        """
        Pins the correction rather than the claim it replaced.

        _restore_modified_time fires only when a write was applied, so an
        unshared file never paid for it either before or after the skip. This
        asserts the short-circuit exists -- which is what makes the saving one
        call and not two -- instead of asserting an absence that was already
        true and could not fail.
        """
        calls = []
        migrator._retry = lambda fn: calls.append("would have called") or None
        migrator._restore_modified_time(
            "tgt-1", {"modifiedTime": "2024-03-01T09:00:00Z"}, 0)
        assert calls == [], "restore ran with zero writes applied"

        migrator._restore_modified_time(
            "tgt-1", {"modifiedTime": "2024-03-01T09:00:00Z"}, 1)
        assert calls == ["would have called"], "restore skipped a real write"

    def test_an_absent_shared_field_still_lists(self, migrator, auth):
        """None means the caller did not ask for the field. Guessing there
        would trade a round trip for silently dropped ACLs."""
        applied = migrator._sync_acls("src-1", "tgt-1", None)
        assert migrator.src.call_count("permissions.list") == 1
        assert applied == 0

    def test_only_an_explicit_false_skips(self, migrator):
        migrator._sync_acls("src-1", "tgt-1", False)
        assert migrator.src.call_count("permissions.list") == 0


class TestTheRetryHookCannotBecomeANewFailure:
    """
    before_retry makes an API call from inside an exception handler, at the
    moment the network is known to be unwell -- which is the state that got us
    there. An unguarded call lets a second failure propagate out of the
    handler and kill the whole retry, and it does so precisely when a copy of
    the work may already exist on the server: the case the hook exists to make
    safer.
    """

    def _fn_failing_once(self):
        state = {"n": 0}

        def fn():
            state["n"] += 1
            if state["n"] == 1:
                raise ConnectionResetError("reset")
            return "second attempt"

        return fn, state

    def test_a_raising_hook_does_not_kill_the_retry(self):
        from resilience import retry_on_google_error

        fn, state = self._fn_failing_once()

        def exploding_hook():
            raise ConnectionResetError("the lookup failed too")

        wrapped = retry_on_google_error(
            max_retries=3, base_delay=0.001, max_delay=0.002,
            before_retry=exploding_hook)(fn)

        assert wrapped() == "second attempt"
        assert state["n"] == 2

    def test_a_hook_returning_none_falls_through_to_the_retry(self):
        from resilience import retry_on_google_error

        fn, state = self._fn_failing_once()
        wrapped = retry_on_google_error(
            max_retries=3, base_delay=0.001, max_delay=0.002,
            before_retry=lambda: None)(fn)

        assert wrapped() == "second attempt"

    def test_a_hook_returning_a_value_short_circuits(self):
        from resilience import retry_on_google_error

        fn, state = self._fn_failing_once()
        wrapped = retry_on_google_error(
            max_retries=3, base_delay=0.001, max_delay=0.002,
            before_retry=lambda: {"id": "already-there"})(fn)

        assert wrapped() == {"id": "already-there"}
        assert state["n"] == 1, "re-executed despite adopting existing work"

    def test_the_lookup_asks_spam_and_trash_too(self, auth, db, settings,
                                                identity):
        """An inserted message can land in Spam, and a lookup that cannot see
        it there concludes nothing arrived and inserts a second copy."""
        import gmail_engine
        from tests.conftest import SRC_USER, TGT_USER

        m = gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER)
        m._find_by_message_id("<x@y>")

        listed = auth.target_gmail(TGT_USER).calls_to("messages.list")
        assert listed, "no lookup was issued"
        assert listed[0].get("includeSpamTrash") is True
