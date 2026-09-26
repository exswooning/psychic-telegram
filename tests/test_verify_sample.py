"""The one-to-one verifier must be unable to flatter a migration.

Two kinds of test. The comparisons are pure and tested one rule at a time. Then the
important ones: run the REAL engines on the fakes, verify what they produced, and
damage the target in each way a migration can go wrong -- a file changed, a file
gone, a file copied twice, a message altered, a contact missing -- and require that
every one of them is found. A verifier that passes is only worth something if it is
shown to fail.
"""
import base64
import hashlib

import pytest

import calendar_engine
import contacts_engine
import drive_engine
import gmail_engine
import tasks_engine
import verify_sample as V
from tests.conftest import SRC_USER, TGT_USER

MSG = (b"Message-ID: <m%d@tenanta.com>\r\nFrom: bob@tenanta.com\r\nTo: alice@tenanta.com\r\n"
       b"Date: Mon, 3 Jun 2019 10:00:00 +0000\r\nSubject: n%d\r\n\r\nbody %d\r\n")


class TestCompareDriveFile:
    A = {"name": "a.pdf", "mimeType": "application/pdf", "size": "5", "md5Checksum": "m", "modifiedTime": "2024-01-01T00:00:00Z"}

    def test_the_same_is_no_difference(self):
        assert V.compare_drive_file(self.A, dict(self.A), "/x", "/x") == []

    @pytest.mark.parametrize("field,val,needle", [("name", "b.pdf", "name differs"), ("mimeType", "text/plain", "type differs"),
                                                  ("md5Checksum", "z", "MD5 differs"), ("size", "9", "size differs"),
                                                  ("modifiedTime", "2025-06-01T00:00:00Z", "modified time")])
    def test_each_kind_of_damage_is_named(self, field, val, needle):
        assert any(needle in d for d in V.compare_drive_file(self.A, {**self.A, field: val}, "/x", "/x"))

    def test_a_different_folder_is_a_difference(self):
        assert any("folder path" in d for d in V.compare_drive_file(self.A, self.A, "/a/b", "/a"))

    def test_sub_second_time_noise_is_not(self):
        assert V.compare_drive_file(self.A, {**self.A, "modifiedTime": "2024-01-01T00:00:00.123Z"}, "/", "/") == []


class TestCompareMessage:
    RAW = MSG % (1, 1, 1)

    def test_byte_identical(self):
        assert V.compare_message(self.RAW, self.RAW) == ("identical", [])

    def test_a_link_rewrite_recomputed_here_is_recognised_as_intended(self):
        new = self.RAW.replace(b"body", b"BODY")
        assert V.compare_message(self.RAW, new, expected_after_rewrite=new)[0] == "identical after rewrite"

    def test_a_rewrite_that_is_not_the_expected_one_is_different(self):
        assert V.compare_message(self.RAW, self.RAW.replace(b"body", b"OTHER"),
                                 expected_after_rewrite=self.RAW.replace(b"body", b"BODY"))[0] == "different"

    def test_headers_google_added_are_said_not_hidden_and_are_not_a_difference(self):
        added = b"Received: from x by y\r\n" + self.RAW
        verdict, notes = V.compare_message(self.RAW, added)
        assert verdict == "equivalent" and "Received" in notes[0]

    def test_an_altered_body_is_different(self):
        verdict, notes = V.compare_message(self.RAW, self.RAW.replace(b"body 1", b"body 2"))
        assert verdict == "different" and any("body differs" in n for n in notes)

    def test_an_altered_subject_is_different(self):
        assert V.compare_message(self.RAW, self.RAW.replace(b"n1", b"zz"))[0] == "different"

    def test_labels(self):
        assert V.compare_labels({"INBOX", "Work"}, {"INBOX", "Work"}) == []
        assert any("missing" in d for d in V.compare_labels({"INBOX", "Work"}, {"INBOX"}))
        assert any("added" in d for d in V.compare_labels({"INBOX"}, {"INBOX", "Spam"}))


