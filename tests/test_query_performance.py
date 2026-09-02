"""Reads that must not degrade into a full table scan.

audit_log is the biggest table in the product -- 1.27M rows on a real
tenant, and it only grows. A read that scans it is fine on a test fixture
and a 502 on a live ledger, which is exactly how it shipped: Mission
Control's own /api/v2/users took 2.67s and Caddy gave up on it.

These assert the SHAPE of the query rather than a timing, because a timing
on a ten-row fixture proves nothing at all.
"""

from __future__ import annotations

import sqlite3

import control_plane_db as cpdb


def _plan(conn, q, args=()):
    return " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + q, args))


class TestLikeCanStillUseAnIndex:
    """LIKE is case-insensitive by default, which disqualifies it from
    SQLite's prefix optimisation -- so `status LIKE 'FAILED%'` scanned the
    whole table a dozen times over."""

    def test_ro_sets_case_sensitive_like(self, tmp_path):
        db = tmp_path / "t.db"
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE audit_log (source_user TEXT, status TEXT, item_type TEXT)")
        c.execute("CREATE INDEX ix ON audit_log(status, item_type)")
        c.commit(); c.close()
        # PRAGMA case_sensitive_like is write-only in SQLite -- it cannot be
        # read back, so the plan IS the assertion. That is the better test
        # anyway: it checks the effect, not the setting.
        with cpdb.ro(str(db)) as conn:
            plan = _plan(conn, "SELECT 1 FROM audit_log WHERE status LIKE 'FAILED%'")
        assert "SEARCH" in plan.upper(), f"LIKE fell back to a full scan: {plan}"


class TestUserProgressIsOnePassNotOnePerUser:
    """It ran one query per identity against audit_counts -- a VIEW that
    groups the whole audit_log by source_user, so 200 users re-grouped 1.27M
    rows 200 times. 2.67s, and a 502 under load."""

    def test_it_does_not_query_per_user(self, tmp_path, monkeypatch):
        db = tmp_path / "t.db"
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE identity_map (source_email TEXT PRIMARY KEY, "
                  "target_email TEXT, status TEXT, services_done TEXT)")
        c.execute("CREATE TABLE audit_log (source_user TEXT, status TEXT, item_type TEXT)")
        c.execute("CREATE TABLE audit_rollup (source_user TEXT, item_type TEXT, "
                  "status TEXT, n INTEGER)")
        for i in range(25):
            c.execute("INSERT INTO identity_map VALUES (?,?,?,?)",
                      (f"u{i}@a.com", f"u{i}@b.com", "DONE", ""))
            c.execute("INSERT INTO audit_log VALUES (?,?,?)", (f"u{i}@a.com", "SUCCESS", "file"))
        c.commit(); c.close()

        real_connect = sqlite3.connect
        counter = {"n": 0}

        class CountingConn(sqlite3.Connection):
            def execute(self, *a, **k):
                counter["n"] += 1
                return super().execute(*a, **k)

        monkeypatch.setattr(sqlite3, "connect",
                            lambda *a, **k: real_connect(*a, factory=CountingConn,
                                                         **{x: y for x, y in k.items()
                                                            if x != "factory"}))
        rows = cpdb.user_progress(str(db))
        assert len(rows) == 25
        # identity read + one grouped rollup + the PRAGMA -- nowhere near 25
        assert counter["n"] < 10, f"{counter['n']} queries for 25 users -- N+1 is back"

    def test_every_user_still_gets_its_own_counts(self, tmp_path):
        db = tmp_path / "t.db"
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE identity_map (source_email TEXT PRIMARY KEY, "
                  "target_email TEXT, status TEXT, services_done TEXT)")
        c.execute("CREATE TABLE audit_log (source_user TEXT, status TEXT, item_type TEXT)")
        c.execute("CREATE TABLE audit_rollup (source_user TEXT, item_type TEXT, "
                  "status TEXT, n INTEGER)")
        c.execute("INSERT INTO identity_map VALUES ('a@x','a@y','DONE','')")
        c.execute("INSERT INTO identity_map VALUES ('b@x','b@y','DONE','')")
        for _ in range(3):
            c.execute("INSERT INTO audit_log VALUES ('a@x','SUCCESS','file')")
        c.execute("INSERT INTO audit_log VALUES ('a@x','FAILED','file')")
        c.execute("INSERT INTO audit_log VALUES ('b@x','SUCCESS','file')")
        # a pruned user's counts live only in the rollup
        c.execute("INSERT INTO audit_rollup VALUES ('b@x','file','SUCCESS',9)")
        c.commit(); c.close()

        by = {r["source_email"]: r for r in cpdb.user_progress(str(db))}
        assert by["a@x"]["itemsDone"] == 3
        assert by["a@x"]["itemsFailed"] == 1
        # rollup must still be counted, or a finished user reads as having
        # migrated nothing
        assert by["b@x"]["itemsDone"] == 10


class TestTheDashboardRollupHasACoveringIndex:
    """tui.collect_snapshot() is called by every SPA payload and polled every
    few seconds by the console. It groups audit_log by
    (source_user, item_type, status) and sums bytes_moved.

    ix_audit_status leads on (source_user, status) and carries neither
    item_type nor bytes_moved, so that grouping sorted the whole table in a
    temp B-tree. On a real 1.27M-row ledger: 8.17s, and every dashboard page
    waited on it.
    """

    def _ledger(self, tmp_path):
        import db as dbmod
        p = tmp_path / "m.db"
        dbmod.MigrationDB(str(p)).close()
        return str(p)

    def test_the_index_exists_with_the_grouping_columns_in_order(self, tmp_path):
        import sqlite3
        conn = sqlite3.connect(self._ledger(tmp_path))
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='ix_audit_rollup_cover'").fetchone()
        assert sql, "the dashboard rollup index is missing"
        cols = sql[0].split("(", 1)[1].rstrip(") ").replace(" ", "")
        # order matters: it is what removes the sort. bytes_moved is what
        # keeps it covering, so the table is never touched.
        assert cols == "source_user,item_type,status,bytes_moved", cols

    def test_the_rollup_query_uses_it_and_does_not_sort(self, tmp_path):
        import sqlite3
        conn = sqlite3.connect(self._ledger(tmp_path))
        plan = " | ".join(r[3] for r in conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT source_user, item_type, status, COUNT(*) n, "
            "COALESCE(SUM(bytes_moved),0) b FROM audit_log "
            "GROUP BY source_user, item_type, status")).upper()
        assert "COVERING INDEX" in plan, plan
        assert "TEMP B-TREE" not in plan, f"still sorting the whole table: {plan}"
