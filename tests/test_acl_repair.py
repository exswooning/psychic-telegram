"""Grants that genuinely never landed had no way back.

acl_reconcile answers "is this grant actually missing?" and stops -- its own
docstring says it "resolves reporting, never the underlying work". The one
code path that applies ACLs, drive_engine._sync_acls, runs only after a file
is freshly copied, and those files are already mapped, so a re-run skips
them and never reaches the ACL step.

Live: 4,784 grants confirmed absent from the target, on files that had
migrated perfectly, and no command in the tool could put them back.
"""
import acl_repair
import db as dbmod


def _db(tmp_path):
    d = dbmod.MigrationDB(str(tmp_path / "m.db"))
    d.conn.execute("INSERT INTO identity_map(source_email,target_email) "
                   "VALUES('u@src','u@tgt')")
    d.conn.commit()
    return d


class TestOnlyFilesThatExistOnTheTargetAreRepairable:
    def test_a_failed_grant_on_a_migrated_file_is_picked_up(self, tmp_path):
        d = _db(tmp_path)
        d.record_mapping("u@src", "srcfile1", "tgtfile1", "file")
        d.log_audit("u@src", "srcfile1:bob@tgt", "acl", "FAILED",
                    "Quota exceeded")
        work = acl_repair.files_needing_grants(d)
        assert work == {"u@src": [("srcfile1", "tgtfile1")]}
        d.close()

    def test_a_grant_on_a_file_that_never_copied_is_not_claimed(self, tmp_path):
        """The file has to migrate first. Reporting it as repairable is a lie
        the next run has to correct."""
        d = _db(tmp_path)
        d.log_audit("u@src", "ghostfile:bob@tgt", "acl", "FAILED", "Quota")
        assert acl_repair.files_needing_grants(d) == {}
        d.close()

    def test_one_file_with_many_failed_grants_is_visited_once(self, tmp_path):
        """_sync_acls re-reads the whole permission list for a file, so
        visiting it per grantee would multiply the work by the number of
        grantees -- which on this corpus was about 50."""
        d = _db(tmp_path)
        d.record_mapping("u@src", "srcfile1", "tgtfile1", "file")
        for who in ("a@tgt", "b@tgt", "c@tgt"):
            d.log_audit("u@src", f"srcfile1:{who}", "acl", "FAILED", "Quota")
        assert acl_repair.files_needing_grants(d)["u@src"] == [
            ("srcfile1", "tgtfile1")]
        d.close()

    def test_a_succeeded_grant_is_not_revisited(self, tmp_path):
        d = _db(tmp_path)
        d.record_mapping("u@src", "srcfile1", "tgtfile1", "file")
        d.log_audit("u@src", "srcfile1:bob@tgt", "acl", "SUCCESS")
        assert acl_repair.files_needing_grants(d) == {}
        d.close()

    def test_quota_only_narrows_to_throttled_grants(self, tmp_path):
        d = _db(tmp_path)
        d.record_mapping("u@src", "f1", "t1", "file")
        d.record_mapping("u@src", "f2", "t2", "file")
        d.log_audit("u@src", "f1:a@tgt", "acl", "FAILED", "Quota exceeded")
        d.log_audit("u@src", "f2:b@tgt", "acl", "FAILED",
                    "emailAddress is invalid")
        only = acl_repair.files_needing_grants(d, include_quota_only=True)
        assert only == {"u@src": [("f1", "t1")]}
        d.close()


class TestTheNullQuota:
    def test_it_reserves_and_refunds_nothing(self):
        """Re-applying a permission moves no bytes. Charging it against the
        750 GB/day cap would let a repair pass stop a migration."""
        q = acl_repair._NullQuota()
        assert q.reserve(10_000_000) is None
        assert q.refund(10_000_000) is None


class TestDryRunIsTheDefault:
    def test_it_reports_without_touching_anything(self, tmp_path):
        d = _db(tmp_path)
        d.record_mapping("u@src", "srcfile1", "tgtfile1", "file")
        d.log_audit("u@src", "srcfile1:bob@tgt", "acl", "FAILED", "Quota")
        stats = acl_repair.repair(None, d, None, dry_run=True)
        assert stats["files"] == 1
        assert stats["applied"] == 0
        d.close()

    def test_an_empty_ledger_does_no_work(self, tmp_path):
        d = _db(tmp_path)
        stats = acl_repair.repair(None, d, None, dry_run=False)
        assert stats["files"] == 0 and stats["applied"] == 0
        d.close()


class TestAUserWithNoMappingIsCounted:
    def test_it_is_reported_rather_than_silently_dropped(self, tmp_path):
        d = dbmod.MigrationDB(str(tmp_path / "m.db"))
        d.record_mapping("nomap@src", "f1", "t1", "file")
        d.log_audit("nomap@src", "f1:a@tgt", "acl", "FAILED", "Quota")
        stats = acl_repair.repair(None, d, None, dry_run=False)
        assert stats["skipped_no_target"] == 1
        d.close()
