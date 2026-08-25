"""audit_log grew to 10,661,866 rows and 6.1 GB on one 818k-item tenant.

10,604,474 of those were SUCCESS -- 99.5% of the database, describing work
id_mapping already proves happened. Zero free pages, so none of it was
reclaimable by VACUUM: all live data. Several tenants of that size share one
VPS under job_admission, and the disk was already at 82%.
"""
import audit_retention
import db as dbmod


def _db(tmp_path, status="DONE"):
    d = dbmod.MigrationDB(str(tmp_path / "m.db"))
    d.conn.execute("INSERT INTO identity_map(source_email,target_email,status) "
                   "VALUES('u@src','u@tgt',?)", (status,))
    d.conn.commit()
    return d


class TestOnlyFinishedUsersAreCollapsed:
    def test_a_done_users_successes_are_collapsible(self, tmp_path):
        d = _db(tmp_path)
        for i in range(5):
            d.log_audit("u@src", f"f{i}", "file", "SUCCESS")
        assert sum(r["n"] for r in audit_retention.prunable(d)) == 5
        d.close()

    def test_a_running_users_rows_are_left_alone(self, tmp_path):
        """A running user's SUCCESS rows are how a resume knows what it
        already did."""
        d = _db(tmp_path, status="RUNNING")
        for i in range(5):
            d.log_audit("u@src", f"f{i}", "file", "SUCCESS")
        assert audit_retention.prunable(d) == []
        d.close()

    def test_a_failed_users_rows_are_left_alone(self, tmp_path):
        d = _db(tmp_path, status="FAILED")
        d.log_audit("u@src", "f1", "file", "SUCCESS")
        assert audit_retention.prunable(d) == []
        d.close()


class TestNonSuccessRowsAreNeverTouched:
    def test_failures_survive(self, tmp_path):
        """They are the entire reason anyone opens this table."""
        d = _db(tmp_path)
        d.log_audit("u@src", "f1", "file", "SUCCESS")
        d.log_audit("u@src", "f2", "file", "FAILED", "boom")
        audit_retention.prune(d, audit_retention.prunable(d), dry_run=False)
        left = d.conn.execute(
            "SELECT status FROM audit_log").fetchall()
        assert [r["status"] for r in left] == ["FAILED"]
        d.close()

    def test_skips_survive(self, tmp_path):
        d = _db(tmp_path)
        d.log_audit("u@src", "f1", "file", "SKIPPED_EXPORT_TOO_LARGE")
        audit_retention.prune(d, audit_retention.prunable(d), dry_run=False)
        assert d.conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"] == 1
        d.close()

    def test_a_row_carrying_modified_time_survives(self, tmp_path):
        """modified_time is the one column a SUCCESS row has that id_mapping
        does not -- the delta pass reads it to decide whether a source item
        changed since it was copied."""
        d = _db(tmp_path)
        d.log_audit("u@src", "f1", "file", "SUCCESS",
                    modified_time="2026-01-01T00:00:00Z")
        assert audit_retention.prunable(d) == []
        d.close()


