"""What the check finds GONE from the target is copied again by the repair, not only by a migration pass.
Live (audit@, account 3, 6 Nov 2026): 2 task lists, 1 contact and 23 drafts stayed missing through every
repair. The engines confirm each item is gone (404 for that very item) before forgetting its mapping."""
import contacts_engine
import finding_repairs as F
import gmail_engine
import tasks_engine
from tests.conftest import SRC_USER, TGT_USER

MSG = b"From: a@s.test\r\nTo: b@s.test\r\nSubject: draft %d\r\nMessage-ID: <d%d@s.test>\r\n\r\nbody %d"


def test_a_task_list_gone_from_the_target_is_copied_again(auth, db, settings, identity):
    settings.source_read_only = True
    settings.migrate_tasks = True
    src = auth.source_tasks(SRC_USER)
    src.add_task(src.add_list("My Tasks"), "Book the window")
    tasks_engine.TasksMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
    tgt = auth.target_tasks(TGT_USER)
    tgt.lists.clear(); tgt.task_store.clear()
    per = {"tasks": {"errors": ["task list 'My Tasks': HTTP 404 (notFound): <HttpError 404 ...>"]}}
    out = F.recopy_gone_items(auth, db, settings, SRC_USER, per)
    assert "tasks" in out["recopied"] and not out["errors"], out
    assert len(tgt.lists) == 1 and sum(len(v) for v in tgt.task_store.values()) == 1


def test_a_contact_gone_from_the_target_is_copied_again(auth, db, settings, identity):
    settings.source_read_only = True
    settings.migrate_contacts = True
    auth.source_people(SRC_USER).add_contact("Ada", "ada@example.com")
    contacts_engine.ContactsMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
    tgt = auth.target_people(TGT_USER)
    tgt.contacts.clear()
    per = {"contacts": {"missing": [{"source": "people/c1", "target": "people/c2",
                                     "why": "not on the target (Requested entity was not found.)"}]}}
    out = F.recopy_gone_items(auth, db, settings, SRC_USER, per)
    assert "contacts" in out["recopied"] and not out["errors"], out
    assert len(tgt.contacts) == 1


def test_nothing_is_copied_when_nothing_is_gone(auth, db, settings, identity):
    settings.source_read_only = True
    out = F.recopy_gone_items(auth, db, settings, SRC_USER, {"tasks": {"errors": []}, "gmail": {"missing": []}})
    assert out["candidates"] == 0 and out["recopied"] == []


def test_a_dry_run_copies_nothing(auth, db, settings, identity):
    settings.source_read_only = True
    settings.dry_run = True
    per = {"tasks": {"errors": ["task list 'My Tasks': HTTP 404 (notFound)"]}}
    assert F.recopy_gone_items(auth, db, settings, SRC_USER, per)["recopied"] == []


def test_a_draft_gone_from_the_target_is_copied_again(auth, db, settings, identity):
    settings.source_read_only = True
    settings.rewrite_drive_links = False
    sg = auth.source_gmail(SRC_USER)
    sg.add_draft(MSG % (1, 1, 1))
    gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
    tg = auth.target_gmail(TGT_USER)
    assert len(tg.drafts) == 1
    source_id, target_id = next(iter(db.conn.execute(
        "SELECT source_id, target_id FROM id_mapping WHERE source_user=? AND type='draft'", (SRC_USER,))))
    tg.drafts.clear()                                              # the target mailbox was emptied
    per = {"gmail": {"missing": [{"source": source_id, "target": target_id,
                                  "why": "draft not on the target (HTTP 404 (notFound))"}]}}
    out = F.recopy_gone_items(auth, db, settings, SRC_USER, per)
    assert "drafts" in out["recopied"] and not out["errors"], out
    assert len(tg.drafts) == 1