class TestCompareTheRest:
    same = staticmethod(lambda a: a)

    def test_event(self):
        e = {"summary": "S", "start": {"dateTime": "2024-01-01T10:00:00Z"}, "end": {"dateTime": "2024-01-01T11:00:00Z"},
             "attendees": [{"email": "a@tenanta.com"}]}
        t = {**e, "attendees": [{"email": "a@tenantb.com"}]}
        tr = lambda x: x.replace("tenanta", "tenantb")  # noqa: E731
        assert V.compare_event(e, t, tr) == []
        assert any("summary" in d for d in V.compare_event(e, {**t, "summary": "X"}, tr))
        assert any("start" in d for d in V.compare_event(e, {**t, "start": {"dateTime": "2024-01-01T12:00:00Z"}}, tr))
        assert any("attendees" in d for d in V.compare_event(e, {**t, "attendees": []}, tr))

    def test_contact_ignores_phone_formatting_but_not_the_number(self):
        c = {"names": [{"givenName": "Ann"}], "phoneNumbers": [{"value": "+1 (555) 010-2000"}]}
        assert V.compare_contact(c, {**c, "phoneNumbers": [{"value": "15550102000"}]}, self.same) == []
        assert any("phones" in d for d in V.compare_contact(c, {**c, "phoneNumbers": [{"value": "15550109999"}]}, self.same))

    def test_task(self):
        t = {"title": "T", "status": "needsAction", "due": "2024-05-01T00:00:00.000Z"}
        assert V.compare_task(t, {**t, "due": "2024-05-01T09:00:00.000Z"}) == []
        assert any("status" in d for d in V.compare_task(t, {**t, "status": "completed"}))


class TestTheVerdict:
    def _r(self, **over):
        r = V.Verifier._blank(); r.update(checked=1, identical=1); r.update(over)
        return r

    def test_identical_only_when_nothing_is_off(self):
        assert V.verdict_of({"u": {"drive": self._r()}}, ("drive",))[0] == "IDENTICAL"

    @pytest.mark.parametrize("label", ["differences", "missing", "duplicates", "notCopied"])
    def test_each_real_problem_makes_it_differences(self, label):
        assert V.verdict_of({"u": {"drive": self._r(**{label: [{}]})}}, ("drive",))[0] == "DIFFERENCES"

    def test_a_check_that_could_not_be_made_is_incomplete_never_a_pass(self):
        assert V.verdict_of({"u": {"drive": self._r(errors=["boom"])}}, ("drive",))[0] == "INCOMPLETE"

    def test_a_service_that_was_never_checked_is_incomplete(self):
        assert V.verdict_of({"u": {}}, ("drive",))[0] == "INCOMPLETE"

    def test_strays_alone_are_informational(self):
        """A new mailbox arrives with welcome messages nobody migrated."""
        assert V.verdict_of({"u": {"gmail": self._r(extras=[{}])}}, ("gmail",))[0] == "IDENTICAL"

    def test_differences_outrank_incomplete(self):
        assert V.verdict_of({"u": {"drive": self._r(errors=["x"], differences=[{}])}}, ("drive",))[0] == "DIFFERENCES"


@pytest.fixture
def migrated(auth, db, settings, identity, quota):
    """Real engines run over a small source; returns everything needed to tamper."""
    settings.rewrite_drive_links = False
    settings.migrate_tasks = True
    sd = auth.source_drive(SRC_USER)
    folder = sd.add_folder("Projects")
    sd.add_binary("report.pdf", parent=folder, data=b"the quarterly report " * 50)
    sd.add_binary("notes.txt", data=b"plain notes", mime="text/plain")
    sd.add_native("Plan", parent=folder, kind="document", export_bytes=b"native plan text")
    sg = auth.source_gmail(SRC_USER)
    for n in range(4):
        sg.add_message(MSG % (n, n, n), ["INBOX"] if n else ["INBOX", "UNREAD"])
    auth.source_calendar(SRC_USER).add_event("Kickoff")
    auth.source_people(SRC_USER).add_contact("Ann", "ann@tenanta.com")
    st = auth.source_tasks(SRC_USER)
    st.add_task(st.add_list("Move"), "Book the cutover")
    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
    gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
    calendar_engine.CalendarMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
    contacts_engine.ContactsMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
    tasks_engine.TasksMigrator(auth, db, settings, SRC_USER, TGT_USER).run()

    def target_of(kind, name=None):
        rows = db.conn.execute("SELECT source_id, target_id, source_name FROM id_mapping WHERE source_user=? AND type=?",
                               (SRC_USER, kind)).fetchall()
        return [r["target_id"] for r in rows]
    return type("M", (), {"auth": auth, "db": db, "settings": settings, "target_of": staticmethod(target_of)})


def verify(m, services=V.ALL_SERVICES):
    return V.run(m.auth, m.db, m.settings, [SRC_USER], services, progress=lambda *_: None)


