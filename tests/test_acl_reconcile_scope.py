"""The reconciler's job is to retire failures the target has since satisfied.

It resolved 0 of 273 on a live ledger while a sample of eight showed the
target holding every grant. Two reasons, both in how it looked the item up.
"""
import acl_reconcile
import db as dbmod


class FakeFiles:
    def __init__(self, perms):
        self._perms = perms

    def get(self, fileId=None, fields=None, supportsAllDrives=None):
        class _R:
            def __init__(self, p):
                self._p = p

            def execute(self):
                return {"permissions": self._p}
        return _R(self._perms.get(fileId, []))


class FakeDrive:
    def __init__(self, perms):
        self._f = FakeFiles(perms)

    def files(self):
        return self._f


class FakeAuth:
    """Each target user sees only their own copy's permissions."""

    def __init__(self, by_user):
        self.by_user = by_user

    def target_drive(self, user):
        return FakeDrive(self.by_user.get(user, {}))


def _db(tmp_path):
    d = dbmod.MigrationDB(str(tmp_path / "m.db"))
    d.conn.execute("INSERT INTO identity_map(source_email,target_email) "
                   "VALUES('a@src','a@tgt')")
    d.conn.execute("INSERT INTO identity_map(source_email,target_email) "
                   "VALUES('b@src','b@tgt')")
    d.conn.commit()
    return d


class TestFoldersCount:
    def test_a_folder_grant_is_reconciled_not_skipped(self, tmp_path):
        # Sharing is applied at folder level here, so restricting the lookup
        # to type='file' made every surviving failure invisible.
        d = _db(tmp_path)
        d.record_mapping("a@src", "SRCF", "TGTF", "folder")
        d.log_audit("a@src", "SRCF:x@t.com", "acl", "FAILED", "Quota exceeded")
        auth = FakeAuth({"a@tgt": {"TGTF": [{"emailAddress": "x@t.com"}]}})
        stats = acl_reconcile.reconcile(auth, d, None)
        assert stats["resolved"] == 1
        assert stats["unreadable"] == 0
        d.close()

    def test_a_file_grant_still_reconciles(self, tmp_path):
        d = _db(tmp_path)
        d.record_mapping("a@src", "SRC1", "TGT1", "file")
        d.log_audit("a@src", "SRC1:x@t.com", "acl", "FAILED", "Quota exceeded")
        auth = FakeAuth({"a@tgt": {"TGT1": [{"emailAddress": "x@t.com"}]}})
        assert acl_reconcile.reconcile(auth, d, None)["resolved"] == 1
        d.close()

    def test_a_grant_the_target_lacks_stays_failed(self, tmp_path):
        d = _db(tmp_path)
        d.record_mapping("a@src", "SRCF", "TGTF", "folder")
        d.log_audit("a@src", "SRCF:x@t.com", "acl", "FAILED", "Quota exceeded")
        auth = FakeAuth({"a@tgt": {"TGTF": [{"emailAddress": "other@t.com"}]}})
        stats = acl_reconcile.reconcile(auth, d, None)
        assert stats["resolved"] == 0 and stats["still_failed"] == 1
        d.close()


class TestOneUsersCopyCannotVouchForAnothers:
    """A Drive id is the same id in every drive it is shared into."""

    def test_two_users_failing_on_one_shared_id_are_checked_separately(
            self, tmp_path):
        d = _db(tmp_path)
        d.record_mapping("a@src", "SHARED", "TGT_A", "folder")
        d.record_mapping("b@src", "SHARED", "TGT_B", "folder")
        d.log_audit("a@src", "SHARED:x@t.com", "acl", "FAILED", "Quota exceeded")
        d.log_audit("b@src", "SHARED:x@t.com", "acl", "FAILED", "Quota exceeded")
        # a's copy has the grant; b's does not. Keyed by file alone, b's
        # failure was resolved on the strength of a's copy.
        auth = FakeAuth({
            "a@tgt": {"TGT_A": [{"emailAddress": "x@t.com"}]},
            "b@tgt": {"TGT_B": []},
        })
        stats = acl_reconcile.reconcile(auth, d, None)
        assert stats["resolved"] == 1, "only a@src's grant is actually present"
        assert stats["still_failed"] == 1, "b@src's is genuinely missing"
        d.close()

    def test_each_user_is_looked_up_in_their_own_mapping(self, tmp_path):
        d = _db(tmp_path)
        d.record_mapping("a@src", "SHARED", "TGT_A", "folder")
        d.record_mapping("b@src", "SHARED", "TGT_B", "folder")
        d.log_audit("b@src", "SHARED:x@t.com", "acl", "FAILED", "Quota exceeded")
        auth = FakeAuth({"b@tgt": {"TGT_B": [{"emailAddress": "x@t.com"}]}})
        assert acl_reconcile.reconcile(auth, d, None)["resolved"] == 1
        d.close()


class TestReportingOnly:
    def test_a_dry_run_leaves_the_row_failed(self, tmp_path):
        d = _db(tmp_path)
        d.record_mapping("a@src", "SRCF", "TGTF", "folder")
        d.log_audit("a@src", "SRCF:x@t.com", "acl", "FAILED", "Quota exceeded")
        auth = FakeAuth({"a@tgt": {"TGTF": [{"emailAddress": "x@t.com"}]}})
        acl_reconcile.reconcile(auth, d, None, dry_run=True)
        row = d.conn.execute(
            "SELECT status FROM audit_log WHERE item_id='SRCF:x@t.com'"
        ).fetchone()
        assert row["status"] == "FAILED"
        d.close()

    def test_an_unmigrated_item_is_not_resolved(self, tmp_path):
        # No mapping means the item never copied; its grant failure is real.
        d = _db(tmp_path)
        d.log_audit("a@src", "GONE:x@t.com", "acl", "FAILED", "Quota exceeded")
        stats = acl_reconcile.reconcile(FakeAuth({}), d, None)
        assert stats["resolved"] == 0 and stats["unreadable"] == 1
        d.close()
