"""
tests/test_job_history_archive.py
=================================
job_result_path keys a saved run on the job NAME alone, so every run of a
name overwrote the one before it: the Jobs page could show "the last run of
each thing" and nothing else. Wiping a tenant twice left one row, and
opening it handed back the second wipe's transcript under the first one's
date. Job._save_result now also writes an archive per run, and
completed_jobs lists those -- which is what makes it a history rather than
a snapshot.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

import webui


@pytest.fixture
def account_id():
    aid = 999999011
    yield aid
    shutil.rmtree(os.path.dirname(webui.job_result_path(aid, "x")),
                  ignore_errors=True)


def _finish(account_id, name, rc, finished, lines):
    """One completed run, saved the way Job._save_result saves it."""
    job = webui.Job(account_id)
    job.name, job.rc = name, rc
    job.started, job.finished = finished - 5, finished
    job.lines = list(lines)
    job._save_result()


class TestEveryRunIsKept:
    def test_two_runs_of_one_name_are_two_rows(self, account_id):
        _finish(account_id, "wipe tenant data", 0, 1_700_000_000, ["first"])
        _finish(account_id, "wipe tenant data", 1, 1_700_000_600, ["second"])
        rows = webui.completed_jobs(account_id)
        assert len(rows) == 2, rows
        # Newest first, and each carries its own outcome -- not the newest
        # one's applied to both.
        assert [r["rc"] for r in rows] == [1, 0]

    def test_a_row_fetches_its_own_transcript_not_the_latest(self, account_id):
        _finish(account_id, "seed", 0, 1_700_000_000, ["older run"])
        _finish(account_id, "seed", 0, 1_700_000_600, ["newer run"])
        rows = webui.completed_jobs(account_id)
        older = rows[-1]
        got = webui.load_job_archive(account_id, older["runId"])
        assert got["lines"] == ["older run"]

    def test_the_latest_file_is_not_listed_twice(self, account_id):
        """_save_result writes BOTH <stem>.json and an archive. Listing the
        plain file as its own row showed every job twice."""
        _finish(account_id, "seed", 0, 1_700_000_000, ["x"])
        assert len(webui.completed_jobs(account_id)) == 1


class TestArchivesArePruned:
    def test_only_the_newest_survive(self, account_id, monkeypatch):
        monkeypatch.setattr(webui, "ARCHIVE_KEEP", 3)
        for i in range(6):
            _finish(account_id, "seed", 0, 1_700_000_000 + i, [f"run {i}"])
        rows = webui.completed_jobs(account_id)
        # 3 archives + the plain <stem>.json, whose stem now has no archive
        # left older than it... it still matches the newest, so it is
        # skipped. Exactly the kept archives, in other words.
        assert len(rows) == 3, rows
        assert [json.loads(open(
            os.path.join(os.path.dirname(webui.job_result_path(account_id, "x")),
                         f"{r['runId']}.json")).read())["lines"][0]
            for r in rows] == ["run 5", "run 4", "run 3"]


class TestArchiveIdsAreNotPaths:
    @pytest.mark.parametrize("bad", [
        "../../etc/passwd", "seed", "seed.1700000000/../x", "..",
    ])
    def test_a_run_id_that_is_not_one_is_refused(self, account_id, bad):
        """The id arrives from a query string. It must be checked before it
        touches a path, not after."""
        assert webui.load_job_archive(account_id, bad) is None


class TestHugeTranscriptsAreNotReadWhole:
    def test_only_the_tail_is_read(self, account_id):
        """migrate.log on the live box is 77MB; every byte before the last
        few hundred lines is discarded here anyway."""
        path = webui.job_log_path(account_id, "migrate")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(200_000):
                fh.write(f"line {i} padded out to make this file large\n")
        assert os.path.getsize(path) > 512 * 1024
        res = webui.load_job_result(account_id, "migrate")
        assert res["from_transcript"] and res["truncated"]
        assert res["lines"][-1].startswith("line 199999")
        # The count describes what was read, not what the file holds.
        assert res["line_count"] < 200_000


class TestATranscriptWithNoResultFileStillCounts:
    """A .json is written by the in-memory Job when a run finishes; a .log is
    written by the child itself, the whole time. So a run the Job object
    never owned -- started outside it, or one that outlived the restart that
    forgot it -- has a complete transcript and no result file.

    Live: a five-hour 200-user seed left 611 KB of seed.log and no
    seed.json, and the Jobs page showed one unrelated wipe as the entire
    history of that account.
    """

    def test_a_log_only_run_is_listed(self, account_id):
        path = webui.job_log_path(account_id, "seed")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("Seeding 200 users\n  [a@x] done in 3979.8s: 1 files\n")
        names = [r["name"] for r in webui.completed_jobs(account_id)]
        assert "seed" in names, names

    def test_it_is_not_listed_twice_when_a_result_exists(self, account_id):
        """_save_result writes the .json beside the .log it was streaming
        to, so both files exist for an ordinary run."""
        _finish(account_id, "seed", 0, 1_700_000_000, ["x"])
        with open(webui.job_log_path(account_id, "seed"), "w",
                  encoding="utf-8") as fh:
            fh.write("x\n")
        rows = [r for r in webui.completed_jobs(account_id) if r["name"] == "seed"]
        assert len(rows) == 1, rows

    def test_the_transcript_row_is_dated(self, account_id):
        """Undated rows all sort to the bottom together, which is where the
        seed would have gone even once it appeared."""
        path = webui.job_log_path(account_id, "reset target")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("something\n")
        row = [r for r in webui.completed_jobs(account_id)
               if r["name"] == "reset target"][0]
        assert row["finished"], "no timestamp on a transcript-only run"