class TestARealMigrationIsFoundIdentical:
    def test_everything_the_engines_copied_is_identical(self, migrated):
        r = verify(migrated)
        assert r["verdict"] == "IDENTICAL", r["reasons"]
        t = r["totals"]
        assert t["differences"] == t["missing"] == t["duplicates"] == t["notCopied"] == t["errors"] == 0
        assert t["checked"] >= 10 and t["identical"] == t["checked"]

    def test_it_really_opened_the_files_and_read_the_bytes(self, migrated):
        r = verify(migrated)
        opened = [e for e in r["evidence"] if e["service"] == "drive" and e.get("opened")]
        assert {e["name"] for e in opened} >= {"report.pdf", "notes.txt", "Plan"}
        pdf = next(e for e in opened if e["name"] == "report.pdf")
        assert pdf["bytes"] == len(b"the quarterly report " * 50)
        assert pdf["sha256"] == hashlib.sha256(b"the quarterly report " * 50).hexdigest()[:16]
        assert "downloaded from both" in pdf["how"]

    def test_native_documents_are_exported_and_compared(self, migrated):
        r = verify(migrated)
        plan = next(e for e in r["evidence"] if e.get("name") == "Plan")
        assert "exported" in plan["how"]

    def test_it_writes_nothing_to_either_tenant(self, migrated):
        def snap():
            return ({k: dict(v) for k, v in migrated.auth.target_drive(TGT_USER).store.items()},
                    dict(migrated.auth.target_gmail(TGT_USER).messages))
        before = snap()
        verify(migrated)
        assert snap() == before


class TestEveryKindOfDamageIsFound:
    def _drive_target(self, m, name):
        tgt = m.auth.target_drive(TGT_USER)
        return tgt, next(f for f in tgt.store.values() if f.get("name") == name)

    def test_a_file_whose_content_changed(self, migrated):
        tgt, f = self._drive_target(migrated, "report.pdf")
        tgt.content[f["id"]] = b"tampered" * 10
        r = verify(migrated)
        assert r["verdict"] == "DIFFERENCES"
        d = r["users"][SRC_USER]["drive"]["differences"][0]
        assert d["item"] == "report.pdf" and any("content differs" in x for x in d["diffs"])

    def test_a_file_whose_content_changed_even_though_its_metadata_still_matches(self, migrated):
        """Size and MD5 metadata alone would pass this; opening the file does not."""
        tgt, f = self._drive_target(migrated, "notes.txt")
        tgt.content[f["id"]] = b"PLAIN NOTES"                   # same length, metadata untouched
        assert f["md5Checksum"] == hashlib.md5(b"plain notes").hexdigest()
        r = verify(migrated)
        assert any("content differs" in x for d in r["users"][SRC_USER]["drive"]["differences"] for x in d["diffs"])

    def test_a_native_document_whose_text_changed(self, migrated):
        tgt, f = self._drive_target(migrated, "Plan")
        tgt.exports[f["id"]] = b"a different plan"
        r = verify(migrated)
        assert any("exported" in x for d in r["users"][SRC_USER]["drive"]["differences"] for x in d["diffs"])

    def test_a_file_that_is_gone(self, migrated):
        tgt, f = self._drive_target(migrated, "notes.txt")
        del tgt.store[f["id"]]
        r = verify(migrated)
        assert r["verdict"] == "DIFFERENCES" and r["users"][SRC_USER]["drive"]["missing"][0]["name"] == "notes.txt"

    def test_a_file_in_the_trash_counts_as_missing(self, migrated):
        tgt, f = self._drive_target(migrated, "notes.txt")
        f["trashed"] = True
        assert verify(migrated)["users"][SRC_USER]["drive"]["missing"][0]["why"] == "in the target's trash"

    def test_a_file_copied_twice(self, migrated):
        tgt, f = self._drive_target(migrated, "notes.txt")
        dup = dict(f, id="dup1")
        tgt.store["dup1"] = dup
        tgt.content["dup1"] = tgt.content[f["id"]]
        r = verify(migrated)
        assert r["verdict"] == "DIFFERENCES" and r["users"][SRC_USER]["drive"]["duplicates"][0]["name"] == "notes.txt"

    def test_a_file_in_the_wrong_folder(self, migrated):
        tgt, f = self._drive_target(migrated, "report.pdf")
        f["parents"] = [tgt.root_id]
        r = verify(migrated)
        assert any("folder path" in x for d in r["users"][SRC_USER]["drive"]["differences"] for x in d["diffs"])

    def test_a_stray_file_is_reported_but_is_not_a_failure(self, migrated):
        tgt = migrated.auth.target_drive(TGT_USER)
        tgt.add_binary("welcome.txt", data=b"hello", mime="text/plain")
        r = verify(migrated)
        assert r["users"][SRC_USER]["drive"]["extras"][0]["name"] == "welcome.txt" and r["verdict"] == "IDENTICAL"

    def test_a_message_whose_body_changed(self, migrated):
        tg = migrated.auth.target_gmail(TGT_USER)
        mid = next(iter(tg.messages))
        raw = base64.urlsafe_b64decode(tg.messages[mid]["raw"])
        tg.messages[mid]["raw"] = base64.urlsafe_b64encode(raw.replace(b"body", b"BODY")).decode()
        r = verify(migrated)
        assert r["verdict"] == "DIFFERENCES" and r["users"][SRC_USER]["gmail"]["differences"]

    def test_a_message_that_lost_its_unread_state(self, migrated):
        tg = migrated.auth.target_gmail(TGT_USER)
        for m in tg.messages.values():
            m["labelIds"] = [l for l in m["labelIds"] if l != "UNREAD"]
        r = verify(migrated)
        assert any("unread" in x for d in r["users"][SRC_USER]["gmail"]["differences"] for x in d["diffs"])

    def test_a_message_that_is_gone(self, migrated):
        tg = migrated.auth.target_gmail(TGT_USER)
        del tg.messages[next(iter(tg.messages))]
        assert len(verify(migrated)["users"][SRC_USER]["gmail"]["missing"]) == 1

    def test_a_message_delivered_twice(self, migrated):
        tg = migrated.auth.target_gmail(TGT_USER)
        first = next(iter(tg.messages.values()))
        tg.messages["twin"] = dict(first, id="twin")
        r = verify(migrated)
        assert r["users"][SRC_USER]["gmail"]["duplicates"] and r["verdict"] == "DIFFERENCES"

    def test_a_contact_that_is_gone(self, migrated):
        migrated.auth.target_people(TGT_USER).contacts.clear()
        r = verify(migrated)
        assert r["users"][SRC_USER]["contacts"]["missing"] and r["verdict"] == "DIFFERENCES"

    def test_a_task_whose_status_changed(self, migrated):
        for tasks in migrated.auth.target_tasks(TGT_USER).task_store.values():
            for t in tasks:
                t["status"] = "completed"
        r = verify(migrated, ("tasks",))
        assert r["users"][SRC_USER]["tasks"]["differences"]

    def test_an_item_the_engine_failed_to_copy_is_reported_not_forgotten(self, migrated):
        migrated.db.log_audit(SRC_USER, "srcfile9", "file", "FAILED", "storageQuotaExceeded")
        r = verify(migrated)
        assert r["users"][SRC_USER]["drive"]["notCopied"][0]["error"] == "storageQuotaExceeded" and r["verdict"] == "DIFFERENCES"

    def test_a_source_that_cannot_be_read_makes_it_incomplete_not_a_pass(self, migrated, monkeypatch):
        src = migrated.auth.source_drive(SRC_USER)
        real = src.files

        def broken():
            raise RuntimeError("source unreachable")
        monkeypatch.setattr(src, "files", broken)
        r = verify(migrated, ("drive",))
        assert r["verdict"] == "INCOMPLETE" and r["users"][SRC_USER]["drive"]["errors"]


