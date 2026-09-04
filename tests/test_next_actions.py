"""
tests/test_next_actions.py
==========================
"What should I do now?" -- the question forty-three buttons never answered.

Rules, not a score, and the ordering is the design: a tenant with no
identity map does not need to hear about link rot. Each check reads the
ledger, states what it found, and names the action that addresses it.

The one thing this must never do is stay quiet when something is wrong,
because an empty panel reads as "all clear".
"""

from __future__ import annotations

import pytest

from db import MigrationDB
from next_actions import assess, BLOCKED, TODO, WARN, OK


class _S:
    source_domain = "old.test"
    target_domain = "new.test"
    rewrite_drive_links = False


@pytest.fixture
def db(tmp_path):
    d = MigrationDB(str(tmp_path / "m.db"))
    d.init_schema()
    return d


def _user(db, src, status="DONE"):
    with db.write() as c:
        c.execute("INSERT OR REPLACE INTO identity_map "
                  "(source_email,target_email,entity_type,status) VALUES (?,?,?,?)",
                  (src, src.replace("old.test", "new.test"), "user", status))


def titles(db):
    return [i["title"] for i in assess(db, _S())]


class TestItBlocksOnWhatBlocks:
    def test_no_identity_map_is_the_only_thing_said(self, db):
        """A tenant with nothing mapped does not need six other findings."""
        out = assess(db, _S())
        assert len(out) == 1
        assert out[0]["level"] == BLOCKED and out[0]["action"] == "init_db_auto"

    def test_pending_users_are_named(self, db):
        _user(db, "a@old.test", "PENDING")
        _user(db, "b@old.test", "DONE")
        assert any("1 of 2 users have never run" in t for t in titles(db))

    def test_a_running_migration_is_reported_as_ok_not_as_work(self, db):
        _user(db, "a@old.test", "RUNNING")
        out = assess(db, _S())
        running = [i for i in out if "running now" in i["title"]]
        assert running and running[0]["level"] == OK


class TestFailuresAreSeparatedByWhetherAnythingCanBeDone:
    def test_orphaned_failures_are_not_presented_as_work(self, db):
        """29 unfixable rows are how the 1 real one hides."""
        _user(db, "a@old.test")
        db.log_audit("gone@old.test", "x", "acl", "FAILED", "old generation")
        out = assess(db, _S())
        orph = [i for i in out if "no longer mapped" in i["title"]]
        assert orph and orph[0]["level"] == OK and orph[0]["action"] is None

    def test_failures_on_current_users_are_actionable(self, db):
        _user(db, "a@old.test")
        db.log_audit("a@old.test", "x", "acl", "FAILED", "429")
        out = assess(db, _S())
        act = [i for i in out if "on current users" in i["title"]]
        assert act and act[0]["level"] == WARN and act[0]["action"] == "resolve_dry"

    def test_the_two_are_counted_separately(self, db):
        _user(db, "a@old.test")
        db.log_audit("a@old.test", "x", "acl", "FAILED", "429")
        for i in range(3):
            db.log_audit("gone@old.test", f"y{i}", "acl", "FAILED", "old")
        joined = " ".join(titles(db))
        assert "1 failure(s) on current users" in joined
        assert "3 failure(s) belong to users no longer mapped" in joined


class TestServiceOrdering:
    def test_drive_done_and_mail_not_is_surfaced(self, db):
        _user(db, "a@old.test")
        db.record_mapping("a@old.test", "f1", "t1", "file")
        assert any("mail has not" in t for t in titles(db))

    def test_it_is_silent_once_mail_has_landed(self, db):
        _user(db, "a@old.test")
        db.record_mapping("a@old.test", "f1", "t1", "file")
        db.record_mapping("a@old.test", "m1", "t2", "message")
        assert not any("mail has not" in t for t in titles(db))

    def test_it_reads_id_mapping_not_audit_log(self, db):
        """audit_log outlives a target wipe, so counting it would report
        Drive as done for files that are no longer there."""
        _user(db, "a@old.test")
        db.log_audit("a@old.test", "f1", "file", "SUCCESS")
        assert not any("mail has not" in t for t in titles(db))


