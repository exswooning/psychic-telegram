"""Recoverable (the 10/10 plan): the process is killed at the worst moment -- Google has accepted a write and
the ledger has not recorded it -- and the rerun converges: every item on the target exactly once.

A real kill (SIGKILL, the OOM killer, a power cut) leaves exactly this state: copies on the target the
ledger does not know about. It is made here directly -- run, then erase the ledger's record of some
writes that did land -- because a kill cannot be raised inside the engines' worker threads."""
import drive_engine
import gmail_engine
from tests.conftest import SRC_USER, TGT_USER

MSG = (b"Message-ID: <kill%d@x>\r\nDate: Mon, 1 Jan 2024 00:00:00 +0000\r\nSubject: s%d\r\n"
       b"From: a@x\r\nTo: b@y\r\n\r\nbody %d")


def _forget(db, kind_clause: str, n: int) -> int:
    """The ledger loses its record of n landed writes (mapping and audit row), as a kill would leave it."""
    rows = db.conn.execute(f"SELECT source_id FROM id_mapping WHERE source_user=? AND {kind_clause} "
                           "ORDER BY source_id LIMIT ?", (SRC_USER, n)).fetchall()
    ids = [r[0] for r in rows]
    with db.write() as c:
        for sid in ids:
            c.execute("DELETE FROM id_mapping WHERE source_user=? AND source_id=?", (SRC_USER, sid))
            c.execute("DELETE FROM audit_log WHERE source_user=? AND item_id=?", (SRC_USER, sid))
    if hasattr(db, "_mapping_cache"):
        db._mapping_cache.clear()
    return len(ids)


def _live_files(store):
    return [f for f in store.values() if not f.get("trashed")
            and f.get("mimeType") != "application/vnd.google-apps.folder"]


def test_drive_rerun_after_a_kill_has_every_file_exactly_once(auth, db, settings, quota, identity):
    settings.rewrite_drive_links = False
    sd = auth.source_drive(SRC_USER)
    folder = sd.add_folder("Projects")
    for n in range(8):
        fid = sd.add_binary(f"file{n}.pdf", parent=folder if n % 2 else None, data=f"bytes {n} ".encode() * 40)
        sd.store[fid]["createdTime"] = f"2024-01-0{n + 1}T00:00:00.000Z"     # every real Drive file has one
    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
    td = auth.target_drive(TGT_USER)
    assert len(_live_files(td.store)) == 8
    assert _forget(db, "type NOT IN ('folder')", 3) == 3

    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
    names = sorted(f["name"] for f in _live_files(td.store))
    assert names == sorted(f"file{n}.pdf" for n in range(8)), f"every file once, none twice: {names}"


def test_mail_rerun_after_a_kill_has_every_message_exactly_once(auth, db, settings, identity):
    sg = auth.source_gmail(SRC_USER)
    for n in range(6):
        sg.add_message(MSG % (n, n, n), ["INBOX"])
    gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
    tg = auth.target_gmail(TGT_USER)
    assert len(tg.messages) == 6
    assert _forget(db, "type='message'", 3) == 3

    gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
    assert len(tg.messages) == 6, f"every message once, none twice: {len(tg.messages)} on the target"
    mapped = db.conn.execute("SELECT COUNT(*) FROM id_mapping WHERE source_user=? AND type='message'",
                             (SRC_USER,)).fetchone()[0]
    assert mapped == 6, "the rerun adopted the copies it found instead of leaving them unknown"


def test_a_full_disk_fails_some_files_and_the_rerun_converges(auth, db, settings, quota, identity, monkeypatch, tmp_path):
    """Download-and-upload stages bytes on this box; a full disk (ENOSPC) must not fail the run or lose a file,
    and a rerun must copy nothing twice."""
    settings.rewrite_drive_links = False
    settings.scratch_dir = str(tmp_path / "scratch")
    sd = auth.source_drive(SRC_USER)
    for n in range(6):
        fid = sd.add_binary(f"doc{n}.pdf", data=f"payload {n} ".encode() * 30)
        sd.store[fid]["createdTime"] = f"2024-02-0{n + 1}T00:00:00.000Z"
    real = drive_engine.DriveMigrator._download_via
    full = {"left": 2}

    def disk_full(self, request_factory):
        if full["left"]:
            full["left"] -= 1
            raise OSError(28, "No space left on device")
        return real(self, request_factory)
    monkeypatch.setattr(drive_engine.DriveMigrator, "_download_via", disk_full)
    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
    td = auth.target_drive(TGT_USER)
    names = lambda: sorted(f["name"] for f in _live_files(td.store))      # noqa: E731
    # Measured: a download that dies on a full disk falls back to Drive's own server-side copy, which needs
    # no disk here -- the two files are not even failed.
    assert names() == [f"doc{n}.pdf" for n in range(6)]
    assert full["left"] == 0, "the disk really was full for two downloads"

    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
    assert names() == [f"doc{n}.pdf" for n in range(6)], "the rerun copies nothing twice"