class TestALinkRewriteIsRecognisedAsIntended:
    def test_a_message_whose_drive_link_was_repointed_verifies_against_the_same_rewrite(self, auth, db, settings, identity):
        settings.rewrite_drive_links = True
        SRC_FILE, TGT_FILE = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345", "1ZyXwVuTsRqPoNmLkJiHgFeDcBa987654"
        db.record_mapping(SRC_USER, SRC_FILE, TGT_FILE, "file")
        raw = (b"Message-ID: <l@tenanta.com>\r\nFrom: bob@tenanta.com\r\nTo: alice@tenanta.com\r\n"
               b"Date: Mon, 3 Jun 2019 10:00:00 +0000\r\nSubject: deck\r\n\r\n"
               + f"see https://drive.google.com/file/d/{SRC_FILE}/view\r\n".encode())
        auth.source_gmail(SRC_USER).add_message(raw, ["INBOX"])
        gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
        r = V.run(auth, db, settings, [SRC_USER], ("gmail",), progress=lambda *_: None)
        g = r["users"][SRC_USER]["gmail"]
        assert r["verdict"] == "IDENTICAL" and any("identical after rewrite" in n for n in g["notes"])

    def test_but_a_message_that_was_rewritten_wrongly_is_not(self, auth, db, settings, identity):
        settings.rewrite_drive_links = True
        SRC_FILE, TGT_FILE = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345", "1ZyXwVuTsRqPoNmLkJiHgFeDcBa987654"
        db.record_mapping(SRC_USER, SRC_FILE, TGT_FILE, "file")
        raw = (b"Message-ID: <l@tenanta.com>\r\nFrom: bob@tenanta.com\r\nTo: alice@tenanta.com\r\n"
               b"Date: Mon, 3 Jun 2019 10:00:00 +0000\r\nSubject: deck\r\n\r\n"
               + f"see https://drive.google.com/file/d/{SRC_FILE}/view\r\n".encode())
        auth.source_gmail(SRC_USER).add_message(raw, ["INBOX"])
        gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
        tg = auth.target_gmail(TGT_USER)
        m = next(iter(tg.messages.values()))
        m["raw"] = base64.urlsafe_b64encode(base64.urlsafe_b64decode(m["raw"]).replace(TGT_FILE.encode(), b"WRONGID" + TGT_FILE[7:].encode())).decode()
        assert V.run(auth, db, settings, [SRC_USER], ("gmail",), progress=lambda *_: None)["verdict"] == "DIFFERENCES"


