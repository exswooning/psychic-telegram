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


class TestReadingADriveTheAdminIsNotIn:
    """
    Domain-admin access covers drives().list and permissions().list -- which
    is why --all-drives can enumerate the whole tenant -- but
    files().list(corpora="drive") has no such override. Verified live against
    a real tenant, the admin gets:

        403 teamDriveMembershipRequired
        "The attempted action requires shared drive membership."

    The first attempt at this granted the admin organizer on the source
    drive. That cannot work and should not: SOURCE_SCOPES is drive.readonly
    on purpose, so the write is refused with insufficientPermissions (also
    verified live), and widening the scope would break both the read-only
    guarantee and every deployment whose Admin Console grant lacks it.

    Reading as a member needs no write and no new scope.
    """

    def test_a_member_is_chosen_to_read_a_drive_the_admin_is_not_in(self, sd):
        sd._members = lambda drive_id: [
            {"type": "user", "role": "reader", "emailAddress": "r@tenanta.com"},
            {"type": "user", "role": "organizer", "emailAddress": "o@tenanta.com"},
        ]

        # Organizer first: the role least likely to lose access mid-run.
        assert sd.reader_for("drv-1", "Finance") == "o@tenanta.com"

    def test_the_admin_is_kept_when_it_is_already_a_member(self, sd):
        """No reason to impersonate anyone else, and the admin's access is
        the least likely to be revoked underneath the run."""
        sd._members = lambda drive_id: [
            {"type": "user", "role": "organizer", "emailAddress": SRC_USER},
            {"type": "user", "role": "writer", "emailAddress": "w@tenanta.com"},
        ]

        assert sd.reader_for("drv-1", "Finance") == SRC_USER

    def test_group_only_membership_is_reported_not_guessed(self, sd, db):
        """A group grant cannot be impersonated -- there is no mailbox to be.
        Skipping loudly beats copying an empty drive."""
        sd._members = lambda drive_id: [
            {"type": "group", "role": "organizer", "emailAddress": "eng@tenanta.com"}]

        assert sd.reader_for("drv-1", "Finance") is None
        row = db.conn.execute(
            "SELECT status FROM audit_log WHERE item_type='shared_drive'").fetchone()
        assert row["status"] == "SKIPPED_NO_READABLE_MEMBER"

    def test_nothing_is_written_to_the_source_tenant(self, sd):
        """The whole point of the redesign: the source credential is
        read-only by construction and must stay that way."""
        sd._members = lambda drive_id: [
            {"type": "user", "role": "organizer", "emailAddress": "o@tenanta.com"}]

        sd.reader_for("drv-1", "Finance")

        assert sd.src.call_count("permissions.create") == 0

    def test_an_unreadable_drive_is_skipped_not_copied_as_empty(self, sd):
        sd.reader_for = lambda drive_id, name="": None
        sd._sync_members = lambda *a: pytest.fail("must not sync members")
        sd._copy_contents = lambda *a, **k: pytest.fail("must not copy contents")

        sd._migrate_one({"id": "drv-1", "name": "Finance"})

        assert sd.stats["unreadable"] == 1

    def test_the_engine_reads_as_the_member_but_bills_the_ledger_to_the_admin(
            self, sd, monkeypatch):
        """Which member is readable can change between runs. If the ledger
        key moved with it, a re-run would re-copy the whole drive instead of
        resuming it."""
        seen = {}

        class _Engine:
            def __init__(self, auth, db, settings, source_user, target_user, quota):
                seen["ledger_user"] = source_user
                self.shared_drive = self.target_drive_id = None

            @property
            def src(self):
                return None

            @src.setter
            def src(self, v):
                seen["impersonated"] = v

            def run(self):
                return {"files": 0, "folders": 0, "failed": 0}

        import shared_drives
        monkeypatch.setattr(shared_drives, "DriveMigrator", _Engine)
        sd.auth.source_drive = lambda u: f"client:{u}"

        sd._copy_contents("drv-1", "drv-2", "Finance", "o@tenanta.com")

        assert seen["ledger_user"] == SRC_USER
        assert seen["impersonated"] == "client:o@tenanta.com"


def _source_domain() -> str:
    """Whatever domain the ambient config actually has.

    seed_argv() gates on Settings().source_domain, which other tests in the
    suite change. Hardcoding one here passes in isolation and fails in the
    full run, which is worse than not testing it.
    """
    from config import Settings

    return (Settings().source_domain or "").strip().lower()


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
            {"confirm_domain": _source_domain(), "scale": "small",
             "shared_drives": 3})
        assert err is None or err == ""
        assert "--shared-drives" in argv
        assert argv[argv.index("--shared-drives") + 1] == "3"

    def test_a_seed_without_the_flag_makes_no_shared_drives(self):
        import webui
        argv, _env, _err = webui.seed_argv(
            {"confirm_domain": _source_domain(), "scale": "small"})
        assert "--shared-drives" not in argv

    def test_a_nonsense_count_is_refused_not_passed_to_the_tenant(self):
        import webui
        _argv, _env, err = webui.seed_argv(
            {"confirm_domain": _source_domain(), "scale": "small",
             "shared_drives": "lots"})
        assert err and "whole number" in err


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


