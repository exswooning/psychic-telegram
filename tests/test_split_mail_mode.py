"""Split mode: the engine moves only mail that carries a Drive link (rewriting
it) and leaves the rest for Google's DMS. It is the default for a full
migration, so what it must never do is look finished when it is not.

Left for the DMS is OWED, not declined. It is written as SKIPPED_NO_DRIVE_LINK,
and everything that reads SKIPPED% as "a decision the tool made" would otherwise
report a split run that never reached the DMS as complete -- 97% of the mail
missing from the target and mail parity perfect."""
import base64

import pytest

import benchmarks as B
import run_report as RR
import tally as T
from config import DEFERRED_TO_DMS
from tests.conftest import SRC_USER, TGT_USER

SRC_FILE, TGT_FILE = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345", "1ZyXwVuTsRqPoNmLkJiHgFeDcBa987654"
LINKED = (b"Message-ID: <linked@tenanta.com>\r\nFrom: bob@tenanta.com\r\nTo: alice@tenanta.com\r\n"
          b"Date: Mon, 3 Jun 2019 10:00:00 +0000\r\nSubject: The deck\r\n\r\n"
          + f"Here it is: https://drive.google.com/file/d/{SRC_FILE}/view\r\n".encode())
PLAIN = (b"Message-ID: <plain@tenanta.com>\r\nFrom: bob@tenanta.com\r\nTo: alice@tenanta.com\r\n"
         b"Date: Mon, 3 Jun 2019 11:00:00 +0000\r\nSubject: Lunch\r\n\r\nNoon?\r\n")


@pytest.fixture
def split(gmail_migrator, auth, db, settings):
    settings.rewrite_drive_links = True
    settings.mail_only_with_links = True
    db.record_mapping(SRC_USER, SRC_FILE, TGT_FILE, "file")      # Drive has already run
    src = auth.source_gmail(SRC_USER)
    src.add_message(LINKED, ["INBOX"])
    src.add_message(PLAIN, ["INBOX"])
    return gmail_migrator


def _target_raws(auth):
    return [base64.urlsafe_b64decode(m["raw"].encode()) for m in auth.target_gmail(TGT_USER).messages.values()]


class TestTheEngine:
    def test_only_the_mail_with_a_drive_link_is_inserted(self, split, auth):
        split.run()
        raws = _target_raws(auth)
        assert len(raws) == 1 and b"linked@tenanta.com" in raws[0]

    def test_and_its_link_is_rewritten_to_the_target_file(self, split, auth):
        split.run()
        raw = _target_raws(auth)[0]
        assert TGT_FILE.encode() in raw and SRC_FILE.encode() not in raw

    def test_the_rest_is_recorded_as_deferred_to_the_dms(self, split, db):
        split.run()
        row = db.conn.execute("SELECT status, error_message FROM audit_log WHERE item_type='message' "
                              "AND status=?", (DEFERRED_TO_DMS,)).fetchone()
        assert row is not None and "DMS" in row["error_message"]
        assert db.conn.execute("SELECT COUNT(*) c FROM audit_log WHERE status=?", (DEFERRED_TO_DMS,)).fetchone()["c"] == 1

    def test_nothing_is_copied_that_the_dms_is_meant_to_carry(self, split, auth):
        split.run()
        assert not any(b"plain@tenanta.com" in r for r in _target_raws(auth))

    def test_without_split_everything_goes_through_the_engine(self, gmail_migrator, auth, db, settings):
        """The unchanged default for every other caller."""
        settings.rewrite_drive_links = True
        settings.mail_only_with_links = False
        db.record_mapping(SRC_USER, SRC_FILE, TGT_FILE, "file")
        src = auth.source_gmail(SRC_USER)
        src.add_message(LINKED, ["INBOX"]); src.add_message(PLAIN, ["INBOX"])
        gmail_migrator.run()
        assert len(_target_raws(auth)) == 2


