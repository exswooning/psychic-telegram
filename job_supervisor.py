"""Notice a run that has stopped making progress, and end it.

Every other recovery in this tool assumes a process either finishes or
dies. A deadlocked one does neither: it holds its admission slot, keeps its
users marked RUNNING, and reports nothing. Live, a delta wedged on the
logging lock six minutes in and sat there until a person went looking with
py-spy and sent SIGKILL by hand. Nothing in the tool would ever have
noticed.

Two signals together, never one alone:

  * the account's ledger has not been written for `stale_seconds`, and
  * the process has burned no CPU since the previous check.

Either on its own produces false kills. A Drive scan can enumerate for
minutes without writing an audit row -- but it burns CPU the whole time. A
process waiting on a slow API call burns no CPU -- but a run that is
genuinely working writes rows. Requiring both is what makes this safe to
run unattended against a live migration.
"""
from __future__ import annotations

import calendar
import logging
import os
import signal
import sqlite3
import time

import job_admission

log = logging.getLogger("job_supervisor")

# Generous on purpose: the cost of a false kill (a re-run) is small, but the
# cost of killing a healthy long-running scan is a confusing failure that
# looks like the tool eating its own work.
STALL_SECONDS = int(os.getenv("JOB_STALL_SECONDS", "900"))


def cpu_ticks(pid: int) -> int | None:
    """utime + stime for a pid, or None if it cannot be read."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            fields = fh.read().split()
        return int(fields[13]) + int(fields[14])
    except (OSError, IndexError, ValueError):
        return None


def last_ledger_write(db_path: str) -> str | None:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            return conn.execute(
                "SELECT MAX(timestamp) FROM audit_log").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _age_seconds(iso: str | None, now: float) -> float | None:
    """Seconds since an ISO 'YYYY-MM-DDTHH:MM:SSZ' stamp.

    calendar.timegm, not time.mktime: the ledger writes UTC, and mktime
    reads a struct as LOCAL time. On a box an hour off UTC that is an hour
    of error in the one number deciding whether to kill a live migration.
    """
    if not iso:
        return None
    try:
        stamp = time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return now - calendar.timegm(stamp)


class Supervisor:
    """Holds the previous CPU reading, so 'burned no CPU' has a baseline."""

    def __init__(self, db_path_for, stall_seconds: int = STALL_SECONDS,
                 cpu_fn=cpu_ticks, kill_fn=None, now_fn=None):
        self.db_path_for = db_path_for
        self.stall_seconds = stall_seconds
        self.cpu_fn = cpu_fn
        self.kill_fn = kill_fn or (lambda pid: os.kill(pid, signal.SIGKILL))
        self.now_fn = now_fn or time.time
        self._last_cpu: dict[int, int] = {}
        self._first_seen_stale: dict[int, float] = {}

    def check_once(self) -> list[dict]:
        """One pass. Returns what it killed, so a caller can report it."""
        killed: list[dict] = []
        now = self.now_fn()
        live_pids = set()

        for job in job_admission.list_active():
            pid = job.get("pid")
            if not pid or not job_admission.is_live(job):
                continue          # reap_dead owns the dead ones
            live_pids.add(pid)

            db_path = self.db_path_for(job.get("account_id"))
            if not db_path:
                continue
            written = last_ledger_write(db_path)
            age = _age_seconds(written, now)
            cpu = self.cpu_fn(pid)
            prev = self._last_cpu.get(pid)
            self._last_cpu[pid] = cpu if cpu is not None else prev

            # Unknown CPU means we cannot tell working from wedged, and a
            # guess here kills real work. Leave it alone and say so once.
            if cpu is None or prev is None:
                continue

            quiet_ledger = age is not None and age >= self.stall_seconds
            no_cpu = cpu == prev
            if not (quiet_ledger and no_cpu):
                self._first_seen_stale.pop(pid, None)
                continue

            # Both signals, twice running: one sample could straddle a
            # genuinely idle moment between two units of work.
            first = self._first_seen_stale.setdefault(pid, now)
            if now - first < self.stall_seconds:
                continue

            log.error("job %s (pid %s, account %s) has written nothing for "
                      "%.0fs and used no CPU since the last check; killing it "
                      "so the slot frees and the next run can resume",
                      job.get("job_name"), pid, job.get("account_id"), age)
            try:
                self.kill_fn(pid)
                killed.append({"pid": pid, "job_name": job.get("job_name"),
                               "account_id": job.get("account_id"),
                               "silent_for": age})
            except (OSError, ProcessLookupError) as exc:
                log.warning("could not kill wedged pid %s: %s", pid, exc)
            self._first_seen_stale.pop(pid, None)

        for pid in [p for p in self._last_cpu if p not in live_pids]:
            self._last_cpu.pop(pid, None)
            self._first_seen_stale.pop(pid, None)
        job_admission.reap_dead()
        return killed