class TestDriveLevelRolesAreNotReplayedPerFile:
    """
    A shared drive's membership is inherited by every file inside it, so
    organizer/fileOrganizer turn up in each file's permission list. They are
    not per-file grants, and Drive refuses them as such:

        403 organizerOnNonTeamDriveNotSupported

    Replaying them can never succeed, so every attempt is a permanent
    FAILED row. The first live shared-drive migration wrote 29 of them.
    shared_drives.py restores drive-level membership in _sync_members, which
    is where these belong.
    """

    def _acl_roles_sent(self, migrator, perms):
        src, tgt = migrator.src, migrator.tgt
        src.store["f1"] = {"id": "f1", "name": "f.txt", "parents": ["root-source"],
                           "mimeType": "text/plain"}
        src.perms["f1"] = perms
        tgt.store["t1"] = {"id": "t1", "name": "f.txt", "parents": ["root-target"],
                           "mimeType": "text/plain"}
        migrator._sync_acls("f1", "t1")
        return [c["body"]["role"] for c in tgt.calls_to("permissions.create")]

    def test_organizer_and_fileorganizer_are_skipped(self, migrator, identity):
        roles = self._acl_roles_sent(migrator, [
            {"id": "p1", "type": "user", "role": "organizer",
             "emailAddress": "o@tenanta.com"},
            {"id": "p2", "type": "user", "role": "fileOrganizer",
             "emailAddress": "fo@tenanta.com"},
        ])
        assert roles == []

    def test_real_per_file_roles_still_go_through(self, migrator, identity):
        """The guard must not swallow the grants that are genuinely
        per-file -- that would be a worse bug than the one it fixes."""
        roles = self._acl_roles_sent(migrator, [
            {"id": "p3", "type": "user", "role": "writer",
             "emailAddress": SRC_USER},   # mapped, so it is not skipped as unmapped
        ])
        assert "writer" in roles


class TestResetCanSeeWhatItSeeded:
    def test_it_lists_with_domain_admin_access(self):
        """A plain drives().list only returns drives the admin is a MEMBER
        of. A seeded drive need not be one -- anything created by another
        user is invisible without this and survives a --reset that reports
        success. Hit for real: SEEDED-SD-NOADMIN outlived its own cleanup."""
        import os
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "data-generator",
            "seed_shared_drives.py"), encoding="utf-8").read()
        body = src[src.index("def reset("):src.index("def main(")]
        assert "useDomainAdminAccess=True" in body


class TestSharedDriveStatsReachTheUI:
    """
    A shared-drive migration could be started from the Services page and then
    report nothing back to it. The only number anywhere was one "Shared
    Drives" tile on the Final Report, counting drives -- silent about the
    files inside them, the membership restored, and the drives that could not
    be read at all.
    """

    def _payload(self, db):
        import webui_spa
        return webui_spa.shared_drives_payload(db.conn)

    def test_it_counts_drives_members_and_losses(self, db):
        db.record_mapping(SRC_USER, "d1", "t1", "shared_drive", source_name="Fin")
        db.log_audit(SRC_USER, "u1", "shared_drive_member", "SUCCESS")
        db.log_audit(SRC_USER, "u2", "shared_drive_member", "SUCCESS")
        db.log_audit(SRC_USER, "ghost@x", "shared_drive_member",
                     "SKIPPED_UNMAPPED_IDENTITY", "organizer lost")
        db.log_audit(SRC_USER, "d2", "shared_drive", "SKIPPED_NO_READABLE_MEMBER")
        db.log_audit(SRC_USER, "d3", "shared_drive", "FAILED", "boom")

        p = self._payload(db)

        assert p["drives"] == 1
        assert p["members"] == 2
        # The two that matter most: access lost, and data never seen.
        assert p["unmappedMembers"] == 1
        assert p["unreadable"] == 1
        assert p["failed"] == 1

    def test_a_ledger_with_no_shared_drive_pass_reports_zeros_not_noise(self, db):
        """Distinct from "ran and found nothing" only at the UI layer, which
        renders nothing at all until the pass has run."""
        p = self._payload(db)
        assert p["drives"] == 0 and p["members"] == 0 and p["unreadable"] == 0

    def test_the_page_polls_it_and_hides_the_row_until_it_has_run(self):
        import os
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))),
            "migration-webui/src/pages/Services.tsx"), encoding="utf-8").read()
        assert "fetchSharedDrives" in src
        assert "shared-drive-stats" in src
        # a row of zeros reads as "migrated nothing", not "has not run"
        assert "{sd && (" in src
