"""
tests/test_run_status.py
========================
The monitor that does not lie.

Five ad-hoc monitors gave five wrong answers about ONE live migration in a
single evening -- a run reported alive after it exited (pgrep matching the
polling command's own arguments), the bash wrapper's 600 KB reported as the
engine's memory (`pgrep | head -1`), a delta reading as "nothing running"
(pattern matched `migrate` only), a premature "RUN FINISHED", and a time
window that matched every row ever written (`datetime('now')` uses a space,
the ledger writes `T`).

Not one was an engine bug. All five were bugs in how the question was asked,
and all five were invisible because nothing tested the asking.
"""

from __future__ import annotations

import pytest

import run_status


class FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def all_identities(self):
        return self._rows


def u(status, kind="user"):
    return {"source_email": f"{status}{id(object())}@s.com",
            "entity_type": kind, "status": status}


@pytest.fixture
def db():
    return FakeDB([u("DONE")] * 285 + [u("RUNNING")] * 5
                  + [u("FAILED")] * 8 + [u("PENDING")] * 2)


class TestLivenessComesFromTheLedgerNotAProcessScan:
    def test_it_reports_a_live_job(self, db, monkeypatch):
        job = {"account_id": 2, "job_name": "delta", "pid": 865440,
               "started_at": "2026-09-20T14:35:00.000Z"}
        monkeypatch.setattr(run_status, "_live_jobs", lambda a=None: [job])
        monkeypatch.setattr(run_status, "_memory", lambda: MEM)
        out = run_status.render(run_status.snapshot(db))
        assert "RUNNING" in out and "865440" in out

    def test_it_says_idle_rather_than_guessing(self, db, monkeypatch):
        monkeypatch.setattr(run_status, "_live_jobs", lambda a=None: [])
        monkeypatch.setattr(run_status, "_memory", lambda: MEM)
        assert "IDLE" in run_status.render(run_status.snapshot(db))

    def test_a_dead_pid_is_not_reported_as_running(self, monkeypatch):
        """The failure that printed a premature RUN FINISHED, inverted: a
        row in the table is not proof the process still exists."""
        import job_admission
        monkeypatch.setattr(job_admission, "list_active",
                            lambda: [{"account_id": 2, "job_name": "delta",
                                      "pid": 999999999,
                                      "started_at": "2026-09-20T14:35:00.000Z"}])
        monkeypatch.setattr(job_admission, "is_live", lambda j, **k: False)
        assert run_status._live_jobs() == []

    def test_it_never_shells_out_to_a_process_list(self):
        """Every one of the five bad monitors was a ps/pgrep pattern. The
        cheapest way not to write a sixth is to have no such call here."""
        src = open(run_status.__file__).read()
        code = src.split('"""', 2)[-1]          # past the module docstring
        for banned in ("pgrep", "subprocess", "ps -eo", "os.popen"):
            assert banned not in code, f"{banned} is back"


MEM = {"available_gb": 0.4, "total_gb": 3.7, "swap_used_gb": 1.1,
       "swap_total_gb": 4.0, "under_pressure": False}


class TestProgressIsCountsNeverOnePercentage:
    def test_each_state_is_its_own_figure(self, db, monkeypatch):
        monkeypatch.setattr(run_status, "_live_jobs", lambda a=None: [])
        monkeypatch.setattr(run_status, "_memory", lambda: MEM)
        out = run_status.render(run_status.snapshot(db))
        assert "DONE 285" in out and "FAILED 8" in out and "RUNNING 5" in out

    def test_a_state_with_nothing_in_it_still_prints_zero(self, monkeypatch):
        """An absent line reads as 'not measured', which is a different
        claim from 'none'."""
        monkeypatch.setattr(run_status, "_live_jobs", lambda a=None: [])
        monkeypatch.setattr(run_status, "_memory", lambda: MEM)
        out = run_status.render(run_status.snapshot(FakeDB([u("DONE")])))
        assert "FAILED 0" in out

    def test_non_user_rows_are_not_counted_as_users(self):
        db = FakeDB([u("DONE"), u("DONE", kind="group")])
        assert run_status._user_counts(db)["TOTAL"] == 1


class TestTheErrorRateDenominatorIsWhatWasAttempted:
    def test_pending_users_do_not_inflate_the_rate(self):
        """8 failed of 293 attempted is 2.7%. Counting the 2 PENDING as
        attempted would understate it, and counting only DONE would
        overstate it -- the denominator is the claim."""
        db = FakeDB([u("DONE")] * 285 + [u("FAILED")] * 8 + [u("PENDING")] * 2)
        snap = _snap(db)
        assert snap["error_rate"] == pytest.approx(8 / 293)

    def test_nothing_attempted_is_None_not_zero(self):
        """0% failed and 'no data' must not render the same."""
        snap = _snap(FakeDB([u("PENDING")] * 10))
        assert snap["error_rate"] is None

    def test_it_says_so_in_words(self):
        assert "nothing attempted" in run_status.render(
            _snap(FakeDB([u("PENDING")])))


def _snap(db):
    import unittest.mock as m
    with m.patch.object(run_status, "_live_jobs", lambda a=None: []), \
         m.patch.object(run_status, "_memory", lambda: MEM):
        return run_status.snapshot(db)


class TestMemoryAgreesWithTheEngine:
    def test_pressure_is_the_engines_own_verdict(self, monkeypatch):
        """status and the memory watchdog must never disagree about whether
        the box is in trouble, so both read resources.py."""
        import resources

        class R:
            ram_usable_gb, ram_total_gb = 0.2, 3.7
            swap_used_gb, swap_total_gb = 3.8, 4.0
            under_memory_pressure = True

        monkeypatch.setattr(resources, "probe", lambda: R())
        assert run_status._memory()["under_pressure"] is True

    def test_it_is_shown_when_true(self, monkeypatch):
        monkeypatch.setattr(run_status, "_live_jobs", lambda a=None: [])
        monkeypatch.setattr(run_status, "_memory",
                            lambda: dict(MEM, under_pressure=True))
        assert "UNDER PRESSURE" in run_status.render(
            run_status.snapshot(FakeDB([u("DONE")])))
