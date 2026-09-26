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
