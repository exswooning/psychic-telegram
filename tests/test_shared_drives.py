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


class TestTheEngineActuallyWalksAsharedDrive:
    """
    TestReuseOfTheDriveEngine above stubs out _walk, so it proves the roots
    are substituted and nothing more -- no test in this suite had ever run
    the real tree walk with `shared_drive` set.

    That is how the first live run of this module reached production
    migrating 0 files: it created both target drives and all 10 memberships,
    then died inside engine.run() with a sqlite3 InterfaceError ("Error
    binding parameter 1 - probably unsupported type"), which _copy_contents
    recorded as a one-line string with the traceback discarded.
    """

    def test_files_in_a_shared_drive_are_copied(self, auth, db, settings,
                                                identity, quota):
        import drive_engine

        src = auth.source_drive(SRC_USER)
        drive_id = "src-drive-1"
        src.shared_drives[drive_id] = {"id": drive_id, "name": "Engineering"}
        folder = src.add_folder("Specs", parent=drive_id)
        src.add_binary("spec.pdf", parent=folder)
        src.add_binary("notes.pdf", parent=drive_id)

        tgt = auth.target_drive(TGT_USER)
        tgt_drive = "tgt-drive-1"
        tgt.shared_drives[tgt_drive] = {"id": tgt_drive, "name": "Engineering"}

        engine = drive_engine.DriveMigrator(auth, db, settings, SRC_USER,
                                            TGT_USER, quota)
        engine.shared_drive = drive_id
        engine.target_drive_id = tgt_drive

        result = engine.run()

        assert result["failed"] == 0, f"walk failed: {result}"
        assert result["files"] == 2, f"expected both files, got {result}"
        assert result["folders"] == 1


def _raise_on_create(sd, message: str) -> None:
    """Make src.permissions().create(...).execute() raise.

    permissions() hands back a fresh object per call, so patching the one an
    earlier call returned does nothing -- the code under test asks for its
    own. Replace the factory instead.
    """
    class _Raising:
        def create(self, **_kw):
            class _Exec:
                @staticmethod
                def execute():
                    raise RuntimeError(message)
            return _Exec()

    sd.src.permissions = lambda: _Raising()


class TestAccessToDrivesTheAdminIsNotIn:
    """
    Domain-admin access is not a skeleton key. It covers drives().list and
    permissions().list -- which is why --all-drives can enumerate the whole
    tenant -- but files().list(corpora="drive") has no such override and
    needs real membership.

    So --all-drives lists a drive the admin does not belong to and then reads
    nothing out of it. The drive inventories as empty and migrates as empty,
    which is indistinguishable from a drive that genuinely is empty: the exact
    silent-undercount this module exists to prevent, one level up.
    """

    def test_the_admin_is_added_as_organizer_on_the_source_drive(self, sd):
        assert sd.ensure_access("drv-1", "Finance") is True

        call = sd.src.calls_to("permissions.create")[0]
        assert call["fileId"] == "drv-1"
        # organizer, not writer: a lesser role cannot read every file in the
        # drive, which is the whole point of asking.
        assert call["body"]["role"] == "organizer"
        assert call["body"]["emailAddress"] == SRC_USER
        # Without this the grant is refused for a drive the admin is not in
        # -- which is precisely the drive that needs it.
        assert call["useDomainAdminAccess"] is True
        assert call["sendNotificationEmail"] is False

    def test_it_targets_the_source_tenant_not_the_target(self, sd):
        """Every other permissions.create in this codebase writes to the
        target. This one is the exception and must not drift into it."""
        sd.ensure_access("drv-1", "Finance")

        assert sd.src.call_count("permissions.create") == 1
        assert sd.tgt.call_count("permissions.create") == 0

    def test_the_grant_is_audited_because_it_changes_the_source_tenant(
            self, sd, db):
        sd.ensure_access("drv-1", "Finance")

        row = db.conn.execute(
            "SELECT status, error_message FROM audit_log "
            "WHERE item_type='shared_drive_access'").fetchone()
        assert row["status"] == "SUCCESS"
        assert "Finance" in row["error_message"]
        assert sd.stats["granted"] == 1

    def test_an_existing_membership_is_success_not_failure(self, sd, db):
        """Idempotent: re-running must not count a no-op as a failure."""
        _raise_on_create(sd, "Permission already exists on this item")

        assert sd.ensure_access("drv-1", "Finance") is True
        assert sd.stats["failed"] == 0
        assert sd.stats["granted"] == 0

    def test_a_real_refusal_is_recorded_and_stops_that_drive(self, sd, db):
        _raise_on_create(sd, "insufficientFilePermissions")

        assert sd.ensure_access("drv-1", "Finance") is False
        row = db.conn.execute(
            "SELECT status FROM audit_log "
            "WHERE item_type='shared_drive_access'").fetchone()
        assert row["status"] == "FAILED"
        assert sd.stats["failed"] == 1

    def test_dry_run_grants_nothing(self, sd):
        sd.settings.dry_run = True

        assert sd.ensure_access("drv-1", "Finance") is True
        assert sd.src.call_count("permissions.create") == 0

    def test_a_drive_we_cannot_reach_is_not_then_copied_as_empty(self, sd):
        """The failure that motivated this: reading on regardless produces a
        confident, wrong "0 files" instead of a recorded access problem."""
        sd.ensure_access = lambda drive_id, name="": False
        sd._sync_members = lambda *a: pytest.fail("must not sync members")
        sd._copy_contents = lambda *a: pytest.fail("must not copy contents")

        sd._migrate_one({"id": "drv-1", "name": "Finance"})

    def test_no_grant_leaves_source_permissions_alone(self, sd):
        """An operator who does not want the tool touching source ACLs."""
        sd._sync_members = lambda *a: None
        sd._copy_contents = lambda *a: None

        sd._migrate_one({"id": "drv-1", "name": "Finance"}, grant=False)

        assert sd.src.call_count("permissions.create") == 0


