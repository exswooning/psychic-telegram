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
                seen["engine"] = self
                self.shared_drive = self.target_drive_id = None

            def run(self):
                return {"files": 0, "folders": 0, "failed": 0}

        import shared_drives
        monkeypatch.setattr(shared_drives, "DriveMigrator", _Engine)
        sd.copiers_for = lambda drive_id, managers_only=False: []

        sd._copy_contents("drv-1", "drv-2", "Finance", "o@tenanta.com")

        assert seen["ledger_user"] == SRC_USER
        # A name the engine resolves per thread, never a client built on this one: the
        # engine's file pool would all drive this thread's one socket.
        assert seen["engine"].reader == "o@tenanta.com"
        assert seen["engine"].copiers == ["o@tenanta.com"]


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


class TestStagingLeftoversCanBeReclaimed:
    """
    DriveMigrator tears down its staging drive in a `finally`, but a run that
    is killed outright never reaches it -- and the drive's only organizers are
    the users it staged for, so once those accounts are deleted it has no
    living member: unreadable, unlistable by name, undeletable by anyone.

    Found on a real tenant as 188 of 188 target shared drives, every one an
    orphan, spanning nine days of runs.
    """

    @pytest.fixture
    def tgt_with_staging(self, auth, settings, monkeypatch):
        from tests.fakes import FakeDrive
        t = FakeDrive(TGT_USER, "target")
        t.shared_drives["s1"] = {"id": "s1", "name": f"{settings.staging_drive_prefix}-alice"}
        t.shared_drives["s2"] = {"id": "s2", "name": f"{settings.staging_drive_prefix}-bob"}
        t.shared_drives["keep"] = {"id": "keep", "name": "Finance"}
        monkeypatch.setattr(auth, "target_drive", lambda u: t)
        return t

    def _run(self, auth, settings, tgt, apply):
        import shared_drives
        return shared_drives.cleanup_staging_drives(auth, settings, TGT_USER,
                                                    apply=apply)

    def test_it_only_touches_staging_drives(self, auth, settings, tgt_with_staging):
        r = self._run(auth, settings, tgt_with_staging, apply=True)
        assert r["found"] == 2          # "Finance" is not ours to delete
        deleted = [c["driveId"] for c in tgt_with_staging.calls_to("drives.delete")]
        assert "keep" not in deleted

    def test_a_dry_run_deletes_nothing(self, auth, settings, tgt_with_staging):
        r = self._run(auth, settings, tgt_with_staging, apply=False)
        assert r["found"] == 2 and r["deleted"] == 0
        assert tgt_with_staging.call_count("drives.delete") == 0

    def test_a_staging_drive_holding_files_is_left_alone(
            self, auth, settings, tgt_with_staging):
        """Those are files copied but never moved. Deleting the drive would
        destroy them -- the same invariant _teardown_staging_drive keeps."""
        tgt_with_staging.store["orphan"] = {
            "id": "orphan", "name": "half-copied.bin", "parents": ["s1"],
            "mimeType": "application/octet-stream"}

        r = self._run(auth, settings, tgt_with_staging, apply=True)

        assert r["not_empty"] == 1
        assert "s1" not in [c["driveId"] for c in tgt_with_staging.calls_to("drives.delete")]

    def test_it_is_reachable_from_the_ui(self):
        import webui
        act = webui.ACTIONS["staging_drives_cleanup"]
        assert act["destructive"] is True and act["confirm"]
        assert "--cleanup-staging" in act["argv"]