class TestTheCountSurvivesThePrune:
    def test_counts_move_to_the_rollup(self, tmp_path):
        d = _db(tmp_path)
        for i in range(7):
            d.log_audit("u@src", f"f{i}", "file", "SUCCESS")
        audit_retention.prune(d, audit_retention.prunable(d), dry_run=False)
        n = d.conn.execute(
            "SELECT SUM(n) c FROM audit_counts WHERE status='SUCCESS'"
        ).fetchone()["c"]
        assert n == 7, "the view must still report every success"
        d.close()

    def test_a_pruned_user_is_not_demoted_as_false_done(self, tmp_path):
        """The sharpest failure mode: finished_but_unmapped demotes a DONE
        user with no SUCCESS rows, so a prune that forgot the count would
        mark every finished user as failed."""
        d = _db(tmp_path)
        d.record_mapping("u@src", "s1", "t1", "file")
        d.log_audit("u@src", "s1", "file", "SUCCESS")
        d.log_audit("u@src", "x1", "file", "FAILED", "boom")
        audit_retention.prune(d, audit_retention.prunable(d), dry_run=False)
        # Mapping still present, so it is not a false-DONE either way; the
        # check must also see the success count through the view.
        n = d.conn.execute(
            "SELECT SUM(n) c FROM audit_counts WHERE source_user='u@src' "
            "AND status='SUCCESS'").fetchone()["c"]
        assert n == 1
        d.close()

    def test_pruning_twice_accumulates_rather_than_overwrites(self, tmp_path):
        d = _db(tmp_path)
        for i in range(3):
            d.log_audit("u@src", f"a{i}", "file", "SUCCESS")
        audit_retention.prune(d, audit_retention.prunable(d), dry_run=False)
        for i in range(4):
            d.log_audit("u@src", f"b{i}", "file", "SUCCESS")
        audit_retention.prune(d, audit_retention.prunable(d), dry_run=False)
        n = d.conn.execute(
            "SELECT SUM(n) c FROM audit_counts WHERE status='SUCCESS'"
        ).fetchone()["c"]
        assert n == 7
        d.close()

    def test_a_dry_run_changes_nothing(self, tmp_path):
        d = _db(tmp_path)
        for i in range(4):
            d.log_audit("u@src", f"f{i}", "file", "SUCCESS")
        audit_retention.prune(d, audit_retention.prunable(d), dry_run=True)
        assert d.conn.execute(
            "SELECT COUNT(*) c FROM audit_log").fetchone()["c"] == 4
        d.close()


class TestTheGuard:
    def test_it_passes_when_counts_are_intact(self, tmp_path):
        d = _db(tmp_path)
        d.record_mapping("u@src", "s1", "t1", "file")
        d.log_audit("u@src", "s1", "file", "SUCCESS")
        audit_retention.prune(d, audit_retention.prunable(d), dry_run=False)
        ok, _ = audit_retention.counts_match(d)
        assert ok
        d.close()

    def test_it_fails_when_a_count_is_lost(self, tmp_path):
        """A lost count looks exactly like fewer successes than mappings."""
        d = _db(tmp_path)
        for i in range(5):
            d.record_mapping("u@src", f"s{i}", f"t{i}", "file")
            d.log_audit("u@src", f"s{i}", "file", "SUCCESS")
        with d.write() as conn:
            conn.execute("DELETE FROM audit_log")
        ok, detail = audit_retention.counts_match(d)
        assert not ok and "lost" in detail
        d.close()


class TestAResetClearsThePrunedCountsToo:
    """audit_rollup holds counts of SUCCESS rows already pruned out of
    audit_log, and audit_counts sums both. A Drive reset that cleared
    audit_log's drive rows but left the rollup would leave the view
    reporting successes for work the reset just declared un-migrated."""

    def test_resetting_drive_clears_the_drive_rollup(self, tmp_path):
        import reset_drive_ledger
        d = _db(tmp_path)
        d.record_mapping("u@src", "s1", "t1", "file")
        d.log_audit("u@src", "s1", "file", "SUCCESS")
        audit_retention.prune(d, audit_retention.prunable(d), dry_run=False)
        assert d.conn.execute(
            "SELECT SUM(n) c FROM audit_counts WHERE status='SUCCESS'"
        ).fetchone()["c"] == 1

        reset_drive_ledger.reset_service_ledger(d, "u@src", ("drive",))
        left = d.conn.execute(
            "SELECT COALESCE(SUM(n),0) c FROM audit_counts "
            "WHERE status='SUCCESS'").fetchone()["c"]
        assert left == 0, "a reset must not leave counts for work it undid"
        d.close()

    def test_resetting_drive_leaves_a_gmail_rollup_alone(self, tmp_path):
        """Narrowness is the point -- clearing more would discard a completed
        service's record and make the next run redo work that is on target."""
        import reset_drive_ledger
        d = _db(tmp_path)
        d.log_audit("u@src", "m1", "message", "SUCCESS")
        audit_retention.prune(d, audit_retention.prunable(d), dry_run=False)
        reset_drive_ledger.reset_service_ledger(d, "u@src", ("drive",))
        assert d.conn.execute(
            "SELECT SUM(n) c FROM audit_counts WHERE status='SUCCESS'"
        ).fetchone()["c"] == 1
        d.close()
