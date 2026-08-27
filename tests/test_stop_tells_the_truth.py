"""Stop reported success for a job that carried on for another half hour.

Live sequence, all of it observed:

  1. Stop pressed -> {"ok": true, "msg": "interrupt sent to seed"}
  2. the reset was still running 36 minutes later (SIGINT unwinds into
     ThreadPoolExecutor.__exit__, which joins workers blocked inside a
     Google API call -- the force branch already documents this)
  3. a deploy restarted webui; the child survived it and outlived the Job
     object that was tracking it
  4. a SECOND reset was admitted against the same tenant, and Running Now
     went blank because two processes were fighting over one slot

Two destructive jobs on one tenant is precisely what the one-heavy-job
rule exists to prevent, and every step after (1) followed from Stop
answering before it knew the answer.

Stop still does not force by default -- the cooperative interrupt commits
state so a re-run resumes cleanly, and killing throws that away. It just
has to say which of the two happened.
"""
import time

import pytest

import webui


class _Proc:
    """A child that exits after `dies_after` seconds, or never."""

    def __init__(self, dies_after=None):
        self._t0 = time.time()
        self._dies_after = dies_after
        self.signals = []

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.signals.append("KILL")
        self._dies_after = 0

    def poll(self):
        if self._dies_after is None:
            return None
        return 0 if time.time() - self._t0 >= self._dies_after else None


@pytest.fixture
def job(monkeypatch):
    monkeypatch.setattr(webui, "STOP_GRACE_SECONDS", 0.75)
    j = webui.Job(account_id=7)
    j.name = "seed"
    return j


class TestAStopThatWorked:
    def test_it_says_stopped(self, job):
        job.proc = _Proc(dies_after=0.1)
        assert job.stop() == "stopped seed"

    def test_it_sent_the_cooperative_signal_not_a_kill(self, job):
        # SIGINT lets the engine commit state so a re-run resumes cleanly.
        p = job.proc = _Proc(dies_after=0.1)
        job.stop()
        assert p.signals == [2]


class TestAStopThatDidNot:
    def test_it_does_not_claim_success(self, job):
        job.proc = _Proc(dies_after=None)
        msg = job.stop()
        assert "STILL RUNNING" in msg

    def test_it_says_what_to_do_next(self, job):
        job.proc = _Proc(dies_after=None)
        assert "force" in job.stop()

    def test_it_still_does_not_kill_on_its_own(self, job):
        # Forcing by default would throw away the committed-state property
        # the cooperative interrupt exists for.
        p = job.proc = _Proc(dies_after=None)
        job.stop()
        assert "KILL" not in p.signals

    def test_force_kills_immediately_without_waiting(self, job):
        p = job.proc = _Proc(dies_after=None)
        t0 = time.time()
        assert job.stop(force=True) == "killed seed"
        assert "KILL" in p.signals
        assert time.time() - t0 < 0.5, "force must not sit through the grace"


class TestItDoesNotBlockThePage:
    def test_the_wait_happens_outside_the_lock(self):
        """snapshot() is polled every couple of seconds while a job runs;
        holding the lock through the grace period would stall the page."""
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "webui.py"), encoding="utf-8").read()
        body = src.split("    def stop(")[1].split("    def snapshot(")[0]
        before, _, after = body.partition("send_signal(2)")
        assert "with self.lock" in before
        assert "deadline" in after
        # the waiting loop must not be indented inside the `with`
        assert "\n        deadline = " in after

    def test_nothing_running_is_still_cheap(self, job):
        job.proc = None
        t0 = time.time()
        assert job.stop() == "nothing running"
        assert time.time() - t0 < 0.2
