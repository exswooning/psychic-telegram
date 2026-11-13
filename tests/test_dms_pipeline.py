"""The DMS after a split run is the default procedure (operator, 13 Nov 2026): who it may take is decided per
user and recorded first, the import is confirmed to have started (never assumed), deltas run until zero
failed emails, and a verify of every item, with repair, follows on its own as a visible job.

Account 3, 13 Nov: the old gate looked at statuses -- the migration marks every user DONE at the end, so it
would have passed info@ with a message its pass never saw; that message turned out to be new mail with no
Drive link, which is exactly what the DMS is for. And one failed user used to hold all 45 mailboxes.
"""
import base64
import json
import sqlite3
import sys

import pytest

import dms_handoff
import dms_pipeline
from db import MigrationDB


def _raw(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class FakeGmail:
    """users().messages().list/get, enough for the hand-off check."""

    def __init__(self, messages: dict, unreadable=()):
        self.msgs, self.unreadable = messages, set(unreadable)

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, maxResults, pageToken=None, includeSpamTrash=False):
        assert includeSpamTrash, "Spam and Trash count: a message there is still the user's mail"
        self._call = {"messages": [{"id": i} for i in self.msgs]}
        return self

    def get(self, userId, id, format):
        if id in self.unreadable:
            raise RuntimeError("500")
        self._call = {"raw": _raw(self.msgs[id])}
        return self

    def execute(self):
        return self._call


@pytest.fixture
def ledger(tmp_path):
    db = MigrationDB(str(tmp_path / "ledger.db"))

    def user(email, status="DONE"):
        db.conn.execute("INSERT INTO identity_map(source_email, target_email, entity_type, status) VALUES (?,?,?,?)",
                        (email, email.replace("@s", "@t"), "user", status))

    def mapped(email, sid):
        db.conn.execute("INSERT INTO id_mapping(source_user, source_id, target_id, type) VALUES (?,?,?,?)",
                        (email, sid, "t" + sid, "message"))

    def audit(email, sid, status, error=""):
        db.conn.execute("INSERT INTO audit_log(source_user, item_id, item_type, status, error_message) "
                        "VALUES (?,?,?,?,?)", (email, sid, "message", status, error))
    db.user, db.mapped, db.audit = user, mapped, audit
    yield db
    db.conn.commit()


def _assess(db, gmails):
    db.conn.commit()
    with sqlite3.connect(db.db_path if hasattr(db, "db_path") else db.path) as conn:
        return dms_handoff.assess(conn, lambda u: gmails[u])


class TestWhoTheDmsMayTake:
    def test_done_status_is_not_trusted_and_new_link_mail_holds_only_that_user(self, ledger):
        ledger.user("a@s"); ledger.user("b@s")
        ledger.mapped("a@s", "1"); ledger.audit("b@s", "2", "SKIPPED_NO_DRIVE_LINK")
        held, notes = _assess(ledger, {
            "a@s": FakeGmail({"1": "x", "9": "see https://drive.google.com/file/d/abc"}),
            "b@s": FakeGmail({"2": "plain"})})
        assert list(held) == ["a@s"] and "Drive link" in held["a@s"] and "9" in held["a@s"]
        assert "b@s" not in held, "one held user never holds the rest"

    def test_new_mail_without_a_link_goes_to_the_dms(self, ledger):
        ledger.user("info@s")
        held, notes = _assess(ledger, {"info@s": FakeGmail({"new": "an ordinary reply"})})
        assert not held and "none with a Drive link" in notes["info@s"]

    def test_an_unreadable_new_message_holds_its_user(self, ledger):
        ledger.user("a@s")
        held, _ = _assess(ledger, {"a@s": FakeGmail({"x": "?"}, unreadable={"x"})})
        assert "a@s" in held

    def test_failed_link_mail_and_unfinished_users_are_held_but_a_failed_status_alone_is_not(self, ledger):
        ledger.user("run@s", "RUNNING"); ledger.user("lic@s", "BLOCKED")
        ledger.user("bad@s"); ledger.audit("bad@s", "7", "FAILED", "insert refused")
        ledger.user("drivefail@s", "FAILED")
        held, _ = _assess(ledger, {u: FakeGmail({}) for u in ("run@s", "lic@s", "bad@s", "drivefail@s")})
        assert set(held) == {"run@s", "lic@s", "bad@s"}
        assert "drivefail@s" not in held, "a failed Drive item says nothing about mail"

    def test_a_failure_later_mapped_is_history_not_a_hold(self, ledger):
        ledger.user("a@s"); ledger.audit("a@s", "7", "FAILED"); ledger.mapped("a@s", "7")
        held, _ = _assess(ledger, {"a@s": FakeGmail({"7": "x"})})
        assert not held

    def test_a_source_check_that_cannot_run_holds_the_user(self, ledger):
        ledger.user("a@s")
        ledger.conn.commit()
        with sqlite3.connect(ledger.db_path if hasattr(ledger, "db_path") else ledger.path) as conn:
            held, _ = dms_handoff.assess(conn, lambda u: (_ for _ in ()).throw(RuntimeError("no token")))
        assert "could not run" in held["a@s"]


