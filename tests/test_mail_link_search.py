"""Split mail, search-first: Gmail's own search names the link candidates and only
their bodies are read. Exhaustive mode still reads every message."""
import base64
import json

import pytest

import gmail_engine
from config import DEFERRED_TO_DMS
from tests.conftest import SRC_USER
from tests.test_split_mail_mode import SRC_FILE, TGT_FILE, LINKED, PLAIN

URL = f"https://drive.google.com/file/d/{SRC_FILE}/view".encode()


def _msg(n, body=b"Noon?"):
    return (f"Message-ID: <m{n}@tenanta.com>\r\nFrom: bob@tenanta.com\r\nTo: alice@tenanta.com\r\n"
            f"Date: Mon, 3 Jun 2019 11:00:00 +0000\r\nSubject: s{n}\r\n\r\n").encode() + body + b"\r\n"


def _attached(n, name, text):
    return (f"Message-ID: <a{n}@tenanta.com>\r\nFrom: bob@tenanta.com\r\nTo: alice@tenanta.com\r\n"
            f"Date: Mon, 3 Jun 2019 11:00:00 +0000\r\nSubject: a{n}\r\nMIME-Version: 1.0\r\n"
            "Content-Type: multipart/mixed; boundary=b\r\n\r\n--b\r\nContent-Type: text/plain\r\n\r\nsee attached\r\n"
            f"--b\r\nContent-Type: text/html\r\nContent-Disposition: attachment; filename=\"{name}\"\r\n\r\n").encode() \
        + text + b"\r\n--b--\r\n"


def _hidden(n):
    """A link Gmail's phrase search cannot see in the bytes: base64 transfer encoding."""
    part = base64.b64encode(b"<a href='" + URL + b"'>the deck</a>")
    return (f"Message-ID: <h{n}@tenanta.com>\r\nFrom: bob@tenanta.com\r\nTo: alice@tenanta.com\r\n"
            f"Date: Mon, 3 Jun 2019 11:00:00 +0000\r\nSubject: h{n}\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/html\r\nContent-Transfer-Encoding: base64\r\n\r\n").encode() + part + b"\r\n"


@pytest.fixture
def mailbox(gmail_migrator, auth, db, settings):
    settings.rewrite_drive_links = settings.mail_only_with_links = True
    db.record_mapping(SRC_USER, SRC_FILE, TGT_FILE, "file")
    src = auth.source_gmail(SRC_USER)
    ids = {"linked": src.add_message(LINKED), "plain": src.add_message(PLAIN),
           "attached": src.add_message(_attached(1, "notes.html", URL)),
           "chip": src.add_message(_msg(0, b"a chip only")),
           "draft": src.add_message(_msg(1), ["DRAFT"]),
           "hidden": src.add_message(_hidden(1))}
    for n in range(2, 32):
        src.add_message(_msg(n))
    src.chip_hits["has:drive"] = {ids["chip"]}
    src.chip_hits["has:document"] = {ids["chip"]}          # a duplicate hit, read once
    return gmail_migrator, src, ids


def _reads(src):
    return [kw["id"] for kw in src.calls_to("messages.get") if kw.get("format") == "full"]


def _report(db):
    return json.loads(db.conn.execute("SELECT report FROM mail_link_scans").fetchone()[0])


def test_search_reads_only_the_candidate_bodies(mailbox, db, settings):
    migrator, src, ids = mailbox
    settings.mail_link_scan = "search"
    migrator.run()
    assert sorted(_reads(src)) == sorted([ids["linked"], ids["attached"], ids["chip"]])
    r = _report(db)
    assert r["state"] == "DONE" and r["messages"] == 36 and r["bodiesRead"] == 3
    assert r["notRead"] == 33 and r["candidates"] == 3 and r["recallMissed"] == 0
    assert r["searches"]["has:drive"] == 1 and r["searches"]['"drive.google.com"'] == 2


def test_exhaustive_still_reads_every_message(mailbox, db, settings):
    migrator, src, _ = mailbox
    settings.mail_link_scan = "exhaustive"
    migrator.run()
    assert len(set(_reads(src))) == 36
    assert _report(db)["notRead"] == 0


def test_an_unread_message_says_it_was_not_read_and_a_draft_stays_with_the_drafts_pass(mailbox, db, settings):
    migrator, _, ids = mailbox
    settings.mail_link_scan = "search"
    migrator.run()
    plain = db.get_audit(SRC_USER, ids["plain"], "message")
    assert plain["status"] == DEFERRED_TO_DMS and "not read" in plain["error_message"]
    assert db.get_audit(SRC_USER, ids["draft"], "message")["status"] == "SKIPPED_IS_DRAFT"


