"""
tests/test_link_transfer.py
===========================
The link_flip transfer mode, which makes files publicly readable in order to
move them across an organisation boundary.

This is the most dangerous code in the project, so it is tested for the
failure cases rather than the happy path. Every test below corresponds to a
real bug found by exercising the module for the first time:

  * the module could not run at all -- it called a database method that does
    not exist, having been written and committed without ever being executed
  * a restore that failed dropped the file out of the audit list, so a file
    left *publicly readable* stopped being reported
  * a file that was already link-shared had that sharing deleted on restore

The invariant: at no point may a file be public without a durable record of
what its sharing was, and anything still public must appear in the audit.
"""

from __future__ import annotations

import json

import pytest

import link_transfer
from db import MigrationDB


class FakeDrive:
    """Records what was asked of Drive; can be told to fail at one step."""

    def __init__(self, perms, fail_on=None):
        self._perms = perms
        self.fail_on = fail_on
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self._mode = ""

    def permissions(self):
        return self

    def list(self, **kw):
        if self.fail_on == "list":
            raise RuntimeError("permissions.list failed")
        self._mode = "list"
        return self

    def create(self, **kw):
        if self.fail_on == "create":
            raise RuntimeError("permissions.create failed")
        self.created.append(kw.get("body"))
        self._mode = "create"
        return self

    def delete(self, **kw):
        if self.fail_on == "delete":
            raise RuntimeError("permissions.delete failed")
        self.deleted.append(kw.get("permissionId"))
        self._mode = "delete"
        return self

    def execute(self):
        if self._mode == "list":
            return {"permissions": self._perms}
        if self._mode == "create":
            return {"id": "created-perm"}
        return {}


PRIVATE = [
    {"id": "p1", "type": "user", "role": "writer", "emailAddress": "bob@c.com"},
    {"id": "p2", "type": "domain", "role": "reader", "domain": "c.com"},
]
ALREADY_PUBLIC = [
    {"id": "pa", "type": "anyone", "role": "reader"},
    {"id": "p1", "type": "user", "role": "writer", "emailAddress": "bob@c.com"},
]


@pytest.fixture
def db(tmp_path):
    d = MigrationDB(str(tmp_path / "m.db"))
    d.init_schema()
    link_transfer.ensure_schema(d)
    return d


class TestSchema:
    def test_the_module_can_actually_initialise(self, tmp_path):
        """It could not: it called db._connect(), which does not exist, and
        then executescript() inside a transaction, which ends it."""
        d = MigrationDB(str(tmp_path / "x.db"))
        d.init_schema()
        link_transfer.ensure_schema(d)
        link_transfer.ensure_schema(d)          # idempotent
        assert link_transfer.outstanding(d) == []


class TestExposureOrdering:
    def test_the_acl_is_saved_before_the_file_is_exposed(self, db):
        drive = FakeDrive(PRIVATE)
        link_transfer.flip_to_public(drive, db, "a@c.com",
                                     {"id": "F1", "name": "Plan"})
        rows = link_transfer.outstanding(db)
        assert len(rows) == 1
        saved = db.conn.execute(
            "SELECT permissions FROM acl_backup WHERE file_id='F1'").fetchone()
        assert len(json.loads(saved["permissions"])) == 2

    def test_an_unreadable_acl_exposes_nothing(self, db):
        """A file whose original sharing cannot be recorded must not be made
        public -- there would be no way to put it back."""
        drive = FakeDrive(PRIVATE, fail_on="list")
        with pytest.raises(RuntimeError):
            link_transfer.flip_to_public(drive, db, "a@c.com",
                                         {"id": "F2", "name": "X"})
        assert drive.created == []
        assert link_transfer.outstanding(db) == []


class TestRestore:
    def test_restore_reinstates_the_acl_and_removes_the_public_grant(self, db):
        link_transfer.flip_to_public(FakeDrive(PRIVATE), db, "a@c.com",
                                     {"id": "F1", "name": "Plan"})
        row = link_transfer.outstanding(db)[0]

        drive = FakeDrive(PRIVATE)
        ok, _ = link_transfer.restore_one(drive, db, row)

        assert ok
        addrs = [c.get("emailAddress") or c.get("domain") for c in drive.created]
        assert addrs == ["bob@c.com", "c.com"]
        assert drive.deleted == ["created-perm"]
        assert link_transfer.outstanding(db) == []

    def test_a_failed_restore_stays_in_the_audit(self, db):
        """The bug that mattered: RESTORE_FAILED dropped out of the list, so a
        file still readable by anyone stopped being reported as such."""
        link_transfer.flip_to_public(FakeDrive(PRIVATE), db, "a@c.com",
                                     {"id": "F3", "name": "Y"})
        row = link_transfer.outstanding(db)[0]

        ok, note = link_transfer.restore_one(
            FakeDrive(PRIVATE, fail_on="delete"), db, row)

        assert not ok and "still present" in note
        still = link_transfer.outstanding(db)
        assert [r["file_id"] for r in still] == ["F3"]
        assert still[0]["state"] == "RESTORE_FAILED"

    def test_the_public_grant_is_removed_last(self, db):
        """If re-adding the real ACL fails, the file must stay reachable rather
        than become one nobody can open."""
        link_transfer.flip_to_public(FakeDrive(PRIVATE), db, "a@c.com",
                                     {"id": "F5", "name": "W"})
        row = link_transfer.outstanding(db)[0]

        drive = FakeDrive(PRIVATE, fail_on="create")
        ok, _ = link_transfer.restore_one(drive, db, row)

        assert not ok
        assert drive.deleted == [], "public grant dropped despite a failed restore"

    def test_restore_without_a_saved_acl_refuses(self, db):
        ok, note = link_transfer.restore_one(
            FakeDrive(PRIVATE), db,
            {"source_user": "a@c.com", "file_id": "nope", "public_perm": "x"})
        assert not ok and "no saved ACL" in note


