"""
tests/test_metrics_worker_count.py
====================================
Metrics.snapshot()'s "workers" figure used to be len(a set of every thread
NAME record() had ever seen) -- which only ever grew, never shrank, so a run
that keeps recreating short-lived per-user pools (drive_engine._open_file_pool,
one fresh ThreadPoolExecutor per user) reported a "workers" count that climbed
past the process's real, currently-configured concurrency for the whole life
of the run. Confirmed live: a run showed 304 there against 195 real threads
alive (ps -o nlwp) on the same process. Now threading.active_count() at
snapshot time -- live, not accumulated.
"""
from __future__ import annotations

import threading

import metrics as metrics_mod


def _fresh() -> metrics_mod.Metrics:
    m = metrics_mod.Metrics()
    m.enabled = True
    return m


class TestWorkersIsLiveNotAccumulated:
    def test_recording_from_many_now_exited_threads_does_not_inflate_it(self):
        m = _fresh()

        def once():
            m.record("drive.files.copy", 0.1)

        # Simulate the exact leak shape: many distinct, short-lived threads,
        # each recording once and then exiting -- the old set kept every one
        # of their names forever.
        for i in range(50):
            t = threading.Thread(target=once, name=f"drive-user{i}-0")
            t.start()
            t.join()

        workers = m.snapshot()["workers"]
        # None of those 50 threads are alive any more; the figure must
        # reflect that, not the 50 distinct names record() once saw.
        assert workers < 50

    def test_it_matches_threading_active_count(self):
        m = _fresh()
        m.record("drive.files.copy", 0.1)
        assert m.snapshot()["workers"] == threading.active_count()

    def test_never_zero(self):
        m = _fresh()
        assert m.snapshot()["workers"] >= 1

    def test_reset_no_longer_needs_to_clear_a_thread_set(self):
        """reset() must still work at all -- it no longer owns any
        thread-tracking state, but must not raise."""
        m = _fresh()
        m.record("drive.files.copy", 0.1)
        m.reset()
        assert m.snapshot()["calls"] == 0