class TestTheIdentityFile:
    def test_held_users_leave_the_file_and_the_header_stays(self, tmp_path):
        src = tmp_path / "ids.csv"
        src.write_text("source_email,target_email\na@s,a@t\nB@s,b@t\nc@s,c@t\n")
        out = tmp_path / "ready.csv"
        assert dms_pipeline.ready_identities(str(src), {"b@s": "why"}, str(out)) == 2
        assert out.read_text().splitlines() == ["source_email,target_email", "a@s,a@t", "c@s,c@t"]


def _child(script: str) -> list[str]:
    return [sys.executable, "-u", "-c", script]


class TestTheStartIsConfirmed:
    def test_an_import_status_confirms_it(self):
        assert dms_pipeline.run_confirmed(_child("print('[dwd] import status: In progress {}')"), 5) == 0

    def test_silence_past_the_window_stops_it(self):
        assert dms_pipeline.run_confirmed(_child("import time; print('signing in'); time.sleep(30)"), 1) == 3

    def test_a_run_that_ends_without_ever_confirming_is_not_a_start(self):
        assert dms_pipeline.run_confirmed(_child("print('signed in')"), 5) == 3

    def test_waiting_for_the_source_admin_extends_the_window(self):
        script = ("import time\n"
                  "for _ in range(3):\n"
                  "    print('waiting for the source admin to approve; 10m left', flush=True); time.sleep(0.8)\n"
                  "print('import status: In progress {}')")
        assert dms_pipeline.run_confirmed(_child(script), 1.5) == 0

    def test_its_own_failure_is_passed_on(self):
        assert dms_pipeline.run_confirmed(_child("import sys; print('import status: x'); sys.exit(4)"), 5) == 4


class TestDeltasUntilZero:
    def _metrics(self, path, failed, status="complete"):
        path.write_text(json.dumps({"status": status, "metrics": {"Emails failed": failed}}))

    def test_zero_needs_no_delta(self, tmp_path, monkeypatch):
        m = tmp_path / "m.json"; self._metrics(m, 0)
        runs = []
        assert dms_pipeline.delta_until_zero(lambda: runs.append(1), str(m), 5, 3) == 0 and not runs

    def test_it_reruns_while_the_count_falls(self, tmp_path, monkeypatch):
        m = tmp_path / "m.json"; counts = iter([5, 2, 0])
        self._metrics(m, 17)
        assert dms_pipeline.delta_until_zero(lambda: self._metrics(m, next(counts)), str(m), 5, 3) == 0

    def test_a_count_that_stops_falling_stops_with_an_incident(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(dms_pipeline, "_incident", lambda **kw: seen.append(kw))
        m = tmp_path / "m.json"; self._metrics(m, 17)
        assert dms_pipeline.delta_until_zero(lambda: self._metrics(m, 17), str(m), 5, 3) == 2
        assert seen and seen[0]["fingerprint"] == "dms_stuck:3"

    def test_an_unknown_result_is_never_read_as_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dms_pipeline, "_incident", lambda **kw: None)
        assert dms_pipeline.delta_until_zero(lambda: None, str(tmp_path / "missing.json"), 5, 3) == 2


class TestTheVerifyFollowsTheDms:
    def test_repairs_are_switched_on_and_every_item_is_verified_as_its_own_job(self, tmp_path, monkeypatch):
        import api_server as A
        import verification_repairs
        MigrationDB(str(tmp_path / "l.db"))
        monkeypatch.setattr(A, "_account_db_path", lambda aid: str(tmp_path / "l.db"))
        enabled, jobs = [], []
        monkeypatch.setattr(verification_repairs, "enable", lambda db, operator=False: enabled.append(operator))
        monkeypatch.setattr(A, "_run_admitted", lambda argv, aid, name, env=None, then=None:
                            jobs.append((argv, name)) or (True, "started"))
        A._follow_on("verify-after-dms", 3)
        assert enabled == [True]
        argv, name = jobs[0]
        assert name == "verify" and argv[1] == "verify_sample.py"
        assert argv[argv.index("--limit") + 1] == "0" and "--repair-after-user" in argv
        assert "chat" not in argv[argv.index("--services") + 1]


class FakePage:
    """A consents page whose Approve button appears only after `ready_after` polls."""

    def __init__(self, ready_after):
        self.ready_after, self.polls, self.clicked, self.reloads = ready_after, 0, None, 0

    def get_by_role(self, role, name):
        page = self

        class Btn:
            def count(self):
                return 1 if page.polls >= page.ready_after and name.match("Approve") else 0

            @property
            def first(self):
                return self

            def is_enabled(self):
                return True

            def click(self):
                page.clicked = name.pattern

        return Btn()

    def wait_for_timeout(self, ms):
        self.polls += 1

    def reload(self, **kw):
        self.reloads += 1


class TestTheSourceApprovalWaitsForThePage:
    def test_a_button_that_draws_late_is_still_pressed(self):
        import dms_migrate
        page = FakePage(ready_after=5)       # one look after 8 s missed it on account 3, 13 Nov
        assert dms_migrate.press_approve(page) == "Approve" and page.clicked

    def test_it_gives_up_after_its_polls_having_reloaded_once(self):
        import dms_migrate
        page = FakePage(ready_after=10_000)
        assert dms_migrate.press_approve(page, polls=6) is None and page.reloads == 1 and not page.clicked