class TestAlreadyPublicFiles:
    def test_a_link_shared_file_is_not_given_a_second_grant(self, db):
        """Drive keeps one `anyone` permission per file, so creating one on an
        already-public file returns the owner's existing grant."""
        drive = FakeDrive(ALREADY_PUBLIC)
        perm = link_transfer.flip_to_public(drive, db, "a@c.com",
                                            {"id": "F4", "name": "Z"})
        assert perm == ""
        assert drive.created == []

    def test_its_original_sharing_survives_restore(self, db):
        """Deleting that grant would strip sharing the owner deliberately set
        — a silent permission loss caused by the migration itself."""
        link_transfer.flip_to_public(FakeDrive(ALREADY_PUBLIC), db, "a@c.com",
                                     {"id": "F4", "name": "Z"})
        row = link_transfer.outstanding(db)[0]

        drive = FakeDrive(ALREADY_PUBLIC)
        ok, _ = link_transfer.restore_one(drive, db, row)

        assert ok
        assert drive.deleted == []
        assert not any(c.get("type") == "anyone" for c in drive.created)


class TestAuditSurface:
    def test_nothing_flipped_means_nothing_exposed(self, db):
        assert link_transfer.outstanding(db) == []

    def test_every_outstanding_row_carries_what_is_needed_to_fix_it(self, db):
        link_transfer.flip_to_public(FakeDrive(PRIVATE), db, "a@c.com",
                                     {"id": "F1", "name": "Plan"})
        row = link_transfer.outstanding(db)[0]
        for key in ("source_user", "file_id", "file_name", "public_perm",
                    "state", "saved_at"):
            assert key in row


class TestTransferModeIsValidated:
    """
    An unrecognised TRANSFER_MODE used to be accepted and then behave as
    download_upload, because every check in the engine was `== "server_side"`.

    So `TRANSFER_MODE=link_flip` -- a mode that was documented -- silently did
    nothing, and a typo like `server_sied` streamed every byte through the
    migration host. That is precisely what someone setting the variable is
    usually trying to avoid, and nothing said otherwise.
    """

    def test_the_three_real_modes_are_accepted(self, monkeypatch):
        from config import Settings

        for mode in ("download_upload", "server_side", "link_flip"):
            monkeypatch.setenv("TRANSFER_MODE", mode)
            assert Settings().transfer_mode == mode

    def test_a_typo_is_rejected_rather_than_silently_downgraded(self, monkeypatch):
        from config import Settings

        monkeypatch.setenv("TRANSFER_MODE", "server_sied")
        with pytest.raises(ValueError, match="not recognised"):
            Settings()

    def test_the_error_lists_what_is_valid(self, monkeypatch):
        from config import Settings

        monkeypatch.setenv("TRANSFER_MODE", "nonsense")
        with pytest.raises(ValueError) as exc:
            Settings()
        for mode in ("download_upload", "server_side", "link_flip"):
            assert mode in str(exc.value)

    def test_an_empty_value_is_rejected(self, monkeypatch):
        from config import Settings

        monkeypatch.setenv("TRANSFER_MODE", "")
        with pytest.raises(ValueError):
            Settings()


class TestLinkFlipIsWired:
    def test_link_flip_takes_the_staging_drive_path(self, monkeypatch):
        """It copies server-side; the flip is an addition, not a replacement."""
        import drive_engine
        from config import Settings

        monkeypatch.setenv("TRANSFER_MODE", "link_flip")
        s = Settings()
        mig = drive_engine.DriveMigrator.__new__(drive_engine.DriveMigrator)
        mig.settings = s
        assert mig.server_side is True
        assert mig.link_flip is True

    def test_server_side_alone_never_flips(self, monkeypatch):
        """The safe mode must not start exposing files."""
        import drive_engine
        from config import Settings

        monkeypatch.setenv("TRANSFER_MODE", "server_side")
        mig = drive_engine.DriveMigrator.__new__(drive_engine.DriveMigrator)
        mig.settings = Settings()
        assert mig.server_side is True
        assert mig.link_flip is False

    def test_link_flip_requires_the_drive_write_scope_on_the_source(self, monkeypatch):
        """It rewrites source permissions, which read-only cannot do."""
        from config import DRIVE_READONLY_SCOPE, DRIVE_WRITE_SCOPE, Settings, source_scopes

        monkeypatch.setenv("TRANSFER_MODE", "link_flip")
        scopes = source_scopes(Settings())
        assert DRIVE_WRITE_SCOPE in scopes
        assert DRIVE_READONLY_SCOPE not in scopes

    def test_the_restore_runs_in_a_finally(self):
        """A failed copy must not leave the file public."""
        import inspect

        import drive_engine

        src = inspect.getsource(drive_engine.DriveMigrator._sync_server_side)
        flip_at = src.index("flip_to_public")
        finally_at = src.index("finally:")
        restore_at = src.index("restore_one")
        assert flip_at < finally_at < restore_at

    def test_an_unrecordable_acl_fails_the_item_without_exposing_it(self):
        import inspect

        import drive_engine

        src = inspect.getsource(drive_engine.DriveMigrator._sync_server_side)
        assert "could not record the ACL" in src
