"""
tests/test_benchmark_run.py
===========================
The benchmark's verdict function.

This is the component that decides whether a run is allowed to be called a
result, and it had no tests at all. That is how B5's first attempt was
reported as PASS: the engine died after 17 seconds with SIGABRT having
copied 0 files, and every fidelity gate -- extra grants, fidelity percent,
missing files, checksums -- passed, because each is a statement about what
landed on the target and nothing had.

A judge that cannot fail an empty run is not a safety net. These tests pin
the two gates that catch that, and the existing gates they must not weaken.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import benchmark_run as _br      # noqa: E402
from benchmark_run import judge  # noqa: E402
from db import MigrationDB       # noqa: E402


def _result(**over) -> dict:
    """A clean, passing run. Each test breaks exactly one thing."""
    base = {
        "migrateReturnCode": 0,
        "totalFiles": 1342,
        "migrateLog": "benchmarks/x-migrate.log",
        "checksumFailures": 0,
        "rateLimitHits": 0,
        "users": {"alice@src": {"failed": 0, "files": 1342}},
        "acl": {"extraGrants": 0, "fidelityPct": 100.0,
                "missingGrants": 0, "missingFiles": 0},
    }
    base.update(over)
    return base


class TestTheRunActuallyHappened:
    def test_a_clean_run_passes(self):
        passed, fails = judge(_result(), set())
        assert passed, fails

    def test_a_crashed_migrate_fails(self):
        """rc=-6 is SIGABRT -- the glibc heap corruption from sharing one
        httplib2.Http across the file pool. It was reported as PASS."""
        passed, fails = judge(_result(migrateReturnCode=-6, totalFiles=0), set())
        assert not passed
        assert any("MIGRATE CRASHED" in f and "signal 6" in f for f in fails), fails

    def test_a_nonzero_exit_fails(self):
        passed, fails = judge(_result(migrateReturnCode=2), set())
        assert not passed
        assert any("exit 2" in f for f in fails), fails

    def test_zero_files_fails_even_when_migrate_exited_cleanly(self):
        """The dangerous shape: nothing crashed, nothing moved, and every
        fidelity gate is vacuously satisfied. Has already happened once, when
        the ledger was not reset and migrate skipped every user as DONE."""
        passed, fails = judge(_result(totalFiles=0), set())
        assert not passed
        assert any("NOTHING MIGRATED" in f for f in fails), fails

    def test_an_empty_run_is_not_rescued_by_perfect_fidelity(self):
        passed, fails = judge(
            _result(totalFiles=0,
                    acl={"extraGrants": 0, "fidelityPct": 100.0,
                         "missingGrants": 0, "missingFiles": 0}), set())
        assert not passed


class TestExistingGatesStillBite:
    def test_extra_grants_fail(self):
        """link_flip left 93 target files world-readable this way."""
        passed, fails = judge(_result(acl={"extraGrants": 93, "fidelityPct": 100.0,
                                           "missingGrants": 0, "missingFiles": 0}),
                              set())
        assert not passed
        assert any("SECURITY" in f for f in fails), fails

    def test_grant_loss_fails(self):
        """B4 scored 0% here while reporting a clean run."""
        passed, fails = judge(_result(acl={"extraGrants": 0, "fidelityPct": 0.0,
                                           "missingGrants": 20714,
                                           "missingFiles": 0}), set())
        assert not passed
        assert any("FIDELITY" in f for f in fails), fails

    def test_checksum_mismatch_fails(self):
        passed, fails = judge(_result(checksumFailures=1), set())
        assert not passed
        assert any("CORRUPTION" in f for f in fails), fails

    def test_unverified_fidelity_fails(self):
        passed, fails = judge(_result(acl={"error": "audit did not run"}), set())
        assert not passed
        assert any("FIDELITY UNVERIFIED" in f for f in fails), fails

    def test_rate_limit_hits_warn_but_do_not_fail(self):
        """The engine retries them, so they are a tuning signal, not a
        correctness one."""
        r = _result(rateLimitHits=12)
        passed, _ = judge(r, set())
        assert passed
        assert any("rate-limit" in w for w in r["warnings"])

    def test_known_dead_accounts_are_excluded_from_the_warning(self):
        r = _result(users={"gone@src": {"failed": 5, "files": 0}})
        judge(r, {"gone@src"})
        assert not r["warnings"]


class TestStatsAreScopedToTheRun:
    """
    audit_log outlives a wipe. reset_drive_ledger.py clears Drive rows and
    nothing else, so Gmail/Chat/Calendar rows and every previous run's ACL
    failures are still sitting there when the next benchmark is judged.

    B5 came back warning that 10 users had failed items when exactly one
    had; the other nine carried only B4's 20,714 stale ACL failures from the
    day before. The Drive counts happened to be clean because the ledger
    reset had removed the old ones -- luck, not a guarantee, and it does not
    survive --skip-wipe.
    """

    def test_unscoped_stats_see_everything(self, tmp_path, monkeypatch):
        db = self._seed(tmp_path, monkeypatch)
        rows = _br.per_user_stats()
        assert rows["old@src"]["failed"] == 1, "control: the stale row exists"
        assert set(rows) == {"old@src", "new@src"}
        del db

    def test_scoped_stats_exclude_previous_runs(self, tmp_path, monkeypatch):
        self._seed(tmp_path, monkeypatch)
        rows = _br.per_user_stats(since_iso="2098-01-01T00:00:00Z")
        assert "old@src" not in rows, "a previous run's user leaked into the report"
        assert rows["new@src"]["failed"] == 1

    @staticmethod
    def _seed(tmp_path, monkeypatch):
        import sqlite3
        path = str(tmp_path / "m.db")
        monkeypatch.setenv("MIGRATION_DB", path)
        MigrationDB(path).close()
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO audit_log (source_user,item_id,item_type,status,"
                     "timestamp) VALUES ('old@src','a','acl','FAILED',"
                     "'2020-01-01T00:00:00Z')")
        conn.execute("INSERT INTO audit_log (source_user,item_id,item_type,status,"
                     "timestamp) VALUES ('new@src','b','file','FAILED',"
                     "'2099-01-01T00:00:00Z')")
        conn.commit()
        conn.close()
        return path


class TestSelectedAccount:
    @staticmethod
    def _configured_pair(tmp_path, monkeypatch):
        import control_plane_db as cpdb

        control = str(tmp_path / "control.db")
        ledger = tmp_path / "data" / "accounts" / "3" / "migration.db"
        ledger.parent.mkdir(parents=True)
        monkeypatch.setenv("CONTROL_PLANE_DB", control)
        monkeypatch.setenv("MIGRATION_DB", str(tmp_path / "other-tenant.db"))
        monkeypatch.setenv("SOURCE_DOMAIN", "other-source.test")
        monkeypatch.setenv("TARGET_DOMAIN", "other-target.test")
        monkeypatch.setenv("SOURCE_SA_KEY", str(tmp_path / "other-source.json"))
        monkeypatch.setenv("TARGET_SA_KEY", str(tmp_path / "other-target.json"))
        cpdb.apply_migrations()
        with cpdb.rw() as conn:
            conn.execute("INSERT INTO accounts (id,email,password_hash,name) VALUES (3,?,?,?)",
                         ("operator@example.test", "unused", "Benchmark operator"))
            for side, domain in (("source", "source.test"), ("target", "target.test")):
                key = tmp_path / "keys" / "3" / f"{side}-sa.json"
                conn.execute("INSERT INTO tenant_configs (account_id,side,domain,admin_email,sa_key_path,db_path) "
                             "VALUES (?,?,?,?,?,?)",
                             (3, side, domain, f"admin@{domain}", str(key), str(ledger)))
        MigrationDB(os.environ["MIGRATION_DB"]).close()
        db = MigrationDB(str(ledger))
        with db.write() as conn:
            conn.execute("INSERT INTO audit_log (source_user,item_id,item_type,status,timestamp) "
                         "VALUES ('user@source.test','artifact','file','SUCCESS','2099-01-01T00:00:00Z')")
        db.close()
        monkeypatch.setattr(_br, "OUT_DIR", str(tmp_path / "benchmarks"))
        return ledger

    def test_valid_selected_pair_binds_every_child_and_report(self, tmp_path, monkeypatch):
        import json
        import subprocess
        from pathlib import Path

        from config import Settings

        ledger = self._configured_pair(tmp_path, monkeypatch)
        calls = []

        def run(argv, **kwargs):
            if argv[0] in ("ps", "git"):
                return subprocess.CompletedProcess(argv, 0, "", "")
            script = argv[1]
            account = int(argv[argv.index("--account-id") + 1]) if "--account-id" in argv else None
            calls.append((script, account))
            # Every benchmark subprocess is faked. Resolve its actual Settings
            # constructor under the environment handed to it by the runner.
            with monkeypatch.context() as child_env:
                for name, value in kwargs["env"].items():
                    child_env.setenv(name, value)
                effective = Settings(account_id=account)
                assert (effective.source_domain, effective.target_domain) == ("source.test", "target.test")
                assert (effective.source_admin, effective.target_admin) == ("admin@source.test", "admin@target.test")
                assert Path(effective.source_sa_key).parent == tmp_path / "keys" / "3"
                assert Path(effective.target_sa_key).parent == tmp_path / "keys" / "3"
                assert Path(effective.db_path) == ledger
                if script == "reset_target.py":
                    assert argv[argv.index("--confirm-domain") + 1] == effective.target_domain
                if script == "reset_drive_ledger.py":
                    assert argv[argv.index("--confirm-domain") + 1] == effective.source_domain
            if script == "acl_audit.py":
                Path(argv[-1]).write_text(json.dumps({"totals": {"grants_source": 1, "grants_matched": 1}}))
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(_br, "sh", run)
        rc = _br.main(["--account-id", "3", "--label", "T1", "--yes", "--confirm-domain", "target.test"])

        assert rc == 0
        assert calls == [("reset_target.py", None), ("reset_drive_ledger.py", None),
                         ("main.py", 3), ("acl_audit.py", None)]
        report = next((tmp_path / "benchmarks").glob("T1-*[0-9]Z.json"))
        assert set(json.loads(report.read_text())["users"]) == {"user@source.test"}

    def test_skip_wipe_still_checks_selected_typed_target_first(self, tmp_path, monkeypatch, capsys):
        self._configured_pair(tmp_path, monkeypatch)
        def unexpected(*args, **kwargs):
            raise AssertionError("the runner called a subprocess before validating the target")
        monkeypatch.setattr(_br, "sh", unexpected)

        rc = _br.main(["--account-id", "3", "--label", "T1", "--yes", "--skip-wipe",
                       "--confirm-domain", "other-target.test"])

        assert rc == 2
        assert "does not match the target domain" in capsys.readouterr().err