class TestACopyThatDidNothingIsNotVerified:
    """Found on the first live run: every service failed for every user, nothing was
    copied, and the verifier would have said IDENTICAL, 0 of 0."""

    def test_an_empty_migration_is_incomplete_not_identical(self, auth, db, settings, identity):
        r = V.run(auth, db, settings, [SRC_USER], V.ALL_SERVICES, progress=lambda *_: None)
        assert r["totals"]["checked"] == 0 and r["verdict"] == "INCOMPLETE"
        assert "nothing was copied" in " ".join(r["reasons"])

    def test_each_empty_service_says_so(self, auth, db, settings, identity):
        r = V.run(auth, db, settings, [SRC_USER], ("drive",), progress=lambda *_: None)
        assert any("nothing was copied for this service" in n for n in r["users"][SRC_USER]["drive"]["notes"])

    @pytest.mark.parametrize("svc", V.ALL_SERVICES)
    def test_a_service_that_failed_outright_is_reported_as_not_copied(self, auth, db, settings, identity, svc):
        """migrate_user records it under the service's own name."""
        db.log_audit(SRC_USER, SRC_USER, svc, "FAILED", "unauthorized_client: not yet usable")
        r = V.run(auth, db, settings, [SRC_USER], (svc,), progress=lambda *_: None)
        got = r["users"][SRC_USER][svc]["notCopied"]
        assert got and "unauthorized_client" in got[0]["error"]
        assert r["verdict"] == "DIFFERENCES"

    def test_one_service_empty_among_others_that_verified_does_not_spoil_them(self, migrated):
        r = verify(migrated)
        assert r["verdict"] == "IDENTICAL"


class TestTheReport:
    def test_it_is_saved_and_the_latest_can_be_read_back(self, migrated, tmp_path):
        r = verify(migrated)
        jp, mp = V.save(r, base=str(tmp_path))
        assert V.latest(None, base=str(tmp_path))["verdict"] == "IDENTICAL"
        md = open(mp, encoding="utf-8").read()
        assert md.startswith("# One-to-one verification: IDENTICAL") and "What was opened" in md and "report.pdf" in md

    def test_a_failing_verdict_is_what_the_report_says_first(self, migrated, tmp_path):
        tgt = migrated.auth.target_drive(TGT_USER)
        del tgt.store[next(f["id"] for f in tgt.store.values() if f.get("name") == "notes.txt")]
        jp, mp = V.save(verify(migrated), base=str(tmp_path))
        md = open(mp, encoding="utf-8").read()
        assert md.startswith("# One-to-one verification: DIFFERENCES") and "Why not IDENTICAL" in md

    def test_no_report_yet_reads_as_none(self, tmp_path):
        assert V.latest(None, base=str(tmp_path)) is None


@pytest.fixture
def shared(auth, db, settings, identity, quota):
    """One file shared four ways: with a colleague who exists on the target, with the
    whole company, with an outsider who has no Google account, and with a colleague
    nobody mapped."""
    from db import bulk_seed_identities
    bulk_seed_identities(db, [("bob@tenanta.com", "bob@tenantb.com")])
    settings.rewrite_drive_links = False
    sd = auth.source_drive(SRC_USER)
    fid = sd.add_binary("deck.pdf", data=b"deck")
    sd.add_permission(fid, "user", "writer", email="bob@tenanta.com")
    sd.add_permission(fid, "domain", "reader", domain="tenanta.com")
    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
    tid = next(f["id"] for f in auth.target_drive(TGT_USER).store.values() if f.get("name") == "deck.pdf")
    return type("S", (), {"auth": auth, "db": db, "settings": settings, "fid": fid, "tid": tid})


