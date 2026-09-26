"""A sample migration copies a small slice and must leave the user UNFINISHED.

The slice is the point of a quick migration: a minute instead of hours, small
enough to check one to one. But a sample that marked the user DONE would be
skipped by the next full migration, which would never copy the rest, while every
screen said the user was done. That is the property below that must never break.
"""
import threading

import pytest

import calendar_engine
import contacts_engine
import drive_engine
import gmail_engine
import main
import tasks_engine
from config import FOLDER_MIME, Settings
from sample_budget import Budget
from tests.conftest import SRC_USER, TGT_USER

MSG = (b"Message-ID: <m{n}@tenanta.com>\r\nFrom: bob@tenanta.com\r\nTo: alice@tenanta.com\r\n"
       b"Date: Mon, 3 Jun 2019 10:00:00 +0000\r\nSubject: n{n}\r\n\r\nbody {n}\r\n")


class TestTheBudget:
    def test_unlimited_always_says_yes_and_is_never_exhausted(self):
        b = Budget(None)
        assert all(b.take() for _ in range(1000)) and not b.exhausted and not b.limited

    def test_limited_says_yes_exactly_that_many_times(self):
        b = Budget(3)
        assert [b.take() for _ in range(5)] == [True, True, True, False, False]
        assert b.exhausted and b.limited

    def test_trim_keeps_the_first_and_spends_them(self):
        b = Budget(4)
        assert b.trim([1, 2, 3]) == [1, 2, 3]
        assert b.trim([4, 5, 6]) == [4] and b.exhausted
        assert b.trim([7]) == []

    def test_trim_unlimited_returns_everything(self):
        assert Budget(None).trim([1, 2, 3]) == [1, 2, 3]

    def test_a_pool_of_file_workers_cannot_overspend_it(self):
        """Drive spends it from many threads."""
        b, taken, lock = Budget(50), [], threading.Lock()

        def worker():
            for _ in range(40):
                if b.take():
                    with lock:
                        taken.append(1)
        ts = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        assert len(taken) == 50


class TestTheSetting:
    @pytest.mark.parametrize("raw,want", [("5", 5), (" 20 ", 20), ("0", None), ("-3", None), ("abc", None), ("", None)])
    def test_it_reads_the_environment_and_ignores_nonsense(self, monkeypatch, raw, want):
        monkeypatch.setenv("SAMPLE_LIMIT", raw)
        assert Settings().sample_limit == want

    def test_unset_is_an_ordinary_migration(self, monkeypatch):
        monkeypatch.delenv("SAMPLE_LIMIT", raising=False)
        assert Settings().sample_limit is None


