"""A finished run's failure count is not one number.

Live on 201 users it was 119,600, across three causes needing three
different responses, reported as a single red figure that made a migration
whose Drive data was intact look broken:

  55,807  ACL grants refused because the grantee had no account -- recorded
          in a 21-minute window when the target accounts had been deleted.
  27,597  ACL grants refused for quota. Most land anyway; a previous
          reconcile resolved 124,303 of 127,852 as already present.
  32,967  Gmail messages rejected as "Invalid label", from label_map
          pointing into a mailbox that had been recreated.
"""
import db as dbmod
import repair


def _db(tmp_path):
    d = dbmod.MigrationDB(str(tmp_path / "m.db"))
    d.conn.execute("INSERT INTO identity_map(source_email,target_email) "
                   "VALUES('u@src','u@tgt')")
    d.conn.commit()
    return d


class FakeDirectory:
    def __init__(self, existing):
        self.existing = existing
        self.calls = 0

    def users(self):
        return self

    def get(self, userKey, fields=None):
        self.calls += 1
        outer = self

        class _R:
            def execute(self):
                if userKey not in outer.existing:
                    raise RuntimeError("<HttpError 404 ... Resource Not Found>")
                return {"primaryEmail": userKey}
        return _R()


class TestSurveyNamesTheFamilies:
    def test_it_separates_the_three_causes(self, tmp_path):
        d = _db(tmp_path)
        d.log_audit("u@src", "f1:a@tgt", "acl", "FAILED",
                    "Bad Request ... no Google accounts associated with these "
                    "email addresses ...")
        d.log_audit("u@src", "f2:b@tgt", "acl", "FAILED",
                    "Quota exceeded for quota metric 'Queries'")
        d.log_audit("u@src", "m1", "message", "FAILED",
                    'HTTP 400 (invalidArgument): "Invalid label"')
        s = repair.survey(d)
        assert s["total"] == 3
        assert s["acl_no_account"] == 1
        assert s["acl_quota"] == 1
        assert s["gmail_invalid_label"] == 1
        d.close()

    def test_an_unclassified_failure_still_counts_in_the_total(self, tmp_path):
        """A survey that only counts what it recognises reports a smaller,
        friendlier number than the truth."""
        d = _db(tmp_path)
        d.log_audit("u@src", "x1", "file", "FAILED", "something new")
        s = repair.survey(d)
        assert s["total"] == 1
        assert s["acl_no_account"] == 0
        d.close()


class TestStaleGranteeDetection:
    def _seed(self, tmp_path, grantee):
        d = _db(tmp_path)
        d.log_audit("u@src", f"file1:{grantee}", "acl", "FAILED",
                    "Bad Request ... no Google accounts associated with these "
                    "email addresses ...")
        return d

    def test_a_grantee_that_exists_now_is_reported(self, tmp_path):
        d = self._seed(tmp_path, "dara@tgt")
        rows = repair.stale_grantee_failures(d, FakeDirectory({"dara@tgt"}))
        assert len(rows) == 1
        d.close()

    def test_a_grantee_that_still_does_not_exist_is_left_alone(self, tmp_path):
        """That failure is real and current -- resolving it would hide it."""
        d = self._seed(tmp_path, "ghost@tgt")
        assert repair.stale_grantee_failures(d, FakeDirectory(set())) == []
        d.close()

    def test_without_a_directory_it_claims_nothing(self, tmp_path):
        """"The mailbox exists today" is the whole claim. Guessing it is how
        a real failure gets hidden."""
        d = self._seed(tmp_path, "dara@tgt")
        assert repair.stale_grantee_failures(d, None) == []
        d.close()

    def test_each_grantee_is_asked_about_once(self, tmp_path):
        """55,807 rows across a few hundred grantees must not be 55,807
        Directory calls."""
        d = _db(tmp_path)
        for i in range(40):
            d.log_audit("u@src", f"file{i}:dara@tgt", "acl", "FAILED",
                        "no Google accounts associated with these email "
                        "addresses")
        api = FakeDirectory({"dara@tgt"})
        rows = repair.stale_grantee_failures(d, api)
        assert len(rows) == 40
        assert api.calls == 1
        d.close()


