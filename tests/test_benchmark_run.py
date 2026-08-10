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

from benchmark_run import judge  # noqa: E402


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