class TestCompareGrants:
    T = staticmethod(lambda e: e.replace("tenanta", "tenantb"))

    def _g(self, s, t, skipped=(), sid="f1"):
        return V.compare_grants(s, t, self.T, "tenanta.com", "tenantb.com", set(skipped), sid)

    def test_the_same_people_with_the_same_roles(self):
        g = self._g([{"type": "user", "role": "writer", "emailAddress": "bob@tenanta.com"}],
                    [{"type": "user", "role": "writer", "emailAddress": "bob@tenantb.com"}])
        assert g["matched"] == 1 and not g["missing"] and not g["extra"]

    def test_the_owner_is_not_a_grant(self):
        assert self._g([{"type": "user", "role": "owner", "emailAddress": "a@tenanta.com"}], [])["matched"] == 0

    def test_a_domain_grant_follows_the_tenant(self):
        g = self._g([{"type": "domain", "role": "reader", "domain": "tenanta.com"}],
                    [{"type": "domain", "role": "reader", "domain": "tenantb.com"}])
        assert g["matched"] == 1 and not g["missing"]

    def test_a_weaker_role_is_missing_and_a_stronger_one_is_extra(self):
        g = self._g([{"type": "user", "role": "writer", "emailAddress": "bob@tenanta.com"}],
                    [{"type": "user", "role": "reader", "emailAddress": "bob@tenantb.com"}])
        assert g["missing"] == [["user", "bob@tenantb.com", "writer"]] and g["extra"] == [["user", "bob@tenantb.com", "reader"]]

    def test_access_nobody_granted_is_extra(self):
        g = self._g([], [{"type": "anyone", "role": "reader"}])
        assert g["extra"] == [["anyone", "", "reader"]]

    def test_a_grant_the_migration_recorded_as_not_reproducible_is_explained_not_missing(self):
        g = self._g([{"type": "user", "role": "reader", "emailAddress": "x@outside.com"}], [], skipped={"f1:x@outside.com"})
        assert g["notReproduced"] == [["user", "x@outside.com", "reader"]] and g["missing"] == []

    def test_the_same_grant_with_no_such_record_is_missing(self):
        g = self._g([{"type": "user", "role": "reader", "emailAddress": "x@outside.com"}], [])
        assert g["missing"] == [["user", "x@outside.com", "reader"]] and g["notReproduced"] == []

    def test_a_deleted_grant_does_not_count(self):
        assert self._g([{"type": "user", "role": "reader", "emailAddress": "x@y.com", "deleted": True}], [])["missing"] == []


class TestSharingIsVerifiedOnRealMigratedFiles:
    def test_the_engine_reproduces_the_sharing_and_the_verifier_sees_it(self, shared):
        r = V.run(shared.auth, shared.db, shared.settings, [SRC_USER], ("drive",), progress=lambda *_: None)
        d = r["users"][SRC_USER]["drive"]
        assert r["verdict"] == "IDENTICAL", r["reasons"]
        assert d["sharing"]["matched"] == 2 and any("all 2 grants matched" in n for n in d["notes"])

    def test_a_grant_that_was_lost_is_a_difference(self, shared):
        shared.auth.target_drive(TGT_USER).perms[shared.tid] = [
            p for p in shared.auth.target_drive(TGT_USER).perms[shared.tid] if p["type"] != "user"]
        r = V.run(shared.auth, shared.db, shared.settings, [SRC_USER], ("drive",), progress=lambda *_: None)
        assert r["verdict"] == "DIFFERENCES"
        assert any("grant missing on the target" in x for d in r["users"][SRC_USER]["drive"]["differences"] for x in d["diffs"])

    def test_access_that_appeared_from_nowhere_is_a_difference(self, shared):
        shared.auth.target_drive(TGT_USER).add_permission(shared.tid, "anyone", "reader")
        r = V.run(shared.auth, shared.db, shared.settings, [SRC_USER], ("drive",), progress=lambda *_: None)
        assert r["verdict"] == "DIFFERENCES"
        assert any("extra grant" in x for d in r["users"][SRC_USER]["drive"]["differences"] for x in d["diffs"])

    def test_sharing_that_could_not_be_read_makes_it_incomplete(self, shared, monkeypatch):
        monkeypatch.setattr(V.Verifier, "_perms", lambda self, svc, fid: (_ for _ in ()).throw(RuntimeError("403")))
        r = V.run(shared.auth, shared.db, shared.settings, [SRC_USER], ("drive",), progress=lambda *_: None)
        assert r["verdict"] == "INCOMPLETE"


