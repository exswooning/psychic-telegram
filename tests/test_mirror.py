"""The mirror: a target kept as a continuously updated copy of its source.

What must hold, each against the fakes' own change feeds:
  - every change kind is classified correctly, and lands on the SAME target item;
  - a comment alone re-sends no content;
  - a marker the service no longer accepts falls back to a full re-scan;
  - a cycle that would delete more than the cap deletes nothing and waits;
  - keep mode never deletes;
  - an edit made on the mirror is recorded as a conflict and overwritten;
  - a cycle never overlaps another.
"""
from __future__ import annotations

import os

import pytest

import mirror
from contacts_engine import ContactsMigrator
from tasks_engine import TasksMigrator
from tests.conftest import SRC_USER, TGT_USER

RAW_1 = (b"Message-ID: <m1@tenanta.com>\r\nFrom: bob@tenanta.com\r\nTo: alice@tenanta.com\r\n"
         b"Date: Mon, 3 Jun 2019 10:00:00 +0000\r\nSubject: One\r\n\r\nBody.\r\n")
RAW_2 = (b"Message-ID: <m2@tenanta.com>\r\nFrom: bob@tenanta.com\r\nTo: alice@tenanta.com\r\n"
         b"Date: Tue, 4 Jun 2019 10:00:00 +0000\r\nSubject: Two\r\n\r\nBody.\r\n")
LATER = "2030-05-05T05:05:05Z"


@pytest.fixture
def world(auth, db, settings, migrator):
    """One user, migrated: two folders, a binary and a Doc, then a baseline cycle."""
    settings.migrate_chat = False
    src = auth.source_drive(SRC_USER)
    projects, archive = src.add_folder("Projects"), src.add_folder("Archive")
    pdf = src.add_binary("report.pdf", parent=projects, data=b"v1")
    doc = src.add_native("Plan", parent=projects, export_bytes=b"doc v1")
    migrator.run()
    db.set_identity_status(SRC_USER, "DONE")
    db.mark_services_done(SRC_USER, ["drive", "gmail", "calendar", "contacts", "tasks"])
    baseline = cycle(auth, db, settings)
    assert baseline["status"] == "ok", baseline
    return {"src": src, "tgt": auth.target_drive(TGT_USER), "projects": projects,
            "archive": archive, "pdf": pdf, "doc": doc}


def cycle(auth, db, settings, **kw):
    kw.setdefault("check_users", False)
    kw.setdefault("workers", 1)
    return mirror.Cycle(auth, db, settings, **kw).run()


def tid(db, sid, t="file"):
    return db.get_target_id(SRC_USER, sid, t)


# -- classification, on its own --------------------------------------------------------
class TestClassify:
    base = {"id": "f", "mimeType": "application/pdf", "name": "a.pdf", "parents": ["p"],
            "md5Checksum": "m1", "headRevisionId": "r1", "modifiedTime": "2024-01-01T00:00:00Z"}

    def fp(self, f=None, **over):
        return {**mirror.fingerprint_of(f or self.base, share="s1"), **over}

    def test_nothing_changed_is_nothing(self):
        assert mirror.classify(self.base, self.fp(), share="s1") == set()

    @pytest.mark.parametrize("change,kind", [
        ({"name": "b.pdf"}, "renamed"),
        ({"parents": ["q"]}, "moved"),
        ({"md5Checksum": "m2", "headRevisionId": "r2", "modifiedTime": LATER}, "edited"),
        ({"mimeType": "application/zip"}, "retyped"),
    ])
    def test_each_kind(self, change, kind):
        assert mirror.classify({**self.base, **change}, self.fp(), share="s1") == {kind}

    def test_sharing(self):
        assert mirror.classify({**self.base, "modifiedTime": LATER}, self.fp(), share="s2") == {"sharing"}

    def test_a_time_that_moved_alone_is_a_comment(self):
        assert mirror.classify({**self.base, "modifiedTime": LATER}, self.fp(), share="s1") == {"comment"}

    def test_a_doc_edit_is_a_new_revision_and_a_comment_is_not(self):
        doc = {**self.base, "mimeType": "application/vnd.google-apps.document",
               "md5Checksum": None, "headRevisionId": None}
        fp = self.fp(doc, revision="rev1")
        moved = {**doc, "modifiedTime": LATER}
        assert mirror.classify(moved, fp, latest_rev={"id": "rev2"}, share="s1") == {"edited"}
        assert mirror.classify(moved, fp, latest_rev={"id": "rev1"}, share="s1") == {"comment"}

    def test_a_docs_first_change_is_judged_by_its_newest_revisions_time(self):
        doc = {**self.base, "mimeType": "application/vnd.google-apps.document"}
        fp = self.fp(doc, revision=None)
        moved = {**doc, "modifiedTime": LATER}
        assert mirror.classify(moved, fp, latest_rev={"id": "r9", "modifiedTime": LATER},
                               share="s1") == {"edited"}
        assert mirror.classify(moved, fp, latest_rev={"id": "r1", "modifiedTime": doc["modifiedTime"]},
                               share="s1") == {"comment"}

    def test_an_inherited_grant_is_not_the_files_own(self):
        own = [{"type": "user", "role": "reader", "emailAddress": "x@y.com"}]
        inherited = own + [{"type": "user", "role": "writer", "emailAddress": "z@y.com",
                            "permissionDetails": [{"inherited": True, "role": "writer"}]}]
        assert mirror.share_hash(own) == mirror.share_hash(inherited)


