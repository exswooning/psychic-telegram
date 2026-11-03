"""Mapped copies that vanished from the target are copied again, not skipped as migrated. Live
(account 3, 2026-11-02): an earlier benchmark emptied audit@t, the ledger still mapped all 106 files,
and the run copied nothing, failing audit@ with 106 'missing'."""
from types import SimpleNamespace

import httplib2
from googleapiclient.errors import HttpError

import main
import migration_checks as C
from tests.conftest import SRC_USER, TGT_USER


def _err(status):
    return HttpError(httplib2.Response({"status": status}), b'{"error":{"message":"x"}}')


class _Drive:
    def __init__(self, answers):
        self.answers = answers

    def files(self):
        return self

    def get(self, fileId, **k):
        a = self.answers[fileId]
        return SimpleNamespace(execute=lambda: (_ for _ in ()).throw(a) if isinstance(a, Exception) else a)


def test_only_confirmed_gone_mappings_are_forgotten(db):
    forgot = []
    db.forget_mapping = lambda user, sid, kind: forgot.append(sid)
    items = [{"source": "gone", "target": "t1", "type": "file"},
             {"source": "binned", "target": "t2", "type": "file"},
             {"source": "elsewhere", "target": "t3", "type": "file"},
             {"source": "unsure", "target": "t4", "type": "file"}]
    drive = _Drive({"t1": _err(404), "t2": {"id": "t2", "trashed": True},
                    "t3": {"id": "t3", "trashed": False}, "t4": _err(500)})
    assert C.reopen_missing_mappings(db, drive, SRC_USER, items) == 2
    assert forgot == ["gone", "binned"]            # present elsewhere or unsure: never forgotten


def test_a_user_whose_target_was_emptied_is_copied_again_in_the_same_run(auth, db, settings, identity):
    settings.migration_checks_enabled = True
    settings.user_workers = 1
    sd = auth.source_drive(SRC_USER)
    for n in range(3):
        sd.add_binary(f"report-{n}.pdf", data=b"quarterly %d" % n)
    main.run_batch(auth, db, settings, {"drive"}, False, 0)
    td = auth.target_drive(TGT_USER)
    copies = [fid for fid, f in td.store.items() if f.get("name", "").startswith("report-")]
    assert len(copies) == 3
    for fid in copies:                                   # the account is emptied behind the ledger's back
        del td.store[fid]
    db.set_identity_status(SRC_USER, "PENDING")
    db.conn.execute("UPDATE identity_map SET services_done='' WHERE source_email=?", (SRC_USER,))
    db.conn.commit()
    out = main.run_batch(auth, db, settings, {"drive"}, False, 0)
    again = [f for f in td.store.values() if f.get("name", "").startswith("report-")]
    assert len(again) == 3, out
    assert out[0]["status"] == "DONE", out


def test_a_mailbox_emptied_behind_the_ledger_is_inserted_again_in_the_same_run(auth, db, settings, identity):
    """audit@ (account 3, 2026-11-03): 9 mapped messages gone from the target FAILED the user."""
    settings.migration_checks_enabled = True
    settings.user_workers = 1
    src = auth.source_gmail(SRC_USER)
    for n in range(4):
        src.add_message(f"Message-ID: <m{n}@t>\r\nSubject: s{n}\r\n\r\nbody {n}".encode())
    out = main.run_batch(auth, db, settings, {"gmail"}, False, 0)
    tg = auth.target_gmail(TGT_USER)
    assert len(tg.messages) == 4, out
    tg.messages.clear()                                   # the account is emptied
    db.set_identity_status(SRC_USER, "PENDING")
    db.conn.execute("UPDATE identity_map SET services_done='' WHERE source_email=?", (SRC_USER,))
    db.conn.commit()
    out = main.run_batch(auth, db, settings, {"gmail"}, False, 0)
    assert len(tg.messages) == 4 and out[0]["status"] == "DONE", out


def test_only_messages_confirmed_gone_are_forgotten(db):
    from tests.fakes import http_error
    forgot = []
    db.forget_mapping = lambda user, sid, kind: forgot.append((sid, kind))

    class _Msgs:
        def get(self, userId, id, **k):
            answer = {"t1": http_error(404), "t2": {"id": "t2"}, "t3": http_error(500)}[id]
            return SimpleNamespace(execute=lambda: (_ for _ in ()).throw(answer) if isinstance(answer, Exception) else answer)

    class _Gmail:
        def users(self):
            return SimpleNamespace(messages=lambda: _Msgs())
    items = [{"source": "gone", "target": "t1"}, {"source": "there", "target": "t2"}, {"source": "unsure", "target": "t3"}]
    assert C.reopen_missing_messages(db, _Gmail(), SRC_USER, items) == 1
    assert forgot == [("gone", "message")]
