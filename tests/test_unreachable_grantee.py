"""A grant that cannot be recreated silently, at all.

Drive refuses to grant access to an address with no Google account unless
the request also emails them. This migration sends
sendNotificationEmail=False on purpose -- a tenant move should not mail every
collaborator a share notification -- so such a grant cannot be recreated.

That is a Google constraint, not a failure of this tool, and it was the ONLY
thing 134 of one run's 422 failures turned out to be: every one for a single
external address on a non-Google domain. Recorded as FAILED it read like
something to fix. It is something to decide about -- and still a real loss of
access, so it is a named skip rather than a silent one.
"""
import drive_engine


class TestDetection:
    NO_ACCOUNT = ('<HttpError 400> "You are trying to invite a@elsewhere.com. '
                  'Since there is no Google account associated with this '
                  'email address, you must check the "Notify people" box."')

    def test_it_recognises_the_singular_form(self):
        assert drive_engine._is_unreachable_grantee(
            Exception(self.NO_ACCOUNT)) is True

    def test_it_recognises_the_plural_form(self):
        assert drive_engine._is_unreachable_grantee(Exception(
            "there are no Google accounts associated with these email "
            "addresses")) is True

    def test_a_quota_error_is_not_swept_in(self):
        """Quota failures are retryable and must keep counting as failures."""
        assert drive_engine._is_unreachable_grantee(
            Exception("Quota exceeded for quota metric 'Queries'")) is False

    def test_a_permission_error_is_not_swept_in(self):
        assert drive_engine._is_unreachable_grantee(
            Exception("insufficientFilePermissions")) is False


class TestItIsRecordedAsANamedSkip:
    def _migrator(self, auth, db, settings):
        class Q:
            def reserve(self, n): pass
            def refund(self, n): pass
        return drive_engine.DriveMigrator(
            auth, db, settings, "u@src", "u@tgt", Q())

    def test_the_row_says_what_was_lost_and_why(self, auth, db, settings,
                                                monkeypatch):
        m = self._migrator(auth, db, settings)
        monkeypatch.setattr(
            m, "_retry",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError(
                "there is no Google account associated with this email address")))
        m._create_permission("tgt1", {"type": "user", "role": "reader"},
                             "file1:a@elsewhere.com")
        row = db.get_audit("u@src", "file1:a@elsewhere.com", "acl")
        assert row["status"] == "SKIPPED_GRANTEE_NOT_ON_GOOGLE"
        assert "does not send share notifications" in row["error_message"]

    def test_it_does_not_count_as_an_acl_failure(self, auth, db, settings,
                                                 monkeypatch):
        """Mixing a constraint into the failure count is how a clean run
        teaches people to ignore red."""
        m = self._migrator(auth, db, settings)
        monkeypatch.setattr(
            m, "_retry",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError(
                "no Google account associated with this email address")))
        before = m.stats.get("acl_failed", 0)
        m._create_permission("tgt1", {"type": "user", "role": "reader"},
                             "file1:a@elsewhere.com")
        assert m.stats.get("acl_failed", 0) == before

    def test_a_real_failure_is_still_recorded_as_one(self, auth, db, settings,
                                                     monkeypatch):
        m = self._migrator(auth, db, settings)
        monkeypatch.setattr(
            m, "_retry",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Quota exceeded")))
        m._create_permission("tgt1", {"type": "user", "role": "reader"},
                             "file1:b@x.com")
        row = db.get_audit("u@src", "file1:b@x.com", "acl")
        assert row["status"] == "FAILED"
