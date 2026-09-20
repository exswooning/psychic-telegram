"""
memtrace.py
===========
Find out where a migration's memory goes, per user.

The engine reaches ~3 GB RSS on a 3.7 GB box and stays there; killing it
returns the memory, so it is held, not leaked to the OS. Nothing so far has
said WHAT is held, because the only evidence has been total RSS -- which
cannot distinguish "each user costs 10 MB and we run 48" (working as
designed) from "something accumulates per user and never frees" (a leak).
Those need opposite fixes: the first wants fewer workers, the second wants
the reference broken.

The distinguishing measurement is RSS at the USER BOUNDARY -- after a user
finishes and its per-user state should be gone. If that number climbs
monotonically across completed users, it is the second case.

Off by default. tracemalloc roughly doubles allocation cost, so the cheap
half (RSS deltas) runs whenever BITPORT_MEMTRACE is set, and the expensive
half (per-allocation-site attribution) only at BITPORT_MEMTRACE=full.
"""

from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("memtrace")

_MODE = os.getenv("BITPORT_MEMTRACE", "").strip().lower()
ENABLED = _MODE in ("1", "true", "yes", "full")
FULL = _MODE == "full"

_lock = threading.Lock()
_completed = 0
_baseline_kb: int | None = None
_snapshot = None


def rss_kb() -> int | None:
    """This process's resident set, without adding a dependency.

    psutil is not in requirements.txt and this is not worth adding one for;
    /proc/self/statm is the same number on Linux, which is what the VPS and
    the migration nodes run. Returns None where that file does not exist
    (macOS dev boxes, Windows) rather than guessing.
    """
    try:
        with open("/proc/self/statm", encoding="utf-8") as fh:
            pages = int(fh.read().split()[1])
        return pages * (os.sysconf("SC_PAGE_SIZE") // 1024)
    except (OSError, ValueError, IndexError):
        return None


def start() -> None:
    if not ENABLED:
        return
    global _baseline_kb
    _baseline_kb = rss_kb()
    if FULL:
        import tracemalloc

        tracemalloc.start(10)
    log.warning("memtrace on (%s): baseline RSS %s MB", _MODE,
                None if _baseline_kb is None else _baseline_kb // 1024)


def user_finished(source_user: str, every: int = 10) -> None:
    """Call once per completed user. Logs the trend, not every sample.

    `every` exists because one sample per user on a 300-user run is 300 log
    lines nobody reads; the shape is what matters and it shows up in a
    tenth of them.
    """
    if not ENABLED:
        return
    global _completed, _snapshot
    with _lock:
        _completed += 1
        n = _completed
        if n % every:
            return
        now = rss_kb()
        if now is None or _baseline_kb is None:
            return
        grown = now - _baseline_kb
        log.warning(
            "memtrace: %d users done, RSS %d MB (%+d MB since start, "
            "%.1f MB per completed user)",
            n, now // 1024, grown // 1024, grown / 1024 / n)
        if not FULL:
            return
        import tracemalloc

        current = tracemalloc.take_snapshot()
        if _snapshot is not None:
            top = current.compare_to(_snapshot, "lineno")[:5]
            for stat in top:
                log.warning("memtrace:   %s", stat)
        _snapshot = current


def growth_per_user_mb() -> float | None:
    """For tests and for the end-of-run line: MB retained per user, or None
    if nothing was measured."""
    if _baseline_kb is None or not _completed:
        return None
    now = rss_kb()
    if now is None:
        return None
    return (now - _baseline_kb) / 1024 / _completed