# -- Drive, end to end on the fakes ----------------------------------------------------
class TestDrive:
    def test_every_kind_lands_on_the_same_target_file(self, world, auth, db, settings):
        src, tgt = world["src"], world["tgt"]
        pdf_t, doc_t = tid(db, world["pdf"]), tid(db, world["doc"])
        src.touch(world["pdf"], LATER, data=b"v2 of the report")
        src.edit_native(world["doc"], b"doc v2", LATER)
        other = src.add_binary("notes.txt", parent=world["projects"], data=b"n", mime="text/plain")
        # (migrated by the cycle below as a new file, then renamed and moved in the next)
        out = cycle(auth, db, settings)
        assert out["by_service"]["drive"]["edited"] == 2
        assert out["by_service"]["drive"]["new"] == 1
        assert (tid(db, world["pdf"]), tid(db, world["doc"])) == (pdf_t, doc_t)
        assert tgt.content[pdf_t] == b"v2 of the report"
        assert tgt.exports[doc_t] == b"doc v2"
        assert tgt.store[pdf_t]["modifiedTime"] == LATER        # the source's time put back

        other_t = tid(db, other)
        src.rename(other, "notes-final.txt")
        src.move(other, world["archive"])
        src.add_permission(other, "user", "reader", email="carol@partner.com")
        out = cycle(auth, db, settings)
        assert tid(db, other) == other_t
        assert tgt.store[other_t]["name"] == "notes-final.txt"
        assert tgt.store[other_t]["parents"] == [tid(db, world["archive"], "folder")]
        assert [p["emailAddress"] for p in tgt.perms[other_t]] == ["carol@partner.com"]
        drive = out["by_service"]["drive"]
        assert (drive["renamed"], drive["moved"], drive["sharing"]) == (1, 1, 1)

    def test_a_grant_removed_on_the_source_is_removed_on_the_target(self, world, auth, db, settings):
        src, tgt = world["src"], world["tgt"]
        src.add_permission(world["pdf"], "user", "reader", email="carol@partner.com")
        cycle(auth, db, settings)
        pdf_t = tid(db, world["pdf"])
        assert len(tgt.perms[pdf_t]) == 1
        src.remove_permission(world["pdf"], "carol@partner.com")
        cycle(auth, db, settings)
        assert tgt.perms[pdf_t] == []

    def test_a_comment_alone_sends_no_content(self, world, auth, db, settings):
        src, tgt = world["src"], world["tgt"]
        doc_t = tid(db, world["doc"])
        tgt.reset_calls()
        src.comment_on(world["doc"], "looks good", LATER)
        out = cycle(auth, db, settings)
        assert out["by_service"]["drive"]["comment"] == 1
        assert "edited" not in out["by_service"]["drive"]
        assert not [c for c in tgt.calls_to("files.update") if c.get("media_body") is not None]
        assert [c["content"].endswith("looks good") for c in tgt.comment_store[doc_t]] == [True]

    def test_a_comment_made_beside_an_edit_lands_too(self, world, auth, db, settings):
        src, tgt = world["src"], world["tgt"]
        src.add_comment(world["doc"], "said beside the edit")
        src.edit_native(world["doc"], b"doc v2", LATER)
        out = cycle(auth, db, settings)
        assert out["by_service"]["drive"]["edited"] == 1
        assert [c["content"].endswith("said beside the edit")
                for c in tgt.comment_store[tid(db, world["doc"])]] == [True]

    def test_an_edited_native_has_its_links_repointed(self, world, auth, db, settings, monkeypatch):
        """A re-import comes from an export that names the SOURCE's files."""
        import link_rewrite
        seen = []
        monkeypatch.setattr(link_rewrite, "rewrite_native",
                            lambda kind, svc, fid, lookup, pace=None, apply=True: seen.append(fid) or 0)
        monkeypatch.setattr(auth, "api", lambda *a: None, raising=False)
        world["src"].edit_native(world["doc"], b"doc v2", LATER)
        cycle(auth, db, settings)
        assert seen == [tid(db, world["doc"])]

    def test_a_new_file_in_a_new_folder(self, world, auth, db, settings):
        src, tgt = world["src"], world["tgt"]
        folder = src.add_folder("Q3", parent=world["projects"])
        f = src.add_binary("q3.pdf", parent=folder, data=b"q3")
        cycle(auth, db, settings)
        f_t, folder_t = tid(db, f), tid(db, folder, "folder")
        assert tgt.store[f_t]["parents"] == [folder_t]
        assert tgt.store[folder_t]["parents"] == [tid(db, world["projects"], "folder")]

    def test_an_expired_marker_falls_back_to_a_rescan(self, world, auth, db, settings):
        src, tgt = world["src"], world["tgt"]
        src.change_tokens_expired = True
        src.rename(world["pdf"], "renamed-while-expired.pdf")
        out = cycle(auth, db, settings)
        assert out["by_service"]["drive"]["rescans"] == 1
        assert tgt.store[tid(db, world["pdf"])]["name"] == "renamed-while-expired.pdf"
        src.change_tokens_expired = False
        src.rename(world["pdf"], "and-again.pdf")
        out = cycle(auth, db, settings)
        assert "rescans" not in out["by_service"]["drive"]
        assert tgt.store[tid(db, world["pdf"])]["name"] == "and-again.pdf"

    def test_the_copy_is_re_created_only_when_it_is_gone(self, world, auth, db, settings):
        src, tgt = world["src"], world["tgt"]
        old = tid(db, world["pdf"])
        tgt.files().delete(fileId=old).execute()
        src.touch(world["pdf"], LATER, data=b"v2")
        out = cycle(auth, db, settings)
        assert out["by_service"]["drive"]["recreated"] == 1
        new = tid(db, world["pdf"])
        assert new and new != old and tgt.content[new] == b"v2"