def test_a_link_search_cannot_see_is_missed_by_search_and_caught_by_exhaustive(mailbox, db, settings):
    """The limitation, pinned: a base64-encoded link is invisible to a phrase search."""
    migrator, src, ids = mailbox
    settings.mail_link_scan = "search"
    migrator.run()
    assert ids["hidden"] not in _reads(src)
    assert db.get_audit(SRC_USER, ids["hidden"], "message")["status"] == DEFERRED_TO_DMS
    db.conn.execute("DELETE FROM audit_log WHERE item_id=?", (ids["hidden"],))
    settings.mail_link_scan = "exhaustive"
    migrator.run()
    assert db.get_target_id(SRC_USER, ids["hidden"], "message")


def test_a_message_the_ledger_proved_link_bearing_is_read_even_when_search_misses_it(mailbox, db, settings):
    migrator, src, ids = mailbox
    settings.mail_link_scan = "search"
    db.log_audit(SRC_USER, ids["hidden"], "link_rewrite", "SUCCESS", "1 Drive link(s) repointed")
    migrator.run()
    assert ids["hidden"] in _reads(src)
    assert _report(db)["recallMissed"] == 1


def test_a_delta_window_bounds_every_search_and_does_not_claim_recall(mailbox, db, settings):
    migrator, src, _ = mailbox
    settings.mail_link_scan = "search"
    migrator.run(delta=True, since_epoch_days=3)
    qs = [kw["q"] for kw in src.calls_to("messages.list")]
    assert qs and all("newer_than:3d" in q for q in qs)
    assert _report(db)["recallMissed"] is None


def test_an_interrupted_scan_is_recorded_as_interrupted(mailbox, db, settings, monkeypatch):
    migrator, _, _ = mailbox
    settings.mail_link_scan = "search"
    monkeypatch.setattr(gmail_engine, "shutdown_requested", lambda: True)
    with pytest.raises(RuntimeError, match="interrupted"):
        migrator._scan_split_before_delivery("")
    assert _report(db)["state"] == "INTERRUPTED"


def test_an_unknown_mode_is_refused(mailbox, settings):
    settings.mail_link_scan = "fast"
    with pytest.raises(ValueError, match="MAIL_LINK_SCAN"):
        mailbox[0]._scan_split_before_delivery("")


def test_a_blocked_scan_owes_the_clean_mail_to_the_dms_so_repair_cannot_copy_it(mailbox, db, settings):
    """Live: a blocked scan left no rows, the verifier read 734 messages as lost, and the repair's
    missing-mail recovery copied the whole mailbox through the engine ahead of the DMS."""
    migrator, src, ids = mailbox
    settings.mail_link_scan = "exhaustive"
    unmapped = "UNKNOWNFILEID" + "x" * 21        # a file the source still holds, no run mapped
    src.messages[ids["linked"]]["raw"] = base64.urlsafe_b64encode(
        LINKED.replace(SRC_FILE.encode(), unmapped.encode())).decode()
    migrator.auth.source_drive(SRC_USER).store[unmapped] = {
        "id": unmapped, "owners": [{"emailAddress": SRC_USER}]}
    with pytest.raises(RuntimeError, match="blocked"):
        migrator.run()
    assert db.get_audit(SRC_USER, ids["plain"], "message")["status"] == DEFERRED_TO_DMS
    assert db.get_audit(SRC_USER, ids["linked"], "message")["status"] == "FAILED_SPLIT"
    assert not db.get_target_id(SRC_USER, ids["plain"], "message")


def test_the_scan_has_its_own_limiter_and_worker_count(mailbox, db, settings):
    """Live: held to per_user_qps (3/s) the scan took 257 s for 758 messages; at 25/s, 33 s."""
    migrator, _, _ = mailbox
    settings.mail_scan_qps, settings.mail_scan_workers, settings.mail_workers = 77.0, 9, 1
    settings.mail_link_scan = "exhaustive"
    migrator = type(migrator)(migrator.auth, db, settings, SRC_USER, migrator.target_user)
    migrator._scan_split_before_delivery("")
    assert migrator.scan_limiter.rate == 77.0
    assert (migrator._link_scan["scanWorkers"], migrator._link_scan["scanQps"]) == (9, 77.0)