class TestExternalMembersKeepTheirAccess:
    """
    drive_engine._sync_acls has always carried an external grantee's address
    over unchanged: they are not in identity_map and never will be, because
    they are not part of this migration, but their address is just as valid
    on the target.

    _sync_members did the opposite -- anything unmapped was dropped. So an
    external partner kept their grants on individual files inside a shared
    drive and silently lost their membership OF it, which is the access that
    actually cascades.
    """

    def _grant(self, sd, members):
        sd._members = lambda drive_id: members
        sd._sync_members("drv-1", "drv-2", "Finance")
        return {c["body"]["emailAddress"]: c["body"]["role"]
                for c in sd.tgt.calls_to("permissions.create")}

    def test_an_external_member_carries_over_unchanged(self, sd):
        got = self._grant(sd, [{"type": "user", "role": "writer",
                                "emailAddress": "partner@othercorp.com"}])

        assert got == {"partner@othercorp.com": "writer"}
        assert sd.stats["external_members"] == 1
        assert sd.stats["unmapped_members"] == 0

    def test_a_mapped_in_domain_member_is_rewritten(self, sd):
        got = self._grant(sd, [{"type": "user", "role": "organizer",
                                "emailAddress": SRC_USER}])

        assert got == {TGT_USER: "organizer"}

    def test_an_unmapped_source_domain_member_is_still_dropped(self, sd, db):
        """Not the same case: they ARE part of this migration and simply have
        no target account, so there is nobody to grant it to."""
        got = self._grant(sd, [{"type": "user", "role": "writer",
                                "emailAddress": "ghost@tenanta.com"}])

        assert got == {}
        assert sd.stats["unmapped_members"] == 1
        assert sd.stats["external_members"] == 0

    def test_a_member_with_no_address_is_a_deleted_account_not_a_failure(self, sd, db):
        """Live: SEEDED-SD-3 had a reader with a blank emailAddress. It fell into
        the external branch, was granted to "", and stayed the run's last FAILED
        row under an empty key. Same rule drive_engine._sync_acls already had."""
        got = self._grant(sd, [{"id": "p9", "type": "user", "role": "reader", "emailAddress": ""}])

        assert got == {}                      # nothing sent to Google
        assert sd.stats["external_members"] == 0 and sd.stats["failed"] == 0
        row = db.conn.execute("SELECT item_id, status FROM audit_log "
                              "WHERE item_type='shared_drive_member'").fetchone()
        assert (row["item_id"], row["status"]) == ("drv-1:p9", "SKIPPED_UNMAPPED_IDENTITY")

    def test_the_role_survives_for_an_external_member(self, sd):
        """Dropping them to reader would quietly demote a partner who had
        edit rights."""
        got = self._grant(sd, [{"type": "user", "role": "fileOrganizer",
                                "emailAddress": "p@vendor.io"}])
        assert got["p@vendor.io"] == "fileOrganizer"


class TestTheSeedUIOffersSharedDrives:
    """The server accepted a shared_drives count from the day the seeder
    grew one, but nothing on screen could send it -- so the only way to seed
    a shared drive was to POST /api/seed by hand. An option the UI cannot
    reach is an option the product does not have."""

    def _page(self):
        import os
        return open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))),
            "migration-webui/src/pages/SeedWizard.tsx"), encoding="utf-8").read()

    def test_the_wizard_has_a_control_for_it(self):
        src = self._page()
        assert "shared-drives" in src
        assert "sharedDrives" in src

    def test_it_reaches_runSeed(self):
        """Asserts sharedDrives is passed, not that it is the LAST argument --
        pinning the closing paren made this fail the moment runSeed grew a
        further parameter, for a change that kept the behaviour intact."""
        src = self._page()
        call = src.split("runSeed(", 1)[1].split(")", 1)[0]
        assert "sharedDrives" in call

    def test_the_client_sends_it_to_the_server(self):
        import os
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))),
            "migration-webui/src/api/client.ts"), encoding="utf-8").read()
        assert "shared_drives: sharedDrives" in src

    def test_it_says_why_the_per_user_seed_cannot_make_one(self):
        import re
        src = re.sub(r"\s+", " ", self._page())
        assert "belongs to no user" in src


