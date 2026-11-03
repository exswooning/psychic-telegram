"""A message on the target twice: the mapped copy stays, the other goes to Trash (audit@t, 2026-11-03:
734 Message-IDs held twice, listed as extras because the check only knew the ones it compared)."""
import base64

import gmail_duplicates as G
from tests.conftest import SRC_USER, TGT_USER


def _put(gm, mid, header):
    raw = base64.urlsafe_b64encode(f"Message-ID: {header}\r\nSubject: s\r\n\r\nbody".encode()).decode()
    gm.messages[mid] = {"id": mid, "threadId": mid, "labelIds": ["INBOX"], "raw": raw}


def _setup(auth, db, settings, identity):
    settings.source_read_only = True
    gm = auth.target_gmail(TGT_USER)
    _put(gm, "old", "<a@x>"); _put(gm, "mine", "<a@x>")       # a twice, one mapped
    _put(gm, "only", "<b@x>")                                  # b once
    _put(gm, "p", "<c@x>"); _put(gm, "q", "<c@x>")             # c twice, none mapped
    db.record_mapping(SRC_USER, "s1", "mine", "message")
    return gm


def test_trashes_only_the_unmapped_copy_of_a_mapped_message(auth, db, settings, identity):
    gm = _setup(auth, db, settings, identity)
    out = G.trash_duplicates(auth, db, settings, SRC_USER, apply=True)
    assert out["changed"] == 1 and not out["errors"]
    assert "TRASH" in gm.messages["old"]["labelIds"]
    assert "TRASH" not in gm.messages["mine"]["labelIds"] and "TRASH" not in gm.messages["only"]["labelIds"]
    assert {g["messageId"] for g in out["left"]} == {"<c@x>"}                 # no mapped copy: not guessed
    assert "TRASH" not in gm.messages["p"]["labelIds"] and "TRASH" not in gm.messages["q"]["labelIds"]


def test_dry_run_changes_nothing(auth, db, settings, identity):
    gm = _setup(auth, db, settings, identity)
    out = G.trash_duplicates(auth, db, settings, SRC_USER, apply=False)
    assert out["candidates"] == 1 and out["changed"] == 0
    assert "TRASH" not in gm.messages["old"]["labelIds"]
