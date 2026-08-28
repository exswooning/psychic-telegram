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


def _epoch(iso: str | None) -> float | None:
    """An active_jobs started_at as epoch seconds, UTC like the ledger."""
    if not iso:
        return None
    try:
        return calendar.timegm(time.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def last_output_write(job_name: str, account_id) -> float | None:
    """When this job last wrote to its own transcript, as an epoch time.

    The ledger is not evidence for every job. seed, reset, provision,
    check_seed and teardown write no id_mapping rows at all, so
    last_ledger_write returns whatever a PREVIOUS run left -- arbitrarily
    old -- and the ledger half of the stall test is satisfied from the
    moment they start. What kept those from being killed on sight was the
    CPU half, which is a coincidence rather than a design.

    Their transcript is the honest signal: the child writes to it directly,
    unbuffered, and a healthy run touches it.
    """
    try:
        import webui
        path = webui.job_log_path(account_id, job_name or "")
    except Exception as exc:  # noqa: BLE001 - never break a supervisor pass
        log.debug("no transcript path for %s/%s: %r", account_id, job_name, exc)
        return None
    try:
        return os.path.getmtime(path)
    except OSError:
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


def _spawn(argv: list[str], cwd: str | None) -> int:
    """Relaunch detached, with its output on a file.

    start_new_session so the child outlives this process -- the supervisor
    lives inside the API server, and a deploy restarts that. DEVNULL on
    stdin and a file for output because a pipe with nobody reading it fills
    its ~64KB buffer and deadlocks the child inside logging: that is a
    failure this codebase has already paid for once.
    """
    import subprocess

    out = os.path.join(cwd or os.getcwd(), "logs", "supervisor-resume.log")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "ab", buffering=0) as fh:
        proc = subprocess.Popen(argv, cwd=cwd or None, stdin=subprocess.DEVNULL,
                                stdout=fh, stderr=fh, start_new_session=True)
    return proc.pid


