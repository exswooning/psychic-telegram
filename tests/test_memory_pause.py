"""
tests/test_memory_pause.py
==========================
Memory-pause watchdog: pressure_severe() + cached_probe() + _memory_watchdog().

Converts catastrophic memory pressure into a clean, resumable pause: sustained
severe pressure flips SHUTDOWN, in-flight workers finish their current service,
and the run exits PAUSED to resume from the ledger. These tests pin the pure
predicate, the probe cache, and the watchdog's set-shutdown-exactly-once /
require-sustained-pressure / never-die-on-probe-failure behaviour.
"""

from __future__ import annotations

import threading
import time

import pytest

import main
import resources


def make(ram_usable=8.0, ram_total=16.0, swap_used=0.0, swap_total=0.0):
    return resources.SystemResources(
        cpu_logical=4, cpu_physical=4,
        ram_total_gb=ram_total, ram_usable_gb=ram_usable,
        swap_total_gb=swap_total, swap_used_gb=swap_used,
        platform="test",
    )


SEVERE = make(ram_usable=0.2, ram_total=16.0)      # swapless, floor 0.80 GB
HEALTHY = make()


class TestPressureSevere:
    def test_swapped_host_over_threshold(self):
        assert resources.pressure_severe(
            make(ram_usable=6.0, swap_used=5.0, swap_total=8.0))      # 0.625

    def test_swapped_host_below_threshold(self):
        assert not resources.pressure_severe(
            make(swap_used=4.0, swap_total=8.0))      # 0.50

    def test_swapped_host_at_the_boundary_is_severe(self):
        assert resources.pressure_severe(
            make(swap_used=4.8, swap_total=8.0))      # exactly 0.60

    def test_a_swapped_host_is_judged_on_swap_alone(self):
        """With swap configured, swap use is the signal: an unused swapfile
        still has room to spill, so low usable RAM is not yet catastrophic."""
        assert not resources.pressure_severe(
            make(ram_usable=0.05, swap_used=0.0, swap_total=8.0))

    def test_swapless_host_with_room_is_fine(self):
        assert not resources.pressure_severe(make())

    def test_swapless_host_below_the_floor(self):
        assert resources.pressure_severe(
            make(ram_usable=0.5, ram_total=16.0))    # floor max(0.128, 0.8)

    def test_swapless_host_at_the_floor_boundary_is_fine(self):
        assert not resources.pressure_severe(
            make(ram_usable=0.8, ram_total=16.0))    # exactly the floor

    def test_the_fraction_dominates_on_a_big_host(self):
        assert resources.pressure_severe(
            make(ram_usable=5.0, ram_total=128.0))   # floor 6.40
        assert not resources.pressure_severe(
            make(ram_usable=7.0, ram_total=128.0))

    def test_the_hard_floor_dominates_on_a_small_host(self):
        assert resources.pressure_severe(
            make(ram_usable=0.1, ram_total=1.0))     # floor 0.128
        assert not resources.pressure_severe(
            make(ram_usable=0.15, ram_total=1.0))


class TestCachedProbe:
    def test_reuses_the_result_within_the_ttl(self, monkeypatch):
        calls: list = []
        snapshot = make()
        monkeypatch.setattr(resources, "probe",
                            lambda: (calls.append(1), snapshot)[1])
        monkeypatch.setattr(resources, "_probe_cache", None)
        monkeypatch.setattr(resources, "_probe_cache_until", 0.0)

        assert resources.cached_probe(ttl=10.0) is snapshot
        assert resources.cached_probe(ttl=10.0) is snapshot
        assert len(calls) == 1

    def test_reprobes_after_the_ttl_expires(self, monkeypatch):
        monkeypatch.setattr(resources, "_probe_cache", None)
        monkeypatch.setattr(resources, "_probe_cache_until", 0.0)
        first, second = make(ram_usable=1.0), make(ram_usable=2.0)
        it = iter([first, second])
        monkeypatch.setattr(resources, "probe", lambda: next(it))

        assert resources.cached_probe(ttl=0.01) is first
        time.sleep(0.02)
        assert resources.cached_probe(ttl=0.01) is second


class _CountingEvent:
    """An Event that records how many times set() was called."""

    def __init__(self):
        self.event = threading.Event()
        self.count = 0

    def set(self):
        self.count += 1
        self.event.set()

    def is_set(self):
        return self.event.is_set()


class TestMemoryWatchdog:
    @staticmethod
    def _seq(*snapshots):
        """Yield each snapshot, then the last one forever."""
        i = 0
        while True:
            yield snapshots[min(i, len(snapshots) - 1)]
            i += 1

    def _start(self, probe_fn, *, poll=0.002, samples=3):
        stop = threading.Event()
        shutdown = _CountingEvent()
        pause = _CountingEvent()
        t = threading.Thread(
            target=main._memory_watchdog, args=(stop,),
            kwargs={"shutdown": shutdown, "pause_event": pause,
                    "probe_fn": probe_fn, "poll": poll, "samples": samples},
            daemon=True)
        t.start()
        return stop, shutdown, pause, t

    def test_sustained_pressure_sets_shutdown_exactly_once(self):
        stop, shutdown, pause, t = self._start(
            lambda: next(self._seq(SEVERE)), poll=0.002)

        assert shutdown.event.wait(timeout=5)
        time.sleep(0.01)                       # let it run in drain mode a while
        stop.set()
        t.join(timeout=2)
        assert shutdown.count == 1
        assert pause.count == 1

    def test_transient_pressure_is_ignored(self):
        snapshots = self._seq(SEVERE, SEVERE, HEALTHY)
        stop, shutdown, pause, t = self._start(
            lambda: next(snapshots), poll=0.002)

        time.sleep(0.03)
        assert not shutdown.event.is_set()
        assert pause.count == 0
        stop.set()
        t.join(timeout=2)

    def test_probe_failures_do_not_kill_the_watchdog(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] <= 3:
                raise OSError("probe failed")
            return SEVERE

        stop, shutdown, pause, t = self._start(flaky, poll=0.002)

        assert shutdown.event.wait(timeout=5)
        stop.set()
        t.join(timeout=2)
        assert shutdown.count == 1
        assert pause.count == 1

    def test_permanent_probe_failures_leave_the_watchdog_running(self):
        def broken():
            raise OSError("probe failed")

        stop, shutdown, pause, t = self._start(broken, poll=0.002)

        time.sleep(0.02)
        assert t.is_alive()
        assert not shutdown.event.is_set()
        stop.set()
        t.join(timeout=2)
        assert not t.is_alive()


class TestPausedExit:
    def test_helper_exits_paused_when_the_watchdog_fired(self, monkeypatch):
        fired = threading.Event()
        monkeypatch.setattr(main, "MEMORY_PAUSE", fired)

        def fake_run_batch(*args, **kwargs):
            fired.set()                        # the watchdog fired mid-run
            return []

        monkeypatch.setattr(main, "run_batch", fake_run_batch)

        with pytest.raises(SystemExit) as exc:
            main._run_with_memory_pause(None, None, None, {"drive"}, False, 0)
        assert exc.value.code == main.EXIT_PAUSED

    def test_helper_returns_normally_when_not_paused(self, monkeypatch):
        monkeypatch.setattr(main, "MEMORY_PAUSE", threading.Event())
        monkeypatch.setattr(main, "run_batch",
                            lambda *a, **k: [{"source": "alice"}])

        out = main._run_with_memory_pause(None, None, None, {"drive"}, False, 0)
        assert out == [{"source": "alice"}]