class TestFalsePositivesFoundOnTheFirstLiveVerification:
    """The first real run said DIFFERENCES for things that were the migration working:
    a Drive link in an event repointed on purpose, drafts read as strays, Google's own
    welcome mail read as strays, and a failure a later run had already overtaken."""

    SRC_ID, TGT_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345", "1ZyXwVuTsRqPoNmLkJiHgFeDcBa987654"
    ident = staticmethod(lambda a: a)

    def _event(self, text):
        return {"summary": "S", "description": text, "start": {"dateTime": "2024-01-01T10:00:00Z"},
                "end": {"dateTime": "2024-01-01T11:00:00Z"}}

    def test_an_event_link_repointed_at_the_copy_is_no_difference(self):
        notes = []
        src = self._event(f"Agenda: https://drive.google.com/drive/folders/{self.SRC_ID}")
        tgt = self._event(f"Agenda: https://drive.google.com/drive/folders/{self.TGT_ID}")
        lookup = {self.SRC_ID: self.TGT_ID}.get
        assert V.compare_event(src, tgt, self.ident, lookup, notes) == []
        assert any("repointed" in n for n in notes)

    def test_but_a_link_pointing_anywhere_else_still_is(self):
        src = self._event(f"Agenda: https://drive.google.com/drive/folders/{self.SRC_ID}")
        tgt = self._event("Agenda: https://drive.google.com/drive/folders/1WrongWrongWrongWrongWrong12345")
        assert any("description" in d for d in V.compare_event(src, tgt, self.ident, {self.SRC_ID: self.TGT_ID}.get))

    def test_without_a_lookup_the_text_must_match_exactly(self):
        src = self._event(f"see {self.SRC_ID}")
        assert V.compare_event(src, self._event(f"see {self.TGT_ID}"), self.ident)

    def test_a_service_failure_overtaken_by_a_later_success_is_not_reported(self, auth, db, settings, identity):
        db.log_audit(SRC_USER, SRC_USER, "drive", "FAILED", "unauthorized_client: not yet usable")
        db.conn.execute("UPDATE audit_log SET timestamp='2026-01-01T00:00:00Z' WHERE item_type='drive'")
        db.log_audit(SRC_USER, "f1", "file", "SUCCESS")
        db.conn.execute("UPDATE audit_log SET timestamp='2026-01-01T00:05:00Z' WHERE item_id='f1'")
        db.conn.commit()
        v = V.Verifier(auth, db, settings, SRC_USER, TGT_USER)
        assert v._failed(("file", "folder", "drive")) == []

    def test_a_service_failure_nothing_overtook_still_is(self, auth, db, settings, identity):
        db.log_audit(SRC_USER, SRC_USER, "drive", "FAILED", "unauthorized_client: not yet usable")
        db.log_audit(SRC_USER, "m1", "message", "SUCCESS")       # a DIFFERENT service's success
        db.conn.commit()
        v = V.Verifier(auth, db, settings, SRC_USER, TGT_USER)
        assert len(v._failed(("file", "folder", "drive"))) == 1

    def test_drafts_are_compared_and_are_not_strays(self, auth, db, settings, identity):
        raw = MSG % (7, 7, 7)
        auth.source_gmail(SRC_USER).add_draft(raw)
        gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
        tg = auth.target_gmail(TGT_USER)
        # Gmail lists a draft's message among the mailbox's messages
        did = next(iter(tg.drafts))
        tg.messages[did] = {"id": did, "raw": tg.drafts[did]["message"]["raw"], "labelIds": ["DRAFT"]}
        g = V.run(auth, db, settings, [SRC_USER], ("gmail",), progress=lambda *_: None)["users"][SRC_USER]["gmail"]
        assert g["checked"] == 1 and g["identical"] == 1 and g["extras"] == []

    def test_an_altered_draft_is_found(self, auth, db, settings, identity):
        auth.source_gmail(SRC_USER).add_draft(MSG % (7, 7, 7))
        gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
        tg = auth.target_gmail(TGT_USER)
        d = next(iter(tg.drafts.values()))["message"]
        d["raw"] = base64.urlsafe_b64encode(base64.urlsafe_b64decode(d["raw"]).replace(b"body 7", b"body X")).decode()
        r = V.run(auth, db, settings, [SRC_USER], ("gmail",), progress=lambda *_: None)
        assert r["verdict"] == "DIFFERENCES"

    def test_a_draft_missing_from_the_target_is_found(self, auth, db, settings, identity):
        auth.source_gmail(SRC_USER).add_draft(MSG % (7, 7, 7))
        gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
        auth.target_gmail(TGT_USER).drafts.clear()
        r = V.run(auth, db, settings, [SRC_USER], ("gmail",), progress=lambda *_: None)
        assert r["users"][SRC_USER]["gmail"]["missing"] and r["verdict"] == "DIFFERENCES"

    def test_googles_own_welcome_mail_is_said_and_not_counted_as_a_stray(self, auth, db, settings, identity):
        auth.source_gmail(SRC_USER).add_message(MSG % (1, 1, 1), ["INBOX"])
        gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
        auth.target_gmail(TGT_USER).add_message(
            b"Message-ID: <w@mail.gmail.com>\r\nFrom: Gmail Team <mail-noreply@google.com>\r\n"
            b"To: t@x\r\nSubject: Tips for using your new inbox\r\n\r\nhi\r\n", ["INBOX"])
        g = V.run(auth, db, settings, [SRC_USER], ("gmail",), progress=lambda *_: None)["users"][SRC_USER]["gmail"]
        assert g["extras"] == [] and any("welcome mail" in n for n in g["notes"])

    def test_any_other_stray_is_still_a_stray(self, auth, db, settings, identity):
        auth.source_gmail(SRC_USER).add_message(MSG % (1, 1, 1), ["INBOX"])
        gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
        auth.target_gmail(TGT_USER).add_message(
            b"Message-ID: <s@elsewhere>\r\nFrom: eve@example.com\r\nTo: t@x\r\nSubject: hi\r\n\r\nhi\r\n", ["INBOX"])
        g = V.run(auth, db, settings, [SRC_USER], ("gmail",), progress=lambda *_: None)["users"][SRC_USER]["gmail"]
        assert len(g["extras"]) == 1