class Supervisor:
    """Holds the previous CPU reading, so 'burned no CPU' has a baseline."""

    def __init__(self, db_path_for, stall_seconds: int = STALL_SECONDS,
                 cpu_fn=cpu_ticks, kill_fn=None, now_fn=None,
                 spawn_fn=None,
                 output_fn=None, signal_fn=None):
        self.db_path_for = db_path_for
        self.stall_seconds = stall_seconds
        self.cpu_fn = cpu_fn
        self.kill_fn = kill_fn or (lambda pid: os.kill(pid, signal.SIGKILL))
        # Tried first, and on its own pass: the engine handles SIGINT
        # cooperatively, finishing in-flight items and committing state so a
        # re-run resumes instead of redoing the work.
        #
        # Falls back to kill_fn, not to os.kill, when a caller supplied one:
        # anything that took control of how this process ends did so to stop
        # real signals being sent, and adding a second signalling path with
        # its own default quietly re-armed that. A test doing exactly this
        # SIGINT-ed the pytest process running it.
        self.spawn_fn = spawn_fn or _spawn
        self.signal_fn = signal_fn or kill_fn or (
            lambda pid: os.kill(pid, signal.SIGINT))
        self.now_fn = now_fn or time.time
        self.output_fn = output_fn or last_output_write
        self._last_cpu: dict[int, int] = {}
        self._first_seen_stale: dict[int, float] = {}
        self._interrupted: dict[int, float] = {}

    def _resume(self, job: dict) -> str:
        """Start the killed job again, or say why not.

        Returns a short reason string rather than a bool: "why didn't it come
        back" is the first question asked, and a silent False leaves the
        supervisor looking like the thing that broke it.
        """
        argv, cwd = job_admission.resumable(job)
        if argv is None:
            return "not resumable (no recorded command -- started outside the UI)"
        used = int(job.get("resumes") or 0)
        if used >= job_admission.MAX_RESUMES:
            log.error("job %s has already been resumed %s time(s); leaving it "
                      "down for a person to look at", job.get("job_name"), used)
            return f"budget spent ({used}/{job_admission.MAX_RESUMES})"
        try:
            n = job_admission.note_resume(job["id"])
            proc = self.spawn_fn(argv, cwd)
        except Exception as exc:      # noqa: BLE001 - never break the pass
            log.error("could not resume %s: %r", job.get("job_name"), exc)
            return f"resume failed: {str(exc)[:120]}"
        log.error("resumed %s as pid %s (resume %s of %s)",
                  job.get("job_name"), proc, n, job_admission.MAX_RESUMES)
        return f"resumed as pid {proc} ({n}/{job_admission.MAX_RESUMES})"

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

            # Whichever signal is FRESHEST proves the job is alive: a
            # migration writes ledger rows, a seed writes only its
            # transcript, and either one means it is working.
            ages = []
            db_path = self.db_path_for(job.get("account_id"))
            if db_path:
                ages.append(_age_seconds(last_ledger_write(db_path), now))
            # A transcript from a PREVIOUS run of the same job is not
            # evidence about this one -- it is the identical trap
            # last_ledger_write falls into, and it read a day-old file as
            # "silent for 102487s" and interrupted a healthy run. Only
            # count the file if this run has actually written to it.
            out_at = self.output_fn(job.get("job_name"), job.get("account_id"))
            started = _epoch(job.get("started_at"))
            if out_at is not None and (started is None or out_at >= started):
                ages.append(now - out_at)
            ages = [a for a in ages if a is not None]
            if not ages:
                continue          # no evidence either way; never guess
            age = min(ages)
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
                self._interrupted.pop(pid, None)
                continue

            # Both signals, twice running: one sample could straddle a
            # genuinely idle moment between two units of work.
            first = self._first_seen_stale.setdefault(pid, now)
            if now - first < self.stall_seconds:
                continue

            # Interrupt first, kill only if that did not take. A child can
            # accept SIGINT and still hang: seed_sandbox unwinds into
            # ThreadPoolExecutor.__exit__, which joins workers blocked in a
            # Google API call, and sits in _wait_for_tstate_lock forever.
            # That is the case a human had to notice and kill by hand.
            interrupted_at = self._interrupted.get(pid)
            if interrupted_at is None:
                log.error("job %s (pid %s, account %s) has written nothing "
                          "for %.0fs and used no CPU since the last check; "
                          "interrupting it",
                          job.get("job_name"), pid, job.get("account_id"), age)
                try:
                    self.signal_fn(pid)
                    self._interrupted[pid] = now
                except (OSError, ProcessLookupError) as exc:
                    log.warning("could not interrupt wedged pid %s: %s", pid, exc)
                continue
            if now - interrupted_at < self.stall_seconds:
                continue          # give the cooperative path its chance
            log.error("job %s (pid %s) ignored the interrupt for %.0fs; "
                      "killing it so the slot frees",
                      job.get("job_name"), pid, now - interrupted_at)
            try:
                self.kill_fn(pid)
                entry = {"pid": pid, "job_name": job.get("job_name"),
                         "account_id": job.get("account_id"),
                         "silent_for": age}
                # Freeing the slot was only ever half of it. Until this, a
                # migration that wedged unattended stayed down until a person
                # noticed and pressed the button again -- which is precisely
                # what an unattended run cannot depend on.
                entry["resumed"] = self._resume(job)
                killed.append(entry)
            except (OSError, ProcessLookupError) as exc:
                log.warning("could not kill wedged pid %s: %s", pid, exc)
            self._first_seen_stale.pop(pid, None)
            self._interrupted.pop(pid, None)

        for pid in [p for p in self._last_cpu if p not in live_pids]:
            self._last_cpu.pop(pid, None)
            self._first_seen_stale.pop(pid, None)
            self._interrupted.pop(pid, None)
        job_admission.reap_dead()
        return killed