class TestDrivesMigrateConcurrently:
    """A tenant's shared drives are independent trees with no ordering
    between them; only the loop serialised them. The per-drive work is
    almost entirely waiting on Google, which is what overlaps well."""

    def test_several_drives_are_migrated_in_parallel(self, sd, monkeypatch):
        import threading, time
        seen, lock = [], threading.Lock()
        concurrent = {"max": 0, "now": 0}

        def slow(drive):
            with lock:
                concurrent["now"] += 1
                concurrent["max"] = max(concurrent["max"], concurrent["now"])
            time.sleep(0.25)
            with lock:
                concurrent["now"] -= 1
                seen.append(drive["id"])

        sd.list_source_drives = lambda all_drives: [
            {"id": f"d{i}", "name": f"D{i}"} for i in range(4)]
        sd._migrate_one = slow

        sd.migrate_all(True, workers=2)

        assert sorted(seen) == ["d0", "d1", "d2", "d3"], "a drive was dropped"
        assert concurrent["max"] > 1, "drives still ran one at a time"

    def test_one_drive_failing_does_not_lose_the_others(self, sd, db):
        def boom(drive):
            if drive["id"] == "d1":
                raise RuntimeError("drive exploded")
        sd.list_source_drives = lambda all_drives: [
            {"id": "d0", "name": "A"}, {"id": "d1", "name": "B"},
            {"id": "d2", "name": "C"}]
        sd._migrate_one = boom

        stats = sd.migrate_all(True, workers=2)

        assert stats["failed"] == 1
        row = db.conn.execute(
            "SELECT status FROM audit_log WHERE item_type='shared_drive' "
            "AND status='FAILED'").fetchone()
        assert row is not None, "the failure was not recorded anywhere"

    def test_workers_of_one_is_still_the_serial_path(self, sd):
        order = []
        sd.list_source_drives = lambda all_drives: [
            {"id": "d0"}, {"id": "d1"}]
        sd._migrate_one = lambda d: order.append(d["id"])
        sd.migrate_all(True, workers=1)
        assert order == ["d0", "d1"]


class TestConcurrencyIsActuallyThreadSafe:
    """The first parallel version aborted the process at the C level:

        rc=-6   corrupted size vs. prev_size

    __init__ built self.src/self.tgt once, on whichever thread constructed
    the migrator, and each holds an httplib2.Http -- which is not
    thread-safe, and is the first thing requirements.txt says about it. Every
    worker then hammered that one shared object. AuthManager already caches
    services in threading.local() for exactly this reason; this class had
    cached them on the instance instead and defeated it.
    """

    def test_clients_are_resolved_per_access_not_cached_on_the_instance(self, sd):
        calls = []
        real = sd.auth.source_drive
        sd.auth.source_drive = lambda u: (calls.append(u), real(u))[1]

        sd.src; sd.src; sd.src

        # three accesses, three asks -- so a worker thread gets its own
        assert len(calls) == 3, "src was cached instead of re-resolved"
        assert "src" not in sd.__dict__ and "tgt" not in sd.__dict__

    def test_stats_do_not_lose_counts_under_concurrency(self, sd):
        """stats[k] += 1 is read-modify-write. Two drives finishing together
        silently dropped a count, and a number that undercounts is worse than
        no number because it looks like data."""
        import threading

        def hammer():
            for _ in range(400):
                sd._bump("members")

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert sd.stats["members"] == 1600, sd.stats["members"]


class TestCreatingTheTargetDriveSurvivesAResend:
    """SEEDED-SD-10: a bare create with a random requestId got a transient 409
    ("requestId has already been used") on a resend, recorded FAILED and
    returned before members or contents -- the whole drive was missing."""

    def test_the_request_id_is_derived_not_random(self, sd):
        sd._create_target_drive("drv-1", "Finance")
        sd._create_target_drive("drv-1", "Finance")
        ids = [c["requestId"] for c in sd.tgt.calls_to("drives.create")]
        assert ids[0] == ids[1]                      # a resend is the same request

    def test_a_409_adopts_the_drive_the_earlier_attempt_made(self, sd):
        sd.tgt.shared_drives["made-earlier"] = {"id": "made-earlier", "name": "Finance"}
        sd.tgt.fail_next("drives.create", status=409, reason="conflict")
        assert sd._create_target_drive("drv-1", "Finance") == "made-earlier"
        assert len(sd.tgt.calls_to("drives.create")) == 1

    def test_a_409_with_nothing_to_adopt_retries_with_a_fresh_id(self, sd):
        sd.tgt.fail_next("drives.create", status=409, reason="conflict")
        got = sd._create_target_drive("drv-1", "Finance")
        calls = sd.tgt.calls_to("drives.create")
        assert got and len(calls) == 2 and calls[0]["requestId"] != calls[1]["requestId"]

    def test_a_drive_another_source_owns_is_never_adopted(self, sd, db):
        sd.tgt.shared_drives["taken"] = {"id": "taken", "name": "Finance"}
        db.record_mapping(SRC_USER, "other-src", "taken", "shared_drive")
        sd.tgt.fail_next("drives.create", status=409, reason="conflict")
        assert sd._create_target_drive("drv-1", "Finance") != "taken"


