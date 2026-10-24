"""What dharan's 438 unclassified Drive findings were (2026-10-24), each pinned.

- ~77 files personal accounts put into the users' OWN folders: never walked, never copied.
- ~118 of Bitport's own copies, owned by the target domain and still readable by the
  source user who made them: read by the verifier as unmapped source files.
- 105+ files the ledger skipped with a reason: read by the verifier as unmapped.
"""
import verify_sample as V
from tests.conftest import SRC_USER, TGT_USER


def _own_folder_with(src, owner):
    folder = src.add_folder("Shared with a ward clerk")
    fid = src.add_binary("receipt.pdf", parent=folder)
    src.store[fid]["owners"] = [{"emailAddress": owner}]
    return folder, fid


def test_an_outsiders_file_in_the_users_own_folder_is_copied_there(migrator, auth, db, settings):
    settings.migrate_external_shares = True
    src = auth.source_drive(SRC_USER)
    folder, fid = _own_folder_with(src, "ward15tax@gmail.com")
    migrator.run()
    tid = db.get_target_id(SRC_USER, fid, "file")
    assert tid, "nobody else's run can carry it"
    tgt = auth.target_drive(TGT_USER)
    assert tgt.store[tid]["parents"] == [db.get_target_id(SRC_USER, folder, "folder")]


def test_a_colleagues_or_the_targets_file_there_is_left_alone(migrator, auth, db, settings):
    settings.migrate_external_shares = True
    src = auth.source_drive(SRC_USER)
    _, colleague = _own_folder_with(src, "bob@tenanta.com")
    _, ours = _own_folder_with(src, "alice@tenantb.com")
    migrator.run()
    assert not db.get_target_id(SRC_USER, colleague, "file")     # the colleague's run carries it
    assert not db.get_target_id(SRC_USER, ours, "file")          # already Bitport's own copy


def test_the_verifier_does_not_count_the_targets_own_copies(auth, db, settings, identity):
    settings.migrate_external_shares = True
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("karyapalika baithak.jpg")
    src.store[fid]["owners"] = [{"emailAddress": "alice@tenantb.com"}]
    r = V.run(auth, db, settings, [SRC_USER], ("drive",), progress=lambda *_: None)
    d = r["users"][SRC_USER]["drive"]
    assert not d["notCopied"] and d["sourceInventory"]["unmapped"] == 0


def test_the_verifier_accounts_for_a_reasoned_skip(auth, db, settings, identity):
    src = auth.source_drive(SRC_USER)
    skipped = src.add_binary("in another org's drive.pdf")
    lost = src.add_binary("never copied.pdf")
    db.log_audit(SRC_USER, skipped, "file", "SKIPPED_OTHER_ORG_SHARED_DRIVE", "stays there")
    r = V.run(auth, db, settings, [SRC_USER], ("drive",), progress=lambda *_: None)
    d = r["users"][SRC_USER]["drive"]
    assert [i["id"] for i in d["notCopied"]] == [lost]
    assert d["sourceInventory"]["notCopiedByDecision"] == 1


def test_an_office_file_is_compared_by_its_bytes_not_the_docs_api(migrator, auth, db, settings, identity):
    """Live: 31 .docx/.pptx read 'must not be an Office file' -- Word's mimeType ends in
    '.document' and PowerPoint's in '.presentation', so they went to the Docs/Slides API."""
    src = auth.source_drive(SRC_USER)
    for name, mime in (("a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                       ("b.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation")):
        src.add_binary(name, mime=mime)
    migrator.run()
    r = V.run(auth, db, settings, [SRC_USER], ("drive",), progress=lambda *_: None)
    d = r["users"][SRC_USER]["drive"]
    assert not d["errors"], d["errors"]
    assert r["verdict"] == "IDENTICAL"


def test_repair_re_walks_a_user_the_check_found_an_uncopied_file_for(db):
    import repair
    from db import bulk_seed_identities
    bulk_seed_identities(db, [(SRC_USER, TGT_USER)])
    db.conn.execute("CREATE TABLE IF NOT EXISTS verification_repair_queue (source_user TEXT, service TEXT, "
                    "state TEXT, detail TEXT)")
    db.conn.execute("INSERT INTO verification_repair_queue VALUES (?, 'drive', 'QUEUED', ?)",
                    (SRC_USER, '{"status": "UNMAPPED_SOURCE", "id": "x"}'))
    assert repair.unmapped_source_users(db) == [SRC_USER]
    db.conn.execute("UPDATE verification_repair_queue SET state='RESOLVED'")
    assert repair.unmapped_source_users(db) == []
