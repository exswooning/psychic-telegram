"""
tests/test_single_flight_cache.py
=================================
The cache in front of the migration-detail aggregate. It exists to stop a
5-second dashboard poll from restarting a 20-second query, and it does that
well.

What it also did was keep every key's last value for the life of the
process. An expired entry is never SERVED -- get() recomputes past the
deadline regardless -- so nothing noticed, except that what it was holding
is the aggregate of a 2.95M-row audit_log. Measured live: api_server.py at
905 MB RSS on a 3.8 GB box, in swap, squeezing the seed run it was
reporting on. Idle it is flat at 57 MB; the growth arrives with somebody
browsing.
"""

from __future__ import annotations

import threading
import time

import api_server


def cache(ttl=60.0):
    return api_server._SingleFlightCache(ttl=ttl)


class TestItStillDoesItsJob:
    def test_a_hit_is_not_recomputed(self):
        c = cache()
        calls = []
        for _ in range(3):
            c.get("k", lambda: calls.append(1) or "v")
        assert len(calls) == 1

    def test_the_value_is_returned_every_time(self):
        c = cache()
        first = c.get("k", lambda: "v")
        assert c.get("k", lambda: "other") == first == "v"

    def test_it_recomputes_once_stale(self):
        c = cache(ttl=0.01)
        c.get("k", lambda: "old")
        time.sleep(0.02)
        assert c.get("k", lambda: "new") == "new"

    def test_keys_do_not_collide(self):
        c = cache()
        assert c.get("a", lambda: 1) == 1
        assert c.get("b", lambda: 2) == 2

    def test_concurrent_callers_compute_once(self):
        """The whole point: N pollers and a second browser tab cost one
        query, not N."""
        c = cache()
        calls = []

        def slow():
            time.sleep(0.05)
            calls.append(1)
            return "v"

        threads = [threading.Thread(target=lambda: c.get("k", slow))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(calls) == 1


class TestItDoesNotHoldWhatItWillNeverServe:
    def test_an_expired_entry_is_dropped(self):
        c = cache(ttl=0.01)
        c.get("k", lambda: "big")
        time.sleep(0.02)
        c.get("other", lambda: "x")
        assert "k" not in c._entries, "kept a value it can never serve again"

    def test_the_dropped_value_is_actually_released(self):
        """Not just unreachable from the dict -- unreferenced, so the
        megabytes come back."""
        import weakref

        class Big:
            pass

        c = cache(ttl=0.01)
        ref = weakref.ref(c.get("k", Big))
        time.sleep(0.02)
        c.get("other", lambda: "x")
        import gc

        gc.collect()
        assert ref() is None, "the aggregate was still referenced"

    def test_a_live_entry_is_untouched(self):
        c = cache(ttl=60.0)
        c.get("k", lambda: "v")
        c.get("other", lambda: "x")
        assert "k" in c._entries

    def test_many_expired_keys_do_not_accumulate(self):
        """The shape of the live failure: distinct keys over hours."""
        c = cache(ttl=0.01)
        for i in range(50):
            c.get(("detail", i), lambda: "payload")
        time.sleep(0.02)
        c.get("wake", lambda: "x")
        assert len(c._entries) == 1


class TestInvalidateStillWorks:
    def test_an_invalidated_key_is_recomputed(self):
        c = cache()
        c.get("k", lambda: "old")
        c.invalidate("k")
        assert c.get("k", lambda: "new") == "new"

    def test_invalidating_something_absent_is_not_an_error(self):
        cache().invalidate("never-seen")
