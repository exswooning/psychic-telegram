"""
tests/test_job_persistence.py
==============================
webui.py's Job lives entirely in the process's own memory -- a redeploy or
restart minutes (or seconds) after a seed/reset-target/deploy run finished
used to lose the entire result, even though the run itself had already
succeeded. Job._save_result() writes the last completed run of each
(account, job name) pair to disk so /api/job_history can still answer "what
did that actually do" after the process that ran it is long gone.
"""

from __future__ import annotations

import os
import shutil

import pytest

import webui


def _cleanup(account_id):
    d = os.path.dirname(webui.job_result_path(account_id, "x"))
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def account_id():
    # Not a real account -- just a directory name under logs/jobs/ that
    # cannot collide with any real one, cleaned up unconditionally after.
    aid = 999999001
    yield aid
    _cleanup(aid)


class TestJobResultPath:
    def test_different_accounts_get_different_files(self):
        p1 = webui.job_result_path(1, "seed")
        p2 = webui.job_result_path(2, "seed")
        assert p1 != p2
        assert "1" in p1.split(os.sep) and "2" in p2.split(os.sep)

    def test_the_operators_own_none_account_gets_its_own_directory(self):
        """None must not collide with a real account_id -- "_none" (not,
        say, the string "None" or an empty path segment) keeps it a
        distinct, valid path."""
        p = webui.job_result_path(None, "deploy")
        assert "_none" in p.split(os.sep)

    def test_job_names_are_filesystem_safe(self):
        """"reset target" has a space; must not produce a broken or
        surprising path (e.g. two args in a shell command later)."""
        p = webui.job_result_path(1, "reset target")
        assert " " not in os.path.basename(p)
        assert os.path.basename(p) == "reset_target.json"


class TestSaveAndLoad:
    def test_a_saved_result_round_trips(self, account_id):
        job = webui.Job(account_id)
        job.name = "seed"
        job.lines = ["line one", "line two"]
        job.started = 1000.0
        job.finished = 1042.5
        job.rc = 0
        job._save_result()

        result = webui.load_job_result(account_id, "seed")
        assert result is not None
        assert result["name"] == "seed"
        assert result["rc"] == 0
        assert result["elapsed"] == 42.5
        assert result["lines"] == ["line one", "line two"]

    def test_loading_a_result_that_was_never_saved_returns_none(self, account_id):
        assert webui.load_job_result(account_id, "seed") is None

    def test_a_second_run_overwrites_the_first(self, account_id):
        """Only the LAST completed run is kept -- this is a status file,
        not a history (operator_actions_log already covers history for
        gated actions)."""
        job = webui.Job(account_id)
        job.name = "seed"
        job.started, job.finished, job.rc = 1000.0, 1010.0, 1
        job.lines = ["first run failed"]
        job._save_result()

        job.started, job.finished, job.rc = 2000.0, 2005.0, 0
        job.lines = ["second run ok"]
        job._save_result()

        result = webui.load_job_result(account_id, "seed")
        assert result["rc"] == 0
        assert result["lines"] == ["second run ok"]

    def test_different_job_names_do_not_clobber_each_other(self, account_id):
        seed_job = webui.Job(account_id)
        seed_job.name = "seed"
        seed_job.started, seed_job.finished, seed_job.rc = 1000.0, 1010.0, 0
        seed_job.lines = ["seeding"]
        seed_job._save_result()

        reset_job = webui.Job(account_id)
        reset_job.name = "reset target"
        reset_job.started, reset_job.finished, reset_job.rc = 2000.0, 2010.0, 0
        reset_job.lines = ["resetting"]
        reset_job._save_result()

        assert webui.load_job_result(account_id, "seed")["lines"] == ["seeding"]
        assert webui.load_job_result(account_id, "reset target")["lines"] == ["resetting"]

    def test_only_the_most_recent_2000_lines_are_kept(self, account_id):
        """Independent cap from the in-memory 4000-line trim -- this file
        is read back in one GET, not streamed, so it stays well under what
        a browser tab should ever render in one response."""
        job = webui.Job(account_id)
        job.name = "seed"
        job.started, job.finished, job.rc = 1000.0, 1010.0, 0
        job.lines = [f"line {i}" for i in range(3000)]
        job._save_result()

        result = webui.load_job_result(account_id, "seed")
        assert len(result["lines"]) == 2000
        assert result["lines"][-1] == "line 2999"

    def test_a_write_failure_does_not_raise(self, account_id, monkeypatch):
        """_drain() calls this from a background thread with nothing
        watching for an exception -- a failed save must degrade silently,
        not crash the thread and take the rest of _drain() (and
        on_finish) down with it."""
        monkeypatch.setattr(webui.os, "makedirs",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        job = webui.Job(account_id)
        job.name = "seed"
        job.started, job.finished, job.rc = 1000.0, 1010.0, 0
        job._save_result()  # must not raise