class TestMirrorSideEdits:
    def test_a_rename_on_the_mirror_is_a_conflict_and_is_put_back(self, world, auth, db, settings):
        tgt = world["tgt"]
        pdf_t = tid(db, world["pdf"])
        tgt.rename(pdf_t, "someone renamed the mirror")
        out = cycle(auth, db, settings)
        assert out["conflicts"] == 1
        assert tgt.store[pdf_t]["name"] == "report.pdf"
        row = db.conn.execute("SELECT * FROM mirror_conflicts").fetchone()
        assert (row["item_id"], row["target_id"]) == (world["pdf"], pdf_t)

    def test_content_edited_on_the_mirror_is_overwritten(self, world, auth, db, settings):
        tgt = world["tgt"]
        pdf_t = tid(db, world["pdf"])
        tgt.touch(pdf_t, LATER, data=b"edited on the mirror")
        out = cycle(auth, db, settings)
        assert out["conflicts"] == 1
        assert tgt.content[pdf_t] == b"v1"
        assert tid(db, world["pdf"]) == pdf_t

    def test_our_own_write_landing_late_is_not_a_conflict(self, world, auth, db, settings):
        """Docs bumps a commented Doc minutes after a re-import, stamped with the write's
        own time: no conflict, no second re-import, and the source's time put back."""
        src, tgt = world["src"], world["tgt"]
        src.edit_native(world["doc"], b"doc v2", "2024-02-02T00:00:00Z")
        cycle(auth, db, settings)
        doc_t = tid(db, world["doc"])
        tgt.store[doc_t]["modifiedTime"] = "2001-01-01T00:00:00Z"   # before the record
        tgt._mark(doc_t)
        out = cycle(auth, db, settings)
        assert out["conflicts"] == 0 and "native_reimported" not in out["by_service"].get("drive", {})
        assert tgt.store[doc_t]["modifiedTime"] == "2024-02-02T00:00:00Z"
        assert cycle(auth, db, settings)["conflicts"] == 0       # and the version is recorded

    def test_our_own_writes_are_not_conflicts(self, world, auth, db, settings):
        world["src"].rename(world["pdf"], "a.pdf")
        cycle(auth, db, settings)
        out = cycle(auth, db, settings)
        assert out["conflicts"] == 0