class TestResolvePreservesHistory:
    def _stale(self, tmp_path):
        d = _db(tmp_path)
        d.log_audit("u@src", "file1:dara@tgt", "acl", "FAILED",
                    "no Google accounts associated with these email addresses")
        return d, repair.stale_grantee_failures(d, FakeDirectory({"dara@tgt"}))

    def test_a_dry_run_changes_nothing(self, tmp_path):
        d, rows = self._stale(tmp_path)
        assert repair.resolve(d, rows, "X", "note", dry_run=True) == 1
        assert repair.survey(d)["acl_no_account"] == 1
        d.close()

    def test_applying_clears_the_failure(self, tmp_path):
        d, rows = self._stale(tmp_path)
        repair.resolve(d, rows, "SKIPPED_GRANTEE_RECREATED", "note",
                       dry_run=False)
        assert repair.survey(d)["total"] == 0
        d.close()

    def test_the_row_survives_with_its_new_status(self, tmp_path):
        """Not a delete: the audit row is the record that this was attempted,
        and a migration that erases its own history cannot explain itself."""
        d, rows = self._stale(tmp_path)
        repair.resolve(d, rows, "SKIPPED_GRANTEE_RECREATED", "why", False)
        row = d.conn.execute(
            "SELECT status, error_message FROM audit_log "
            "WHERE item_id='file1:dara@tgt'").fetchone()
        assert row["status"] == "SKIPPED_GRANTEE_RECREATED"
        assert "why" in (row["error_message"] or "")
        d.close()


class TestPatternsMatchWhatGoogleActuallySends:
    """Drive writes the grantee-missing 400 two ways: plural for several
    grantees, singular for one. A pattern written from a single observed
    message caught only the plural, leaving 2,900 rows of a known cause
    sitting in "unclassified" where they read as an unknown problem."""

    def _with(self, tmp_path, msg):
        d = _db(tmp_path)
        d.log_audit("u@src", "f1:a@tgt", "acl", "FAILED", msg)
        return d

    def test_the_plural_form_is_recognised(self, tmp_path):
        d = self._with(tmp_path,
                       'You are trying to invite a@t, b@t. Since there are no '
                       'Google accounts associated with these email addresses, '
                       'you must check the "Notify people" box')
        assert repair.survey(d)["acl_no_account"] == 1
        d.close()

    def test_the_singular_form_is_recognised(self, tmp_path):
        d = self._with(tmp_path,
                       'You are trying to invite a@t. Since there is no '
                       'Google account associated with this email address, '
                       'you must check the "Notify people" box')
        assert repair.survey(d)["acl_no_account"] == 1
        d.close()

    def test_an_unrelated_400_is_not_swept_in(self, tmp_path):
        d = self._with(tmp_path, "Bad Request. User message: something else")
        assert repair.survey(d)["acl_no_account"] == 0
        d.close()


class TestOnlyASuccessfulLookupConfirmsAnAccount:
    """The first version treated anything that was not a 404 as confirmation,
    turning a 403 -- an address this admin may not query -- into "the account
    is back", and would have marked a real failure resolved on a permission
    error. Live, the directory pass emitted exactly that 403."""

    def _seed(self, tmp_path):
        d = _db(tmp_path)
        d.log_audit("u@src", "file1:x@tgt", "acl", "FAILED",
                    "no Google account associated with this email address")
        return d

    def test_a_403_does_not_confirm_the_account(self, tmp_path):
        class Forbidden(FakeDirectory):
            def get(self, userKey, fields=None):
                class _R:
                    def execute(self):
                        raise RuntimeError("<HttpError 403 ... forbidden>")
                return _R()

        d = self._seed(tmp_path)
        assert repair.stale_grantee_failures(d, Forbidden(set())) == []
        d.close()

    def test_a_network_error_does_not_confirm_the_account(self, tmp_path):
        class Broken(FakeDirectory):
            def get(self, userKey, fields=None):
                class _R:
                    def execute(self):
                        raise RuntimeError("connection reset")
                return _R()

        d = self._seed(tmp_path)
        assert repair.stale_grantee_failures(d, Broken(set())) == []
        d.close()

    def test_a_real_lookup_still_confirms(self, tmp_path):
        d = self._seed(tmp_path)
        assert len(repair.stale_grantee_failures(
            d, FakeDirectory({"x@tgt"}))) == 1
        d.close()


