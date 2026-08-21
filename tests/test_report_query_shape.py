"""The ledger must be indexed for the question the dashboard asks.

audit_log carried ix_audit_status(source_user, status) -- the right index
for "how is this mailbox doing" and useless for "how is this migration
doing", which is what every poll asks. Live on a 2.95M-row ledger that
GROUP BY took 8.3 seconds, ran every 5 seconds, and held api_server.py at
44% CPU on a 2-core VPS: more CPU than the migration it was reporting on,
taken from the same two cores.
"""
import sqlite3


class TestTheReportingIndexExists:
    def _cols(self, conn, index):
        return [r[2] for r in conn.execute(f"PRAGMA index_info('{index}')")]

    def test_the_reporting_index_exists(self, tmp_path):
        import db
        d = db.MigrationDB(str(tmp_path / "m.db"))
        with sqlite3.connect(str(tmp_path / "m.db")) as c:
            names = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='audit_log'")]
            assert "ix_audit_status_type" in names
        d.close()

    def test_there_is_only_one_of_them(self, tmp_path):
        """A second (item_type, status) index served the GROUP BY no better
        -- SQLite covers it from this one -- and every redundant index is
        paid for on each of the millions of writes a migration makes."""
        import db
        d = db.MigrationDB(str(tmp_path / "m.db"))
        with sqlite3.connect(str(tmp_path / "m.db")) as c:
            names = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='audit_log'")]
            assert "ix_audit_type_status" not in names
        d.close()

    def test_the_failure_scan_is_indexed(self, tmp_path):
        """The failures panel filters on status first, so it needs the
        columns the other way round -- an index leading on item_type cannot
        serve WHERE status='FAILED'."""
        import db
        d = db.MigrationDB(str(tmp_path / "m.db"))
        with sqlite3.connect(str(tmp_path / "m.db")) as c:
            assert self._cols(c, "ix_audit_status_type") == ["status", "item_type"]
        d.close()

    def test_the_planner_actually_uses_it(self, tmp_path):
        """An index the query planner ignores is disk with a name. Asserted
        through EXPLAIN rather than by trusting that creating it was enough."""
        import db
        d = db.MigrationDB(str(tmp_path / "m.db"))
        with sqlite3.connect(str(tmp_path / "m.db")) as c:
            plan = " ".join(
                str(r) for r in c.execute(
                    "EXPLAIN QUERY PLAN SELECT item_type, status, COUNT(*) "
                    "FROM audit_log GROUP BY 1,2"))
            assert "ix_audit_status_type" in plan, plan
            assert "COVERING INDEX" in plan, plan
        d.close()


class TestPollsShareOneComputation:
    """The dashboard polls the most expensive endpoint in the API every 5
    seconds, and the query behind it took ~20s. Without deduplication each
    poll started the whole aggregate again on its own worker thread, and
    those threads accumulated rather than queued."""

    def _cache(self, ttl=60.0):
        import api_server
        return api_server._SingleFlightCache(ttl=ttl)

    def test_concurrent_callers_compute_once(self):
        import threading
        calls = []
        started = threading.Event()

        def slow():
            calls.append(1)
            started.set()
            import time as t
            t.sleep(0.3)
            return "value"

        cache = self._cache()
        results = []
        threads = [threading.Thread(
            target=lambda: results.append(cache.get("k", slow)))
            for _ in range(8)]
        for t_ in threads:
            t_.start()
        for t_ in threads:
            t_.join()
        assert len(calls) == 1, f"computed {len(calls)} times, expected 1"
        assert results == ["value"] * 8

    def test_a_fresh_value_is_reused(self):
        calls = []
        cache = self._cache()
        for _ in range(5):
            cache.get("k", lambda: calls.append(1) or "v")
        assert len(calls) == 1

    def test_it_recomputes_after_the_ttl(self):
        """Cached forever is not a cache, it is a stale dashboard."""
        import time as t
        calls = []
        cache = self._cache(ttl=0.05)
        cache.get("k", lambda: calls.append(1) or "v")
        t.sleep(0.1)
        cache.get("k", lambda: calls.append(1) or "v")
        assert len(calls) == 2

    def test_different_accounts_do_not_share_a_value(self):
        """Keyed per account: one tenant's report must never be served to
        another, which is a correctness property, not a performance one."""
        cache = self._cache()
        assert cache.get(("d", 1), lambda: "one") == "one"
        assert cache.get(("d", 2), lambda: "two") == "two"

    def test_invalidate_forces_the_next_read_to_recompute(self):
        calls = []
        cache = self._cache()
        cache.get("k", lambda: calls.append(1) or "v")
        cache.invalidate("k")
        cache.get("k", lambda: calls.append(1) or "v")
        assert len(calls) == 2
