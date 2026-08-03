"""
tests/test_shared_drives.py
===========================
Shared Drives, the gap that lets a "fully reconciled" migration miss an org's
largest body of data.

A shared drive's files are owned by the drive, not by any user, so they appear
in nobody's `'me' in owners` query. Every per-user engine can report 100% and
leave every shared drive untouched -- which is why the first test here is
about the query, not about the copy.
"""

from __future__ import annotations

import pytest

from tests.conftest import SRC_USER, TGT_USER


class TestOwnedOnlyDoesNotApply:
    def test_owned_only_is_dropped_inside_a_shared_drive(self, migrator):
        """`'me' in owners` matches nothing in a shared drive, so leaving the
        filter on would silently migrate an empty drive and call it done."""
        migrator.settings.owned_only = True
        migrator.shared_drive = "drv-1"

        list(migrator._list_children("drv-1"))

        q = migrator.src.calls_to("files.list")[0]["q"]
        assert "'me' in owners" not in q
        assert migrator.src.calls_to("files.list")[0]["driveId"] == "drv-1"
        assert migrator.src.calls_to("files.list")[0]["corpora"] == "drive"

    def test_owned_only_still_applies_to_my_drive(self, migrator):
        """The filter exists to stop a file shared with four colleagues being
        copied five times; shared drives must not disable it everywhere."""
        migrator.settings.owned_only = True

        list(migrator._list_children("root-source"))

        assert "'me' in owners" in migrator.src.calls_to("files.list")[0]["q"]


@pytest.fixture
def sd(auth, db, settings, identity):
    import shared_drives

    return shared_drives.SharedDriveMigrator(auth, db, settings, SRC_USER,
                                             TGT_USER)


class TestMembership:
    def test_an_organizer_is_restored_before_lesser_roles(self, sd):
        """A drive that arrives without an organizer cannot be administered
        by anyone on the target."""
        import shared_drives

        roles = ["reader", "organizer", "writer"]
        ordered = sorted(roles, key=lambda r: shared_drives.ROLE_ORDER.index(r))
        assert ordered[0] == "organizer"

    def test_an_unmapped_member_is_recorded_with_the_role_that_was_lost(
            self, sd, db, auth):
        sd._members = lambda drive_id: [
            {"type": "user", "role": "organizer",
             "emailAddress": "ghost@tenanta.com"}]

        sd._sync_members("drv-1", "drv-2", "Finance")

        row = db.conn.execute(
            "SELECT status, error_message FROM audit_log "
            "WHERE item_type='shared_drive_member'").fetchone()
        assert row["status"] == "SKIPPED_UNMAPPED_IDENTITY"
        assert "organizer" in row["error_message"]
        assert sd.stats["unmapped_members"] == 1

    def test_a_group_grant_is_recorded_not_guessed_at(self, sd, db):
        """It needs the group to exist on the target first, which is a
        separate provisioning job."""
        sd._members = lambda drive_id: [
            {"type": "group", "role": "writer", "emailAddress": "eng@tenanta.com"}]

        sd._sync_members("drv-1", "drv-2", "Finance")

        row = db.conn.execute(
            "SELECT status FROM audit_log "
            "WHERE item_type='shared_drive_member'").fetchone()
        assert row["status"] == "SKIPPED_NOT_A_USER"


class TestReuseOfTheDriveEngine:
    def test_the_shared_drive_id_replaces_both_roots(self, auth, db, settings,
                                                     identity, quota):
        """Reuse is the whole design: a parallel engine would need every fix
        this one has absorbed applied twice."""
        import drive_engine

        engine = drive_engine.DriveMigrator(auth, db, settings, SRC_USER,
                                            TGT_USER, quota)
        engine.shared_drive = "src-drive"
        engine.target_drive_id = "tgt-drive"
        engine._walk = lambda s, t, depth: setattr(engine, "_roots", (s, t))
        engine._fixup_shortcuts = lambda: None

        engine.run()

        assert engine._roots == ("src-drive", "tgt-drive")
        # No files.get(fileId='root') on either side: a shared drive id is
        # already its own root folder id.
        assert engine.src.call_count("files.get") == 0