class TestRunAllFixesOnlyWhatItCanConfirm:
    """Runs at the end of every migration, so the count an operator reads is
    the residue that needs them. Live those differed by about 91,000."""

    class _Auth:
        def __init__(self, existing=()):
            self._d = FakeDirectory(set(existing))

        def directory(self, tenant):
            return self._d

    def _db_with(self, tmp_path, **kinds):
        d = _db(tmp_path)
        for i in range(kinds.get("no_account", 0)):
            d.log_audit("u@src", f"f{i}:dara@tgt", "acl", "FAILED",
                        "no Google account associated with this email address")
        for i in range(kinds.get("label", 0)):
            d.log_audit("u@src", f"m{i}", "message", "FAILED",
                        'HTTP 400 (invalidArgument): "Invalid label"')
        return d

    def test_it_resolves_confirmed_stale_grantees(self, tmp_path):
        d = self._db_with(tmp_path, no_account=3)
        out = repair.run_all(d, self._Auth({"dara@tgt"}), None, apply=True)
        assert out["resolved"] == 3
        assert repair.survey(d)["acl_no_account"] == 0
        d.close()

    def test_it_leaves_gmail_label_failures_alone(self, tmp_path):
        """They are repaired at their source and re-inserted by the next
        pass. Rewriting their audit rows here would report them fixed before
        the data had moved."""
        d = self._db_with(tmp_path, label=4)
        repair.run_all(d, self._Auth(), None, apply=True)
        assert repair.survey(d)["gmail_invalid_label"] == 4
        d.close()

    def test_a_dry_run_changes_nothing(self, tmp_path):
        d = self._db_with(tmp_path, no_account=2)
        repair.run_all(d, self._Auth({"dara@tgt"}), None, apply=False)
        assert repair.survey(d)["acl_no_account"] == 2
        d.close()

    def test_a_broken_directory_does_not_raise(self, tmp_path):
        """A repair pass that can break the migration it follows is worse
        than no repair pass."""
        class Broken:
            def directory(self, tenant):
                raise RuntimeError("network down")

        d = self._db_with(tmp_path, no_account=2)
        out = repair.run_all(d, Broken(), None, apply=True)
        assert out["errors"]
        assert repair.survey(d)["acl_no_account"] == 2
        d.close()

    def test_a_clean_ledger_does_no_work(self, tmp_path):
        d = _db(tmp_path)
        out = repair.run_all(d, self._Auth(), None, apply=True)
        assert out["resolved"] == 0 and not out["errors"]
        d.close()


class TestSummaryLine:
    def test_it_names_what_happened(self):
        line = repair.summarise({
            "survey": {"total": 119600, "gmail_invalid_label": 32967},
            "resolved": 58080, "reconciled": 24000, "errors": []})
        assert "119,600" in line and "58,080" in line and "24,000" in line

    def test_a_clean_run_says_so(self):
        assert "no failed items" in repair.summarise({"survey": {"total": 0}})

    def test_errors_are_surfaced_not_swallowed(self):
        line = repair.summarise({"survey": {"total": 5}, "errors": ["boom"]})
        assert "boom" in line