class TestDeletions:
    def _trash(self, world, n):
        src = world["src"]
        made = [src.add_binary(f"t{i}.pdf", parent=world["projects"], data=b"x") for i in range(n)]
        return made

    def test_under_the_cap_they_go_to_the_targets_bin(self, world, auth, db, settings):
        src, tgt = world["src"], world["tgt"]
        src.trash(world["pdf"])
        out = cycle(auth, db, settings, cap_pct=50)
        assert out["deletions"] == {"proposed": 1, "applied": 1, "held": 0}
        assert tgt.store[tid(db, world["pdf"])]["trashed"] is True     # binned, not deleted

    def test_over_the_cap_nothing_is_deleted_and_it_waits(self, world, auth, db, settings):
        src, tgt = world["src"], world["tgt"]
        made = self._trash(world, 6)
        cycle(auth, db, settings)
        for f in made:
            src.trash(f)
        held = []
        out = cycle(auth, db, settings, cap_pct=1, on_hold=lambda n, cap: held.append((n, cap)))
        assert out["deletions"]["held"] == 6 and out["deletions"]["applied"] == 0
        assert held == [(6, 1)]
        assert not any(tgt.store[tid(db, f)].get("trashed") for f in made)
        assert len(db.mirror_deletions("awaiting")) == 6
        assert mirror.decide_held(auth, db, "apply") == {"applied": 6, "failed": 0, "kept": 0}
        assert all(tgt.store[tid(db, f)]["trashed"] for f in made)

    def test_the_cap_counts_only_the_users_it_follows(self, world, auth, db, settings):
        src = world["src"]
        made = self._trash(world, 3)
        cycle(auth, db, settings, only=[SRC_USER])
        # A big ledger of someone else's: the cap must not grow with it.
        for i in range(10_000):
            db.record_mapping("other@tenanta.com", f"o{i}", f"t{i}", "file")
        for f in made:
            src.trash(f)
        out = cycle(auth, db, settings, cap_pct=10, only=[SRC_USER])
        assert out["deletions"]["held"] == 3 and out["deletions"]["applied"] == 0

    def test_keep_them_leaves_the_target_alone(self, world, auth, db, settings):
        src, tgt = world["src"], world["tgt"]
        src.trash(world["pdf"])
        cycle(auth, db, settings, deletions_paused=True)
        assert mirror.decide_held(auth, db, "keep") == {"applied": 0, "failed": 0, "kept": 1}
        assert not tgt.store[tid(db, world["pdf"])].get("trashed")

    def test_keep_mode_never_deletes(self, world, auth, db, settings):
        src, tgt = world["src"], world["tgt"]
        src.trash(world["pdf"])
        out = cycle(auth, db, settings, deletion_mode="keep", cap_pct=100)
        assert out["deletions"]["applied"] == 0
        assert not tgt.store[tid(db, world["pdf"])].get("trashed")
        assert db.mirror_deletions("kept")[0]["item_id"] == world["pdf"]


class TestOnlyWhatTheLedgerSaysIsDone:
    def test_a_service_reset_in_the_ledger_is_not_mirrored(self, auth, db, settings, migrator):
        """After a ledger reset of Drive a user stays DONE on the strength of the other
        services. Mirroring their Drive then would copy all of it again as new."""
        settings.migrate_chat = False
        src = auth.source_drive(SRC_USER)
        src.add_binary("a.pdf", data=b"x")
        db.set_identity_status(SRC_USER, "DONE")
        db.mark_services_done(SRC_USER, ["contacts"])
        src.reset_calls()
        out = cycle(auth, db, settings)
        assert out["status"] == "ok"
        assert src.calls == [] and auth.target_drive(TGT_USER).count() == 0
        assert db.mirror_marker(SRC_USER, "drive") is None