class TestEachEngineStopsAtTheLimit:
    def test_gmail(self, auth, db, settings, identity):
        settings.sample_limit = 3
        src = auth.source_gmail(SRC_USER)
        for n in range(10):
            src.add_message(MSG.replace(b"{n}", str(n).encode()), ["INBOX"])
        gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
        assert len(auth.target_gmail(TGT_USER).messages) == 3

    def test_calendar(self, auth, db, settings, identity):
        settings.sample_limit = 2
        src = auth.source_calendar(SRC_USER)
        for n in range(6):
            src.add_event(f"e{n}")
        calendar_engine.CalendarMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
        tgt = auth.target_calendar(TGT_USER)
        inserted = len(tgt.calls_to("events.import")) + len(tgt.calls_to("events.insert"))
        assert inserted == 2 and len(tgt.store) + sum(len(v) for v in tgt.cal_events.values()) == 2

    def test_contacts(self, auth, db, settings, identity):
        settings.sample_limit = 2
        src = auth.source_people(SRC_USER)
        for n in range(6):
            src.add_contact(f"c{n}", f"c{n}@x.com")
        contacts_engine.ContactsMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
        assert len(auth.target_people(TGT_USER).contacts) == 2

    def test_tasks(self, auth, db, settings, identity):
        settings.migrate_tasks = True
        settings.sample_limit = 3
        src = auth.source_tasks(SRC_USER)
        tl = src.add_list("Move")
        for n in range(8):
            src.add_task(tl, f"t{n}")
        tasks_engine.TasksMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
        moved = sum(len(v) for v in auth.target_tasks(TGT_USER).task_store.values())
        assert moved == 3

    def test_drive_files(self, auth, db, settings, identity, quota):
        settings.sample_limit = 4
        src = auth.source_drive(SRC_USER)
        for n in range(12):
            src.add_binary(f"f{n}.bin")
        drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
        tgt = auth.target_drive(TGT_USER)
        assert sum(1 for f in tgt.store.values() if f.get("mimeType") not in (FOLDER_MIME,)
                   and f["name"].startswith("f")) == 4

    def test_drive_stops_walking_once_the_sample_is_taken(self, auth, db, settings, identity, quota):
        """Folders are listed first, so a sample stops creating them as soon as the
        files inside the earlier ones have used it up -- it does not walk the whole
        tree to leave empty folders behind."""
        settings.sample_limit = 2
        src = auth.source_drive(SRC_USER)
        for n in range(6):
            src.add_binary(f"in{n}.bin", parent=src.add_folder(f"folder{n}"))
        drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
        tgt = auth.target_drive(TGT_USER)
        files = [f for f in tgt.store.values() if f.get("mimeType") != FOLDER_MIME and f["name"].startswith("in")]
        assert len(files) == 2 and tgt.count(mime=FOLDER_MIME) == 2

    def test_a_sample_takes_small_files_and_leaves_the_big_ones_uncounted(self, auth, db, settings, identity, quota):
        """Every file in a sample has to be small enough to compare byte for byte.
        A 50 MB file is skipped WITHOUT spending the budget, so it cannot crowd the
        small ones out."""
        settings.sample_limit = 3
        settings.sample_max_file_bytes = 1000
        src = auth.source_drive(SRC_USER)
        for n in range(4):
            src.add_binary(f"big{n}.bin", data=b"x" * 5000)
        for n in range(5):
            src.add_binary(f"small{n}.bin", data=b"y" * 10)
        drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
        names = sorted(f["name"] for f in auth.target_drive(TGT_USER).store.values()
                       if f.get("mimeType") != FOLDER_MIME and f["name"].endswith(".bin"))
        assert len(names) == 3 and all(n.startswith("small") for n in names)

    def test_the_size_ceiling_does_not_apply_outside_a_sample(self, auth, db, settings, identity, quota):
        settings.sample_limit = None
        settings.sample_max_file_bytes = 1000
        auth.source_drive(SRC_USER).add_binary("big.bin", data=b"x" * 5000)
        drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
        assert any(f["name"] == "big.bin" for f in auth.target_drive(TGT_USER).store.values())

    @pytest.mark.parametrize("raw,mb", [("", 10), ("2", 2), ("0.5", 0.5)])
    def test_the_ceiling_reads_the_environment(self, monkeypatch, raw, mb):
        monkeypatch.setenv("SAMPLE_MAX_FILE_MB", raw)
        assert Settings().sample_max_file_bytes == int(mb * 1024 * 1024)

    def test_no_limit_moves_everything_as_before(self, auth, db, settings, identity):
        settings.sample_limit = None
        src = auth.source_gmail(SRC_USER)
        for n in range(10):
            src.add_message(MSG.replace(b"{n}", str(n).encode()), ["INBOX"])
        gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
        assert len(auth.target_gmail(TGT_USER).messages) == 10

    def test_a_second_sample_run_looks_at_the_same_items_and_copies_nothing_new(self, auth, db, settings, identity):
        settings.sample_limit = 3
        src = auth.source_gmail(SRC_USER)
        for n in range(10):
            src.add_message(MSG.replace(b"{n}", str(n).encode()), ["INBOX"])
        for _ in range(2):
            gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
        assert len(auth.target_gmail(TGT_USER).messages) == 3


class TestAnEngineBuiltWithoutInitStillHasABudget:
    """Tests (and anything else) build engines with __new__, bypassing __init__. The
    unlimited default lives on the class so they still work -- and it must never be
    a limited one, or every such engine would share a counter."""

    @pytest.mark.parametrize("cls", [drive_engine.DriveMigrator, gmail_engine.GmailMigrator,
                                     calendar_engine.CalendarMigrator, contacts_engine.ContactsMigrator,
                                     tasks_engine.TasksMigrator])
    def test_it_is_unlimited(self, cls):
        obj = cls.__new__(cls)
        assert not obj.budget.limited and all(obj.budget.take() for _ in range(100))

    def test_a_sample_gets_its_own_and_does_not_touch_the_shared_one(self, auth, db, settings, identity):
        settings.sample_limit = 2
        m = gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER)
        assert m.budget.limited and m.budget is not gmail_engine.GmailMigrator.budget
        assert not gmail_engine.GmailMigrator.budget.limited


