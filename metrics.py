"""
metrics.py
==========
Per-request latency and throughput, recorded at the one point every Google
call already passes through.

Why this comes before the concurrency work
------------------------------------------
Every estimate in this engine's performance analysis rests on a round-trip
time nobody has measured. The serial-Gmail ceiling was computed from a guessed
200 ms; the per-file Drive cost is now measured in *calls* but not in seconds;
and an adaptive controller cannot ramp on a signal it does not collect. Sizing
an in-flight window against a guessed RTT would produce a number that looks
principled and is not.

So this lands first, the A/B runs against it, and #6 gets built on a
distribution instead of an assumption.

What it records
---------------
One sample per API call attempt: elapsed seconds, whether it succeeded, and
the thread that made it. That is enough for the three numbers that matter --
p50/p95 latency (the steering signal for an adaptive controller, because
Google queues before it rejects), requests per second per worker (the
diagnostic metric), and the retry rate (the emergency brake).

Deliberately not a time series. A migration runs for hours and nothing here
should grow without bound: samples are kept in a fixed-size reservoir per
label, so memory is constant regardless of run length.
"""

from __future__ import annotations

import random
import threading
import time
from collections import defaultdict, deque

# Per label. 4,000 samples is enough for a stable p95 and costs ~32 KB.
RESERVOIR = 4000

# The control window, in samples per label.
#
# A uniform reservoir answers "what was p95 over the whole run", which is the
# right statistic for a report and the wrong one for a controller. After
# 100,000 calls a latency inflection in the last two minutes moves it by
# almost nothing -- so an AIMD loop steering on it would be blind to exactly
# the signal it exists to detect, while showing a number that is real, stable,
# and useless. The controller reads this instead: the most recent N samples,
# where a change in conditions shows up within N calls rather than being
# averaged into eight hours of history.
#
# 200 is roughly a minute of one worker's calls at the measured 284 ms p50,
# so it reacts inside the time a human would notice a stall.
RECENT = 200


class _Reservoir:
    """Fixed-size uniform sample. Constant memory over an eight-hour run."""

    __slots__ = ("samples", "seen")

    def __init__(self) -> None:
        self.samples: list[float] = []
        self.seen = 0

    def add(self, value: float) -> None:
        self.seen += 1
        if len(self.samples) < RESERVOIR:
            self.samples.append(value)
            return
        # Classic reservoir sampling: every observation has an equal chance of
        # being retained, so a slow patch at hour six is as visible as one at
        # hour one.
        j = random.randrange(self.seen)
        if j < RESERVOIR:
            self.samples[j] = value


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lat: dict[str, _Reservoir] = defaultdict(_Reservoir)
        # The control signal, kept separately from the report signal because
        # they answer different questions. deque with maxlen is O(1) and
        # self-trimming, so this costs nothing over a long run.
        self._recent: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=RECENT))
        self._calls: dict[str, int] = defaultdict(int)
        self._retries: dict[str, int] = defaultdict(int)
        self._failures: dict[str, int] = defaultdict(int)
        self._threads: set[str] = set()
        self._started = time.monotonic()
        self.enabled = True

    def record(self, label: str, seconds: float, *, ok: bool = True,
               retried: bool = False) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._lat[label].add(seconds)
            self._recent[label].append(seconds)
            self._calls[label] += 1
            if retried:
                self._retries[label] += 1
            if not ok:
                self._failures[label] += 1
            self._threads.add(threading.current_thread().name)

    def recent(self, label: str | None = None) -> dict:
        """
        Latency over the last RECENT samples -- the controller's steering
        signal.

        Deliberately separate from snapshot(): that one reports the run, this
        one reports now. A controller polling snapshot() would ramp against a
        number that cannot move.
        """
        with self._lock:
            if label is not None:
                values = list(self._recent.get(label, ()))
            else:
                values = [v for d in self._recent.values() for v in d]
        # Sorted outside the lock: every record() contends on it, and a
        # controller polling once a second would otherwise stall every worker
        # while it sorts.
        return {
            "n": len(values),
            "p50": self._pct(values, 50),
            "p95": self._pct(values, 95),
        }

    def reset(self) -> None:
        with self._lock:
            self._lat.clear()
            self._recent.clear()
            self._calls.clear()
            self._retries.clear()
            self._failures.clear()
            self._threads.clear()
            self._started = time.monotonic()

    # -- reading -------------------------------------------------------------
    @staticmethod
    def _pct(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        k = min(len(ordered) - 1, int(round((p / 100.0) * (len(ordered) - 1))))
        return ordered[k]

    def snapshot(self) -> dict:
        # Copy under the lock, sort outside it. Percentiles sort every
        # reservoir, and record() takes the same lock on every API call -- so
        # computing in here would block every worker on fourteen sorted
        # arrays each time anything asked for a reading.
        with self._lock:
            elapsed = max(time.monotonic() - self._started, 1e-6)
            total = sum(self._calls.values())
            workers = max(len(self._threads), 1)
            raw = {label: (list(res.samples), self._calls[label],
                           self._retries[label], self._failures[label])
                   for label, res in self._lat.items()}

        all_lat = [v for samples, *_ in raw.values() for v in samples]
        per_label = {}
        for label, (samples, calls, retries, failures) in raw.items():
            per_label[label] = {
                "calls": calls,
                "retries": retries,
                "failures": failures,
                "p50": self._pct(samples, 50),
                "p95": self._pct(samples, 95),
            }
        return {
            "elapsed_sec": elapsed,
            "calls": total,
            "workers": workers,
            "requests_per_sec": total / elapsed,
            # The metric this analysis kept quoting without collecting.
            "requests_per_sec_per_worker": total / elapsed / workers,
            "p50": self._pct(all_lat, 50),
            "p95": self._pct(all_lat, 95),
            "p99": self._pct(all_lat, 99),
            "retries": sum(self._retries.values()),
            "failures": sum(self._failures.values()),
            "by_label": per_label,
        }

    def report(self) -> str:
        s = self.snapshot()
        if not s["calls"]:
            return "  no API calls recorded"
        lines = [
            f"  elapsed            {s['elapsed_sec']:.0f}s",
            f"  API calls          {s['calls']:,}",
            f"  worker threads     {s['workers']}",
            f"  requests/sec       {s['requests_per_sec']:.2f}",
            f"  per worker         {s['requests_per_sec_per_worker']:.2f}",
            f"  latency p50/p95/p99  {s['p50'] * 1000:.0f} / "
            f"{s['p95'] * 1000:.0f} / {s['p99'] * 1000:.0f} ms",
            f"  retries            {s['retries']:,} "
            f"({s['retries'] / s['calls'] * 100:.1f}%)",
            f"  failures           {s['failures']:,}",
        ]
        if len(s["by_label"]) > 1:
            lines += ["", f"  {'call':<28}{'n':>7}{'p50':>8}{'p95':>8}{'retry':>7}"]
            for label, d in sorted(s["by_label"].items(),
                                   key=lambda kv: -kv[1]["calls"])[:14]:
                lines.append(
                    f"  {label[:26]:<28}{d['calls']:>7}"
                    f"{d['p50'] * 1000:>7.0f}m{d['p95'] * 1000:>7.0f}m"
                    f"{d['retries']:>7}")
        return "\n".join(lines)


# One collector per process. The engines reach it through resilience's retry
# decorator rather than importing it directly, so no call site has to change.
METRICS = Metrics()
