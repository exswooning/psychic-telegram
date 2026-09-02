"""
tests/test_link_survival.py
===========================
The two halves of "the link still works after the source tenant is deleted".

A Drive URL names a file by id and nothing else, and files.copy mints a new
id, so a migrated email's links point at files that are about to be deleted.
link_rewrite fixes the copies we own; external_shares reports the ones we
never can. The expiry test is here rather than with the other ACL tests
because it is the same class of bug -- access that silently changes shape in
the move -- and it changes it in the dangerous direction.
"""

from __future__ import annotations

import base64
import email

import pytest

from db import MigrationDB
from link_rewrite import rewrite_raw
import external_shares

SRC = "1" + "A" * 32
TGT = "9" + "Z" * 32


def wrap(body: bytes, cte: str = "7bit", ctype: str = "text/plain") -> str:
    raw = (b"From: a@old.test\r\nTo: b@old.test\r\nMessage-ID: <keep@me>\r\n"
           b"Subject: s\r\nMIME-Version: 1.0\r\n"
           + f"Content-Type: {ctype}; charset=utf-8\r\n".encode()
           + f"Content-Transfer-Encoding: {cte}\r\n\r\n".encode() + body)
    return base64.urlsafe_b64encode(raw).decode()


def body_of(raw: str) -> bytes:
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw + "==="))
    return next(p for p in msg.walk() if not p.is_multipart()).get_payload(decode=True)


class TestRewritingMailWeOwn:
    def test_a_link_is_repointed_at_the_copy(self):
        raw, n = rewrite_raw(
            wrap(f"see https://docs.google.com/document/d/{SRC}/edit".encode()),
            {SRC: TGT}.get)
        assert n == 1
        assert TGT.encode() in body_of(raw)
        assert SRC.encode() not in body_of(raw)

    def test_a_message_with_nothing_to_rewrite_is_returned_untouched(self):
        """Most mail has no Drive link. It must not pay a re-encode, and more
        importantly must not risk one: re-serialising a message we had no
        reason to change is pure downside."""
        original = wrap(b"no links here at all")
        raw, n = rewrite_raw(original, {SRC: TGT}.get)
        assert n == 0
        assert raw is original

    def test_an_unknown_id_is_left_alone_not_mangled(self):
        """Links to files outside the migration -- another tenant, a drive we
        skipped -- have to survive as they are. A half-rewritten URL is worse
        than a dead one, because it looks live."""
        other = "7" + "Q" * 32
        raw, n = rewrite_raw(
            wrap(f"https://docs.google.com/document/d/{other}/edit".encode()),
            {SRC: TGT}.get)
        assert n == 0 and other.encode() in body_of(raw)

    @pytest.mark.parametrize("cte,body", [
        # The two cases a regex over the raw MIME cannot see. Quoted-printable
        # splits a long URL across a soft break, frequently mid-id...
        ("quoted-printable",
         b"https://docs.google.com/document/d/1AAAAAAAAAAAAAAA=\r\nAAAAAAAAAAAAAAAAA/edit"),
        # ...and a base64 part contains no readable URL whatsoever.
        ("base64",
         base64.encodebytes(f"https://docs.google.com/d/{SRC}/edit".encode())),
    ])
    def test_encoded_bodies_are_decoded_before_matching(self, cte, body):
        raw, n = rewrite_raw(wrap(body, cte), {SRC: TGT}.get)
        assert n == 1, f"{cte} body was not decoded before matching"
        assert TGT.encode() in body_of(raw)

    def test_the_message_id_survives(self):
        """The insert path keys its duplicate check on the RFC822 Message-ID.
        A rewrite that changed it would turn a resumed run into a mailbox
        full of doubles."""
        raw, _ = rewrite_raw(
            wrap(f"https://docs.google.com/document/d/{SRC}/edit".encode()),
            {SRC: TGT}.get)
        msg = email.message_from_bytes(base64.urlsafe_b64decode(raw + "==="))
        assert msg["Message-ID"] == "<keep@me>"

    def test_a_corrupt_message_is_passed_through_rather_than_lost(self):
        raw, n = rewrite_raw("!!!not base64!!!", {SRC: TGT}.get)
        assert n == 0 and raw == "!!!not base64!!!"