class TestDraftsAreComparedOnWhatAPersonWrote:
    """Found by opening a real migrated draft: Gmail gives it a new Message-Id, a Date of now and
    a normalised From, so a byte comparison can never pass -- and a comparison that ignores the
    whole draft would pass a draft that lost its recipient."""
    SRC = (b"Message-Id: <a@mail.gmail.com>\r\nFrom: tom@tenanta.com\r\nTo: bob@tenanta.com\r\n"
           b"Date: Fri, 18 Sep 2026 08:56:11 -0700\r\nSubject: Half-finished\r\nMIME-Version: 1.0\r\n"
           b"Content-Type: text/plain; charset=UTF-8\r\n\r\nStill drafting this.\r\n")
    tr = staticmethod(lambda a: a.replace("tenanta", "tenantb"))

    def _tgt(self, old=b"", new=b""):
        raw = (self.SRC.replace(b"<a@mail.gmail.com>", b"<zzz@mail.gmail.com>")
               .replace(b"From: tom@tenanta.com", b"From: Tom User <tom@tenantb.com>")
               .replace(b"Fri, 18 Sep 2026 08:56:11 -0700", b"Sat, 26 Sep 2026 07:43:38 -0700"))
        return raw.replace(old, new) if old else raw

    def test_a_regenerated_message_id_date_and_from_is_the_same_draft(self):
        assert V.compare_draft(self.SRC, self._tgt(), self.tr) == ("equivalent", [])

    @pytest.mark.parametrize("old,new,needle", [
        (b"To: bob@tenanta.com", b"To: eve@tenanta.com", "to"),
        (b"Subject: Half-finished", b"Subject: Something else", "subject"),
        (b"Still drafting this.", b"Still drafting THAT.", "body"),
        (b"Still drafting this.", b"", "body"),
    ])
    def test_a_draft_that_lost_what_was_written_is_different(self, old, new, needle):
        verdict, diffs = V.compare_draft(self.SRC, self._tgt(old, new), self.tr)
        assert verdict == "different" and any(needle in d for d in diffs)

    def test_a_draft_from_the_wrong_mailbox_is_different(self):
        verdict, diffs = V.compare_draft(self.SRC, self._tgt(b"tom@tenantb.com", b"eve@tenantb.com"), self.tr)
        assert verdict == "different" and any(d.startswith("from") for d in diffs)

    def test_an_encoded_subject_equals_the_same_subject_written_out(self):
        src = self.SRC.replace(b"Subject: Half-finished", "Subject: Reply \u2014 review".encode("utf-8"))
        tgt = self._tgt().replace(b"Subject: Half-finished", b"Subject: =?UTF-8?B?UmVwbHkg4oCUIHJldmlldw==?=")
        assert V.compare_draft(src, tgt, self.tr)[0] == "equivalent"


class TestTheCommandLineCanReadWhatItVerifies:
    """Found re-verifying a live run by hand: the credential's scopes follow the migrate_* flags, main.py
    switches them on for the services a run names, and the standalone command did not -- so it could not
    read a single contact or task (403 insufficient scopes) and said so as errors."""

    def _scopes_for(self, monkeypatch, *argv):
        import auth as auth_mod
        import config
        import db as db_mod
        seen = {}
        monkeypatch.setattr(auth_mod, "AuthManager", lambda settings: object())
        monkeypatch.setattr(db_mod, "MigrationDB", lambda path: object())
        monkeypatch.setattr(V, "run_and_save", lambda a, d, settings, users, services, **kw: (
            seen.update(settings=settings) or {"verdict": "IDENTICAL"}))
        assert V.main(list(argv)) == 0
        return config.source_scopes(seen["settings"])

    def test_contacts_and_tasks_are_readable_when_asked_for(self, monkeypatch):
        import config
        scopes = self._scopes_for(monkeypatch, "--services", "contacts,tasks")
        assert config.CONTACTS_READONLY_SCOPE in scopes and config.TASKS_READONLY_SCOPE in scopes

    def test_and_are_not_requested_when_they_are_not(self, monkeypatch):
        import config
        scopes = self._scopes_for(monkeypatch, "--services", "drive")
        assert config.CONTACTS_READONLY_SCOPE not in scopes and config.TASKS_READONLY_SCOPE not in scopes