class TestAMappedDriveIsNotReGrantedMemberByMember:
    """Measured live: re-creating every member of 20 already-mapped drives was
    ~99% of a whole-tenant run's shared-drive step (36 min; each drive's content
    walk took 2.5 s). Present members are now read once and skipped."""

    def test_only_missing_or_changed_members_are_granted(self, sd):
        src = [{"type": "user", "role": "writer", "emailAddress": "partner@othercorp.com"},
               {"type": "user", "role": "reader", "emailAddress": "new@othercorp.com"},
               {"type": "user", "role": "writer", "emailAddress": "promoted@othercorp.com"}]
        tgt = [{"type": "user", "role": "writer", "emailAddress": "partner@othercorp.com"},
               {"type": "user", "role": "reader", "emailAddress": "promoted@othercorp.com"}]
        sd._members = lambda drive_id, svc=None: tgt if svc is not None else src
        sd._sync_members("drv-1", "drv-2", "Finance")
        granted = {c["body"]["emailAddress"] for c in sd.tgt.calls_to("permissions.create")}
        assert granted == {"new@othercorp.com", "promoted@othercorp.com"}
        assert sd.stats["members_present"] == 1


def _managed_drive(auth, db, settings, quota, managers):
    """A shared drive of four 100-byte files that every manager can see, each manager
    allowed 200 bytes a day, and the engine set to copy it as them in turn."""
    import drive_engine

    settings.transfer_mode = "server_side"
    settings.effective_upload_cap = lambda: 200      # two 100-byte files a day each
    main = auth._get("source", "drive", SRC_USER)
    main.shared_drives["src-drive"] = {"id": "src-drive", "name": "Finance"}
    for i in range(4):
        main.add_binary(f"f{i}.pdf", parent="src-drive", data=bytes([i]) * 100)
    for m in managers:                 # every member sees the same drive
        auth._get("source", "drive", m)
        auth._svcs[("source", "drive", m)] = main
    auth.target_drive(TGT_USER).shared_drives["tgt-drive"] = {"id": "tgt-drive",
                                                               "name": "Finance"}
    e = drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota)
    e.shared_drive, e.target_drive_id = "src-drive", "tgt-drive"
    e.reader, e.copiers = SRC_USER, list(managers)
    return e


