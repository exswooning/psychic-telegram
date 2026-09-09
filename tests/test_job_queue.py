"""
tests/test_job_queue.py
=======================
job_admission refused a request over the cap with "capacity is full, try
again shortly". On a one-operator box that is fine. With several accounts
sharing a deployment it means whoever retries at the right moment wins, and
everyone else learns about the refusal from a page that says nothing is
running.

The queue makes "try again shortly" the system's job. What it must not do is
lose work, run the same thing twice, or strand a row as running with no
process behind it.
"""

from __future__ import annotations

import json

import pytest

import control_plane_db as cpdb
import job_admission
import job_queue as Q


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "cp.db")
    cpdb.apply_migrations(path)
    monkeypatch.setenv("CONTROL_PLANE_DB", path)
    monkeypatch.setattr(cpdb, "_db_path", lambda: path)
    return path


def payload(**kw):
    return dict({"argv": ["python", "seed_sandbox.py"], "env": {},
                 "cwd": "/x", "label": "seed"}, **kw)


class TestNothingIsLost:
    def test_a_queued_job_is_still_there_after_a_restart(self, db):
        """The whole reason it is a table: webui restarts on every deploy,
        several times an hour, and an in-memory queue would drop every
        waiting request silently."""
        Q.enqueue(1, "seed", payload())
        assert [w["job_name"] for w in Q.waiting()] == ["seed"]

    def test_the_payload_survives_verbatim(self, db):
        """The queue does not interpret it -- it hands it back to the same
        starter that would have run it immediately."""
        seen = {}
        Q.enqueue(1, "seed", payload(argv=["a", "--groups"], env={"K": "V"}))
        Q.dispatch_one(lambda a, n, p: (seen.update(p) or (True, "")))
        assert seen["argv"] == ["a", "--groups"]
        assert seen["env"] == {"K": "V"}


class TestOneAccountCannotDoubleBook:
    def test_the_same_job_twice_is_refused(self, db):
        """A double-clicked button must not become two five-hour seeds
        against one tenant."""
        Q.enqueue(1, "seed", payload())
        with pytest.raises(Q.QueueFull):
            Q.enqueue(1, "seed", payload())

    def test_a_different_job_is_fine(self, db):
        Q.enqueue(1, "seed", payload())
        Q.enqueue(1, "reset target", payload())
        assert len(Q.waiting()) == 2

    def test_another_account_is_fine(self, db):
        Q.enqueue(1, "seed", payload())
        Q.enqueue(2, "seed", payload())
        assert len(Q.waiting()) == 2

    def test_queueing_again_is_allowed_once_the_first_has_run(self, db):
        Q.enqueue(1, "seed", payload())
        Q.dispatch_one(lambda a, n, p: (True, ""))
        Q.enqueue(1, "seed", payload())      # must not raise


class TestItRunsOldestFirst:
    def test_first_in_first_out(self, db):
        Q.enqueue(1, "seed", payload())
        Q.enqueue(2, "seed", payload())
        started = []
        Q.dispatch_one(lambda a, n, p: (started.append(a) or (True, "")))
        assert started == [1]

    def test_position_counts_from_one(self, db):
        first = Q.enqueue(1, "seed", payload())
        second = Q.enqueue(2, "seed", payload())
        assert first["position"] == 1 and second["position"] == 2

    def test_position_is_zero_once_it_is_no_longer_waiting(self, db):
        qid = Q.enqueue(1, "seed", payload())["id"]
        Q.dispatch_one(lambda a, n, p: (True, ""))
        assert Q.position_of(qid) == 0


class TestCapacityIsStillRespected:
    def test_nothing_starts_when_the_box_is_full(self, db, monkeypatch):
        monkeypatch.setattr(job_admission, "MAX_CONCURRENT_TENANT_JOBS", 1)
        job_admission.try_admit(9, "migrate", pid=None)
        Q.enqueue(1, "seed", payload())
        assert Q.dispatch_one(lambda a, n, p: (True, "")) is None
        assert len(Q.waiting()) == 1, "the job was consumed without running"

    def test_it_starts_once_a_slot_frees(self, db, monkeypatch):
        monkeypatch.setattr(job_admission, "MAX_CONCURRENT_TENANT_JOBS", 1)
        job_admission.try_admit(9, "migrate", pid=None)
        Q.enqueue(1, "seed", payload())
        assert Q.dispatch_one(lambda a, n, p: (True, "")) is None
        job_admission.release(9, "migrate")
        out = Q.dispatch_one(lambda a, n, p: (True, ""))
        assert out and out["started"] is True


class TestAFailedStartDoesNotStrandAnything:
    def test_the_slot_is_given_back(self, db, monkeypatch):
        monkeypatch.setattr(job_admission, "MAX_CONCURRENT_TENANT_JOBS", 1)
        Q.enqueue(1, "seed", payload())
        Q.dispatch_one(lambda a, n, p: (False, "no key on file"))
        assert job_admission.list_active() == [], "a slot was held for a job that never ran"

    def test_the_reason_is_recorded_not_swallowed(self, db):
        Q.enqueue(1, "seed", payload())
        Q.dispatch_one(lambda a, n, p: (False, "no key on file"))
        row = Q.recent(1)[0]
        assert row["status"] == "failed" and "no key" in row["detail"]

    def test_a_starter_that_raises_is_the_same_as_one_that_refuses(self, db):
        Q.enqueue(1, "seed", payload())
        def boom(a, n, p):
            raise RuntimeError("payload made no sense")
        Q.dispatch_one(boom)
        assert Q.recent(1)[0]["status"] == "failed"
        assert job_admission.list_active() == []


class TestHistoryOutlivesTheQueue:
    def test_a_finished_row_is_kept(self, db):
        """A queue that deletes its history can only say what is waiting
        now, never what happened to the thing asked for an hour ago."""
        Q.enqueue(1, "seed", payload())
        Q.dispatch_one(lambda a, n, p: (True, ""))
        assert Q.waiting() == []
        assert [r["status"] for r in Q.recent()] == ["done"]

    def test_cancelling_only_touches_a_waiting_job(self, db):
        qid = Q.enqueue(1, "seed", payload())["id"]
        assert Q.cancel(qid) is True
        assert Q.cancel(qid) is False, "cancelled something already finished"

    def test_one_account_cannot_cancel_anothers(self, db):
        qid = Q.enqueue(1, "seed", payload())["id"]
        assert Q.cancel(qid, account_id=2) is False
        assert Q.cancel(qid, account_id=1) is True


class TestAnEmptyQueueIsNotAnError:
    def test_dispatch_with_nothing_waiting(self, db):
        assert Q.dispatch_one(lambda a, n, p: (True, "")) is None
