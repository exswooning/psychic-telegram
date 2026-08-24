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