class TestManagersShareTheDailyCap:
    """Google charges a copy to the account that MAKES it -- not the drive it lands in,
    not whoever moves it after (sandbox pair, 2026-10-09: a manager refused at ~740 GB of
    copies while a second copied 25 GB into the same drive; accounts already refused moved
    5-25 GB in every direction). One account moves a 5 TB drive in a week; its managers in
    turn, in a day."""

    MANAGERS = ["m1@tenanta.com", "m2@tenanta.com", "m3@tenanta.com"]

    @pytest.fixture
    def engine(self, auth, db, settings, identity, quota):
        return _managed_drive(auth, db, settings, quota, self.MANAGERS)

    def test_each_copies_until_its_own_allowance_is_spent(self, engine, auth, db):
        result = engine.run()
        assert (result["files"], result["failed"]) == (4, 0)
        spent = [db.bytes_sent_24h(m) for m in self.MANAGERS]
        assert spent == [200, 200, 0]                  # m3 never needed
        assert db.bytes_sent_24h(TGT_USER) == 0      # a copy is not the target's
        staged = {c["body"]["emailAddress"]
                  for c in auth.target_drive(TGT_USER).calls_to("permissions.create")}
        assert staged == {"m1@tenanta.com", "m2@tenanta.com"}   # only who copied

    def test_google_refusing_one_first_hands_the_file_to_the_next(self, engine, auth, db):
        """The cap reads like a rate limit and outlasts the retry ladder: what the
        manager uploaded itself today counts too, so Google can say no first."""
        main = auth._svcs[("source", "drive", SRC_USER)]
        m1 = type(main)("m1@tenanta.com", "source")
        m1.store, m1.content, m1.shared_drives, m1.peer = (
            main.store, main.content, main.shared_drives, main.peer)
        m1.fail_next("files.copy", status=403, reason="userRateLimitExceeded", times=50)
        auth._svcs[("source", "drive", "m1@tenanta.com")] = m1

        result = engine.run()

        assert (result["files"], result["failed"]) == (4, 0)
        assert db.bytes_sent_24h("m1@tenanta.com") == 200     # spent for the next 24 hours
        assert [db.bytes_sent_24h(m) for m in self.MANAGERS[1:]] == [200, 200]

    def test_with_every_one_spent_the_drive_stops_without_failing_files(self, engine, db):
        from resilience import QuotaExhausted

        engine.copiers = self.MANAGERS[:1]
        with pytest.raises(QuotaExhausted):
            engine.run()
        assert db.conn.execute("SELECT COUNT(*) FROM audit_log WHERE item_type='file' "
                               "AND status LIKE 'FAILED%'").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM id_mapping WHERE type='file'"
                               ).fetchone()[0] == 2

    def test_copiers_are_the_drives_own_users_who_can_copy(self, sd, db):
        from db import bulk_seed_identities

        bulk_seed_identities(db, [(m, m.replace("tenanta", "tenantb"))
                                  for m in ("w@tenanta.com", "o@tenanta.com",
                                            "r@tenanta.com", "c@tenanta.com")])
        sd._members = lambda drive_id, svc=None: [
            {"type": "user", "role": "writer", "emailAddress": "w@tenanta.com"},
            {"type": "user", "role": "reader", "emailAddress": "r@tenanta.com"},
            {"type": "user", "role": "organizer", "emailAddress": "partner@other.com"},
            {"type": "user", "role": "organizer", "emailAddress": "gone@tenanta.com"},
            {"type": "group", "role": "organizer", "emailAddress": "team@tenanta.com"},
            {"type": "user", "role": "commenter", "emailAddress": "c@tenanta.com"},
            {"type": "user", "role": "organizer", "emailAddress": "o@tenanta.com"},
        ]
        assert sd.copiers_for("drv-1") == ["o@tenanta.com", "w@tenanta.com"]

    def test_each_shared_drive_gets_its_own_staging_drive(self, engine, auth, db,
                                                          settings, quota):
        """Two drives migrate at once under the one admin; a shared staging drive was
        deleted by whichever finished first while the other still copied into it."""
        import drive_engine

        other = drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota)
        other.shared_drive = "src-drive-2"
        mine = drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota)
        assert len({engine._staging_drive_name(), other._staging_drive_name(),
                    mine._staging_drive_name()}) == 3
        assert mine._staging_drive_name() == f"{settings.staging_drive_prefix}-alice"