class TestTheSeederCanMakeThem:
    """
    shared_drives.py had nothing to migrate: the per-user seeder cannot make
    a shared drive (they belong to no user), and seed_shared_drives.py was
    never referenced from anywhere -- not an action, not the seed endpoint,
    not seed_sandbox.py. A whole migration pass with no way to produce input.
    """

    def test_seed_sandbox_exposes_shared_drives(self):
        import subprocess, sys, os
        out = subprocess.run(
            [sys.executable, "seed_sandbox.py", "--help"],
            cwd=os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "data-generator"),
            capture_output=True, text=True, timeout=60).stdout
        assert "--shared-drives" in out

    def test_the_seed_endpoint_passes_the_count_through(self):
        import webui
        argv, _env, err = webui.seed_argv(
            {"confirm_domain": "tenanta.com", "scale": "small", "shared_drives": 3})
        assert err is None or err == ""
        assert "--shared-drives" in argv
        assert argv[argv.index("--shared-drives") + 1] == "3"

    def test_a_seed_without_the_flag_makes_no_shared_drives(self):
        import webui
        argv, _env, _err = webui.seed_argv(
            {"confirm_domain": "tenanta.com", "scale": "small"})
        assert "--shared-drives" not in argv

    def test_a_nonsense_count_is_refused_not_passed_to_the_tenant(self):
        import webui
        _argv, _env, err = webui.seed_argv(
            {"confirm_domain": "tenanta.com", "scale": "small",
             "shared_drives": "lots"})
        assert err and "whole number" in err


class TestGrantAccessIsReachableFromTheUI:
    def test_the_action_exists_and_is_confirm_gated(self):
        import webui
        act = webui.ACTIONS["shared_drives_grant_access"]
        # It writes to the customer's SOURCE tenant, so it must not be a
        # one-click: every other source-mutating action here is gated too.
        assert act["destructive"] is True
        assert act["confirm"]
        assert "--grant-access" in act["argv"]


class TestTheSeederBuildsTheCorpusItClaimsTo:
    """
    seed_shared_drives.seed() had no test at all, and it now runs as part of
    an ordinary --shared-drives seed rather than only when someone remembers
    the standalone script. What it produces is the entire input to
    shared_drives.py, so "it ran without raising" is not enough -- a drive
    with no organizer, or with every file native, silently narrows what the
    migration pass is ever exercised against.
    """

    @pytest.fixture
    def seeded(self, settings, monkeypatch):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "data-generator"))
        import seed_shared_drives as ssd
        from tests.fakes import FakeDrive

        drive = FakeDrive("admin@tenanta.com", "source")
        monkeypatch.setattr(ssd, "_drive_client", lambda *a, **k: drive)
        # Stand in for MediaInMemoryUpload with the two attributes the fake
        # actually reads, so the binary path is exercised for real rather
        # than skipped.
        class _Media:
            def __init__(self, blob, mime):
                self._blob, self.mimetype = blob, mime

            def read_all(self):
                return self._blob

        monkeypatch.setattr(ssd, "_media", _Media)
        members = [f"u{i}@tenanta.com" for i in range(5)]
        made = ssd.seed(settings, "admin@tenanta.com", members,
                        n_drives=2, files_per_folder=4)
        return drive, made, ssd

    def test_it_creates_the_drives_it_reports(self, seeded):
        drive, made, _ = seeded
        assert len(made["drives"]) == 2
        assert drive.call_count("drives.create") == 2

    def test_every_drive_level_role_is_represented(self, seeded):
        """All five, not just a writer: they cascade to every file inside and
        are restored organizer-first, so a corpus missing organizer never
        exercises the ordering shared_drives.py is built around."""
        drive, _made, ssd = seeded
        roles = {c["body"]["role"] for c in drive.calls_to("permissions.create")
                 if c["body"].get("role") in ssd.ROLES}
        assert roles == set(ssd.ROLES)

    def test_the_tree_is_nested_not_flat(self, seeded):
        """A flat drive would never exercise the traversal."""
        _drive, made, _ = seeded
        assert made["folders"] == 4          # 2 levels x 2 drives

    def test_both_native_and_binary_files_are_present(self, seeded):
        """server_side copies natives without export and download_upload
        round-trips them through OOXML -- only one of those can be wrong at
        a time, so a corpus of one kind proves half the path."""
        drive, _made, _ = seeded
        created = [c["body"] for c in drive.calls_to("files.create")]
        natives = [b for b in created
                   if b.get("mimeType") == "application/vnd.google-apps.document"]
        binaries = [b for b in created if b.get("name", "").endswith(".bin")]
        assert natives and binaries

    def test_a_per_file_grant_inside_a_shared_drive_exists(self, seeded):
        """_sync_acls' own docstring records this case as unverified, so
        leaving it out of the corpus leaves the one genuinely unknown
        behaviour untested."""
        _drive, made, _ = seeded
        assert made["acls"] >= 1

    def test_names_are_prefixed_so_reset_can_find_exactly_these(self, seeded):
        """--reset deletes by prefix. 'Delete every shared drive on the
        tenant' is not a thing a seeding tool should be able to do."""
        drive, _made, ssd = seeded
        names = [c["body"]["name"] for c in drive.calls_to("drives.create")]
        assert all(n.startswith(ssd.PREFIX) for n in names)