class TestOwedIsNotDeclined:
    """The default cannot be safe unless a run that stopped at the handoff says so."""

    def _ledger(self, db, deferred=97, declined=3, moved=10):
        for i in range(deferred):
            db.log_audit(SRC_USER, f"d{i}", "message", DEFERRED_TO_DMS, "left for the DMS pass")
        for i in range(declined):
            db.log_audit(SRC_USER, f"s{i}", "message", "SKIPPED_IS_DRAFT", "drafts pass owns it")
        for i in range(moved):
            db.log_audit(SRC_USER, f"m{i}", "message", "SUCCESS")

    def test_the_tally_still_expects_the_deferred_mail_on_the_target(self, db):
        self._ledger(db)
        assert T.skipped_by_user(db.conn) == {SRC_USER: {"mail": 3}}      # the 3 real decisions only

    def test_so_a_split_run_that_never_reached_the_dms_does_not_score_perfect_parity(self, db):
        """110 messages at the source; 10 moved, 3 declined, 97 owed. The target
        holds 10. Mail parity has to say so."""
        self._ledger(db)
        row = {"user": SRC_USER, "target_user": TGT_USER, "skipped": T.skipped_by_user(db.conn)[SRC_USER],
               "source": {"counts": {"mail": 110}, "errors": {}}, "target": {"counts": {"mail": 10}, "errors": {}}}
        agg = T.aggregate([row])
        assert agg["services"]["mail"]["expected"] == 107 and agg["countParity"] == pytest.approx(10 / 107)

    def test_once_the_dms_has_delivered_it_parity_is_whole(self, db):
        self._ledger(db)
        row = {"user": SRC_USER, "target_user": TGT_USER, "skipped": T.skipped_by_user(db.conn)[SRC_USER],
               "source": {"counts": {"mail": 110}, "errors": {}}, "target": {"counts": {"mail": 107}, "errors": {}}}
        assert T.aggregate([row])["countParity"] == 1.0

    def test_the_report_counts_it_apart_from_a_skip(self, db):
        self._ledger(db)
        ledger = RR._ledger(db.conn)
        assert ledger["deferred"] == 97 and ledger["skipped"] == 3
        assert ledger["byType"]["message"]["deferred"] == 97

    def test_deferred_mail_is_not_an_attempt_so_it_does_not_move_the_failure_rate(self, db):
        self._ledger(db)
        assert RR._ledger(db.conn)["attempts"] == 10

    def test_the_first_next_step_says_the_mail_is_not_on_the_target_and_when_to_run_the_dms(self, db):
        self._ledger(db)
        facts = {"kind": "migration", "ledger": RR._ledger(db.conn), "run": {}}
        steps = RR.next_steps(facts, B.evaluate(facts))
        assert "97 mail message(s)" in steps[0] and "NOT on the target yet" in steps[0]
        assert "after this run, never before" in steps[0]

    def test_a_run_with_nothing_deferred_says_nothing_about_the_dms(self, db):
        db.log_audit(SRC_USER, "m1", "message", "SUCCESS")
        facts = {"kind": "migration", "ledger": RR._ledger(db.conn), "run": {}}
        steps = RR.next_steps(facts, B.evaluate(facts))
        assert not any("Data Migration" in s for s in steps)

    def test_the_constant_is_what_the_engine_writes(self):
        assert DEFERRED_TO_DMS == "SKIPPED_NO_DRIVE_LINK"


class TestWhatThePageIsTold:
    def test_the_migrations_page_counts_deferred_apart_from_skipped(self, db, settings, identity, monkeypatch):
        import api_server
        import config
        for i in range(97):
            db.log_audit(SRC_USER, f"d{i}", "message", DEFERRED_TO_DMS, "left for the DMS pass")
        for i in range(3):
            db.log_audit(SRC_USER, f"s{i}", "message", "SKIPPED_IS_DRAFT", "drafts pass owns it")
        monkeypatch.setattr(config, "Settings", lambda account_id=None: settings)
        out = api_server._migration_progress(1)
        assert out["itemsDeferred"] == 97 and out["itemsSkipped"] == 3

    def test_a_pass_marker_is_read_only_for_the_live_process(self, monkeypatch):
        import api_server
        log = ["PASS 1/3 pid=111: drive", "PASS 2/3 pid=111: gmail",
               "PASS 1/3 pid=222: drive", "PASS 2/3 pid=222: gmail"]     # 111 was an earlier run
        monkeypatch.setattr(api_server, "_job_log_lines", lambda *a, **k: log)
        monkeypatch.setattr(api_server.job_admission, "list_active",
                            lambda: [{"account_id": 5, "job_name": "migrate", "pid": 222}])
        monkeypatch.setattr(api_server.job_admission, "is_live", lambda j: True)
        assert api_server._run_pass(5) == {"pass": 2, "of": 3, "services": ["gmail"]}

    def test_a_run_that_has_not_printed_its_first_pass_is_not_given_the_previous_runs(self, monkeypatch):
        import api_server
        monkeypatch.setattr(api_server, "_job_log_lines", lambda *a, **k: ["PASS 3/3 pid=111: calendar,chat"])
        monkeypatch.setattr(api_server.job_admission, "list_active",
                            lambda: [{"account_id": 5, "job_name": "migrate", "pid": 222}])
        monkeypatch.setattr(api_server.job_admission, "is_live", lambda j: True)
        assert api_server._run_pass(5) is None

    def test_nothing_running_means_no_pass(self, monkeypatch):
        import api_server
        monkeypatch.setattr(api_server, "_job_log_lines", lambda *a, **k: ["PASS 2/3 pid=111: gmail"])
        monkeypatch.setattr(api_server.job_admission, "list_active", lambda: [])
        assert api_server._run_pass(5) is None

    def test_another_accounts_run_is_not_this_ones(self, monkeypatch):
        import api_server
        monkeypatch.setattr(api_server, "_job_log_lines", lambda *a, **k: ["PASS 2/3 pid=222: gmail"])
        monkeypatch.setattr(api_server.job_admission, "list_active",
                            lambda: [{"account_id": 9, "job_name": "migrate", "pid": 222}])
        monkeypatch.setattr(api_server.job_admission, "is_live", lambda j: True)
        assert api_server._run_pass(5) is None

    def test_the_run_prints_the_marker_the_api_parses(self):
        import re
        import main
        import api_server
        src = open(main.__file__, encoding="utf-8").read()
        assert 'print(f"PASS {i}/{len(plan)} pid={os.getpid()}: ' in src
        assert api_server._PASS_LINE.match("PASS 2/3 pid=4242: calendar,contacts,tasks,chat")