class TestReportingWhatWeCannotRewrite:
    @pytest.fixture
    def ledger(self, tmp_path):
        db = MigrationDB(str(tmp_path / "m.db"))
        db.init_schema()
        db.record_mapping("u@old.test", SRC, TGT, "file", source_name="Budget")
        for grantee in ("ext@partner.test", "colleague@old.test",
                        "moved@new.test", "anyone"):
            db.log_audit("u@old.test", f"{SRC}:{grantee}", "acl", "SUCCESS")
        return db

    @pytest.fixture
    def settings(self):
        class S:
            source_domain = "old.test"
            target_domain = "new.test"
        return S()

    def test_only_genuinely_external_people_are_listed(self, ledger, settings):
        """Both tenants' own domains are ours. Listing a colleague as an
        external collaborator would bury the handful of people who actually
        need emailing."""
        report = external_shares.collect(ledger, settings)
        assert [c["email"] for c in report["collaborators"]] == ["ext@partner.test"]

    def test_the_reported_url_points_at_the_target_copy(self, ledger, settings):
        report = external_shares.collect(ledger, settings)
        files = report["collaborators"][0]["files"]
        assert files == [{"name": "Budget",
                          "url": f"https://drive.google.com/open?id={TGT}"}]

    def test_public_links_are_counted_separately(self, ledger, settings):
        """An 'anyone with the link' grant has no one to notify, but the link
        still breaks -- so it is reported, not silently dropped."""
        assert external_shares.collect(ledger, settings)["public_link_grants"] == 1

    def test_a_grant_on_an_unmapped_file_is_flagged_not_guessed(self, ledger, settings):
        ledger.log_audit("u@old.test", "GHOSTID:ext@partner.test", "acl", "SUCCESS")
        report = external_shares.collect(ledger, settings)
        assert report["collaborators"][0]["unresolved"] == 1

    def test_external_shared_drive_members_are_included(self, ledger, settings):
        ledger.log_audit("admin@old.test", "ext@partner.test",
                         "shared_drive_member", "SUCCESS", "organizer on Finance")
        report = external_shares.collect(ledger, settings)
        assert report["collaborators"][0]["shared_drives"] == ["organizer on Finance"]