class TestAFileBiggerThanADay:
    """A file bigger than one account's whole daily allowance used to stop its drive (or
    pause its user) on every run: no reservation could ever fit it. It now goes, server-side,
    to an account that has copied nothing yet today -- Google lets an upload past the limit
    finish -- and refused, or with nobody fresh, it is listed for a person while the rest
    of the drive goes on."""

    MANAGERS = TestManagersShareTheDailyCap.MANAGERS

    @pytest.fixture
    def engine(self, auth, db, settings, identity, quota):
        return _managed_drive(auth, db, settings, quota, self.MANAGERS)

    @staticmethod
    def _huge(auth, size=300):
        main = auth._svcs[("source", "drive", SRC_USER)]
        return main, main.add_binary("huge.vmdk", parent="src-drive", data=b"x" * size)

    def _row(self, db, fid):
        return db.get_audit(SRC_USER, fid, "file")

    def test_it_goes_to_an_account_that_has_copied_nothing_today(self, engine, auth, db):
        _, huge = self._huge(auth)
        result = engine.run()
        assert (result["files"], result["failed"]) == (5, 0)
        assert self._row(db, huge)["status"] == "SUCCESS"
        # Four 100-byte files fill two days; the 300-byte one takes a third whole day.
        assert [db.bytes_sent_24h(m) for m in self.MANAGERS] == [200, 200, 200]

    def test_google_refusing_the_copy_sends_it_up_as_an_upload_spending_no_copier(
            self, engine, auth, db):
        """No account may copy more than a day's allowance, but an upload already under way
        may finish past it: refused as a copy, the file goes up through this host instead."""
        import resilience
        from tests.fakes import http_error

        main, huge = self._huge(auth)
        tries = []
        files = type(main).files

        def refusing(svc):
            f = files(svc)
            real = f.copy

            def copy(**kw):
                if kw.get("fileId") == huge:
                    tries.append(1)
                    raise http_error(403, "userRateLimitExceeded", "User rate limit exceeded.")
                return real(**kw)
            f.copy = copy
            return f
        main.files = lambda: refusing(main)

        result = engine.run()

        assert (result["files"], result["failed"]) == (5, 0)
        row = self._row(db, huge)
        assert row["status"] == "SUCCESS" and "uploaded through this host" in row["error_message"]
        assert "Google refused it even from an account with nothing charged" in row["error_message"]
        assert len(tries) == resilience.RATE_LIMIT_RETRY_BUDGET + 1    # one account's ladder
        assert sum(db.bytes_sent_24h(m) for m in self.MANAGERS) == 400  # refunded, none spent
        assert all(kw.get("Range") for kw in main.calls_to("files.get_media"))   # by byte range

    def test_with_nobody_fresh_to_copy_it_it_is_uploaded_and_the_drive_goes_on(
            self, engine, auth, db, settings):
        settings.effective_upload_cap = lambda: 300
        _, huge = self._huge(auth, size=400)
        for m in self.MANAGERS:
            db.add_bytes_sent(m, 1)

        result = engine.run()

        assert result["files"] == 5                 # the drive did not stop, and it went up
        row = self._row(db, huge)
        assert row["status"] == "SUCCESS" and "has copied something today" in \
            row["error_message"]
        assert [db.bytes_sent_24h(m) for m in self.MANAGERS] == [201, 201, 1]

    def test_a_users_own_one_is_streamed_never_saved_to_this_hosts_disk(self, auth, db, settings,
                                                                        identity):
        import drive_engine
        from resilience import DailyQuotaGuard

        settings.transfer_mode = "download_upload"     # saving to disk would be tried first
        settings.effective_upload_cap = lambda: 200
        src = auth._get("source", "drive", SRC_USER)
        huge = src.add_binary("huge.vmdk", data=b"x" * 300)
        db.add_bytes_sent(TGT_USER, 1)
        m = drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER,
                                       DailyQuotaGuard(db, TGT_USER, 200))

        m._download_via = lambda *a, **k: pytest.fail("a file bigger than a day went to disk")

        m.run()

        assert db.get_audit(SRC_USER, huge, "file")["status"] == "SUCCESS"
        assert auth._get("target", "drive", TGT_USER).content[
            db.get_target_id(SRC_USER, huge, "file")] == b"x" * 300
        assert all(kw.get("Range") for kw in src.calls_to("files.get_media"))
        # Google lets the target upload nothing more today; in this mode its guard says so.
        assert DailyQuotaGuard(db, TGT_USER, 200).remaining() == 0
