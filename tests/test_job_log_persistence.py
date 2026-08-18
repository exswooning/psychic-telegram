"""
tests/test_job_log_persistence.py
==================================
A job's output has to survive this server restarting, because
systemd's KillMode=process deliberately keeps the job itself alive across
one (see systemd/bitport-api.service). It did not: Job.start() piped the
child's stdout back into this process, so a restart left the child writing
into a pipe whose only reader was gone.

Three distinct real failures came from that, all confirmed live:

  * the transcript was unrecoverable afterwards, and _process_output_tail()
    silently fell back to migration.log -- the UI streamed a THREE-DAY-OLD
    log from an unrelated migrate as a running seed's "live" output;
  * a full 64KB pipe buffer with no reader blocks the child's next print()
    forever;
  * writing to a pipe with no reader raises BrokenPipeError, the most
    likely explanation for a seed run that vanished mid-deploy with no OOM
    and no saved result.

The fix is that the child writes to a real file, so these tests pin the
properties that make that true rather than the implementation detail.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webui  # noqa: E402


@pytest.fixture
def job_dir(tmp_path, monkeypatch):
    """Redirect the whole logs/jobs tree into tmp, so a test never writes
    beside the real repo's own job history."""
    monkeypatch.setattr(webui, "__file__", str(tmp_path / "webui.py"))
    return tmp_path


def _run(job: webui.Job, name: str, script: str) -> None:
    ok, msg = job.start(name, [sys.executable, "-c", script])
    assert ok, msg
    for _ in range(200):
        if not job.running:
            break
        time.sleep(0.05)
    assert not job.running, "job did not finish in time"


class TestOutputGoesToARealFile:
    def test_the_transcript_is_on_disk_after_the_run(self, job_dir):
        job = webui.Job(account_id=7)
        _run(job, "seed", "print('hello from the child')")

        assert os.path.exists(job.log_path)
        with open(job.log_path, encoding="utf-8") as fh:
            assert "hello from the child" in fh.read()

    def test_the_log_lives_beside_that_jobs_saved_result(self, job_dir):
        """Same (account, name) addressing as job_result_path -- that is what
        lets a restarted server find a job's transcript knowing only its
        name, with no in-memory state to consult."""
        assert (webui.job_log_path(7, "seed")
                == webui.job_result_path(7, "seed")[: -len(".json")] + ".log")

    def test_a_restart_can_still_read_the_output_it_never_held(self, job_dir):
        """The whole point. A brand-new Job object -- standing in for a
        freshly started server process -- has an empty in-memory list, and
        must still be able to serve the previous process's transcript."""
        job = webui.Job(account_id=7)
        _run(job, "seed", "print('written before the restart')")

        reborn = webui.Job(account_id=7)
        assert reborn.lines == []

        tail = webui._process_output_tail(-1, "seed", 7)
        assert any("written before the restart" in ln for ln in tail)

    def test_stdout_is_never_a_pipe(self, job_dir, monkeypatch):
        """The property that actually prevents all three failures above --
        asserted directly, because every one of them is invisible in a
        short, fast-finishing test like the ones here."""
        seen = {}

        real_popen = webui.subprocess.Popen

        def spy(argv, **kwargs):
            seen.update(kwargs)
            return real_popen(argv, **kwargs)

        monkeypatch.setattr(webui.subprocess, "Popen", spy)
        job = webui.Job(account_id=7)
        _run(job, "seed", "pass")

        assert seen["stdout"] is not webui.subprocess.PIPE
        assert hasattr(seen["stdout"], "write"), "stdout should be a file object"


class TestLinesStillStream:
    def test_lines_are_ingested_for_the_since_total_cursor(self, job_dir):
        """JobProgress/JobRunner page through output with since/total, so
        reading from a file must keep serving the same cursor contract."""
        job = webui.Job(account_id=7)
        _run(job, "seed", "print('one')\nprint('two')\nprint('three')")

        assert job.snapshot()["lines"] == ["one", "two", "three"]
        snap = job.snapshot(since=1)
        assert snap["total"] == 3
        assert snap["lines"] == ["two", "three"]

    def test_the_final_lines_are_never_lost_to_drain_thread_timing(self, job_dir):
        """`running` goes false the instant the process exits, which is
        before the drain thread's own final pass -- so a caller that stops
        polling right then used to be able to miss the last lines for good.
        snapshot() reads the file itself precisely so it cannot."""
        job = webui.Job(account_id=7)
        ok, msg = job.start("seed", [sys.executable, "-c", "print('the very last line')"])
        assert ok, msg
        while job.running:
            time.sleep(0.01)

        assert "the very last line" in job.snapshot()["lines"]

    def test_google_futurewarning_noise_is_still_filtered(self, job_dir):
        """These drown the actual output; they were filtered when draining a
        pipe and must stay filtered when reading a file."""
        job = webui.Job(account_id=7)
        _run(job, "seed",
             "print('real line')\n"
             "print('FutureWarning: some google deprecation')\n"
             "print('  warnings.warn(message, FutureWarning)')\n"
             "print('another real line')")

        assert job.snapshot()["lines"] == ["real line", "another real line"]


class TestExternalTailIsNeverAnotherJobsLog:
    def test_an_unresolvable_process_reports_nothing_rather_than_guessing(self, job_dir):
        """The live bug: this used to return migration.log's contents -- a
        different job, days old -- which is strictly worse than nothing,
        because nothing is at least honestly empty."""
        assert webui._process_output_tail(-1, "seed", 7) == []

    def test_a_job_with_no_name_to_resolve_by_reports_nothing(self, job_dir):
        assert webui._process_output_tail(-1) == []


class TestExternalSnapshotHonoursTheCallersCursor:
    """A detached job's transcript used to be diffed against one
    module-level "what did I last send" per pid, so the response depended
    on who polled last rather than on who was asking. Confirmed live: a
    real seed's 21-line transcript was on disk and resolvable, and a
    browser opening the page fresh still rendered an empty feed, because
    an earlier poll in the same server process had already consumed it.
    """

    def _fake_external(self, monkeypatch, lines):
        monkeypatch.setattr(webui, "_external_processes",
                            lambda: [{"pid": 4242, "elapsed": 10, "name": "seed"}])
        monkeypatch.setattr(webui, "_process_output_tail",
                            lambda pid, name="", account_id=None: list(lines))

    def test_a_fresh_client_gets_the_whole_transcript(self, monkeypatch):
        self._fake_external(monkeypatch, ["one", "two", "three"])
        snap = webui._external_job_snapshot(since=0)
        assert snap["lines"] == ["one", "two", "three"]
        assert snap["total"] == 3

    def test_a_second_client_is_unaffected_by_the_first(self, monkeypatch):
        """The actual live symptom: two readers, and the second one saw
        nothing because the first had already 'consumed' the lines."""
        self._fake_external(monkeypatch, ["one", "two", "three"])
        webui._external_job_snapshot(since=0)          # first reader drains
        second = webui._external_job_snapshot(since=0)  # fresh page load
        assert second["lines"] == ["one", "two", "three"]

    def test_a_caller_mid_stream_gets_only_what_is_new_to_it(self, monkeypatch):
        self._fake_external(monkeypatch, ["one", "two", "three"])
        assert webui._external_job_snapshot(since=2)["lines"] == ["three"]

    def test_a_caller_fully_caught_up_gets_nothing(self, monkeypatch):
        self._fake_external(monkeypatch, ["one", "two", "three"])
        assert webui._external_job_snapshot(since=3)["lines"] == []