class TestLinkRot:
    def test_migrated_mail_with_nothing_rewritten_is_flagged(self, db):
        _user(db, "a@old.test")
        db.record_mapping("a@old.test", "m1", "t1", "message")
        out = [i for i in assess(db, _S()) if "point at the source" in i["title"]]
        assert out and out[0]["level"] == WARN

    def test_it_is_silent_once_something_has_rewritten(self, db):
        _user(db, "a@old.test")
        db.record_mapping("a@old.test", "m1", "t1", "message")
        db.log_audit("a@old.test", "m1", "link_rewrite", "SUCCESS", "2 links")
        assert not any("point at the source" in t for t in titles(db))


class TestExternalCollaborators:
    def test_outside_addresses_are_counted_once(self, db):
        _user(db, "a@old.test")
        db.log_audit("a@old.test", "f1:ext@partner.test", "acl", "SUCCESS")
        db.log_audit("a@old.test", "f2:ext@partner.test", "acl", "SUCCESS")
        out = [i for i in assess(db, _S()) if "external collaborator" in i["title"]]
        assert out and "1 external" in out[0]["title"]

    def test_colleagues_on_either_domain_are_not_external(self, db):
        _user(db, "a@old.test")
        db.log_audit("a@old.test", "f1:mate@old.test", "acl", "SUCCESS")
        db.log_audit("a@old.test", "f2:mate@new.test", "acl", "SUCCESS")
        assert not any("external collaborator" in t for t in titles(db))


class TestTheAllClearIsEarned:
    def test_a_clean_tenant_says_so(self, db):
        _user(db, "a@old.test")
        db.record_mapping("a@old.test", "f1", "t1", "file")
        db.record_mapping("a@old.test", "m1", "t2", "message")
        db.log_audit("a@old.test", "m1", "link_rewrite", "SUCCESS", "1 link")
        out = assess(db, _S())
        assert out[-1]["title"] == "Nothing outstanding"

    def test_it_is_withheld_while_anything_is_outstanding(self, db):
        _user(db, "a@old.test", "PENDING")
        assert "Nothing outstanding" not in titles(db)


class TestFailureTriage:
    """"32 failures" and "one worth retrying, thirty-one that cannot move"
    are different instructions, and only the second is actionable. On the
    live tenant 29 of 32 are organizer-role grants that can never succeed on
    a My Drive file -- re-running them forever changes nothing."""

    def test_known_permanent_failures_offer_no_retry(self, db):
        _user(db, "a@old.test")
        for i in range(3):
            db.log_audit("a@old.test", f"f{i}:x@y.test", "acl", "FAILED",
                         "HTTP 403 (organizerOnNonTeamDriveNotSupported): "
                         "Organizer role is only valid for shared drives.")
        item = [i for i in assess(db, _S()) if "on current users" in i["title"]][0]
        assert item["action"] is None
        assert "cannot move" in item["detail"]
        assert item["level"] == OK

    def test_an_unclassified_failure_still_offers_the_retry(self, db):
        """Unclassified means nobody has looked, not that it is hopeless --
        withholding the retry would assert something unchecked."""
        _user(db, "a@old.test")
        db.log_audit("a@old.test", "f1:x@y.test", "acl", "FAILED", "something new")
        item = [i for i in assess(db, _S()) if "on current users" in i["title"]][0]
        assert item["action"] == "resolve_dry" and item["level"] == WARN

    def test_the_family_is_named(self, db):
        _user(db, "a@old.test")
        db.log_audit("a@old.test", "f1:x@y.test", "acl", "FAILED",
                     "organizerOnNonTeamDriveNotSupported")
        item = [i for i in assess(db, _S()) if "on current users" in i["title"]][0]
        assert "acl organizer role" in item["detail"]

    def test_a_retryable_family_is_counted_as_such(self, db):
        _user(db, "a@old.test")
        db.log_audit("a@old.test", "f1:x@y.test", "acl", "FAILED",
                     "Quota exceeded for quota metric")
        item = [i for i in assess(db, _S()) if "on current users" in i["title"]][0]
        assert "1 worth retrying" in item["detail"]
        assert item["action"] == "resolve_dry"