class TestStaleUserFailures:
    """A per-user failure is recorded when the whole user could not be
    started -- almost always a failed impersonation. Live, 175 read
    "invalid_grant: Invalid email or User ID", every one written while that
    target account was deleted. The users migrated fine on a later pass and
    are DONE now, but the rows stayed and kept them in the report's
    did-not-migrate list."""

    def _db_with_user(self, tmp_path, status):
        d = dbmod.MigrationDB(str(tmp_path / "m.db"))
        d.conn.execute("INSERT INTO identity_map(source_email,target_email,"
                       "status) VALUES('u@src','u@tgt',?)", (status,))
        d.conn.commit()
        # Mappings matter: a DONE user with a failure and NOTHING migrated is
        # a false-DONE, which is a different finding and gets demoted rather
        # than resolved. This fixture is the other case -- the user really did
        # migrate, on a later pass, and an old failure row survived.
        d.record_mapping("u@src", "s1", "t1", "file")
        d.log_audit("u@src", "u@src", "user", "FAILED",
                    "('invalid_grant: Invalid email or User ID', ...)")
        return d

    def test_a_user_that_later_migrated_is_resolvable(self, tmp_path):
        d = self._db_with_user(tmp_path, "DONE")
        assert len(repair.stale_user_failures(d)) == 1
        assert repair.survey(d)["user_stale"] == 1
        d.close()

    def test_a_user_that_never_migrated_is_left_alone(self, tmp_path):
        """That failure is the current, true state of the user."""
        d = self._db_with_user(tmp_path, "FAILED")
        assert repair.stale_user_failures(d) == []
        d.close()

    def test_a_still_running_user_is_left_alone(self, tmp_path):
        d = self._db_with_user(tmp_path, "RUNNING")
        assert repair.stale_user_failures(d) == []
        d.close()

    def test_resolving_clears_it_and_keeps_the_row(self, tmp_path):
        d = self._db_with_user(tmp_path, "DONE")
        repair.resolve_users(d, repair.stale_user_failures(d), dry_run=False)
        row = d.conn.execute(
            "SELECT status FROM audit_log WHERE item_type='user'").fetchone()
        assert row["status"] == "SKIPPED_USER_LATER_MIGRATED"
        assert repair.survey(d)["user_stale"] == 0
        d.close()

    def test_a_dry_run_changes_nothing(self, tmp_path):
        d = self._db_with_user(tmp_path, "DONE")
        assert repair.resolve_users(d, repair.stale_user_failures(d),
                                    dry_run=True) == 1
        assert repair.survey(d)["user_stale"] == 1
        d.close()

    def test_it_needs_no_network(self, tmp_path):
        """identity_map.status is what the engine itself writes when a user
        finishes, so it already answers this."""
        d = self._db_with_user(tmp_path, "DONE")
        out = repair.run_all(d, None, None, apply=True)
        assert out["users_resolved"] == 1
        d.close()


class TestUsersMarkedDoneThatMigratedNothing:
    """"Done" has to mean the work happened.

    Live, seeduser382 finished with zero id_mapping rows, zero SUCCESS rows
    and one HTTP 401 -- and the report read "201 done, 0 users failed". A
    user whose every attempt failed was counted as a success, in the one
    number an operator trusts to decide a migration is finished.
    """

    def _user(self, tmp_path, status="DONE", mappings=0, failures=1):
        d = dbmod.MigrationDB(str(tmp_path / "m.db"))
        d.conn.execute("INSERT INTO identity_map(source_email,target_email,"
                       "status) VALUES('u@src','u@tgt',?)", (status,))
        d.conn.commit()
        for i in range(mappings):
            d.record_mapping("u@src", f"s{i}", f"t{i}", "file")
        for i in range(failures):
            d.log_audit("u@src", f"x{i}", "user", "FAILED", "HTTP 401 authError")
        return d

    def test_nothing_migrated_plus_a_failure_is_not_done(self, tmp_path):
        d = self._user(tmp_path)
        assert len(repair.false_done_users(d)) == 1
        assert repair.survey(d)["false_done"] == 1
        d.close()

    def test_an_empty_mailbox_with_no_failures_stays_done(self, tmp_path):
        """A genuinely empty mailbox migrates nothing and that is a correct
        DONE. What makes it wrong is nothing migrated AND something failed."""
        d = self._user(tmp_path, mappings=0, failures=0)
        assert repair.false_done_users(d) == []
        d.close()

    def test_a_user_that_migrated_something_stays_done(self, tmp_path):
        """Partial failure is normal -- most runs have some."""
        d = self._user(tmp_path, mappings=5, failures=3)
        assert repair.false_done_users(d) == []
        d.close()

    def test_demoting_carries_the_real_reason(self, tmp_path):
        d = self._user(tmp_path)
        repair.demote_false_done(d, repair.false_done_users(d), dry_run=False)
        row = d.conn.execute(
            "SELECT status, notes FROM identity_map").fetchone()
        assert row["status"] == "FAILED"
        assert "migrated nothing" in row["notes"]
        assert "401" in row["notes"], "the cause must survive the demotion"
        d.close()

    def test_a_dry_run_changes_nothing(self, tmp_path):
        d = self._user(tmp_path)
        assert repair.demote_false_done(
            d, repair.false_done_users(d), dry_run=True) == 1
        assert d.conn.execute(
            "SELECT status FROM identity_map").fetchone()["status"] == "DONE"
        d.close()

    def test_demotion_runs_before_the_stale_user_pass(self, tmp_path):
        """stale_user_failures keys off status == DONE. Demoting second would
        let a user that migrated nothing have its own failure row resolved as
        "migrated on a later pass" -- erasing the evidence."""
        d = self._user(tmp_path)
        out = repair.run_all(d, None, None, apply=True)
        assert out.get("demoted") == 1
        assert d.conn.execute(
            "SELECT status FROM audit_log WHERE item_type='user'"
        ).fetchone()["status"] == "FAILED"
        d.close()