class TestOneAtATime:
    def test_a_cycle_never_overlaps_another(self, world, auth, db, settings):
        db.mirror_cycle_start(os.getppid())          # another process's live cycle
        out = cycle(auth, db, settings)
        assert out["status"] == "skipped"

    def test_a_cycle_whose_process_died_does_not_block_the_next(self, world, auth, db, settings):
        with db.write() as conn:
            conn.execute("INSERT INTO mirror_cycles (started_at, status, pid) "
                         "VALUES ('2026-01-01T00:00:00Z', 'running', 2147483000)")
        assert cycle(auth, db, settings)["status"] == "ok"
        assert db.conn.execute("SELECT status FROM mirror_cycles WHERE pid=2147483000"
                               ).fetchone()["status"] == "interrupted"


# -- mail, calendar, contacts, tasks ---------------------------------------------------
class TestOtherServices:
    def test_mail_labels_new_mail_and_a_deletion(self, world, auth, db, settings, gmail_migrator):
        g = auth.source_gmail(SRC_USER)
        m1 = g.add_message(RAW_1, ["INBOX", "UNREAD"])
        gmail_migrator.run()
        cycle(auth, db, settings)                      # the mail baseline
        t = auth.target_gmail(TGT_USER)
        t1 = db.get_target_id(SRC_USER, m1, "message")
        g.relabel(m1, remove=("UNREAD", "INBOX"))       # read and archived
        m2 = g.add_message(RAW_2, ["INBOX"])
        out = cycle(auth, db, settings)
        assert "UNREAD" not in t.messages[t1]["labelIds"] and "INBOX" not in t.messages[t1]["labelIds"]
        assert db.get_target_id(SRC_USER, m2, "message")
        assert out["by_service"]["gmail"]["new"] == 1
        g.remove_message(m1)
        out = cycle(auth, db, settings, cap_pct=100)
        assert out["deletions"]["applied"] == 1
        assert "TRASH" in t.messages[t1]["labelIds"]

    def test_mail_trashed_and_restored_in_one_cycle_stays_out_of_the_bin(
            self, world, auth, db, settings, gmail_migrator):
        g = auth.source_gmail(SRC_USER)
        m1 = g.add_message(RAW_1, ["INBOX"])
        gmail_migrator.run()
        cycle(auth, db, settings)
        t1 = db.get_target_id(SRC_USER, m1, "message")
        g.relabel(m1, add=("TRASH",), remove=("INBOX",))
        g.relabel(m1, remove=("TRASH",))
        out = cycle(auth, db, settings, cap_pct=100)
        assert out["deletions"]["proposed"] == 0
        assert "TRASH" not in auth.target_gmail(TGT_USER).messages[t1]["labelIds"]

    def test_an_edited_draft_is_updated_in_place(self, world, auth, db, settings, gmail_migrator):
        g = auth.source_gmail(SRC_USER)
        d = g.add_draft(RAW_1)
        gmail_migrator.run()
        cycle(auth, db, settings)
        td = db.get_target_id(SRC_USER, d, "draft")
        g.edit_draft(d, RAW_2)
        cycle(auth, db, settings)
        assert db.get_target_id(SRC_USER, d, "draft") == td
        assert auth.target_gmail(TGT_USER).drafts[td]["message"]["raw"] == \
            g.drafts[d]["message"]["raw"]

    def test_an_edited_event_is_patched_in_place(self, world, auth, db, settings, cal_migrator):
        c = auth.source_calendar(SRC_USER)
        e = c.add_event("Standup", ical="standup@tenanta.com")
        cal_migrator.run()
        cycle(auth, db, settings)
        key = cal_migrator._event_key("primary", e)
        te = db.get_target_id(SRC_USER, key, "event")
        c.edit_event(e, summary="Standup (moved)")
        out = cycle(auth, db, settings)
        assert db.get_target_id(SRC_USER, key, "event") == te
        assert auth.target_calendar(TGT_USER).store[te]["summary"] == "Standup (moved)"
        assert out["by_service"]["calendar"]["edited"] == 1
        c.cancel_event(e)
        out = cycle(auth, db, settings, cap_pct=100)
        assert out["deletions"]["applied"] == 1
        assert auth.target_calendar(TGT_USER).store[te]["status"] == "cancelled"

    def test_an_expired_calendar_token_is_a_full_sync(self, world, auth, db, settings, cal_migrator):
        c = auth.source_calendar(SRC_USER)
        c.add_event("Standup", ical="standup@tenanta.com")
        cal_migrator.run()
        cycle(auth, db, settings)
        c.sync_expired = True
        out = cycle(auth, db, settings)
        assert out["by_service"]["calendar"]["rescans"] == 1

    def test_a_contact_added_and_one_edited(self, world, auth, db, settings):
        p = auth.source_people(SRC_USER)
        ann = p.add_contact("Ann", email="ann@partner.com")
        ContactsMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
        cycle(auth, db, settings)
        t_ann = db.get_target_id(SRC_USER, ann, "contact")
        ben = p.add_contact("Ben", email="ben@partner.com")
        p.edit_contact(ann, "Anne")
        out = cycle(auth, db, settings)
        tp = auth.target_people(TGT_USER)
        assert tp.contacts[t_ann]["names"][0]["givenName"] == "Anne"
        assert db.get_target_id(SRC_USER, ben, "contact")
        assert (out["by_service"]["contacts"]["edited"], out["by_service"]["contacts"]["new"]) == (1, 1)

    def test_an_edited_task_and_a_deleted_one(self, world, auth, db, settings):
        s = auth.source_tasks(SRC_USER)
        todo = s.add_list("Todo")
        milk = s.add_task(todo, "Buy milk")
        bread = s.add_task(todo, "Buy bread")
        TasksMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
        cycle(auth, db, settings)
        s.edit_task(todo, milk, title="Buy oat milk")
        s.delete_task(todo, bread)
        out = cycle(auth, db, settings)
        tl = db.get_target_id(SRC_USER, todo, "task_list")
        tt = auth.target_tasks(TGT_USER).task_store[tl]
        assert [t["title"] for t in tt if t["id"] == db.get_target_id(SRC_USER, milk, "task")] == ["Buy oat milk"]
        assert out["by_service"]["tasks"]["deletion_unsupported"] == 1
        assert len(tt) == 2                                   # nothing deleted: no bin