class TestASampleNeverFinishesAUser:
    def _run(self, auth, db, settings, **kw):
        return main.migrate_user(auth, db, settings, SRC_USER, TGT_USER, {"gmail"}, False, 0)

    def _status(self, db):
        row = db.conn.execute("SELECT status, services_done FROM identity_map WHERE source_email=?", (SRC_USER,)).fetchone()
        return row["status"], (row["services_done"] or "")

    def test_a_sample_leaves_the_status_and_the_done_markers_untouched(self, auth, db, settings, identity):
        settings.sample_limit = 2
        src = auth.source_gmail(SRC_USER)
        for n in range(6):
            src.add_message(MSG.replace(b"{n}", str(n).encode()), ["INBOX"])
        before = self._status(db)
        self._run(auth, db, settings)
        assert len(auth.target_gmail(TGT_USER).messages) == 2          # it did copy the slice
        assert self._status(db) == before                                # and left the user exactly as it was
        assert "gmail" not in self._status(db)[1]

    def test_so_the_next_full_migration_still_picks_the_user_up(self, auth, db, settings, identity):
        settings.sample_limit = 2
        src = auth.source_gmail(SRC_USER)
        for n in range(6):
            src.add_message(MSG.replace(b"{n}", str(n).encode()), ["INBOX"])
        self._run(auth, db, settings)
        settings.sample_limit = None
        self._run(auth, db, settings)
        assert len(auth.target_gmail(TGT_USER).messages) == 6           # the rest arrived, none twice
        assert self._status(db)[0] == "DONE"

    def test_an_ordinary_run_still_marks_them_done(self, auth, db, settings, identity):
        settings.sample_limit = None
        auth.source_gmail(SRC_USER).add_message(MSG.replace(b"{n}", b"0"), ["INBOX"])
        self._run(auth, db, settings)
        status, done = self._status(db)
        assert status == "DONE" and "gmail" in done


class TestASampleDoesNotWaitForAccountsThatWillNeverAppear:
    """Found on the first live sample: each file was shared with ~28 people who do not
    exist on the empty target, and every grant sat in the ~40 s propagation window
    meant for a user created a moment ago -- hours for a "quick" run."""
    NO_ACCOUNT = "there is no Google account associated with this email address"

    def _m(self, auth, db, settings, sample):
        settings.sample_limit = 5 if sample else None

        class Q:
            def reserve(self, n): pass
            def refund(self, n): pass
        return drive_engine.DriveMigrator(auth, db, settings, "u@src", "u@tgt", Q())

    def _script(self, m, monkeypatch, outcomes):
        calls = []

        def fake(fn, *a, **k):
            calls.append(k.get("max_retries", "unset"))
            out = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            if isinstance(out, Exception):
                raise out
            return out
        monkeypatch.setattr(m, "_retry", fake)
        return calls

    def test_a_sample_takes_one_look_and_records_the_skip(self, auth, db, settings, monkeypatch):
        m = self._m(auth, db, settings, sample=True)
        calls = self._script(m, monkeypatch, [RuntimeError(self.NO_ACCOUNT)])
        assert m._create_permission("t1", {"type": "user"}, "f1:a@x.com") == 0
        assert calls == [0]                                            # no backoff window
        assert db.get_audit("u@src", "f1:a@x.com", "acl")["status"] == "SKIPPED_GRANTEE_NOT_ON_GOOGLE"

    def test_a_real_migration_still_gets_the_full_window(self, auth, db, settings, monkeypatch):
        """There the user really may have been created a moment ago."""
        m = self._m(auth, db, settings, sample=False)
        calls = self._script(m, monkeypatch, [RuntimeError(self.NO_ACCOUNT)])
        m._create_permission("t1", {"type": "user"}, "f1:a@x.com")
        assert calls == [None]

    def test_a_sample_still_retries_properly_when_the_failure_is_not_a_missing_account(self, auth, db, settings, monkeypatch):
        m = self._m(auth, db, settings, sample=True)
        calls = self._script(m, monkeypatch, [RuntimeError("HTTP 503 backend error"), {"id": "p1"}])
        assert m._create_permission("t1", {"type": "user"}, "f1:a@x.com") == 1
        assert calls == [0, None]
        assert db.get_audit("u@src", "f1:a@x.com", "acl")["status"] == "SUCCESS"

    def test_and_records_a_real_failure_if_it_never_clears(self, auth, db, settings, monkeypatch):
        m = self._m(auth, db, settings, sample=True)
        calls = self._script(m, monkeypatch, [RuntimeError("HTTP 503 backend error")])
        assert m._create_permission("t1", {"type": "user"}, "f1:a@x.com") == 0
        assert calls == [0, None] and m.stats.get("acl_failed", 0) == 1
        assert db.get_audit("u@src", "f1:a@x.com", "acl")["status"] == "FAILED"