class TestManualAndAutomaticRepairAgree:
    """cmd_repair once had its own copy of the fix-up sequence, and the copies
    drifted immediately: the false-DONE demotion was added to run_all, and
    `repair --apply` kept reporting the finding on every run without ever
    applying it. The finding was printed, the fix never happened, and the
    output gave no hint of the difference."""

    def test_the_cli_applies_every_repair_run_all_does(self):
        """Asserted against the source, because the failure mode is a second
        implementation that looks right and does less."""
        import inspect

        import main
        src = inspect.getsource(main.cmd_repair)
        assert "run_all" in src, (
            "cmd_repair must delegate to repair.run_all, not reimplement it")
        for own in ("resolve_users(", "demote_false_done(",
                    "stale_grantee_failures("):
            assert f"repair.{own}" not in src, (
                f"cmd_repair calls repair.{own} directly, which is how the "
                f"two paths drifted before")


class TestBrokenFolderShares:
    """A folder's share is what everything inside inherits, so a failed
    folder grant takes every file in that folder with it -- where the old
    per-file recreation left each file holding its own copy. The raw failure
    count says nothing about that difference: live, 265 folder-grant failures
    across 147 folders sat in the same total as 142 file-grant failures,
    while gating 1,050 files against those files' own 142."""

    def _db(self, tmp_path):
        d = dbmod.MigrationDB(str(tmp_path / "m.db"))
        d.conn.execute("INSERT INTO identity_map(source_email,target_email) "
                       "VALUES('u@src','u@tgt')")
        d.conn.commit()
        return d

    def test_it_counts_the_files_behind_a_failed_folder(self, tmp_path):
        d = self._db(tmp_path)
        d.record_mapping("u@src", "dir1", "tD", "folder")
        for i in range(4):
            d.record_mapping("u@src", f"f{i}", f"t{i}", "file",
                             parent_target_id="tD")
        d.log_audit("u@src", "dir1:g@x", "acl", "FAILED", "Quota")
        out = repair.broken_folder_grants(d)
        assert out["folders"] == 1
        assert out["files_behind"] == 4
        d.close()

    def test_a_failed_file_grant_is_not_counted_as_a_door(self, tmp_path):
        """A file's own failed grant costs that one file, not a folder's
        worth -- conflating them is what hid the difference."""
        d = self._db(tmp_path)
        d.record_mapping("u@src", "f1", "t1", "file")
        d.log_audit("u@src", "f1:g@x", "acl", "FAILED", "Quota")
        assert repair.broken_folder_grants(d)["folders"] == 0
        d.close()

    def test_a_clean_ledger_reports_none(self, tmp_path):
        d = self._db(tmp_path)
        assert repair.broken_folder_grants(d) == {
            "folders": 0, "grants": 0, "files_behind": 0}
        d.close()

    def test_run_all_reports_them(self, tmp_path):
        d = self._db(tmp_path)
        d.record_mapping("u@src", "dir1", "tD", "folder")
        d.record_mapping("u@src", "f1", "t1", "file", parent_target_id="tD")
        d.log_audit("u@src", "dir1:g@x", "acl", "FAILED", "Quota")
        out = repair.run_all(d, None, None, apply=False)
        assert out["doors"]["folders"] == 1
        assert "folder share" in repair.summarise(out)
        d.close()