# ----------------------------------------------------------------------
# A timed share has to stay timed.
#
# expirationTime was simply not read from the source permission, so every
# share someone had deliberately time-boxed -- "the auditors get this until
# the 30th" -- was recreated on the target with no expiry at all. Nothing
# failed, nothing was logged, and the migration quietly granted more access
# than the source ever had. That is the one direction an access bug must
# never fail in.
# ----------------------------------------------------------------------
class TestATimedShareStaysTimed:
    @staticmethod
    def _grant(migrator, auth, perm):
        src, tgt = auth.source_drive("x"), auth.target_drive("y")
        src.perms["src-file"] = [dict(perm, id="p1")]
        migrator.src, migrator.tgt = src, tgt
        migrator._sync_acls("src-file", "tgt-file")
        return tgt.perms["tgt-file"]

    def test_a_future_expiry_is_carried_onto_the_new_grant(self, migrator, auth):
        from datetime import datetime, timedelta, timezone
        soon = (datetime.now(timezone.utc) + timedelta(days=30)
                ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        created = self._grant(migrator, auth, {
            "type": "user", "role": "reader",
            "emailAddress": "auditor@partner.test", "expirationTime": soon})
        assert created and created[0].get("expirationTime") == soon

    def test_an_unexpiring_grant_gains_no_expiry(self, migrator, auth):
        created = self._grant(migrator, auth, {
            "type": "user", "role": "writer", "emailAddress": "ext@partner.test"})
        assert created and "expirationTime" not in created[0]

    def test_an_expiry_outside_drives_window_is_dropped_not_sent(self, migrator, auth):
        """Drive rejects an expiry more than a year out, and rejecting it fails
        the whole permissions.create. Losing the time box is bad; losing the
        grant entirely is worse."""
        created = self._grant(migrator, auth, {
            "type": "user", "role": "reader", "emailAddress": "ext@partner.test",
            "expirationTime": "2099-01-01T00:00:00.000Z"})
        assert created, "the grant itself must still be created"
        assert "expirationTime" not in created[0]

    def test_a_domain_grant_carries_none(self, migrator, auth):
        """Drive only accepts an expiry on user and group grants; sending one
        on a domain grant is a 400."""
        created = self._grant(migrator, auth, {
            "type": "domain", "role": "reader", "domain": "partner.test",
            "expirationTime": "2030-01-01T00:00:00.000Z"})
        assert created and "expirationTime" not in created[0]


# ----------------------------------------------------------------------
# Mail must not migrate ahead of Drive while link rewriting is on.
#
# Not a tidiness rule. Rewriting resolves ids through id_mapping, so with no
# Drive rows every link silently stays pointed at the source -- and the run
# reports full success. Re-running does not repair it either: the dedup skips
# a message already inserted, so those links are wrong permanently. The flag
# has to fail loudly or it is worse than being off.
# ----------------------------------------------------------------------
class TestMailWillNotRunAheadOfDrive:
    def test_it_refuses_before_inserting_anything(self, gmail_migrator, settings):
        settings.rewrite_drive_links = True
        with pytest.raises(RuntimeError, match="no Drive files have migrated"):
            gmail_migrator.run()

    def test_it_runs_once_drive_has_migrated(self, gmail_migrator, settings, db):
        settings.rewrite_drive_links = True
        db.record_mapping("alice@tenanta.com", "f1", "t1", "file")
        gmail_migrator.run()          # must not raise

    def test_the_guard_is_off_when_the_feature_is(self, gmail_migrator, settings):
        """An empty ledger is the normal state for a mail-only migration. The
        guard must not break every run that never asked for rewriting."""
        settings.rewrite_drive_links = False
        gmail_migrator.run()


class TestBothFindingsAreActionableNotJustCounted:
    @pytest.fixture
    def ledger(self, tmp_path):
        db = MigrationDB(str(tmp_path / "m.db"))
        db.init_schema()
        db.record_mapping("u@old.test", SRC, TGT, "file", source_name="Budget")
        db.log_audit("u@old.test", f"{SRC}:anyone", "acl", "SUCCESS")
        db.log_audit("u@old.test", f"{SRC}:ext@partner.test", "acl", "SUCCESS")
        # granted, but nothing says where the file went
        db.log_audit("u@old.test", "ORPHAN1:ext@partner.test", "acl", "SUCCESS")
        return db

    @pytest.fixture
    def settings(self):
        class S:
            source_domain = "old.test"
            target_domain = "new.test"
        return S()

    def test_public_links_are_named_with_their_new_url(self, ledger, settings):
        """A public link has nobody to notify, so the only remedy is knowing
        which file it was and where it went."""
        r = external_shares.collect(ledger, settings)
        assert r["public_links"] == [
            {"name": "Budget", "url": f"https://drive.google.com/open?id={TGT}"}]

    def test_unmapped_grants_name_the_file_not_just_a_count(self, ledger, settings):
        c = external_shares.collect(ledger, settings)["collaborators"][0]
        assert c["unresolved"] == 1
        assert c["unresolved_source_ids"] == ["ORPHAN1"]

    def test_the_rendered_report_says_what_to_do_about_them(self, ledger, settings):
        text = external_shares.render(external_shares.collect(ledger, settings))
        assert "ORPHAN1" in text
        assert "no address to notify" in text
