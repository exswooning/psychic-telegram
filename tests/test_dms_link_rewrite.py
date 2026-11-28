"""Mail the DMS carried across still links to the SOURCE's Drive files; dms_link_rewrite replaces
each such copy with a rewritten one -- and never touches an email that is not that DMS copy."""
import base64

import pytest

import dms_link_rewrite as R
from tests.conftest import SRC_USER, TGT_USER

SRC_FILE, TGT_FILE = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345", "1ZyXwVuTsRqPoNmLkJiHgFeDcBa987654"
LINKED = (b"Message-ID: <linked@tenanta.com>\r\nFrom: bob@tenanta.com\r\nTo: alice@tenanta.com\r\n"
          b"Date: Mon, 3 Jun 2019 10:00:00 +0000\r\nSubject: The deck\r\n\r\n"
          + f"Here it is: https://drive.google.com/file/d/{SRC_FILE}/view\r\n".encode())


def _raws(g):
    return {i: base64.urlsafe_b64decode(m["raw"].encode()) for i, m in g.messages.items()}


@pytest.fixture
def after_dms(gmail_migrator, auth, db, settings):
    settings.rewrite_drive_links = True
    db.record_mapping(SRC_USER, SRC_FILE, TGT_FILE, "file")        # Drive migrated
    auth.source_gmail(SRC_USER).add_message(LINKED, ["INBOX"])
    tgt = auth.target_gmail(TGT_USER)
    tgt.add_message(LINKED + b"\r\n", ["INBOX", "IMPORTANT"])        # the DMS copy, as Gmail stores it
    return gmail_migrator, tgt


def test_the_dms_copy_is_replaced_by_a_rewritten_one_in_place(after_dms, db):
    gm, tgt = after_dms
    old = next(iter(tgt.messages))
    r = R.rewrite_user(gm, tgt, apply=True, say=lambda *_: None)
    assert r["counts"] == {"rewritten": 1} and r["left"] == []
    (new_id, raw), = _raws(tgt).items()
    assert new_id != old and TGT_FILE.encode() in raw and SRC_FILE.encode() not in raw
    assert set(tgt.messages[new_id]["labelIds"]) >= {"INBOX", "IMPORTANT"}      # labels kept
    assert db.get_target_id(SRC_USER, gm.src.users().messages().list(userId="me").execute()
                            ["messages"][0]["id"], "message") == new_id
    # run again: nothing left to do
    assert R.rewrite_user(gm, tgt, apply=True, say=lambda *_: None)["counts"] == {"engine copy, already rewritten": 1}


def test_a_dry_run_changes_nothing(after_dms):
    gm, tgt = after_dms
    before = dict(_raws(tgt))
    assert R.rewrite_user(gm, tgt, apply=False, say=lambda *_: None)["counts"] == {"would rewrite": 1}
    assert _raws(tgt) == before


def test_an_email_that_only_shares_the_message_id_is_never_touched(gmail_migrator, auth, db, settings):
    settings.rewrite_drive_links = True
    db.record_mapping(SRC_USER, SRC_FILE, TGT_FILE, "file")
    auth.source_gmail(SRC_USER).add_message(LINKED, ["INBOX"])
    tgt = auth.target_gmail(TGT_USER)
    tgt.add_message(LINKED.replace(b"Here it is", b"Something else entirely"), ["INBOX"])
    before = dict(_raws(tgt))
    r = R.rewrite_user(gmail_migrator, tgt, apply=True, say=lambda *_: None)
    assert r["counts"] == {"target copy is not this email: left": 1} and len(r["left"]) == 1
    assert _raws(tgt) == before


def test_not_yet_imported_mail_waits(gmail_migrator, auth, db, settings):
    settings.rewrite_drive_links = True
    db.record_mapping(SRC_USER, SRC_FILE, TGT_FILE, "file")
    auth.source_gmail(SRC_USER).add_message(LINKED, ["INBOX"])
    r = R.rewrite_user(gmail_migrator, auth.target_gmail(TGT_USER), apply=True, say=lambda *_: None)
    assert r["counts"] == {"not on the target yet": 1}