class TestAMirrorThatFollowsOneMigration:
    def test_a_user_outside_it_is_not_mirrored(self, world, auth, db, settings):
        world["src"].add_binary("late.pdf", parent=world["projects"], data=b"x")
        out = cycle(auth, db, settings, only=["someone-else@tenanta.com"])
        assert out["by_service"].get("drive", {}).get("new", 0) == 0

    def test_a_user_inside_it_is(self, world, auth, db, settings):
        from tests.conftest import SRC_USER
        world["src"].add_binary("late.pdf", parent=world["projects"], data=b"x")
        out = cycle(auth, db, settings, only=[SRC_USER.upper()])
        assert out["by_service"]["drive"]["new"] == 1


class TestNewSourceUsers:
    def test_a_mirror_of_chosen_users_reports_a_new_one_and_never_provisions_it(
            self, db, settings, monkeypatch):
        from db import bulk_seed_identities
        bulk_seed_identities(db, [(SRC_USER, TGT_USER)])

        class Dir:
            def users(self): return self
            def list(self, **k):
                return type("C", (), {"execute": lambda s: {"users": [
                    {"primaryEmail": SRC_USER}, {"primaryEmail": "newbie@tenanta.com"}]}})()
        auth = type("A", (), {"directory": lambda self, side, **k: Dir()})()
        made = []
        monkeypatch.setattr(mirror.Cycle, "_first_run", lambda self, src: made.append(src))
        chosen = mirror.Cycle(auth, db, settings, only=[SRC_USER])
        chosen._check_users([(SRC_USER, TGT_USER)])
        assert made == [] and chosen.users["not_followed"] == ["newbie@tenanta.com"]
        mirror.Cycle(auth, db, settings)._check_users([(SRC_USER, TGT_USER)])
        assert made == ["newbie@tenanta.com"]          # the whole-tenant mirror still does
