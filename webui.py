"""
webui.py
========
Browser front-end for the migration: live status, one-click actions, streaming
output.

Security posture — read this before changing the bind address
-------------------------------------------------------------
This process runs commands on the host. That makes it a remote-code-execution
surface, so it is built to be hard to expose by accident:

* It binds to **127.0.0.1** only. Reach it over an SSH tunnel:

      ssh -L 8080:localhost:8080 root@your-vps
      # then open http://localhost:8080

* There is **no arbitrary command execution**. The browser can only name an
  action from ACTIONS below; the server maps that name to a fixed argv list.
  Nothing the client sends is ever concatenated into a shell string, and
  `shell=True` appears nowhere.

* Destructive actions are marked `destructive` and require the client to echo
  back a confirmation phrase the server checks. That is a guard against a
  mis-click, not against an attacker -- the real protection is the loopback
  bind.

* `--host` exists for unusual setups but prints a loud warning. Binding this
  to a public interface would hand anyone who finds the port a root shell in
  all but name.

No dependencies beyond the standard library, deliberately: this should not
need a pip install on a migration host at 2am.

    python3 webui.py                 # http://127.0.0.1:8080
    python3 webui.py --port 9000
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import account_context  # the one rule both servers answer with
import accounts_auth  # stdlib-only itself; does not break the no-pip-install promise
import fleet_agent  # stdlib-only; shares the process-scan naming rule
import job_admission  # same -- control_plane_db is stdlib-only too

try:
    from wizard import State, build_steps
except Exception:  # noqa: BLE001 - the UI should still load and say why
    State = None
    build_steps = None

log = logging.getLogger("webui")

PY = sys.executable
# This deployment's root. Jobs run from here unless told otherwise (see
# Job.start), so a relative path in an action's argv resolves against it.
HERE = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------
# The complete set of things the browser may ask for. Anything not here
# cannot be run, however the request is crafted.
# ----------------------------------------------------------------------
ACTIONS: dict[str, dict] = {
    "preflight": {
        "label": "Preflight",
        "blurb": "Mint a token and make one call per user, both tenants.",
        "argv": [PY, "main.py", "preflight"],
    },
    "scope": {
        "label": "Show scope + required OAuth",
        "blurb": "What migrates, and the exact scopes this config needs.",
        "argv": [PY, "main.py", "scope"],
    },
    "export_scope": {
        "label": "Export SCOPE.md",
        "blurb": "Write the scope matrix to SCOPE.md for the approval ticket.",
        "argv": [PY, "main.py", "scope", "--format", "markdown", "--out", "SCOPE.md"],
    },
    "discover": {
        "label": "Discover",
        "blurb": "Read-only scan: counts, depth, size, duration estimate.",
        "argv": [PY, "main.py", "discover", "--include-mail"],
    },
    "check_seed_accounts": {
        "label": "Check seed accounts",
        "blurb": "Verify the accounts the seeder will target exist -- every "
                 "user the source tenant already has, or the 5-user default "
                 "if that lookup cannot run.",
        "argv": [PY, "check_seed.py", "accounts"],
    },
    "check_seed_scopes": {
        "label": "Check seed scopes",
        "blurb": "Verify the seeder's write scopes are authorised "
                 "(incl. admin.directory.user).",
        "argv": [PY, "check_seed.py", "scopes"],
    },
    "init_db": {
        "label": "Create database + load identities",
        "blurb": "Reads identities.csv into identity_map. Safe to re-run.",
        "argv": [PY, "main.py", "init-db", "--identities", "identities.csv"],
    },
    "init_db_auto": {
        "label": "Load identities by matching localparts",
        "blurb": "Reads the source directory live and maps each user to the "
                 "same localpart on the target domain, including users the "
                 "target does not have yet so provision-users can create "
                 "them. Needs delegation first. Creates no accounts.",
        # --include-missing is not optional here, whatever its flag name
        # suggests. Without it auto-map pairs only accounts that ALREADY
        # exist on the target, while provision-users only creates accounts
        # already in identity_map -- so on a fresh target neither command
        # can start the other. main.py's own comment records the observed
        # result: a target holding only info@ mapped 1 of 201 source users
        # and reported "nothing to create", correctly and uselessly. This
        # button is reached from the wizard, whose whole premise is a target
        # that has not been provisioned yet.
        "argv": [PY, "main.py", "init-db", "--auto-map", "--include-missing"],
    },
    "provision_dry": {
        "label": "Provision users (dry run)",
        "blurb": "Report which target accounts are missing. Creates nothing.",
        "argv": [PY, "main.py", "provision-users", "--tenant", "target", "--dry-run"],
    },
    "provision": {
        "label": "Create the missing target accounts",
        "blurb": "Only ever creates; existing addresses are left untouched.",
        "argv": [PY, "main.py", "provision-users", "--tenant", "target", "--yes"],
        "destructive": True,
        "confirm": "CREATE",
    },
    "migrate_dry": {
        "label": "Migrate (dry run)",
        "blurb": "Log every intended write, perform none.",
        "argv": [PY, "main.py", "--dry-run", "migrate"],
    },
    "migrate": {
        "label": "Migrate",
        "blurb": "The real copy. Resumable — safe to interrupt.",
        "argv": [PY, "main.py", "migrate", "--services", "drive,gmail,calendar"],
        "destructive": True,
        "confirm": "MIGRATE",
    },
    "delta": {
        "label": "Delta pass",
        "blurb": "Catch up anything changed since the bulk copy.",
        "argv": [PY, "main.py", "delta", "--services", "drive,gmail,calendar",
                 "--days", "2"],
    },
    "verify": {
        "label": "Verify",
        "blurb": "Ask the target directly and compare against source.",
        "argv": [PY, "verify.py", "--samples", "25"],
    },
    "report": {
        "label": "Report",
        "blurb": "Per-user counts and outstanding failures.",
        "argv": [PY, "main.py", "report"],
    },
    "resolve_dry": {
        "label": "Resolve failures (dry run)",
        "blurb": "Show what a retry of FAILED items would do.",
        "argv": [PY, "resolve_failures.py", "--dry-run"],
    },
    "undo_dry": {
        "label": "Undo (dry run)",
        "blurb": "Count exactly what a targeted undo would delete.",
        "argv": [PY, "undo_migration.py", "--dry-run"],
    },
    "undo": {
        "label": "Undo migration",
        "blurb": "Delete only what id_mapping records as migrated.",
        "argv": [PY, "undo_migration.py", "--yes"],
        "destructive": True,
        "confirm": "UNDO",
    },
    "resolve": {
        "label": "Resolve failures",
        "blurb": "Retry every FAILED item with the current code.",
        "argv": [PY, "resolve_failures.py"],
    },
    # There were no backups at all until this existed -- the only record of
    # what a migration moved was one SQLite file on one box, 5.7 GB of it
    # for a single account. VACUUM INTO rather than a copy, because a live
    # ledger has committed pages in the WAL the main file does not have yet.
    # audit_log records every attempt and nothing ever removed one. On the
    # live box one account's ledger reached 5.7 GB, ~99.5% of it SUCCESS
    # rows describing work id_mapping already proves happened -- on a disk
    # 67% full. The tool to collapse them has existed all along with no way
    # to run it.
    "audit_prune_dry": {
        "label": "Prune audit log (dry run)",
        "blurb": "How many SUCCESS rows could be collapsed into counts. "
                 "Only finished users, never a FAILED or SKIPPED row, and "
                 "the counts move to audit_rollup rather than being lost.",
        "argv": [PY, "audit_retention.py"],
    },
    "audit_prune": {
        "label": "Prune audit log",
        "blurb": "Collapse them, verify the counts still add up, then "
                 "reclaim the freed space to the filesystem.",
        "argv": [PY, "audit_retention.py", "--apply", "--vacuum"],
    },

    "backup_now": {
        "label": "Back up the ledgers",
        "blurb": "Consistent, verified copy of the control plane and every "
                 "account ledger. Safe to run mid-migration -- it takes a "
                 "read snapshot rather than copying the file underneath.",
        "argv": [PY, "backup_db.py"],
    },
    "backup_list": {
        "label": "List backups",
        "blurb": "What is on file, and how big.",
        "argv": [PY, "backup_db.py", "--list"],
    },

    # The Maintenance page has listed this in MAINTENANCE_KEYS since it was
    # written, and there was no ACTIONS entry to match -- the page renders
    # `actions[key] && <JobRunner .../>`, so the slot silently drew nothing.
    # A working repair tool, reachable only by SSH, behind a UI that already
    # believed it was offering it.
    "repair_modified_times_dry": {
        "label": "Repair Drive timestamps (dry run)",
        "blurb": "Count files whose target modifiedTime does not match the "
                 "source. Granting an ACL bumps modifiedTime, so data "
                 "migrated before that fix is stamped with the migration "
                 "date -- and neither a re-run nor a delta corrects it.",
        "argv": [PY, "repair_modified_times.py", "--dry-run"],
    },
    "repair_modified_times": {
        "label": "Repair Drive timestamps",
        "blurb": "Patch modifiedTime on the target to match the source. "
                 "Read-only against the source, touches nothing else, safe "
                 "to re-run.",
        "argv": [PY, "repair_modified_times.py"],
    },

    "backfill_drive": {
        "label": "Backfill: Drive done",
        "blurb": "Mark Drive complete on a ledger from before per-service "
                 "tracking existed. Only records what audit_log proves ran.",
        "argv": [PY, "main.py", "backfill-services", "--services", "drive"],
    },

    # -- phased migration: every phase, each reconciled against the tenants
    # directly rather than trusted from the ledger -----------------------
    "phased_count_only": {
        "label": "Reconcile (no migration)",
        "blurb": "Count both tenants and compare, without moving anything.",
        "argv": [PY, "phases.py", "--count-only"],
    },
    "phased_migrate": {
        "label": "Migrate: full scope",
        "blurb": "Drive, shared drives, Gmail, Calendar, Contacts, Tasks, "
                 "Chat — in that order, each reconciled before the next runs. "
                 "Chat/Contacts/Tasks follow the checkboxes above.",
        "argv": [PY, "phases.py", "--continue-on-gap"],
        "destructive": True,
        "confirm": "MIGRATE",
    },

    # -- Google's own Data Migration Service moves the mail. This drives the
    # Admin-console flow (dms_migrate handles all four steps) and then just
    # clicks Start import; Google does the copying server-side, so it does
    # NOT hold the one-heavy-job slot and runs in parallel with the engine
    # migration above. That parallelism is the whole reason mail is handed
    # off: mail is the only service behind Google's 3-writes/sec ceiling.
    "dms_import": {
        "label": "Start Google DMS mail import",
        "blurb": "Hand the mail to Google's Data Migration Service. Runs in "
                 "the browser against the Admin console, spends none of this "
                 "project's Gmail quota, and runs alongside the engine "
                 "migration. Run the full-scope migration with Gmail OFF "
                 "first, so nothing is copied twice.",
        "argv": [PY, "dms_migrate.py", "--apply",
                 "--identities", "identities.csv", "--timeout", "200"],
        "browser": True,        # needs DISPLAY + DWD creds
        "parallel": True,       # exempt from the one-heavy-job admission
        "destructive": True,
        "confirm": "DMS",
    },
    "dms_metrics_refresh": {
        "label": "Refresh DMS metrics",
        "blurb": "Read the live import counters from the Admin console and "
                 "cache them for the UI. Read-only; safe to run any time.",
        "argv": [PY, "dms_migrate.py", "--status", "--timeout", "150"],
        "browser": True,
        "parallel": True,
    },

    # -- shared drives: tenant-wide, so not part of the per-user toggles --
    "shared_drives_inventory": {
        "label": "Shared drives: inventory",
        "blurb": "Count files per shared drive. Read-only.",
        "argv": [PY, "shared_drives.py", "--inventory", "--all-drives"],
    },
    "shared_drives_migrate": {
        "label": "Shared drives: migrate",
        "blurb": "Create each shared drive on the target, restore membership "
                 "organizer-first, then copy its contents.",
        "argv": [PY, "shared_drives.py", "--migrate", "--all-drives"],
        "destructive": True,
        "confirm": "MIGRATE",
    },

    # -- per-file share access, verified one by one, not as a total -------
    "acl_audit": {
        "label": "Verify share access",
        "blurb": "Pairs every source file to the target file it became and "
                 "diffs the grant set. A total can reconcile while sharing "
                 "is wrong; this is the check that would catch it.",
        "argv": [PY, "acl_audit.py", "--json", "acl_audit.json"],
    },

    # -- SSO: org-wide login configuration, so this stays manual on purpose.
    # MIGRATE_SSO must already be set in env.sh; this button does not set it,
    # unlike the per-user services above, because writing an SSO profile
    # changes how everyone signs in, including whoever is running this. --
    "sso_inventory": {
        "label": "SSO: inventory",
        "blurb": "What's migratable (inbound SAML), what can only be listed "
                 "('Sign in with Google' grants), what can't (saved "
                 "passwords). Read-only.",
        "argv": [PY, "sso.py", "--inventory"],
    },
    "sso_migrate": {
        "label": "SSO: create profiles (unassigned)",
        "blurb": "Recreates each SAML profile on the target, unassigned. "
                 "Needs MIGRATE_SSO=true already set in env.sh — this button "
                 "will not set it for you.",
        "argv": [PY, "sso.py", "--migrate"],
        "destructive": True,
        "confirm": "SSO",
    },
}

# ----------------------------------------------------------------------
# A seed job's numeric progress.
#
# seed_sandbox.py has no structured progress protocol -- it is a CLI script
# printing to stdout, not an API. The nav bar's progress figure has to come
# from somewhere, though, and this engine's own discipline (see
# webui_spa.py's module docstring) is to say plainly when a number is a
# proxy rather than inventing a false-precision one. This one is real, just
# derived: seed_sandbox.py always prints "Seeding N users in ..." once
# per run, and exactly one "done in" or "FAILED" line per user as it
# finishes -- both attempted-so-far, so a completed run always reaches
# 100%, and a straggler mid-run never overstates itself.
# ----------------------------------------------------------------------
_SEED_TOTAL_RE = re.compile(r"^Seeding (\d+) users? in\b")
_SEED_DONE_RE = re.compile(r"^\s*\[.+?\]\s+done in\b")
_SEED_FAILED_RE = re.compile(r"^\s*!\s+\S+\s+FAILED:")


# "[7/201] someone@example.com: 4292 messages deleted" -- any job that
# counts its own work this way gets a progress bar for free. Added for the
# seeder's reset, which prints nothing else a percentage could come from:
# _SEED_TOTAL_RE only matches the "Seeding N users" banner a seeding run
# prints, so a reset-only run sat at no progress at all for its whole life.
_COUNTER_RE = re.compile(r"^\s*\[(\d+)\s*/\s*(\d+)\]")


def _counter_progress_pct(lines: list[str]) -> int | None:
    """Highest [done/total] seen, as a percentage. None if nothing counts.

    Highest rather than last: the lines arrive in completion order from a
    thread pool, so the newest line is not reliably the largest.
    """
    best = None
    for ln in lines:
        m = _COUNTER_RE.match(ln)
        if not m:
            continue
        done, total = int(m.group(1)), int(m.group(2))
        if total > 0:
            pct = round(min(done, total) / total * 100)
            best = pct if best is None else max(best, pct)
    return best


def _seed_progress_pct(lines: list[str]) -> int | None:
    total = None
    for ln in lines:
        m = _SEED_TOTAL_RE.match(ln)
        if m:
            total = int(m.group(1))
            break
    if not total:
        return None
    attempted = sum(1 for ln in lines
                    if _SEED_DONE_RE.match(ln) or _SEED_FAILED_RE.match(ln))
    return round(min(attempted, total) / total * 100)


# ----------------------------------------------------------------------
# One job at a time, with its output buffered for streaming to the page.
# ----------------------------------------------------------------------
# How long Stop waits for a cooperative exit before saying so. Long
# enough for an engine to finish an in-flight item, short enough that the
# operator is not left staring at a spinner.
STOP_GRACE_SECONDS = 10.0


class Job:
    def __init__(self, account_id: int | None = None) -> None:
        self.account_id = account_id
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.name = ""
        self.lines: list[str] = []
        self.started = 0.0
        self.finished = 0.0
        self.rc: int | None = None
        self._on_finish: Callable[[int | None], None] | None = None
        # Set per run by start(); the on-disk transcript this job's output
        # is written to, and the thing that makes its output survive a
        # restart of this process. See start()'s own comment.
        self.log_path = ""
        self._log_offset = 0
        self._log_fh = None

    @property
    def running(self) -> bool:
        # Not `proc.poll() is None`: that flips the instant the child exits,
        # which is BEFORE _drain() has read the child's last output, set rc,
        # frozen `finished`, saved the result, or released the admission
        # slot. Every caller here waits for `not running` and then reads
        # exactly those -- so the old definition made all of them racy, and
        # two tests caught it (a finished job whose duration kept growing,
        # and an rc still reading None). rc is set once, by _drain, as the
        # final step, so it is the honest "this job is completely done"
        # signal.
        return self.proc is not None and self.rc is None

    def start(self, name: str, argv: list[str],
              env: dict | None = None, cwd: str | None = None,
              on_finish: Callable[[int | None], None] | None = None
              ) -> tuple[bool, str]:
        with self.lock:
            if self.running:
                return False, f"{self.name} is still running"
            self.name, self.lines, self.rc = name, [], None
            self.started, self.finished = time.time(), 0.0
            # Only deploy_remote.py's caller passes this today -- it is how
            # deploy history learns the outcome of a job that runs detached
            # from the request that started it, without polling its own
            # poll loop just to catch the one moment rc stops being None.
            self._on_finish = on_finish
            env = dict(env or os.environ)
            env.setdefault("PYTHONUNBUFFERED", "1")
            # A FILE, not subprocess.PIPE. A pipe's read end is held only by
            # this process, and systemd's KillMode=process deliberately keeps
            # these children alive across a webui restart -- so every deploy
            # left a running job writing into a pipe whose only reader was
            # gone. Three separate real failures came out of that:
            #   * the job's entire transcript was unrecoverable after a
            #     restart, and _process_output_tail() silently fell back to
            #     an unrelated migration.log -- confirmed live, the UI streamed
            #     a THREE-DAY-OLD log from a different job as a running seed's
            #     "live" output;
            #   * once the 64KB pipe buffer filled with nobody draining it,
            #     the child would block forever on its next print();
            #   * a write to a pipe with no reader raises BrokenPipeError,
            #     which is the most likely explanation for the seed run that
            #     vanished mid-deploy earlier with no OOM and no result file.
            # A file has none of those properties, and it makes the on-disk
            # transcript the single source of truth that survives anything.
            self.log_path = job_log_path(self.account_id, name)
            self._log_offset = 0
            try:
                os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
                # Truncated per run: this is "the current run's output", the
                # same contract the in-memory list had. The previous run's
                # transcript is already preserved by _save_result().
                self._log_fh = open(self.log_path, "w", encoding="utf-8")
            except OSError as exc:
                return False, f"could not open job log {self.log_path}: {exc}"
            try:
                self.proc = subprocess.Popen(
                    argv, stdout=self._log_fh, stderr=subprocess.STDOUT,
                    # A child must never block on a hidden interactive prompt:
                    # input() against the server's terminal hangs the job with
                    # no output, which reads as a stalled migration.
                    stdin=subprocess.DEVNULL,
                    env=env,
                    cwd=cwd or os.path.dirname(os.path.abspath(__file__)),
                )
            except Exception as exc:  # noqa: BLE001
                self._log_fh.close()
                # `running` is (proc is not None and rc is None), so leaving
                # a previous run's proc object here would report this job as
                # permanently running after a failed launch.
                self.proc = None
                return False, str(exc)
            # Tell job_admission which process this admission is for.
            # Only api_server.py did this, so every job webui launches --
            # seed, reset target, reset drive ledger, wipe target, and every
            # ACTIONS button -- left pid NULL in active_jobs. is_live() reads
            # a pid-less row as dead once it is 120s old, so two minutes into
            # any seed the row was reaped and:
            #   * Running Now went blank while the seed was plainly running
            #   * MAX_CONCURRENT_TENANT_JOBS silently stopped applying, so a
            #     second heavy job could start on top of the first
            #   * job_supervisor never saw it, because it iterates the table
            # Confirmed live: a 32-minute seed, mid-run, with active_jobs [].
            try:
                job_admission.record_launch(self.account_id, name,
                                            self.proc.pid, argv, HERE)
            except Exception as exc:  # noqa: BLE001 - never fail a launch
                print(f"could not record pid for {name!r}: {exc}", flush=True)
            threading.Thread(target=self._drain, daemon=True).start()
            return True, "started"

    def _ingest_new_lines(self) -> None:
        """Append whatever the child has written since the last read.

        Offset-based rather than re-reading the file: a long migrate's log
        runs to megabytes, and this is called on a timer for as long as the
        job lives.
        """
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as fh:
                fh.seek(self._log_offset)
                chunk = fh.read()
                self._log_offset = fh.tell()
        except OSError:
            return
        if not chunk:
            return
        # A trailing partial line (the child is mid-write) is left for the
        # next pass rather than shown as a truncated line that then changes.
        if not chunk.endswith("\n"):
            keep = chunk.rfind("\n")
            if keep == -1:
                self._log_offset -= len(chunk.encode("utf-8", "replace"))
                return
            self._log_offset -= len(chunk[keep + 1:].encode("utf-8", "replace"))
            chunk = chunk[:keep + 1]
        with self.lock:
            for line in chunk.splitlines():
                # FutureWarnings from the google libraries drown everything else.
                if "FutureWarning" in line or "warnings.warn" in line:
                    continue
                self.lines.append(line)
            if len(self.lines) > 4000:
                del self.lines[:len(self.lines) - 4000]

    def _drain(self) -> None:
        assert self.proc
        # wait(), not a poll-and-sleep loop: this returns the moment the
        # child exits, so `finished` is frozen at the real end time rather
        # than up to a tick late (which showed up as a completed job whose
        # reported duration kept growing). Nothing needs to be read on a
        # timer here -- snapshot() ingests from the log itself, so live
        # readers stay current without this thread doing anything.
        rc = self.proc.wait()
        # Before rc: `running` is derived from rc, and every caller that
        # waits for `not running` then immediately reads the output. Reading
        # the child's final lines first is what makes that safe.
        self._ingest_new_lines()
        try:
            self._log_fh.close()
        except Exception:  # noqa: BLE001
            pass
        self.finished = time.time()
        self.rc = rc
        self._save_result()
        if self._on_finish is not None:
            try:
                self._on_finish(self.rc)
            except Exception:  # noqa: BLE001
                # A broken history write must never take down the drain
                # thread -- the job itself already finished successfully or
                # not; losing its history entry is a lesser failure than
                # losing the ability to see that the job ran at all.
                pass

    def stop(self, force: bool = False) -> str:
        with self.lock:
            if not self.running:
                return "nothing running"
            if force:
                # SIGKILL, for a child that took the interrupt and then hung
                # anyway. seed_sandbox's reset does exactly this: SIGINT
                # unwinds into ThreadPoolExecutor.__exit__, which joins
                # worker threads that are themselves blocked in a Google API
                # call, so the process sits in _wait_for_tstate_lock and the
                # UI reports "running" forever with no way to end it.
                #
                # Not the default, and never the first thing tried: the
                # engine's cooperative SIGINT commits state so a re-run
                # resumes cleanly, and killing instead of interrupting
                # throws that away.
                self.proc.kill()  # type: ignore[union-attr]
                return f"killed {self.name}"
            # SIGINT, not SIGKILL: the engine handles it cooperatively,
            # finishing in-flight items and committing state so a re-run
            # resumes cleanly.
            self.proc.send_signal(2)  # type: ignore[union-attr]
            proc, name = self.proc, self.name
        # Outside the lock: waiting on the child must not block snapshot(),
        # which the page polls every couple of seconds.
        #
        # And WAIT, rather than reporting success immediately. The force
        # branch above documents that this child can take the interrupt and
        # hang anyway -- seed_sandbox's reset unwinds into
        # ThreadPoolExecutor.__exit__ and joins workers blocked in a Google
        # API call. Live, Stop answered "interrupt sent" for a reset that
        # was still running thirty-six minutes later; a deploy then
        # restarted this process, the orphan outlived the Job object
        # tracking it, and a SECOND reset was admitted against the same
        # tenant. Two destructive jobs on one tenant is the exact thing the
        # one-heavy-job rule exists to prevent.
        deadline = time.time() + STOP_GRACE_SECONDS
        while time.time() < deadline:
            if proc.poll() is not None:
                return f"stopped {name}"
            time.sleep(0.25)
        return (f"interrupt sent to {name}, but it is STILL RUNNING after "
                f"{STOP_GRACE_SECONDS}s -- it is most likely joining worker "
                f"threads blocked in an API call. Press Stop again with "
                f"force to kill it.")

    def snapshot(self, since: int = 0) -> dict:
        # Pull in anything the child wrote since the last read, rather than
        # trusting the drain thread to have already done it. Two reasons,
        # and the second is the one that matters:
        #   * the drain thread ticks on a timer, so a poll landing between
        #     ticks would report output as missing that is already on disk;
        #   * `running` goes false the instant the process exits, which is
        #     BEFORE the drain thread's final pass -- so a caller that
        #     (correctly) stops polling once the job finishes could miss the
        #     last lines permanently, and those are usually the summary or
        #     the traceback, the part most worth seeing.
        # Called outside the lock below: _ingest_new_lines takes it itself,
        # and threading.Lock is not reentrant.
        if self.log_path:
            self._ingest_new_lines()
        # _job_progress() can open a sqlite connection (for migrate/delta/
        # discover's ledger-backed fraction) -- done after releasing the
        # lock below so a slow disk read never blocks start()/stop() for
        # every other request in flight.
        with self.lock:
            name, running, rc = self.name, self.running, self.rc
            elapsed = round((self.finished or time.time()) - self.started, 1) \
                if self.started else 0
            total = len(self.lines)
            lines = self.lines[since:]
            all_lines = self.lines if name == "seed" else None
        # Only seed_sandbox.py's own printed lines and the migrate/delta/
        # discover ledger have a real completion fraction to read --
        # everything else (deploy, reset target, provisioning, ...) has no
        # meaningful percentage at all, so this stays null there rather
        # than guessing. See _job_progress(). Percentage is still worth
        # showing after the job stops (a finished run's final 100%); an ETA
        # is not -- "time left" on a job that already ended is nonsense, so
        # that half is dropped the moment running goes false.
        pct, eta = _job_progress(name, all_lines or [], elapsed)
        if not running:
            eta = None
        return {
            "name": name,
            "running": running,
            "rc": rc,
            # Frozen at completion. Computing this live meant a finished
            # job's "duration" kept climbing with the clock -- an init-db
            # that took under a second was reported as "exit 0 · 105.1s",
            # which reads as a performance problem that does not exist.
            "elapsed": elapsed,
            "total": total,
            "lines": lines,
            "progressPct": pct,
            "etaSeconds": eta,
        }

    def _save_result(self) -> None:
        """Durable record of the last completed run of this job name, for
        this account. Job otherwise lives only in this process's memory --
        a redeploy or restart minutes (or seconds) after a seed/reset-
        target/deploy run finished loses the entire result with nothing to
        show for it, even though the run itself succeeded. Mirrors
        api_server.py's full_setup_start, which already writes its own
        result to logs/{account_id}/... for the same reason.

        Best-effort: a failed save must not take down the drain thread or
        hide the job's own outcome from /api/job, which already has it.
        """
        try:
            with self.lock:
                payload = {
                    "name": self.name, "rc": self.rc,
                    "started": self.started, "finished": self.finished,
                    "elapsed": round(self.finished - self.started, 1) if self.started else 0,
                    # Capped independently of the 4000-line in-memory trim --
                    # this file is read back in one GET, not streamed, so it
                    # stays well under what a browser tab wants to render.
                    "lines": self.lines[-2000:],
                }
            path = job_result_path(self.account_id, self.name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001
            pass


def job_result_path(account_id: int | None, name: str) -> str:
    """One file per (account, job name) -- only the *last* completed run is
    kept, not a history. operator_actions_log already covers audit/history
    for gated actions; this is just "what did the thing I just ran actually
    do", which a restart must not be able to erase.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(here, "logs", "jobs", "_none" if account_id is None else str(account_id))
    safe_name = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "job"
    return os.path.join(d, f"{safe_name}.json")


def job_log_path(account_id: int | None, name: str) -> str:
    """The live transcript of the CURRENT run, beside its saved result.

    Separate from job_result_path's .json (which is only written once the
    run finishes): this is the file the child process writes to directly
    while it runs, so it is readable by anything, at any time, including a
    freshly restarted server that never held this job in memory.
    """
    return job_result_path(account_id, name)[: -len(".json")] + ".log"


def load_job_result(account_id: int | None, name: str) -> dict | None:
    path = job_result_path(account_id, name)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


JOBS: dict[int | None, Job] = {}


def get_job(account_id: int | None) -> Job:
    """One Job instance per account, lazily created -- see the class
    docstring above for what a Job actually is. Only /api/seed,
    /api/reset_target, and the /api/job poller are account-scoped today;
    every other endpoint that launches or reads a Job (Setup Wizard,
    Deploy, stages/report payloads) still reads the plain `JOB` name below,
    which is simply JOBS[None] -- the legacy, single-tenant behaviour this
    had before per-account isolation existed, unchanged for account #1 and
    every caller with no account context to give."""
    if account_id not in JOBS:
        JOBS[account_id] = Job(account_id)
    return JOBS[account_id]


JOB = get_job(None)

_PARALLEL_JOBS: dict = {}


DMS_METRICS_FILE = os.getenv("DMS_METRICS_FILE",
                             os.path.join(HERE, "dms_metrics.json"))


def _dms_metrics_payload() -> dict:
    """The last DMS counters scraped from the console, plus their age and
    whether a refresh scrape is running right now."""
    data, age = None, None
    try:
        with open(DMS_METRICS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        read_at = data.get("read_at")
        if read_at:
            age = int(time.time()) - int(read_at)
    except (FileNotFoundError, ValueError):
        pass
    return {"data": data, "ageSeconds": age}


def get_parallel_job(account_id: int | None, name: str) -> Job:
    """A Job instance for an action that runs alongside the account's main
    migration (e.g. the DMS mail import). Keyed separately so it neither
    blocks nor is blocked by the primary Job, and so its output is its own."""
    key = (account_id, name)
    if key not in _PARALLEL_JOBS:
        _PARALLEL_JOBS[key] = Job(account_id)
    return _PARALLEL_JOBS[key]


def _subscription_ok(account_id: int | None) -> bool:
    """The manual v1 billing gate -- webui.py's side of
    api_server.py's require_active_subscription(). No _gated()-equivalent
    exists here to hook once (this is stdlib http.server, not FastAPI), so
    each of the two account-scoped POST handlers checks inline.

    account_id in (None, 1) exempt for the same reason as the FastAPI
    side: that's the operator's own SSH-tunnel/legacy path, not a client."""
    if account_id in (None, 1):
        return True
    account = accounts_auth.get_account(account_id)
    return bool(account) and bool(account["subscription_active"])


def _seed_ok(account_id: int | None) -> bool:
    """Whether this account may write fabricated data into a tenant.
    Opt-in per account (see control_plane_db.py's column default) --
    unlike subscription, which is opt-out. Same operator exemption as
    _subscription_ok: this tooling exists because the operator needed it
    for rehearsal in the first place."""
    if account_id in (None, 1):
        return True
    account = accounts_auth.get_account(account_id)
    return bool(account) and bool(account["seed_enabled"])


# Which tenant the in-flight consent belongs to, so the callback knows.
_PENDING: dict[str, str] = {}

# ----------------------------------------------------------------------
# Processes started OUTSIDE this webui (over SSH, from the TUI, from cron...)
#
# The webui's own Job only ever sees the children it spawned itself, so an
# operator who kicked off a migrate from another shell watched "idle" no
# matter how much work was running. A live ps scan is the only honest way to
# notice processes that arrive and leave without ever passing through this
# process -- so it is done here, on every job poll. Migration subcommands of
# main.py, plus the standalone job scripts the webui itself can also launch
# (seed/reset/deploy), are the things worth surfacing as "active".
# ----------------------------------------------------------------------
# One definition, shared with fleet_agent.py: both scan the process table
# for the same jobs, and when they disagreed on the name Running Now showed
# a live migration as "--account-id".
from fleet_agent import MAIN_COMMANDS as _EXT_MAIN_CMDS
_EXT_SCRIPTS = {"seed_sandbox.py": "seed", "reset_target.py": "reset target",
                "deploy_remote.py": "deploy", "verify.py": "verify",
                "resolve_failures.py": "resolve-failures",
                "phases.py": "phases"}
# Last-seen output tail per external pid, for the suffix-diff that turns the
# unbounded migration.log into the same "just the new lines" contract the
# webui-launched Job streams.
_EXT_TAIL_LEN: int = 2000


def _process_output_tail(pid: int, name: str = "",
                         account_id: int | None = None) -> list[str]:
    """The last _EXT_TAIL_LEN lines of a process's own output, or [] when
    this process genuinely cannot see it.

    Resolution order, most-specific first:
      1. /proc/<pid>/fd/1, when stdout is redirected to a real file -- the
         normal shape for a migrate run over SSH, and (since Job.start()
         stopped using a pipe) for anything this server launched too.
      2. This job name's own on-disk transcript, which covers a job whose
         fd/1 is a now-dead pipe from a PREVIOUS server process.
      3. Nothing.

    Step 3 used to be "fall back to migration.log" -- confirmed live to be
    actively misleading rather than merely unhelpful: a running seed's
    stdout was an orphaned pipe, so the UI served a THREE-DAY-OLD log from
    an unrelated migrate run as that seed's live output, with no indication
    anything was wrong. An honest empty result lets the caller say "no
    output available" instead of showing a different job's history as if it
    were this one's.
    """
    path = None
    try:
        tgt = os.readlink(f"/proc/{pid}/fd/1")
        if os.path.isabs(tgt) and os.path.exists(tgt):
            path = tgt
    except OSError:
        pass
    if path is None and name:
        candidate = job_log_path(account_id, name)
        if os.path.exists(candidate):
            path = candidate
    if path is None:
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = [ln.rstrip("\n") for ln in fh.readlines()[-_EXT_TAIL_LEN:]]
    except OSError:
        return []
    # Same filter Job._ingest_new_lines applies to a job this process owns.
    # Without it the two paths disagree about what a transcript contains,
    # and a detached run's feed opens with a five-line google FutureWarning
    # before any of its actual output.
    return [ln for ln in lines
            if "FutureWarning" not in ln and "warnings.warn" not in ln]


def _external_processes() -> list[dict]:
    """Running migration processes this webui did not start, as
    {pid, elapsed, name}. A migrate always sorts first so a seed running in
    another tab never hides the real migration."""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,etimes=,args="],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001 - ps missing means nothing external running
        return []
    found: list[dict] = []
    own = str(os.getpid())
    shells = {"bash", "sh", "zsh", "dash", "fish", "-bash", "-sh", "-zsh", "-dash"}
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, etimes, args = parts
        if pid == own:
            continue
        # A `bash -c "… main.py migrate …"` wrapper would otherwise look
        # exactly like a second migration. Only the real program token counts.
        if args.split(None, 1)[0] in shells:
            continue
        try:
            elapsed = int(etimes)
        except ValueError:
            continue
        # (?:^|[\s/]), not (?:^|/) -- Job.start() invokes every one of these
        # as [PY, "seed_sandbox.py", ...] (interpreter and script as
        # separate argv elements), so the real `ps` line always reads
        # "...python seed_sandbox.py ...": the script name is preceded by a
        # SPACE, never a "/". The slash-only version only ever matched a
        # direct path invocation ("/root/migration/seed_sandbox.py"), which
        # is not what this process actually launches -- confirmed live,
        # this silently never matched a single real seed/reset-target run,
        # so _external_processes() always reported none of them as
        # running. Same fix the main.py branch just below already has.
        name = next((label for script, label in _EXT_SCRIPTS.items()
                     if re.search(r"(?:^|[\s/])" + re.escape(script) + r"(?:\s|$)",
                                  args)), None)
        if name is None:
            # Not "the token after main.py": that is a FLAG whenever one
            # is passed -- api_server launches as
            #   main.py --account-id 7 migrate --services drive,gmail
            name = fleet_agent.main_command(args)
        if name is None:
            continue
        found.append({"pid": int(pid), "elapsed": elapsed, "name": name})
    found.sort(key=lambda x: (x["name"] != "migrate", -x["elapsed"], x["pid"]))
    return found


# job_admission.py job names this process itself admits (see get_job()'s own
# /api/seed, /api/reset_target, /api/reset_drive_ledger call sites) -- the
# only ones _reconcile_active_jobs() below has any business releasing.
_OWNED_JOB_NAMES = {"seed", "reset target", "reset drive ledger"}


def _reconcile_active_jobs() -> None:
    """Startup only: this process's own JOBS dict always starts empty, so
    any job_admission.py row for a job type THIS process owns is orphaned
    UNLESS the underlying child (protected from the restart itself by
    KillMode=process) is still actually alive.

    Confirmed live: a restart mid-seed left job_admission showing a seed
    admitted forever -- the real seed_sandbox.py process had long since
    exited, nothing in the new process ever calls release() for a job it
    never started, and job_admission.MAX_CONCURRENT_TENANT_JOBS=1 meant
    that phantom row permanently wedged the whole box at zero capacity --
    every subsequent seed/migrate/full-setup attempt, from any account,
    refused with "capacity is full" for a job that was not running at all.

    Startup-only, not polled: this state can only go stale exactly AT a
    restart, never in between -- reconciling on every request would just
    be a `ps` scan nothing between restarts could ever change the answer to.
    """
    try:
        active = job_admission.list_active()
    except Exception:  # noqa: BLE001 - best-effort, must not block startup
        return
    running = {p["name"]: p for p in _external_processes()}
    for row in active:
        name = row.get("job_name")
        if name not in _OWNED_JOB_NAMES:
            continue
        proc = running.get(name)
        if proc is None:
            job_admission.release(row.get("account_id"), name)
            print(f"released orphaned job_admission row: account={row.get('account_id')} "
                  f"job={name!r} (no matching process found at startup)", flush=True)
            continue
        # Still running: re-attach the row to it. The row this process
        # inherited was written before record_pid existed here, so its pid
        # is NULL -- and is_live() reads a pid-less row as dead once it is
        # 120s old, which reaped the admission of a job that was plainly
        # still going. That emptied Running Now, lifted the one-job cap,
        # and hid the job from job_supervisor, all while it worked.
        try:
            job_admission.record_pid(row.get("account_id"), name, proc["pid"])
            print(f"re-adopted running job: account={row.get('account_id')} "
                  f"job={name!r} pid={proc['pid']}", flush=True)
        except Exception as exc:  # noqa: BLE001 - never block startup
            print(f"could not re-adopt {name!r}: {exc}", flush=True)


def _external_job_snapshot(since: int = 0) -> dict | None:
    """A Job.snapshot()-shaped view of the primary external process, or None
    when nothing is running outside the webui. progressPct/eta reuse the same
    ledger-backed math a webui-launched migrate gets.

    `since` is honoured exactly as Job.snapshot() honours it: the caller's
    own cursor into the transcript. This used to keep a single module-level
    "what did I last send" per pid and return the diff against it, which
    made the response depend on who polled last rather than on who is
    asking -- so a browser opening the page fresh (since=0) received zero
    lines, because an earlier poll in the same server process had already
    "consumed" them. Confirmed live: a real seed's 21-line transcript was
    on disk and resolvable, and a new client still rendered an empty feed.
    Slicing by the caller's own cursor is both correct for concurrent
    clients and the same contract the managed path already had.
    """
    jobs = _external_processes()
    if not jobs:
        return None
    job = jobs[0]
    # name/account: lets the fallback find this job's own on-disk transcript
    # when fd/1 is a dead pipe left by a previous server process. account is
    # the legacy slot deliberately -- _external_processes() is a machine-wide
    # ps scan with no account context (see _job_snapshot's `external` flag),
    # and JOBS[None] is where a detached run's log lands.
    tail = _process_output_tail(job["pid"], job["name"], None)
    lines = tail[since:] if since < len(tail) else []
    pct, eta = _job_progress(job["name"], tail, job["elapsed"])
    return {
        "name": job["name"],
        "running": True,
        "rc": None,
        "elapsed": job["elapsed"],
        "total": len(tail),
        "lines": lines,
        "progressPct": pct,
        "etaSeconds": eta,
        "detached": True,
        "pid": job["pid"],
        "pids": [j["pid"] for j in jobs],
    }


def _job_snapshot(account_id: int | None, since: int) -> dict:
    """/api/job's own logic, pulled out so it's callable without an HTTP
    request -- see the `external` field's own purpose below.

    external: true means this is NOT account_id's own job.
    _external_job_snapshot() is a system-wide ps scan with no concept of
    which account started what, so a job admitted under a DIFFERENT
    account shows up here identically to the caller's own (same "seed"/
    name, no account info at all). Confirmed live: a fresh account with
    nothing of its own running saw another account's real seed job here,
    Stop button included -- this flag is what lets a caller tell "mine"
    from "someone else's, merely detected by scanning the process table"
    apart, both for display (RunningNow.tsx's own account-attributed
    job_admission.py entry would otherwise duplicate this one) and for
    safety (Stop must not offer to kill a job this account never started).
    """
    snap = get_job(account_id).snapshot(since)
    external = False
    if not snap["running"]:
        ext = _external_job_snapshot(since)
        if ext is not None:
            snap = ext
            external = True
    snap["external"] = external
    return snap


# ----------------------------------------------------------------------
# OAuth: the flow a non-technical admin can actually complete.
# ----------------------------------------------------------------------
_FLOWS: dict[str, object] = {}


def _oauth_config(settings):
    """The client secrets are the *vendor's*, created once -- not something a
    tenant admin ever has to make."""
    path = settings.oauth_client_secrets
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def oauth_status() -> dict:
    from config import Settings
    import oauth_store

    st = Settings()
    store = oauth_store.TokenStore(st.oauth_token_dir)
    return {
        "configured": _oauth_config(st) is not None,
        "client_secrets_path": st.oauth_client_secrets,
        "auth_mode": st.auth_mode,
        "source": store.describe("source"),
        "target": store.describe("target"),
    }


def oauth_begin(tenant: str, port: int) -> dict:
    from config import Settings, source_scopes, target_scopes
    import oauth_store

    st = Settings()
    cfg = _oauth_config(st)
    if not cfg:
        return {"ok": False, "error":
                f"no OAuth client secrets at {st.oauth_client_secrets} -- "
                f"create one OAuth client ID (Desktop or Web) in any GCP "
                f"project and save the JSON there. This is done once, by you, "
                f"not by each tenant."}
    scopes = (source_scopes(st) if tenant == "source" else target_scopes(st))
    redirect = f"http://localhost:{port}/oauth/callback"
    try:
        flow = oauth_store.build_flow(cfg, scopes, redirect)
        url = oauth_store.authorization_url(flow)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    _FLOWS[tenant] = flow
    return {"ok": True, "url": url}


def oauth_finish(tenant: str, full_url: str) -> dict:
    import oauth_store
    from config import Settings

    flow = _FLOWS.get(tenant)
    if flow is None:
        return {"ok": False, "error": "no sign-in in progress for that tenant"}
    try:
        flow.fetch_token(authorization_response=full_url)
        creds = flow.credentials
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    if not creds.refresh_token:
        # Without this the migration dies an hour in, when the access token
        # expires and there is nothing to renew it with.
        return {"ok": False, "error":
                "Google returned no refresh token. Remove this app's access at "
                "myaccount.google.com/permissions and try again."}

    account = domain = ""
    try:
        from googleapiclient.discovery import build as _build
        import google_auth_httplib2, httplib2
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
        me = _build("drive", "v3", http=http, cache_discovery=False
                    ).about().get(fields="user").execute()
        account = (me.get("user") or {}).get("emailAddress", "")
        domain = account.split("@")[-1] if account else ""
    except Exception:  # noqa: BLE001 - identity is a nicety, not required
        pass

    st = Settings()
    oauth_store.TokenStore(st.oauth_token_dir).save(
        tenant, oauth_store.credentials_to_dict(creds, account, domain))
    return {"ok": True, "account": account, "domain": domain}


# Which action (if any) actually performs each wizard step. A step with no
# entry here is either manual or has no single command that completes it.
#
# Step 7 (seeding) is absent from ACTIONS but is *not* absent from the UI: it
# runs through /api/seed instead. ACTIONS entries are fixed argv with no
# per-request input, which is what makes them safe to fire from a button --
# and it is exactly the wrong shape for the seeder, which writes fabricated
# data into a live tenant and must be aimed by hand. /api/seed requires the
# source domain typed back before it will build a command.
STEP_ACTIONS: dict[int, list[str]] = {
    4: ["init_db", "init_db_auto"],
    5: ["preflight"],
    6: ["provision_dry", "provision"],
    7: ["check_seed_accounts", "check_seed_scopes"],
    8: ["discover", "migrate_dry", "migrate"],
    9: ["verify", "report", "acl_audit", "resolve"],
}


# ----------------------------------------------------------------------
# Typed input from the browser.
#
# The module contract above says the client can never cause an arbitrary
# command to run. Collecting a domain or a hostname does not weaken that: each
# value is matched against a strict pattern here and then placed into a fixed
# argv list, never concatenated into a shell string. A value that does not
# match is rejected outright rather than escaped -- there is no legitimate
# domain or username that these patterns exclude.
# ----------------------------------------------------------------------
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN_RE = re.compile(rf"^(?:{_LABEL}\.)+[a-z]{{2,63}}$", re.I)
_EMAIL_RE = re.compile(rf"^[a-z0-9._%+-]{{1,64}}@(?:{_LABEL}\.)+[a-z]{{2,63}}$", re.I)
_HOST_RE = re.compile(rf"^(?:{_LABEL}\.)*{_LABEL}$|^\d{{1,3}}(?:\.\d{{1,3}}){{3}}$", re.I)
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$", re.I)

# The React SPA (Mission Control) builds to here via `npm run build`. Served
# under /app rather than at "/" because "/" is this file's own inline setup
# wizard -- an existing, working flow that must not be displaced by a build
# artifact that may not exist yet on a fresh checkout.
SPA_DIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "migration-webui", "dist")

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "env.sh")
IDENTITIES_CSV_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "identities.csv")

# ----------------------------------------------------------------------
# Deploy history: every /api/deploy invocation, appended when it starts and
# updated in place when it finishes. Nothing tracked this before -- "did the
# last deploy to this VPS actually work, and what commit is running there
# now" had no answer except SSHing in and checking by hand. A flat JSON
# array (not migration.db) because a deploy can happen before that database
# exists at all, and this has nothing to do with migration state.
# ----------------------------------------------------------------------
DEPLOY_HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "deploy_history.json")
_DEPLOY_HISTORY_LOCK = threading.Lock()
_MAX_DEPLOY_HISTORY = 100


def load_deploy_history() -> list[dict]:
    """Most recent first. Never raises -- a corrupt or absent history file
    means an empty list, not a broken Deploy tab."""
    try:
        with open(DEPLOY_HISTORY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_deploy_history(records: list[dict]) -> None:
    tmp = DEPLOY_HISTORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2)
    os.replace(tmp, DEPLOY_HISTORY_PATH)  # atomic on the same filesystem


def record_deploy_start(host: str, user: str, port: str, ui_port: str,
                        include_credentials: bool, commit: str) -> str:
    """Appends a new in-progress record and returns its id, used later to
    find and update it once the job finishes."""
    with _DEPLOY_HISTORY_LOCK:
        records = load_deploy_history()
        rec_id = f"{time.time():.6f}"
        records.insert(0, {
            "id": rec_id,
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "finishedAt": None,
            "host": host, "user": user, "port": port, "uiPort": ui_port,
            "includeCredentials": include_credentials,
            "commit": commit,
            "rc": None,
        })
        del records[_MAX_DEPLOY_HISTORY:]
        _save_deploy_history(records)
        return rec_id


def record_deploy_finish(rec_id: str, rc: int | None) -> None:
    with _DEPLOY_HISTORY_LOCK:
        records = load_deploy_history()
        for r in records:
            if r.get("id") == rec_id:
                r["rc"] = rc
                r["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                break
        else:
            return  # record vanished (history trimmed mid-run) -- nothing to update
        _save_deploy_history(records)


_CONFIG_FIELDS = (
    ("source_domain", "SOURCE_DOMAIN", _DOMAIN_RE, "a domain like c.example.com"),
    ("target_domain", "TARGET_DOMAIN", _DOMAIN_RE, "a domain like a.example.com"),
    ("source_admin", "SOURCE_ADMIN", _EMAIL_RE, "a super-admin address in the source domain"),
    ("target_admin", "TARGET_ADMIN", _EMAIL_RE, "a super-admin address in the target domain"),
)


_HOST_INFO_CACHE: dict | None = None


def _primary_ip() -> str:
    """
    This machine's outbound-facing IP, without sending any traffic --
    connect() on a UDP socket only picks a local interface/route, no packet
    actually goes anywhere. Loopback fallback keeps this from ever raising.
    """
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"


def host_info() -> dict:
    """
    Where this process is actually running -- the answer to "is this the
    VPS, or is this still my laptop?", found the hard way once already: a
    local seed run and a deployed VPS instance can both bind 127.0.0.1:8080
    and look identical in the browser, and nothing on screen said which one
    a given tab was talking to.

    `location` is the one field meant to answer that at a glance: a laptop's
    outbound interface is almost always a private/NAT'd address (RFC1918,
    or loopback), while a VPS's is a routable public IP -- so a public
    address is shown as itself (it names the actual VPS), and a private one
    is shown as "Local machine" (the address itself is not useful there;
    every laptop on a LAN has one and it means nothing to the operator).

    hostname + this file's own directory further identify the machine and
    the deployment; commit is best-effort (this is a git checkout locally,
    but a deploy_remote.py target is a plain rsync copy with no .git at all
    -- see its own module docstring for why, so an absent commit there is
    normal, not an error). Cached for the process's lifetime: none of this
    changes while it is running, so there is no reason to shell out to git
    or probe the network on every poll.
    """
    global _HOST_INFO_CACHE
    if _HOST_INFO_CACHE is not None:
        return _HOST_INFO_CACHE

    import ipaddress
    import socket

    code_dir = os.path.dirname(os.path.abspath(__file__))
    commit = ""
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                          cwd=code_dir, capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            commit = r.stdout.strip()
    except Exception:  # noqa: BLE001 - not a git checkout is a normal state
        pass

    ip = _primary_ip()
    try:
        is_private = ipaddress.ip_address(ip).is_private
    except ValueError:
        is_private = True
    location = "Local machine" if is_private else ip

    _HOST_INFO_CACHE = {
        "hostname": socket.gethostname(),
        "ip": ip,
        "location": location,
        "code_path": code_dir,
        "commit": commit,
        "pid": os.getpid(),
    }
    return _HOST_INFO_CACHE


def read_config() -> dict:
    from wizard import load_env

    env = load_env(ENV_PATH)
    return {field: env.get(key, "") for field, key, _, _ in _CONFIG_FIELDS}


def validate_config(body: dict) -> tuple[dict, str]:
    """Returns (clean, error). Empty error means every field is acceptable."""
    clean = {}
    for field, key, pattern, hint in _CONFIG_FIELDS:
        val = (body.get(field) or "").strip().lower()
        if not val:
            return {}, f"{field.replace('_', ' ')} is required — {hint}"
        if not pattern.match(val):
            return {}, f"{val!r} is not {hint}"
        clean[key] = val
    # An admin outside the domain it administers is the single most common
    # entry error here, and it fails much later with a confusing 401.
    for admin_key, domain_key, side in (("SOURCE_ADMIN", "SOURCE_DOMAIN", "source"),
                                        ("TARGET_ADMIN", "TARGET_DOMAIN", "target")):
        if not clean[admin_key].endswith("@" + clean[domain_key]):
            return {}, (f"{clean[admin_key]} is not in {clean[domain_key]} — the "
                        f"{side} admin must be an account in the {side} domain")
    if clean["SOURCE_DOMAIN"] == clean["TARGET_DOMAIN"]:
        return {}, "source and target domains must differ"
    return clean, ""


_DEPLOY_ENV_KEYS = {"host": "DEPLOY_HOST", "user": "DEPLOY_USER",
                    "port": "DEPLOY_PORT", "key": "DEPLOY_KEY",
                    "ui_port": "DEPLOY_UI_PORT"}


def read_deploy_config() -> dict:
    """
    The VPS connection details Deploy last used, if any.

    Previously these lived only in the browser's in-memory JS state (`dep`)
    -- gone on every page reload, and never available at all from the SPA,
    which had no deploy UI. Persisted the same way source/target domain
    config already is: KEY=VALUE pairs in env.sh, so "add my VPS once" and
    "the UI already knows it next time" are the same file.
    """
    from wizard import load_env

    env = load_env(ENV_PATH)
    return {
        "host": env.get("DEPLOY_HOST", ""),
        "user": env.get("DEPLOY_USER", "root"),
        "port": env.get("DEPLOY_PORT", "22"),
        "key": env.get("DEPLOY_KEY", ""),
        "ui_port": env.get("DEPLOY_UI_PORT", "8080"),
    }


def validate_deploy_config(body: dict) -> tuple[dict, str]:
    """
    Returns (env-var pairs to persist, error). Empty error means acceptable.

    Reuses deploy_remote.validate() rather than re-deriving the host/user
    patterns here -- the same check /api/deploy itself relies on before
    ever shelling out to rsync/ssh.
    """
    import deploy_remote

    host = (body.get("host") or "").strip()
    user = (body.get("user") or "root").strip()
    key = (body.get("key") or "").strip()
    try:
        port = int(body.get("port") or 22)
        ui_port = int(body.get("ui_port") or 8080)
    except (TypeError, ValueError):
        return {}, "port must be a number"
    err = deploy_remote.validate(host, user, port, key)
    if err:
        return {}, err
    return {"DEPLOY_HOST": host, "DEPLOY_USER": user, "DEPLOY_PORT": str(port),
            "DEPLOY_KEY": key, "DEPLOY_UI_PORT": str(ui_port)}, ""


def write_config_raw(pairs: dict) -> None:
    """Merge arbitrary KEY=VALUE pairs into env.sh and the live environment."""
    from wizard import load_env

    env = load_env(ENV_PATH)
    env.update(pairs)
    lines = [f"export {k}={v}" for k, v in sorted(env.items())]
    with open(ENV_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    for k, v in pairs.items():
        os.environ[k] = v


def write_config(clean: dict) -> None:
    """Merge into env.sh, preserving anything setup.sh wrote (SA emails, key
    paths, tunables) so saving the form never silently discards them."""
    from wizard import load_env

    env = load_env(ENV_PATH)
    env.update(clean)
    env.setdefault("MIGRATION_DB", os.path.join(os.path.dirname(ENV_PATH), "migration.db"))
    env.setdefault("SCRATCH_DIR", os.path.join(os.path.dirname(ENV_PATH), "scratch"))
    lines = [f"export {k}={v}" for k, v in sorted(env.items())]
    with open(ENV_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    # So the running process picks the values up without a restart.
    for k, v in clean.items():
        os.environ[k] = v
    _invalidate_all()


# ----------------------------------------------------------------------
# Credential upload
#
# The files themselves cannot be created by any API (see UPLOADS below), so
# the least this can do is take the file, check it is the right one, and put
# it in the right place with the right permissions. Getting those three things
# wrong by hand is most of the setup pain.
# ----------------------------------------------------------------------
MAX_UPLOAD = 256 * 1024          # these files are ~2-5 KB; anything larger is a mistake


def _check_oauth_client(data: dict) -> str:
    root = data.get("installed") or data.get("web")
    if not root:
        if data.get("type") == "service_account":
            return ("that is a service-account key, not an OAuth client. The "
                    "OAuth client JSON has an \"installed\" or \"web\" section "
                    "and comes from APIs & Services -> Credentials -> Create "
                    "credentials -> OAuth client ID.")
        return ('not an OAuth client file — expected an "installed" or "web" key')
    for field in ("client_id", "client_secret", "auth_uri", "token_uri"):
        if not root.get(field):
            return f"OAuth client JSON is missing {field}"
    return ""


def _check_sa_key(data: dict) -> str:
    if data.get("type") != "service_account":
        if data.get("installed") or data.get("web"):
            return ("that is an OAuth client file, not a service-account key. "
                    "The key comes from IAM & Admin -> Service Accounts -> "
                    "Keys -> Add key -> Create new key -> JSON.")
        return 'not a service-account key — expected "type": "service_account"'
    for field in ("client_email", "private_key", "client_id", "project_id"):
        if not data.get(field):
            return f"service-account key is missing {field}"
    if "PRIVATE KEY" not in data.get("private_key", ""):
        return "the private_key field does not contain a PEM private key"
    return ""


UPLOADS: dict[str, dict] = {
    "oauth_client": {
        "path": lambda st: st.oauth_client_secrets,
        "check": _check_oauth_client,
        "label": "OAuth client ID",
    },
    "source_key": {
        "path": lambda st: st.source_sa_key,
        "check": _check_sa_key,
        "label": "source service-account key",
    },
    "target_key": {
        "path": lambda st: st.target_sa_key,
        "check": _check_sa_key,
        "label": "target service-account key",
    },
}


def upload_credential(kind: str, content: str) -> dict:
    """Validate an uploaded credential file and store it mode 0600."""
    import stat as _stat

    from config import Settings

    spec = UPLOADS.get(kind)
    if not spec:
        return {"ok": False, "error": f"unknown upload kind {kind!r}"}
    if not content or not content.strip():
        return {"ok": False, "error": "the file is empty"}
    if len(content) > MAX_UPLOAD:
        return {"ok": False, "error": "that file is far too large to be a credential"}
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"not valid JSON ({exc.msg} at line {exc.lineno})"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "expected a JSON object"}

    # Uploading the wrong one of these two files is easy and the resulting
    # runtime error is unrecognisable, so name the mistake here instead.
    err = spec["check"](data)
    if err:
        return {"ok": False, "error": err}

    path = spec["path"](Settings())
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    os.chmod(parent, _stat.S_IRWXU)                     # 0700

    # Keep the previous credential before replacing it. Uploading into the
    # wrong slot is a one-click mistake and the file being replaced may be the
    # only copy on this machine; a stamped backup makes that recoverable
    # instead of final. (Learned the hard way -- an overwrite during testing
    # destroyed the only copy of a real key.)
    backup = ""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = f"{path}.{stamp}.bak"
        try:
            with open(path, "rb") as src, open(backup, "wb") as dst:
                dst.write(src.read())
            os.chmod(backup, _stat.S_IRUSR | _stat.S_IWUSR)
        except OSError:
            backup = ""

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(path, _stat.S_IRUSR | _stat.S_IWUSR)       # 0600

    detail = ""
    if kind == "oauth_client":
        root = data.get("installed") or data.get("web")
        detail = root.get("client_id", "")[:32]
    else:
        detail = data.get("client_email", "")
    _invalidate_all()
    msg = f"saved {spec['label']} to {path} (mode 0600)"
    if backup:
        msg += f"; previous file kept as {os.path.basename(backup)}"
    return {"ok": True, "path": path, "detail": detail,
            "backup": backup, "msg": msg}


AUTH_MODES = {
    "key": {
        "label": "Service-account key",
        "blurb": "Two JSON key files. Migrates every user. The default.",
        "needs": ["source_key", "target_key"],
    },
    "impersonate": {
        "label": "Keyless (impersonation)",
        "blurb": "No files at all. Use when org policy blocks key downloads.",
        "needs": [],
    },
    "oauth": {
        "label": "Browser sign-in (OAuth)",
        "blurb": "An admin clicks Allow. Migrates only that admin's own account.",
        "needs": ["oauth_client"],
    },
}


def set_run_mode(mode: str) -> dict:
    """Switch what this run is for; persisted the same way as AUTH_MODE."""
    from wizard import RUN_MODES

    if mode not in RUN_MODES:
        return {"ok": False, "error": f"unknown run mode {mode!r}"}
    write_config_raw({"RUN_MODE": mode})
    _invalidate_all()
    return {"ok": True, "run_mode": mode,
            "msg": f"this run is now: {RUN_MODES[mode]['label']}"}


def set_auth_mode(mode: str) -> dict:
    """
    Switch credential mode and persist it.

    Written to env.sh *and* os.environ: Settings reads AUTH_MODE at
    construction, and every request builds a fresh Settings, so the change
    takes effect on the next poll rather than needing a restart.
    """
    if mode not in AUTH_MODES:
        return {"ok": False, "error": f"unknown auth mode {mode!r}"}
    write_config_raw({"AUTH_MODE": mode})
    _invalidate_all()
    return {"ok": True, "auth_mode": mode,
            "msg": f"credential mode set to {AUTH_MODES[mode]['label']}"}


def inspect_credential(kind: str) -> dict:
    """
    What is actually in the file on disk.

    "Present" is not the same as "usable". A file can arrive by scp, rsync or
    setup.sh without ever passing through the upload validator, and the two
    credential JSONs are easy to confuse. This re-reads and re-checks whatever
    is there now, and surfaces the fields that matter -- in particular the
    numeric **Client ID**, which is what step 5 needs pasted into the Admin
    Console and which is otherwise buried in the file.
    """
    from config import Settings

    spec = UPLOADS.get(kind)
    if not spec:
        return {"present": False, "valid": False, "error": f"unknown kind {kind!r}"}

    path = spec["path"](Settings())
    info = {"path": path, "present": False, "valid": False, "error": "", "detail": {}}
    if not (os.path.exists(path) and os.path.getsize(path) > 0):
        return info
    info["present"] = True

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        info["error"] = f"unreadable: {exc}"
        return info
    if not isinstance(data, dict):
        info["error"] = "expected a JSON object"
        return info

    err = spec["check"](data)
    if err:
        info["error"] = err
        return info

    info["valid"] = True
    if kind == "oauth_client":
        root = data.get("installed") or data.get("web")
        info["detail"] = {
            "client_id": root.get("client_id", ""),
            "type": "installed" if data.get("installed") else "web",
            "project_id": root.get("project_id", ""),
        }
    else:
        info["detail"] = {
            "client_email": data.get("client_email", ""),
            # The 21-digit uniqueId, NOT the email -- this is the string the
            # Admin Console's domain-wide delegation screen asks for.
            "client_id": data.get("client_id", ""),
            "project_id": data.get("project_id", ""),
        }
    return info


def uploads_status() -> dict:
    out = {kind: inspect_credential(kind) for kind in UPLOADS}

    # One service account in both slots is *allowed*, not broken: delegation is
    # authorised per domain, so the same Client ID can be granted the source
    # scopes in one Admin Console and the target scopes in the other. Say what
    # it implies rather than calling it an error -- but do flag it, because it
    # is also exactly what an accidental upload into the wrong slot looks like.
    src, tgt = out.get("source_key", {}), out.get("target_key", {})
    if src.get("valid") and tgt.get("valid"):
        a = (src.get("detail") or {}).get("client_email", "")
        b = (tgt.get("detail") or {}).get("client_email", "")
        if a and a == b:
            for side in (src, tgt):
                side["warning"] = (
                    f"Both slots hold the same service account ({a}). That works "
                    f"if you authorise this one Client ID in BOTH Admin Consoles, "
                    f"each with its own scope line — but it means a single "
                    f"credential can read the source and write the target. If you "
                    f"meant to use a separate account per tenant, one of these is "
                    f"in the wrong slot."
                )
    return out


def live_check(kind: str) -> dict:
    """
    Does this credential actually work?

    Structural validity says the file is well formed; it says nothing about
    whether Google will accept it. This mints a real token and makes one API
    call as the tenant's admin, which is the only thing that distinguishes
    "valid JSON" from "delegation is granted and the account is reachable".

    Deliberately on demand: it takes seconds and must never run on a poll.
    """
    if kind == "oauth_client":
        return {"ok": False,
                "error": "an OAuth client cannot be tested without a sign-in — "
                         "use 'Connect tenant' below, which is the real test"}

    info = inspect_credential(kind)
    if not info["present"]:
        return {"ok": False, "error": "no file uploaded yet"}
    if not info["valid"]:
        return {"ok": False, "error": info["error"]}

    tenant = "source" if kind == "source_key" else "target"

    from config import Settings

    st = Settings()
    admin = st.source_admin if tenant == "source" else st.target_admin
    if not admin:
        return {"ok": False,
                "error": f"set the {tenant} admin address in step 2 first — a "
                         f"delegated call has to be made as a real user"}
    try:
        from auth import AuthManager

        ok, msg = AuthManager(st).verify_delegation(tenant, admin)
    except (ImportError, AttributeError) as exc:
        # Not a credential problem. Seen live as "module
        # 'googleapiclient.discovery_cache' has no attribute 'get_static_doc'"
        # after a virtualenv under /tmp was partially deleted by the OS's
        # temp-file cleaner. Reporting that as a failed key sends someone to
        # regenerate credentials that were never at fault.
        return {"ok": False, "environment": True,
                "error": f"the Google client library in this environment is "
                         f"broken, so the key could not be tested: {exc}. "
                         f"Reinstall it (pip install -r requirements.txt) and "
                         f"try again — this says nothing about the key itself."}
    except Exception as exc:  # noqa: BLE001 - shown to the operator verbatim
        return {"ok": False, "error": str(exc)[:400]}

    if ok:
        return {"ok": True,
                "msg": f"minted a token and called Drive as {admin} — this key "
                       f"works and delegation is granted"}

    # The three failures here need opposite fixes, so name them apart.
    low = msg.lower()
    if "unauthorized_client" in low:
        hint = ("the key is fine but its Client ID is not authorised in the "
                f"{tenant} Admin Console, or the scope line was pasted "
                "incompletely — see step 5")
    elif "invalid_grant" in low or "not found" in low:
        hint = f"{admin} does not exist in that domain"
    elif "active session is invalid" in low:
        hint = (f"{admin} is unreachable — suspended, awaiting a login "
                f"challenge, or the tenant's trial has expired")
    else:
        hint = msg[:220]
    return {"ok": False, "error": hint}


# ----------------------------------------------------------------------
# Seeding
#
# This writes fabricated data into a live tenant, so it is the one action
# where the friction *is* the feature. The CLI protects itself three ways --
# SANDBOX_MODE=true, --confirm-domain matching SOURCE_DOMAIN exactly, and a
# PROTECTED_DOMAINS deny list. None of those are bypassed here: the browser
# has to supply the domain by typing it, and the server re-checks everything
# before building the command. The seeder then checks again for itself.
# ----------------------------------------------------------------------
SEED_SCALES = ("tiny", "small", "medium", "large", "huge")


def seed_argv(body: dict, account_id: int | None = None) -> tuple[list[str], dict, str]:
    """Build the seeder command, or return why it must not run.

    account_id=None (every caller before this parameter existed) reads the
    domain/env exactly as before, from Settings()/env.sh. Set, it reads
    that account's own tenant_configs row instead, and overlays the
    matching SOURCE_*/MIGRATION_DB env vars onto the child process -- seed_
    sandbox.py itself reads some of these straight from os.environ rather
    than through a Settings object (see data-generator/seed_sandbox.py),
    so the override has to happen at the environment, not just the domain
    check below.
    """
    from config import Settings

    st = Settings(account_id=account_id)
    domain = (st.source_domain or "").strip().lower()
    typed = (body.get("confirm_domain") or "").strip().lower()

    if not domain:
        return [], {}, "set the source domain in step 2 first"
    if not typed:
        return [], {}, f"type the source domain ({domain}) to confirm"
    if typed != domain:
        # The single most dangerous slip is aiming this at the target tenant,
        # or at a real one. An exact match is the only accepted answer.
        target = (st.target_domain or "").strip().lower()
        extra = (" — that is the TARGET domain, which must never be seeded"
                 if typed and typed == target else "")
        return [], {}, f"{typed!r} does not match the source domain {domain!r}{extra}"

    protected = [d.strip().lower()
                 for d in os.getenv("PROTECTED_DOMAINS", "").split(",") if d.strip()]
    if domain in protected:
        return [], {}, f"{domain} is listed in PROTECTED_DOMAINS"

    scale = (body.get("scale") or "medium").strip().lower()
    if scale not in SEED_SCALES:
        return [], {}, f"unknown scale {scale!r}"

    argv = [PY, "seed_sandbox.py", "--confirm-domain", domain, "--scale", scale]
    # Write the identity map where init-db actually reads it.
    #
    # The seeder runs with cwd=<root>/data-generator (see the /api/seed
    # handler), so its default relative --identities-out landed in
    # data-generator/identities.csv. The "Create database + load identities"
    # action runs from <root> and reads <root>/identities.csv. Nothing
    # connected the two, so after a 201-user seed that button silently
    # loaded a ten-row file left over from an unrelated tenant pair
    # (c.anupam-poudel.com.np), and the wizard would have reported step 4
    # done with an identity map for a migration that is not this one.
    argv += ["--identities-out", os.path.join(HERE, "identities.csv")]
    # --yes skips the seeder's interactive "long run?" prompt. In this subprocess
    # stdin is inherited (or DEVNULL, see Job.start), so input() would block
    # forever and the run would seed nothing. The typed-domain gate above is the
    # safety this replaces; the CLI keeps its prompt for terminal use.
    argv.append("--yes")
    if body.get("create_users"):
        argv.append("--create-users")
    if body.get("create_until_full"):
        # Generates and creates accounts one at a time until the Directory
        # API itself refuses one -- the empirical stand-in for
        # --fit-to-licenses when the Reports API is lagging (a low-usage
        # tenant can go days without current data). seed_sandbox.py itself
        # refuses to combine this with --all-users/--fit-to-licenses.
        argv.append("--create-until-full")
    if body.get("all_users"):
        # Seeds every account that already exists in the tenant -- the real
        # headcount via the Directory API, not a fixed 5. seed_sandbox.py
        # itself refuses to combine this with --users/--fit-to-licenses;
        # neither is exposed through this endpoint, so nothing more to guard.
        argv.append("--all-users")
    if body.get("reset"):
        argv.append("--reset")
    # Shared drives belong to no user, so the per-user seed never creates one
    # and shared_drives.py has nothing to migrate. Opt-in: they cost real
    # tenant objects and most seeds do not need them.
    sd = body.get("shared_drives")
    if sd not in (None, "", 0, "0", False):
        try:
            n_sd = int(sd)
        except (TypeError, ValueError):
            return [], {}, f"shared_drives must be a whole number, got {sd!r}"
        if n_sd < 1:
            return [], {}, "shared_drives must be at least 1"
        argv += ["--shared-drives", str(n_sd)]
    # Parallel users. Blank means "size it to this machine" -- seed_sandbox
    # asks resources.recommend(), which budgets memory per worker and is the
    # right default. An override exists because that budget is deliberately
    # conservative and an operator watching a run may know better.
    #
    # Ceiling derived, never hardcoded: a seed worker costs real memory
    # (~101 MB measured), so the cap follows the machine's own recommendation
    # rather than a number that goes stale next time the box changes. Twice
    # the recommendation is the most that has ever been reasonable; beyond
    # that the kernel kills the seed hours in, which is strictly worse than
    # seeding slowly.
    workers = body.get("workers")
    if workers not in (None, "", 0, "0"):
        try:
            n = int(workers)
        except (TypeError, ValueError):
            return [], {}, f"workers must be a whole number, got {workers!r}"
        if n < 1:
            return [], {}, "workers must be at least 1"
        try:
            import resources
            rec = resources.recommend()
            safe = int(rec.get("seed_workers") or 1)
        except Exception:      # noqa: BLE001 - never block a seed on the probe
            safe = n
        ceiling = max(safe * 2, 1)
        if n > ceiling:
            return [], {}, (
                f"{n} workers is more than this machine can hold. It sizes "
                f"itself to {safe} ({rec.get('seed_reason') or 'memory'}), "
                f"and {ceiling} is the most this will accept -- past that the "
                f"kernel kills the seed part-way through, which costs more "
                f"than seeding slowly.")
        argv += ["--workers", str(n)]

    # Prefix for generated usernames. A deleted Workspace address stays
    # taken for 20 days, so a wipe-and-recreate that reuses the fixed
    # GENERATED_LOCALPARTS list fails with "Entity already exists" until the
    # deletions age out. It also makes a run identifiable afterwards.
    prefix = (body.get("localpart_prefix") or "").strip()
    if prefix:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,15}", prefix):
            return [], {}, (
                f"{prefix!r} is not a usable username prefix -- letters, "
                "digits, dot, dash and underscore only, starting with a "
                "letter or digit, at most 16 characters.")
        argv += ["--localpart-prefix", prefix]

    target_gb = body.get("target_gb_per_user")
    if target_gb:
        try:
            argv += ["--target-gb-per-user", str(float(target_gb))]
        except (TypeError, ValueError):
            return [], {}, "target_gb_per_user must be a number"

    env = gcloud_env()
    # Set here rather than asking the operator to export it: the value carries
    # no judgement, the typed domain above is what actually gates this.
    env["SANDBOX_MODE"] = "true"
    if account_id is not None:
        env.update(SOURCE_DOMAIN=st.source_domain, SOURCE_ADMIN=st.source_admin,
                   SOURCE_SA_KEY=st.source_sa_key, MIGRATION_DB=st.db_path)
        # TARGET_DOMAIN too, even though the seeder never writes to the
        # target: it names every row of the identity map
        # `<localpart>@{settings.target_domain}`. Overlaying only the source
        # left that read falling through to env.sh's global placeholder, so a
        # 201-user seed of source.rohitrokaya.com.np produced 201 rows
        # pointing at a.example.com -- a domain in no part of this migration.
        # Loading that map would have aimed provision-users and the whole
        # migration at accounts that cannot exist.
        env.update(TARGET_DOMAIN=st.target_domain, TARGET_ADMIN=st.target_admin)
    return argv, env, ""


def reset_target_argv(body: dict, account_id: int | None = None) -> tuple[list[str], dict, str]:
    """
    Build the reset_target command, or return why it must not run.

    Mirrors seed_argv exactly, pointed at the other tenant: reset_target.py's
    own guard (assert_sandbox) needs SANDBOX_MODE=true and an exact,
    case-insensitive match on TARGET_DOMAIN, and refuses outright if target
    and source are ever the same domain. The typed-domain check here is a
    convenience that fails fast with a specific message; the guard that
    actually matters runs again inside reset_target.py itself regardless.
    """
    from config import Settings

    st = Settings(account_id=account_id)
    domain = (st.target_domain or "").strip().lower()
    typed = (body.get("confirm_domain") or "").strip().lower()

    if not domain:
        return [], {}, "set the target domain in step 2 first"
    if not typed:
        return [], {}, f"type the target domain ({domain}) to confirm"
    if typed != domain:
        source = (st.source_domain or "").strip().lower()
        extra = (" — that is the SOURCE domain, which this would never touch, "
                "but it is also not the target" if typed and typed == source else "")
        return [], {}, f"{typed!r} does not match the target domain {domain!r}{extra}"

    protected = [d.strip().lower()
                for d in os.getenv("PROTECTED_DOMAINS", "").split(",") if d.strip()]
    if domain in protected:
        return [], {}, f"{domain} is listed in PROTECTED_DOMAINS"

    argv = [PY, "reset_target.py", "--confirm-domain", domain, "--yes"]
    # Optional and additive: omitting it keeps today's full-wipe default
    # (reset_target.py's own --services default is all four services), so
    # this never changes behavior for a caller that does not know about it.
    services = body.get("services")
    if services:
        argv += ["--services", services if isinstance(services, str) else ",".join(services)]
    env = gcloud_env()
    env["SANDBOX_MODE"] = "true"
    if account_id is not None:
        env.update(TARGET_DOMAIN=st.target_domain, TARGET_ADMIN=st.target_admin,
                   TARGET_SA_KEY=st.target_sa_key, MIGRATION_DB=st.db_path)
    return argv, env, ""


def wipe_target_argv(body: dict, account_id: int | None = None) -> tuple[list[str], dict, str]:
    """
    Build the wipe_target command, or return why it must not run.

    reset_target empties the seeded *data*; this deletes the target
    ACCOUNTS provisioning created. After a real run the target holds a user
    per source identity, and leaving them behind makes the next rehearsal's
    fidelity check meaningless -- provisioning skips the users that already
    exist and the copy lands on top of the previous one.

    Same typed-domain gate and the same three-deep guard as reset_target:
    the domain compared against comes from Settings(), never the body, and
    wipe_target.py re-runs reset_target.assert_sandbox() itself regardless
    of what this decides. --apply is passed because a UI button that
    reports without doing anything is the one thing nobody wants twice.
    """
    argv, env, err = reset_target_argv(body, account_id)
    if err:
        return [], {}, err
    # reset_target_argv already validated the domain and built the env; only
    # the script and its flags differ.
    domain = argv[argv.index("--confirm-domain") + 1]
    # Deliberately NOT --account-id, even though wipe_target.py accepts one.
    # The env above already points MIGRATION_DB at that account's ledger,
    # and control_plane_db._db_path() is Settings().db_path -- so a child
    # told to resolve an account follows MIGRATION_DB into the per-account
    # ledger looking for tenant_configs, a table that only exists in the
    # control-plane database. Live, that is exactly how this failed:
    #   sqlite3.OperationalError: no such table: tenant_configs
    # The env carries TARGET_DOMAIN, TARGET_ADMIN, TARGET_SA_KEY and the
    # ledger already, which is everything the child actually needs.
    argv = [PY, "wipe_target.py", "--confirm-domain", domain, "--apply"]
    return argv, env, ""


def wipe_source_argv(body: dict, account_id: int | None = None) -> tuple[list[str], dict, str]:
    """Delete the SOURCE tenant's users -- the corpus being migrated.

    Separate from wipe_target_argv rather than a flag on it, because these
    two are not variations of one action. Emptying the target is routine
    between rehearsals; emptying the source destroys what the migration
    exists to move, and is only ever right when reseeding under different
    usernames, where the old accounts must be gone rather than merely
    emptied.

    The typed domain is therefore the SOURCE domain, so muscle memory from
    the target button cannot fire this one.
    """
    from config import Settings

    st = Settings(account_id=account_id)
    domain = (st.source_domain or "").strip().lower()
    typed = (body.get("confirm_domain") or "").strip().lower()

    if not domain:
        return [], {}, "set the source domain in step 2 first"
    if not typed:
        return [], {}, f"type the source domain ({domain}) to confirm"
    if typed != domain:
        target = (st.target_domain or "").strip().lower()
        extra = (" -- that is the TARGET domain; this button deletes the "
                 "SOURCE corpus" if typed and typed == target else "")
        return [], {}, f"{typed!r} does not match the source domain {domain!r}{extra}"

    protected = [d.strip().lower()
                 for d in os.getenv("PROTECTED_DOMAINS", "").split(",") if d.strip()]
    if domain in protected:
        return [], {}, f"{domain} is listed in PROTECTED_DOMAINS"

    argv = [PY, "wipe_target.py", "--side", "source",
            "--confirm-domain", domain, "--apply"]
    env = gcloud_env()
    env["SANDBOX_MODE"] = "true"
    if account_id is not None:
        env.update(SOURCE_DOMAIN=st.source_domain, SOURCE_ADMIN=st.source_admin,
                   SOURCE_SA_KEY=st.source_sa_key, TARGET_DOMAIN=st.target_domain,
                   MIGRATION_DB=st.db_path)
    return argv, env, ""


def resolve_target_account(caller_id: int | None,
                           requested) -> tuple[int | None, str]:
    """Which account an operator action applies to, and whether they may.

    Maintenance actions resolved the account from the caller's own session
    alone, so a superadmin looking at somebody else's migration could only
    ever act on their own -- live, a full ledger reset aimed at account 7
    came back "set the source domain in step 2 first" because it had
    silently resolved to the operator's own empty tenant. Same shape as the
    delta button, which built its command from op.account_id and ran against
    the wrong tenant entirely.

    Returns (account_id, error). An absent request means "my own account",
    which is what a tenant self-serving always wants.
    """
    if requested in (None, "", "null"):
        return caller_id, ""
    try:
        wanted = int(requested)
    except (TypeError, ValueError):
        return None, f"{requested!r} is not an account id"
    if wanted == caller_id:
        return wanted, ""
    account = accounts_auth.get_account(caller_id) if caller_id else None
    if not (account and account.get("is_superadmin")):
        return None, "that migration belongs to another account"
    return wanted, ""


def reset_drive_ledger_argv(body: dict, account_id: int | None = None) -> tuple[list[str], dict, str]:
    """
    Build the reset_drive_ledger command, or return why it must not run.

    Same typed-domain-confirms pattern as reset_target_argv, but against
    SOURCE_DOMAIN: reset_drive_ledger.py always operates on source_email
    keys (see its own --confirm-domain help text) regardless of which
    tenant's files were actually wiped, so confirming against the wrong
    domain here would silently do nothing useful even before its own guard
    catches it.
    """
    from config import Settings

    st = Settings(account_id=account_id)
    domain = (st.source_domain or "").strip().lower()
    typed = (body.get("confirm_domain") or "").strip().lower()

    if not domain:
        return [], {}, "set the source domain in step 2 first"
    if not typed:
        return [], {}, f"type the source domain ({domain}) to confirm"
    if typed != domain:
        return [], {}, f"{typed!r} does not match the source domain {domain!r}"

    argv = [PY, "reset_drive_ledger.py", "--confirm-domain", domain, "--yes"]
    services = body.get("services")
    if services:
        argv += ["--services", services if isinstance(services, str) else ",".join(services)]
    env = dict(os.environ)
    if account_id is not None:
        env.update(SOURCE_DOMAIN=st.source_domain, MIGRATION_DB=st.db_path)
    return argv, env, ""


def gcloud_env() -> dict:
    """
    Child-process environment with gcloud reachable.

    setup.sh calls `command -v gcloud`. This server is typically launched
    without an interactive shell, so it does not inherit the PATH entry the
    SDK installer added to the user's profile -- setup.sh would fail with
    "gcloud not installed" on a machine where gcloud plainly works.
    """
    env = dict(os.environ)
    try:
        from wizard import find_gcloud

        path, _how = find_gcloud()
        if path:
            env["PATH"] = os.path.dirname(path) + os.pathsep + env.get("PATH", "")
            env["GCLOUD_BIN"] = path
    except Exception:  # noqa: BLE001 - never let discovery break a run
        pass
    return env


def dwd_payload() -> dict:
    """
    The exact Client ID and scope line to paste into each Admin Console.

    This is the one step no tool can perform, so the least a tool can do is
    remove every opportunity to get it wrong: the strings are produced from
    the same `config.py` functions the engine authenticates with, so they
    cannot drift from what is actually requested at runtime.
    """
    from config import Settings, TRANSFER_MODES, source_scopes, target_scopes

    def _full_union(base: "Settings", fn) -> list[str]:
        """Every scope `fn` could ever ask for, across every toggle this
        operator might flip on later -- the "one line that never needs
        re-pasting" answer, not just what today's settings happen to need.
        The Admin Console replaces the whole scope line on every edit, and
        each edit re-triggers propagation delay for the entire grant (seen
        live: ~2 min typical, up to 30), so a line that already covers a
        feature you turn on next week is strictly better than one you have
        to keep re-pasting."""
        import dataclasses

        scopes: set[str] = set()
        for mode in TRANSFER_MODES:
            for gmail in (False, True):
                for chat in (False, True):
                    for chat_mode in ("direct", "import"):
                        for contacts in (False, True):
                            for tasks in (False, True):
                                for sso in (False, True):
                                    for cal_acls in (False, True):
                                        s = dataclasses.replace(
                                            base, transfer_mode=mode,
                                            migrate_gmail_settings=gmail,
                                            migrate_chat=chat,
                                            chat_space_mode=chat_mode,
                                            migrate_contacts=contacts,
                                            migrate_tasks=tasks,
                                            migrate_sso=sso,
                                            migrate_calendar_acls=cal_acls,
                                        )
                                        scopes.update(fn(s))
        return sorted(scopes)

    st = Settings()
    out = {"tenants": []}
    for side, key_path, scopes, admin, domain in (
        ("source", st.source_sa_key, source_scopes(st), st.source_admin, st.source_domain),
        ("target", st.target_sa_key, target_scopes(st), st.target_admin, st.target_domain),
    ):
        client_id = ""
        try:
            with open(key_path, encoding="utf-8") as fh:
                client_id = json.load(fh).get("client_id", "")
        except Exception:  # noqa: BLE001 - absent key is a normal early state
            pass
        out["tenants"].append({
            "side": side, "domain": domain, "admin": admin,
            "client_id": client_id,
            "scopes": ",".join(scopes),
            "scope_list": scopes,
        })

    # "MIGRATE SOURCE" / "MIGRATE TARGET" -- the full union across every
    # transfer mode and optional-feature toggle, not just whichever ones are
    # on right now. This is the exact line to paste once and never revisit,
    # matched to the copy-paste blocks already worked out by hand for this
    # tenant pair; the per-tenant `scopes` field above stays as the narrower
    # "what today's settings actually need" answer.
    try:
        out["migrate_source_full"] = _full_union(st, source_scopes)
        out["migrate_target_full"] = _full_union(st, target_scopes)
    except Exception:  # noqa: BLE001 - never break /api/dwd over this extra
        out["migrate_source_full"] = []
        out["migrate_target_full"] = []

    # Provisioning a target account needs admin.directory.user (write), which
    # the migration target set deliberately does not carry. The Admin Console
    # REPLACES the scope line, so a target you intend to provision needs one
    # line carrying both -- paste the migration set alone and provisioning
    # fails with unauthorized_client, exactly as it just did.
    try:
        from config import DIRECTORY_WRITE_SCOPE
        combined = sorted(set(target_scopes(st)) | {DIRECTORY_WRITE_SCOPE})
        out["target_provision"] = {
            "scopes": ",".join(combined),
            "scope_list": combined,
        }
    except Exception:  # noqa: BLE001 - provisioning is optional; never break /api/dwd
        out["target_provision"] = {}

    # Seeding writes; migrating (on the source) reads. The Admin Console's
    # delegation editor REPLACES the scope line rather than adding to it, so a
    # source tenant you intend to seed as well as migrate needs one line
    # carrying both -- paste the migration set alone and seeding fails with
    # unauthorized_client, paste the seed set alone and the migration does.
    try:
        import provision

        data_gen = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "data-generator")
        if data_gen not in sys.path:
            sys.path.insert(0, data_gen)
        from seed_sandbox import (
            DIRECTORY_READONLY_SCOPE, REPORTS_SCOPE, SEED_SCOPES,
            _resolve_key_path,
        )

        # Every optional seed feature's scope, unioned -- --create-users,
        # --fit-to-licenses, and the default/--all-users live Directory
        # discovery all need a scope beyond the base SEED_SCOPES write set.
        # One line covering all of them means never having to work out
        # afterward which feature's unauthorized_client came from a scope
        # that simply was not asked for yet.
        full = sorted(set(SEED_SCOPES) | {
            provision.DIRECTORY_WRITE_SCOPE, REPORTS_SCOPE,
            DIRECTORY_READONLY_SCOPE,
        })
        combined = sorted(set(full) | set(source_scopes(st)))

        # Same client-id resolution seed_sandbox.py itself uses (SEED_SA_KEY,
        # falling back to the source key) -- so this names the exact Admin
        # Console entry the scopes above actually need to land on, the same
        # way the tenants loop above does for source/target.
        seed_client_id = ""
        try:
            with open(_resolve_key_path(st), encoding="utf-8") as fh:
                seed_client_id = json.load(fh).get("client_id", "")
        except Exception:  # noqa: BLE001 - absent key is a normal early state
            pass
        # True only when no dedicated seed-sa.json exists yet and this
        # would-be seed key is actually the read-only source key -- pasting
        # the line below onto it grants it write access, which is exactly
        # the guarantee a separate SEED_SA_KEY exists to avoid.
        shares_source_key = (seed_client_id
                             and seed_client_id == out["tenants"][0]["client_id"])

        out["seed"] = {
            "client_id": seed_client_id,
            "shares_source_key": bool(shares_source_key),
            "scopes": ",".join(full),
            "scope_list": full,
            "combined": ",".join(combined),
            "combined_list": combined,
        }
    except Exception:  # noqa: BLE001 - the seeder is optional; never break /api/dwd
        out["seed"] = {}
    return _widen_to_required(out, st)


def _widen_to_required(out: dict, st) -> dict:
    """Make every paste line a superset of `required_scopes()` for its side.

    Why this exists
    ---------------
    This payload grew five independent scope lists -- one per tenant, a
    "full" line per side, a provisioning line, and the seeder's -- each
    assembled by its own union. They drifted, and the drift was invisible
    because every one of them looks complete on its own. Measured before
    this function existed:

        seed                 12 scopes, missing 4 that source requires
                             (drive.readonly, gmail.readonly,
                              calendar.readonly, admin.directory.group.readonly)
        migrate_source_full  15 scopes, missing 6 that source requires
        target_provision      7 scopes, missing 8

    A tenant set up by seeding therefore came out migrate-*incapable* by
    construction: the seed line grants what the seeder writes with, and the
    four read-only scopes it omits are exactly the ones only a migration
    reads with. That is not hypothetical -- a live source tenant granted
    from the seed line failed its migration on the missing `drive.readonly`
    with a bare `unauthorized_client` naming neither tenant nor scope.

    Widening rather than replacing is deliberate: each line's own union
    still contributes (it covers toggles `required_scopes` does not model),
    and a console grant is monotonic -- authorising a scope nobody requests
    costs nothing, while omitting one fails the entire token exchange. The
    Admin Console also replaces the whole line on every edit and re-triggers
    propagation for the whole grant, so one line that covers seeding *and*
    migration is strictly better than two that each need re-pasting.

    Enforced by tests/test_scope_guard.py, not by convention -- convention
    is what produced the drift.
    """
    import verify_scopes

    # Scopes worth GRANTING but deliberately never REQUIRED.
    #
    # The asymmetry is the point. A console grant is monotonic -- authorising
    # a scope nobody requests costs nothing -- but a scope in the code's own
    # request list that the console has not authorised fails the ENTIRE token
    # exchange, so every migration on every tenant that had not re-pasted
    # would break. These therefore ride along on the paste line, and the
    # features behind them degrade to "not available" until it is pasted.
    optional = verify_scopes.OPTIONAL_SCOPES

    def _widen(entry: dict, side: str) -> None:
        try:
            need = set(verify_scopes.required_scopes(st, side)) | optional
        except Exception:      # noqa: BLE001 - never break /api/dwd
            return
        have = set(entry.get("scope_list") or [])
        if not have:
            have = {x.strip() for x in (entry.get("scopes") or "").split(",")
                    if x.strip()}
        merged = sorted(have | need)
        entry["scopes"] = ",".join(merged)
        entry["scope_list"] = merged
        if "combined_list" in entry or "combined" in entry:
            combined = sorted(set(entry.get("combined_list") or []) | set(merged))
            entry["combined"] = ",".join(combined)
            entry["combined_list"] = combined

    for t in out.get("tenants", []):
        _widen(t, t.get("side") or t.get("tenant") or "source")
    for key, side in (("migrate_source_full", "source"),
                      ("migrate_target_full", "target"),
                      ("target_provision", "target"),
                      # The seeder writes into the source tenant, and shares
                      # that tenant's console entry unless a dedicated
                      # SEED_SA_KEY exists -- so it is the source side that
                      # has to end up complete.
                      ("seed", "source")):
        entry = out.get(key)
        if isinstance(entry, dict) and entry:
            _widen(entry, side)
        elif isinstance(entry, list) and entry:
            wrapper = {"scope_list": entry}
            _widen(wrapper, side)
            out[key] = wrapper["scope_list"]
        elif isinstance(entry, str) and entry:
            wrapper = {"scopes": entry}
            _widen(wrapper, side)
            out[key] = wrapper["scopes"]
    return out


def scope_diagnosis(tenant: str) -> dict:
    """
    Which scope, exactly, is not authorised -- for a tenant whose combined
    request fails.

    A single unauthorised (or not-yet-propagated) scope fails the *entire*
    combined token request with the same generic unauthorized_client error,
    whatever else in that request is fine. Diagnosing which one requires
    minting a separate token per scope and checking each in isolation --
    this was done live, more than once, over SSH by hand before this
    existed. This is that same bisection, as a button instead of a manual
    session.

    Deliberately explicit-trigger only (POST /api/scope_diagnosis), never
    on a poll path: bisecting N scopes is N+1 live token mints against
    Google, the same "no live API call on a poll loop" rule
    webui_spa.py's module docstring states for exactly this reason.
    """
    from config import Settings, source_scopes, target_scopes

    st = Settings()
    if tenant == "source":
        key, admin = st.source_sa_key, st.source_admin
        scopes, domain = source_scopes(st), st.source_domain
    elif tenant == "target":
        key, admin = st.target_sa_key, st.target_admin
        scopes, domain = target_scopes(st), st.target_domain
    else:
        return {"tenant": tenant, "error": "tenant must be 'source' or 'target'"}

    result: dict = {"tenant": tenant, "domain": domain, "admin": admin,
                    "combined_ok": False, "error": "", "scopes": []}
    if not admin:
        result["error"] = f"no admin configured for {tenant} (set it in step 2)"
        return result
    if not (key and os.path.isfile(key)):
        result["error"] = f"no key file for {tenant} yet"
        return result

    def _try(scope_list: list[str]) -> tuple[bool, str]:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_file(
                key, scopes=scope_list).with_subject(admin)
            creds.refresh(Request())
            return True, ""
        except Exception as exc:  # noqa: BLE001 - reporting the failure is the point
            return False, str(exc)[:200]

    ok, err = _try(scopes)
    result["combined_ok"] = ok
    if ok:
        result["scopes"] = [{"scope": s, "ok": True} for s in scopes]
        return result

    # Only bisect on failure -- a passing combined check already answers
    # the question for every scope in it, at 1/N the cost.
    result["error"] = err
    for s in scopes:
        s_ok, s_err = _try([s])
        result["scopes"].append({"scope": s, "ok": s_ok, "error": "" if s_ok else s_err})
    return result


# ----------------------------------------------------------------------
# Status snapshots
#
# Detecting step 5 means running a real preflight -- minting a token per user
# against both tenants. On a five-user pair that measured **9.5 seconds**, and
# the page was polling every 6, so requests piled up faster than they
# completed: concurrent preflight subprocesses on a 2-core VPS, and any poll
# that errored under the load replaced the whole panel with an error box,
# taking whatever was half-typed in the form with it.
#
# So the poll never computes. A background thread recomputes on a slow cycle
# and every request returns the latest finished snapshot immediately.
# Delegation does not change second to second; a stale-by-30s answer is worth
# far more than a UI that stalls.
# ----------------------------------------------------------------------
STATUS_TTL = 30.0

# Keyed by account, because the snapshot describes one tenant's ledger and
# this process serves every tenant. A single shared entry meant whoever
# polled first was answered to everyone else for the next thirty seconds
# -- the same fault the websocket hub had when it was a bare set of
# sockets. None is the key for the operator's own env.sh ledger, which is
# what the SSH-tunnel path has always read.
_snaps: dict = {}
_snap_lock = threading.Lock()
_snap_busy: set = set()


def _compute_status(account_id: int | None = None) -> dict:
    if State is None or build_steps is None:
        return {"error": "wizard.py could not be imported; run from the repo root"}
    return _status_uncached(account_id)


def _refresh_snapshot(key=None, account_id: int | None = None) -> None:
    try:
        data = _compute_status(account_id)
        with _snap_lock:
            _snaps[key] = {"data": data, "at": time.time()}
    finally:
        with _snap_lock:
            _snap_busy.discard(key)


def check_step(n: int) -> dict:
    """
    Re-evaluate one step, now, and say whether it is satisfied.

    Deliberately synchronous and uncached: this is the operator asking "did
    what I just did work?", and the answer has to reflect the last thirty
    seconds, not a snapshot taken before they went to the Admin Console.
    """
    _invalidate_all()
    try:
        data = _compute_status()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"could not evaluate: {exc}"}
    if data.get("error"):
        return {"ok": False, "error": data["error"]}

    step = next((s for s in data["steps"] if s["n"] == n), None)
    if not step:
        return {"ok": False, "error": f"no step {n}"}

    state = step["state"]
    satisfied = state in ("done", "skip")
    return {
        "ok": satisfied,
        "state": state,
        "title": step["title"],
        "detail": step.get("note") or "",
        "msg": ("satisfied" if state == "done" else
                "not needed for this run" if state == "skip" else
                "still outstanding"),
    }


def _invalidate_all() -> None:
    """Both caches. A button press that only clears one leaves half the
    dashboard reporting the world as it was before the press."""
    invalidate_status()
    invalidate_spa_cache()


def invalidate_status() -> None:
    """
    Drop the cached snapshot so the next poll recomputes.

    Anything that changes what the steps *mean* has to call this. Without it a
    configuration change is invisible for up to STATUS_TTL seconds -- switching
    run mode appeared to do nothing at all, because the answer had already been
    computed under the old setting and was simply replayed.
    """
    # Every tenant's entry, not just the caller's: what changed is env.sh
    # or the run mode, which is a property of the box.
    with _snap_lock:
        for entry in _snaps.values():
            entry["at"] = 0.0


def status_payload(account_id: int | None = None) -> dict:
    """The cached snapshot for one account. Never blocks on a preflight."""
    key = account_id
    with _snap_lock:
        entry = _snaps.get(key)
        data = entry["data"] if entry else None
        age = time.time() - entry["at"] if entry else 0.0

    if data is None:
        # First call for this account: nothing to show yet, so pay for it
        # once rather than answering with another tenant's numbers.
        with _snap_lock:
            _snap_busy.add(key)
        _refresh_snapshot(key, account_id)
        with _snap_lock:
            return dict(_snaps[key]["data"], stale=False)

    with _snap_lock:
        refresh = age > STATUS_TTL and key not in _snap_busy
        if refresh:
            _snap_busy.add(key)
    if refresh:
        threading.Thread(target=_refresh_snapshot, args=(key, account_id),
                         daemon=True).start()

    return dict(data, stale=age > STATUS_TTL, age=round(age, 1))


# ----------------------------------------------------------------------
# Cache for the SPA reads that scan the ledger.
#
# Measured against a live 200k-row audit_log, per full dashboard refresh:
#
#     spa_stages_payload        2018 ms
#     spa_users_payload         1263 ms
#     spa_metrics_payload        805 ms
#     snapshot_payload           788 ms
#     spa_verification_payload   783 ms
#                                ------
#                                5.7 s of CPU
#
# useMigration.ts polls every 4000 ms. One open dashboard tab therefore
# asked for more CPU than the box has, and took it from the migration it
# was displaying: webui.py held 43% of a 2-core VPS during a live run, and
# closing the browser returned ~13% throughput to the migration.
#
# A UI whose job is to show progress must not be a meaningful cost to that
# progress. Same shape as the status snapshot above: serve what we have,
# refresh behind it, never block a request on a scan.
# ----------------------------------------------------------------------
SPA_TTL = float(os.getenv("SPA_CACHE_TTL", "15"))

_spa_cache: dict = {}
_spa_lock = threading.Lock()
_spa_busy: set = set()


def _cached_payload(name: str, fn, account_id: int | None):
    """fn(account_id), memoised per (reader, account) for SPA_TTL seconds.

    The first call for a key pays for itself -- there is nothing to show yet
    and a wrong answer is worse than a slow one. Every later call is served
    from the entry while a single background thread refreshes it, so N
    pollers cost the same as one.
    """
    key = (name, account_id)
    now = time.time()
    with _spa_lock:
        entry = _spa_cache.get(key)

    if entry is None:
        data = fn(account_id)
        with _spa_lock:
            _spa_cache[key] = {"data": data, "at": time.time()}
        return data

    if now - entry["at"] > SPA_TTL:
        with _spa_lock:
            start = key not in _spa_busy
            if start:
                _spa_busy.add(key)

        def _refresh() -> None:
            try:
                data = fn(account_id)
                with _spa_lock:
                    _spa_cache[key] = {"data": data, "at": time.time()}
            except Exception as exc:      # noqa: BLE001
                # Keep serving the stale entry: a failed refresh is not a
                # reason to blank a dashboard that was working a moment ago.
                log.warning("could not refresh %s for account %s: %r",
                            name, account_id, exc)
            finally:
                with _spa_lock:
                    _spa_busy.discard(key)

        if start:
            threading.Thread(target=_refresh, daemon=True).start()

    return entry["data"]


def invalidate_spa_cache() -> None:
    """Drop it, so the next poll recomputes. Anything that changes what
    these describe -- a launch, a stop, a reset -- should call this, or the
    UI looks frozen for up to SPA_TTL after a button press."""
    with _spa_lock:
        _spa_cache.clear()


def _RUN_MODES() -> dict:
    from wizard import RUN_MODES

    return {k: {"label": v["label"], "blurb": v["blurb"],
                "setup": v.get("setup", []),
                "requires": v.get("requires", []),
                "runs": v.get("runs", [])}
            for k, v in RUN_MODES.items()}


def _status_uncached(account_id: int | None = None) -> dict:
    st = State(account_id=account_id)
    steps = build_steps(st)
    ok, failed, users_done = st.migration_progress()
    return {
        "env": {k: st.env.get(k, "") for k in
                ("SOURCE_DOMAIN", "TARGET_DOMAIN", "AUTH_MODE", "TRANSFER_MODE")},
        "steps": [{"n": s["n"], "title": s["title"], "state": s["state"],
                   "note": s.get("note", ""),
                   # The wizard UI shows one step at a time, so it needs the
                   # explanatory text too -- not just the status line.
                   "help": s.get("help", []),
                   "auto": s.get("auto", ""),
                   "manual": bool(s.get("manual")),
                   "skipped": bool(s.get("skipped")),
                   "actions": [a for a in STEP_ACTIONS.get(s["n"], []) if a in ACTIONS]}
                  for s in steps],
        # Skipped steps are neither done nor outstanding, so counting them in
        # the total would leave every run stuck below 100%.
        "done": sum(1 for s in steps if s["state"] == "done"),
        "total": sum(1 for s in steps if s["state"] != "skip"),
        "migrated": ok, "failed": failed,
        "users_done": users_done, "users_total": st.identities_loaded(),
    }


# ----------------------------------------------------------------------
# Operator dashboard — the same data the TUI reads, served to the page.
# migration.db is opened read-only (mode=ro), exactly like tui.py does.
# The only mutation state here is the launch toggles below.
# ----------------------------------------------------------------------

# dry-run + which services "Migrate"/"Delta" run. Mirrors the TUI's
# d/t/s keys so the web buttons behave identically to the keyboard.
#
# contacts/tasks off by default like chat: each widens the OAuth grant, and a
# scope the Admin Console has not authorised fails every call outright, so
# enabling one must be a deliberate click, never a default.
_RUN_STATE: dict = {
    "dry_run": False,
    "services": {"drive": True, "gmail": True, "calendar": True,
                "chat": False, "contacts": False, "tasks": False},
}

# Actions whose argv follow the launch toggles (everything else uses its
# fixed ACTIONS argv verbatim).
_LAUNCH_KEYS = ("migrate", "delta")

# The order phases.py runs them in, filtered to the ones that have a toggle.
PHASE_ORDER = ("drive", "gmail", "calendar", "contacts", "tasks", "chat")

# Actions that read the toggles through the environment rather than argv.
# main.py's migrate/delta infer MIGRATE_CHAT/CONTACTS/TASKS from --services,
# so a checkbox reaches them through _action_argv above. phases.py does not:
# its per-phase gate reads those settings straight from the environment
# regardless of --phase, so a toggle only reaches it if this run explicitly
# sets or clears the variable -- otherwise a MIGRATE_CHAT=true left in env.sh
# from an earlier session would run Chat with no visible checkbox for it.
_PHASE_GATED_ACTIONS = ("phased_migrate", "phased_count_only")


# Which ledger item types prove a service ran. Mirrors main.py's
# _SERVICE_ITEMS -- the same question asked for the same reason.
_SERVICE_ITEMS = {
    "chat": ("space", "chat_message"),
    "contacts": ("contact", "contact_group"),
    "tasks": ("task", "task_list"),
}


def _services_in_ledger(account_id: int | None) -> set:
    """Which optional services this migration actually moved something for.

    A verification pass must check what was DONE, not what the toggles
    happen to say now. Live: a reconcile over a run that had migrated 4,975
    contacts and 3,980 tasks reported

        note: contacts requested but MIGRATE_CONTACTS is off -- skipping.
        note: tasks requested but MIGRATE_TASKS is off -- skipping.

    and went on to compare three services out of six. The toggles default
    those off, and they had never been turned on in this process -- so the
    fidelity check silently covered half the migration while presenting
    itself as the fidelity check.
    """
    conn = _db_conn(account_id)
    if conn is None:
        return set()
    found = set()
    try:
        for svc, types in _SERVICE_ITEMS.items():
            marks = ",".join("?" * len(types))
            row = conn.execute(
                f"SELECT 1 FROM audit_log WHERE item_type IN ({marks}) "
                "AND status='SUCCESS' LIMIT 1", types).fetchone()
            if row:
                found.add(svc)
    except Exception as exc:      # noqa: BLE001 - never block an action
        log.warning("could not read services from the ledger: %r", exc)
    finally:
        conn.close()
    return found


def _service_env(account_id: int | None = None,
                 from_ledger: bool = False) -> dict:
    """gcloud_env(), plus the per-user service toggles made explicit.

    from_ledger unions in whatever the ledger proves was migrated, so a
    verification pass covers the run it is verifying rather than the
    checkboxes someone last touched.
    """
    env = gcloud_env()
    on = {k for k, v in _RUN_STATE["services"].items() if v}
    if from_ledger:
        on |= _services_in_ledger(account_id)
    for key, flag in (("chat", "MIGRATE_CHAT"), ("contacts", "MIGRATE_CONTACTS"),
                      ("tasks", "MIGRATE_TASKS")):
        env[flag] = "true" if key in on else "false"
    return env


def _db_conn(account_id: int | None = None):
    """A read-only connection to one account's resume ledger, or None.

    Every SPA payload below opens the ledger through here, so an account
    threaded to this call scopes all of them at once. None keeps the
    operator's env.sh ledger, which is what the SSH-tunnel path reads.
    """
    import sqlite3

    from config import Settings

    path = Settings(account_id=account_id).db_path
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _serialize_snapshot(snap) -> dict:
    return {
        "users": [{
            "source": u.source, "target": u.target, "status": u.status,
            "drive_done": u.drive_done, "drive_failed": u.drive_failed,
            "drive_skipped": u.drive_skipped,
            "mail_done": u.mail_done, "mail_failed": u.mail_failed,
            "mail_skipped": u.mail_skipped,
            "cal_done": u.cal_done, "cal_failed": u.cal_failed,
            "acl_failed": u.acl_failed, "bytes_moved": u.bytes_moved,
            "exp_drive": u.exp_drive, "exp_mail": u.exp_mail,
            "gb_today": u.gb_today,
            "done": u.done, "failed": u.failed,
            "expected": u.expected, "fraction": u.fraction,
        } for u in snap.users],
        "failures": snap.failures,
        "totals": snap.totals,
        "collected_at": snap.collected_at,
        "error": snap.error,
    }


def snapshot_payload(account_id: int | None = None) -> dict:
    """The TUI snapshot plus the launch toggles, for the page's poll loop."""
    import sqlite3

    import tui
    from config import Settings

    conn = _db_conn(account_id)
    if conn is None:
        return {"error": "no database yet — run init-db or create identities.csv",
                "toggles": dict(_RUN_STATE), "snapshot": None}
    try:
        snap = tui.collect_snapshot(conn, Settings(account_id=account_id).effective_upload_cap())
    except sqlite3.Error as exc:
        return {"error": f"db read error: {exc}",
                "toggles": dict(_RUN_STATE), "snapshot": None}
    finally:
        conn.close()
    return {"error": "", "toggles": dict(_RUN_STATE),
            "snapshot": _serialize_snapshot(snap)}


def identities_payload(account_id: int | None = None) -> dict:
    """Every identity_map row, for the Identities page.

    The legacy dashboard's own identities tab called this route and
    /api/identities/save below, but neither ever existed server-side --
    both 404'd. Built for real here rather than porting that dead behavior.
    """
    import sqlite3

    conn = _db_conn(account_id)
    if conn is None:
        return {"error": "no database yet — run init-db or create identities.csv",
                "rows": []}
    try:
        rows = conn.execute(
            "SELECT source_email, target_email, entity_type, status "
            "FROM identity_map ORDER BY source_email").fetchall()
    except sqlite3.Error as exc:
        return {"error": f"db read error: {exc}", "rows": []}
    finally:
        conn.close()
    return {"error": "", "rows": [dict(r) for r in rows]}


def save_identity_pair(source_email: str, target_email: str) -> dict:
    """Append one hand-entered source->target pair to identities.csv.

    Additive only: identities.csv is what init_db/init_db_auto read from
    (see ACTIONS above), so a pair added here takes effect the next time
    either of those actions runs -- this never writes to identity_map
    directly, keeping "what init-db will load" and "what is already
    loaded" the same one honest source of truth.
    """
    import csv

    source_email = source_email.strip().lower()
    target_email = target_email.strip().lower()
    if not source_email or "@" not in source_email:
        return {"ok": False, "error": "source_email must be a real address"}
    if not target_email or "@" not in target_email:
        return {"ok": False, "error": "target_email must be a real address"}

    path = IDENTITIES_CSV_PATH
    existing = read_identity_csv_domains_safe(path)
    if any(r["source_email"].lower() == source_email for r in existing):
        return {"ok": False, "error": f"{source_email} is already in identities.csv"}

    is_new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if is_new:
            writer.writerow(["source_email", "target_email"])
        writer.writerow([source_email, target_email])
    return {"ok": True, "total": len(existing) + 1}


def read_identity_csv_domains_safe(path: str) -> list[dict]:
    """Same shape as main.py's read_identity_csv_domains, without importing
    main.py itself (a large CLI module) just for this one helper)."""
    import csv

    out = []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                out.append({"source_email": (row.get("source_email") or "").strip(),
                           "target_email": (row.get("target_email") or "").strip()})
    except (OSError, csv.Error):
        return []
    return out


def spa_users_payload(account_id: int | None = None) -> dict:
    """User[] for migration-webui, read-only from the ledger. See webui_spa.py."""
    import sqlite3

    import webui_spa
    from config import Settings

    conn = _db_conn(account_id)
    if conn is None:
        return {"error": "no database yet — run init-db or create identities.csv",
               "users": []}
    try:
        return {"error": "", "users": webui_spa.users_payload(
            conn, Settings(account_id=account_id).effective_upload_cap())}
    except sqlite3.Error as exc:
        return {"error": f"db read error: {exc}", "users": []}
    finally:
        conn.close()


def _ledger_progress_fraction(account_id: int | None = None) -> float | None:
    """The same items_done/items_expected fraction the header progress bar
    and snapshot_payload() already compute from the ledger -- reused here
    rather than re-derived, since tui.collect_snapshot() is the one place
    that already resolves partial per-service completion into one number."""
    import sqlite3

    from config import Settings

    conn = _db_conn(account_id)
    if conn is None:
        return None
    try:
        import tui

        totals = tui.collect_snapshot(conn, Settings(account_id=account_id).effective_upload_cap()).totals
        return totals.get("fraction")
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _job_progress(name: str, lines: list[str], elapsed: float
                  ) -> tuple[int | None, int | None]:
    """(progressPct, etaSeconds) for the running job, or (None, None) when
    there is no reliable source for either -- guessing a percentage from
    nothing but log lines is worse than showing none at all.

    ETA is extrapolated linearly from elapsed time and fraction complete:
    it assumes the remaining work costs the same, per item, as the work
    already done. That is wrong the instant one user has 10x another's
    mailbox, but it is the same assumption every "time remaining" bar
    anyone has ever used makes, and it gets more accurate as fraction
    grows -- which is exactly when an operator starts actually watching it.
    """
    # A job that counts itself wins over every heuristic below: it is the
    # job's own statement of where it is, not an inference about it.
    pct = _counter_progress_pct(lines)
    if pct is not None:
        pass
    elif name == "seed":
        pct = _seed_progress_pct(lines)
    elif name in ("migrate", "delta", "discover"):
        frac = _ledger_progress_fraction()
        pct = round(frac * 100) if frac is not None else None
    if pct is None or pct <= 0 or elapsed <= 0:
        return pct, None
    eta = round(elapsed * (100 - pct) / pct)
    return pct, eta


def _job_activity_entry() -> dict | None:
    """
    A synthetic ActivityEvent for the one background job, if any.

    audit_log-backed activity is structurally blind to seed_sandbox.py,
    reset_target.py, and deploy_remote.py: none of them ever write a row
    to it (seed_sandbox.py calls Google's APIs directly; see
    webui_spa.py's module docstring on why nothing here makes a live API
    call of its own). Without this, "what is happening right now" lived in
    two places depending on which kind of work it was -- the ledger-backed
    feed for a migration, and only JobProgress's own panel for everything
    else -- and a seed run looked like nothing was happening at all here.

    Reshapes the same live Job state JobProgress already streams (see
    Job.snapshot()) into one row at the front of the list, so it is not a
    second thing to check.
    """
    snap = JOB.snapshot()
    if not snap["running"]:
        ext = _external_job_snapshot()
        if ext is not None:
            snap = ext
    if not snap["name"]:
        return None
    last = next((ln for ln in reversed(snap["lines"]) if ln.strip()), "")
    if snap["running"]:
        status, action = "in_progress", f"{snap['name']} running"
    elif snap["rc"] == 0:
        status, action = "completed", f"{snap['name']} finished"
    else:
        status, action = "needs_attention", f"{snap['name']} stopped (exit {snap['rc']})"
    return {
        "id": f"job:{snap['name']}:{snap['total']}",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "user": "System",
        "action": action,
        "status": status,
        "details": last[:200] or None,
        # See Job.snapshot()/_job_progress(): percentage can still be
        # meaningful after a job stops (its final figure), an ETA cannot.
        "progressPct": snap["progressPct"],
        "etaSeconds": snap["etaSeconds"],
    }


def spa_activity_payload(account_id: int | None = None) -> dict:
    import sqlite3

    import webui_spa

    job_entry = _job_activity_entry()
    conn = _db_conn(account_id)
    if conn is None:
        # Still surfaced: a job can run before init-db has ever been done
        # (a seed-only session, say), and the operator should see it -- but
        # the "no database yet" note is real diagnostic information too
        # (see ActivityFeed.tsx's error banner), so it is kept, not dropped
        # just because there also happens to be a job to show.
        return {"error": "no database yet", "activity": [job_entry] if job_entry else []}
    try:
        activity = webui_spa.activity_payload(conn)
        return {"error": "", "activity": ([job_entry] if job_entry else []) + activity}
    except sqlite3.Error as exc:
        return {"error": f"db read error: {exc}",
               "activity": [job_entry] if job_entry else []}
    finally:
        conn.close()


def spa_metrics_payload(account_id: int | None = None) -> dict:
    import sqlite3

    import tui
    import webui_spa
    from config import Settings

    settings = Settings(account_id=account_id)
    conn = _db_conn(account_id)
    totals: dict = {}
    if conn is not None:
        try:
            totals = tui.collect_snapshot(conn, settings.effective_upload_cap()).totals
        except sqlite3.Error:
            totals = {}
        finally:
            conn.close()
    return webui_spa.metrics_payload(settings, settings.effective_upload_cap(), totals)


def spa_stages_payload(account_id: int | None = None) -> dict:
    import sqlite3

    import webui_spa
    from config import Settings

    conn = _db_conn(account_id)
    if conn is None:
        return {"error": "no database yet", "stages": []}
    try:
        return {"error": "", "stages": webui_spa.stages_payload(
            conn, Settings(account_id=account_id), JOB.finished)}
    except sqlite3.Error as exc:
        return {"error": f"db read error: {exc}", "stages": []}
    finally:
        conn.close()


def spa_verification_payload(account_id: int | None = None) -> dict:
    import sqlite3

    import webui_spa
    from config import Settings

    conn = _db_conn(account_id)
    if conn is None:
        return {"error": "no database yet", "verification": []}
    try:
        return {"error": "", "verification": webui_spa.verification_payload(
            conn, Settings(account_id=account_id))}
    except sqlite3.Error as exc:
        return {"error": f"db read error: {exc}", "verification": []}
    finally:
        conn.close()


def spa_report_payload(account_id: int | None = None) -> dict:
    import sqlite3

    import webui_spa
    from config import Settings

    conn = _db_conn(account_id)
    if conn is None:
        return {"error": "no database yet", "report": None}
    try:
        # The most recently run job's own timing, not a scan over audit_log --
        # see webui_spa.report_payload's docstring for why.
        report = webui_spa.report_payload(conn, Settings(account_id=account_id), JOB.started, JOB.finished)
        return {"error": "", "report": report}
    except sqlite3.Error as exc:
        return {"error": f"db read error: {exc}", "report": None}
    finally:
        conn.close()


def scope_payload(account_id: int | None = None) -> dict:
    """The scope matrix plus discovered volume, rendered server-side."""
    import scope as scope_mod

    conn = _db_conn(account_id)
    volume = {}
    if conn is not None:
        try:

            class _Shim:
                conn = conn

            volume = scope_mod.planned_volume(_Shim())
        except Exception:  # noqa: BLE001
            volume = {}
        finally:
            conn.close()

    tally = scope_mod.counts()
    lines = [
        "MIGRATION SCOPE — what this engine moves",
        "",
        "  [+] FULL     high fidelity",
        "  [~] PARTIAL  migrated with a named fidelity loss",
        "  [-] NONE     not migrated by this engine",
        "",
    ]
    for svc in scope_mod.SERVICES:
        t = tally.get(svc, {})
        lines.append(f"  {svc:<10} full {t.get('FULL', 0):>2}   "
                     f"partial {t.get('PARTIAL', 0):>2}   none {t.get('NONE', 0):>2}")
    if volume.get("users"):
        lines += ["",
                  f"  Discovered volume: {volume['files']:,} files, "
                  f"{volume['folders']:,} folders, {volume['native']:,} native docs, "
                  f"{_human_bytes(volume['bytes'])}, {volume['messages']:,} messages, "
                  f"max depth {volume['max_depth']}"]
    else:
        lines += ["", "  Discovered volume: run 'discover' to populate."]
    lines += scope_mod.as_text(width=100)
    return {"lines": lines, "totals": tally, "volume": volume}


def _human_bytes(n: int) -> str:
    n = int(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


def available_job_logs() -> list[dict]:
    """Every per-job transcript on disk, newest first.

    These are where a launched run's own stdout and stderr land -- so a
    traceback that kills a migration is in one of these files and in no
    other. Until this listed them, the only way to read one was to log in
    to the box.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.join(here, "logs", "jobs")
    out: list[dict] = []
    for account in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        folder = os.path.join(root, account)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".log"):
                continue
            full = os.path.join(folder, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            out.append({"account": account, "job": name[: -len(".log")],
                        "bytes": st.st_size, "modified": st.st_mtime})
    out.sort(key=lambda r: r["modified"], reverse=True)
    return out


def logs_payload(job: str = "", account: str = "") -> dict:
    """Tail of the engine's own log file (what main.py writes)."""
    from config import Settings

    # A named job reads its own transcript instead of the shared engine log.
    # Resolved through job_log_path so there is exactly one place that knows
    # the layout -- a second copy of it is how two other bugs happened today.
    if job:
        acct = None
        if account and account != "_none":
            try:
                acct = int(account)
            except ValueError:
                acct = None
        path = job_log_path(acct, job)
    else:
        path = Settings().log_file
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return {"path": path, "lines": ["(no log file yet)"],
                "jobs": available_job_logs()}
    return {"path": path, "lines": lines[-600:],
            "jobs": available_job_logs()}


# ----------------------------------------------------------------------
# Groq "active log" — a live diagnostic panel for benchmarking and error
# reporting.
#
# The migration's own log tail answers "what happened", but reading 600
# lines of it to answer "is it going OK, and if not why" is exactly the
# chore an LLM is good at. This exposes a narrow seam for that: the key is
# stored in env.sh like every other setting (case-preserved — the API key
# is not a domain to lowercase), and the panel sends the current log tail
# plus the run's headline metrics to Groq and renders the summary back.
# Nothing here runs commands and nothing the browser sends is executed.
# ----------------------------------------------------------------------
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


def groq_api_key() -> str:
    from wizard import load_env

    return load_env(ENV_PATH).get("GROQ_API_KEY", "")


def save_groq_key(key: str) -> str:
    """Persist the Groq API key to env.sh, case-preserved."""
    key = (key or "").strip()
    if not key:
        return "Groq API key is required"
    write_config_raw({"GROQ_API_KEY": key})
    return ""


def _groq_analyze_log(tail: str, prompt: str, key: str) -> tuple[str, str]:
    """
    Send the log tail to Groq and return (markdown_summary, error).
    Uses urllib only — this file is deliberately stdlib-only.
    """
    url = "https://api.groq.com/openai/v1/chat/completions"
    system = (
        "You are a migration engineer's diagnostic assistant. The user runs a "
        "Google Workspace data migration. You are given the tail of its log "
        "and the run's headline metrics. Produce a concise, actionable "
        "summary in Markdown with three sections if any content matches: "
        "**Status** (one or two lines: healthy or not, and why), **Errors** "
        "(every distinct FAILED/WARNING line, deduplicated, with the count "
        "and the one-line cause), and **Benchmark** (rates, latencies, "
        "progress). If there is nothing notable, say so in one line. Do not "
        "invent failures. Quote the actual log lines when you cite something."
    )
    body = json.dumps({
        "model": GROQ_MODEL,
        "temperature": 0,
        "max_tokens": 700,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"{prompt}\n\n---- LOG TAIL ----\n{tail}"},
        ],
    }).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        return "", f"Groq API error {exc.code}: {detail or exc.reason}"
    except Exception as exc:  # noqa: BLE001 - network/urllib failures
        return "", f"could not reach Groq: {exc}"
    try:
        return data["choices"][0]["message"]["content"], ""
    except (KeyError, IndexError, TypeError):
        return "", f"unexpected Groq response: {str(data)[:200]}"


def _groq_run_summary() -> str:
    """The headline numbers the panel always sends alongside the log tail."""
    parts = []
    try:
        payload = snapshot_payload()
        t = (payload.get("snapshot") or {}).get("totals") or {}
        parts.append(
            f"- Progress: {t.get('items_done', 0)}/{t.get('items_expected', '?')} "
            f"items, {t.get('users_done', 0)}/{t.get('users', 0)} users, "
            f"{t.get('items_failed', 0)} failed, {t.get('bytes_moved', 0)} bytes moved"
        )
    except Exception:  # noqa: BLE001 - a metrics hiccup must not kill the panel
        pass
    try:
        import metrics

        s = metrics.METRICS.snapshot()
        if s.get("calls"):
            parts.append(
                f"- API: {s['calls']:,} calls, {s['requests_per_sec']:.2f} req/s "
                f"across {s['workers']} workers, p50 {s['p50'] * 1000:.0f}ms "
                f"p95 {s['p95'] * 1000:.0f}ms p99 {s['p99'] * 1000:.0f}ms, "
                f"{s['retries']:,} retries, {s['failures']:,} failures"
            )
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(parts)



def set_toggles(body: dict) -> dict:
    """Apply the launch toggles sent by the toolbar."""
    dry = body.get("dry_run")
    if dry is not None:
        _RUN_STATE["dry_run"] = bool(dry)
    svcs = body.get("services")
    if isinstance(svcs, dict):
        for k in _RUN_STATE["services"]:
            if k in svcs:
                _RUN_STATE["services"][k] = bool(svcs[k])
    return {"ok": True, "toggles": dict(_RUN_STATE)}


def _account_env(account_id: int | None, base: dict | None = None) -> dict:
    """Point a child process at one account's tenants and ledger.

    Every ACTIONS button ran with plain gcloud_env(), which is env.sh --
    so a signed-in tenant pressing "Check seed accounts" got a report about
    c.example.com, the placeholder domain, and five users named alice
    through erin that have never existed. Confirmed live from the UI:

        Checking 5 account(s) in c.example.com as admin@c.example.com ...
          MISS alice@c.example.com -> invalid_grant

    while that account's own source tenant was source.rohitrokaya.com.np
    with 201 users. Everything the buttons did -- discover, provision,
    migrate, verify, report -- was aimed at the same wrong place.

    Both sides, because these actions span them: migrate reads source and
    writes target, and a half-overlaid environment is how you migrate one
    tenant's users into another tenant's placeholder.

    NOTE: this sets MIGRATION_DB, so a child pointed here must NOT also be
    passed --account-id -- control_plane_db._db_path() is Settings().db_path
    and would follow it into the per-account ledger looking for
    tenant_configs. See wipe_target_argv.
    """
    env = dict(base) if base is not None else gcloud_env()
    if account_id is None:
        return env
    from config import Settings

    st = Settings(account_id=account_id)
    env.update(SOURCE_DOMAIN=st.source_domain, TARGET_DOMAIN=st.target_domain,
               SOURCE_ADMIN=st.source_admin, TARGET_ADMIN=st.target_admin,
               SOURCE_SA_KEY=st.source_sa_key, TARGET_SA_KEY=st.target_sa_key,
               MIGRATION_DB=st.db_path)
    return env


DWD_ENV_FILE = os.getenv("BITPORT_DWD_ENV", "/etc/bitport/dwd.env")


def _dms_env(account_id: int | None) -> dict:
    """Environment for the DMS browser run: the account's tenants plus a
    headless display and the admin login the Admin console requires.

    The super-admin password is not an API credential Bitport otherwise
    holds, so it lives in a root-only file (mode 600, outside the checkout)
    read only here, never passed on argv where `ps` could see it. DWD_EMAIL
    defaults to the account's target admin so only the secret needs storing.
    """
    env = _account_env(account_id)
    env.setdefault("DISPLAY", os.getenv("BITPORT_XVFB_DISPLAY", ":99"))
    env.setdefault("DWD_EMAIL", env.get("TARGET_ADMIN", ""))
    try:
        with open(DWD_ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


def _phase_argv(base: list) -> list:
    """The phased run, limited to the services actually toggled on.

    phases.py takes --phase, so the toggles can drive it the same way they
    drive migrate. Without this the full-scope button always ran Gmail, and
    there was no way to say "everything except mail" from the UI -- which is
    exactly what you need when Google's own Data Import tool owns the
    mailboxes and the engine should not touch them.

    The phases are always named explicitly. They used to be gated
    indirectly through MIGRATE_CHAT/CONTACTS/TASKS in the environment, which
    meant the argv in the job log claimed to run every phase while the env
    quietly dropped three of them. Naming them makes the log match the run.

    An empty selection falls back to the unfiltered argv rather than
    silently migrating nothing.
    """
    on = [k for k in PHASE_ORDER if _RUN_STATE["services"].get(k)]
    if not on:
        return list(base)
    argv = list(base)
    for k in on:
        argv += ["--phase", k]
        if k == "drive":
            # shared drives are Drive data with no owning user, so they ride
            # with Drive rather than getting a checkbox of their own.
            argv += ["--phase", "shared_drives"]
    return argv


def _action_argv(name: str) -> list:
    """Fixed argv for most actions; migrate/delta follow the launch toggles
    (dry-run + selected services), exactly like the TUI's m/x keys."""
    spec = ACTIONS[name]
    if name in ("phased_migrate", "phased_count_only"):
        return _phase_argv(spec["argv"])
    if name not in _LAUNCH_KEYS:
        return list(spec["argv"])
    chosen = [k for k, v in _RUN_STATE["services"].items() if v]
    argv = [PY, "main.py", "migrate" if name == "migrate" else "delta"]
    if chosen:
        argv += ["--services", ",".join(chosen)]
    if name == "delta":
        argv += ["--days", "2"]
    if _RUN_STATE["dry_run"]:
        argv.insert(2, "--dry-run")
    return argv


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # This UI drives a migration; nothing about it should be cached.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _on_screen(self) -> int | None:
        """The account every read on this page is about.

        One resolver rather than one per route: /api/status and
        /api/spa/users render on the same screen -- Mission Control's
        header came from one and its "11 users tracked" from the other,
        and they named different tenants.
        """
        return account_context.in_context(*self._caller())

    def _caller(self) -> tuple[int | None, bool]:
        """(account, is_superadmin) -- what account_context.in_context needs.

        Resolved from the same cookie as _account_id, plus the one extra
        fact that decides whether a nav click means "my migration" or "the
        migration that is running".
        """
        aid = self._account_id()
        if aid is None:
            return None, False
        try:
            account = accounts_auth.get_account(aid)
        except sqlite3.Error as exc:
            # The caller is still known; only the elevation is unclear, and
            # the safe reading of an unclear elevation is "not elevated".
            log.warning("cannot read account %s: %r", aid, exc)
            return aid, False
        return aid, bool(account and account.get("is_superadmin"))

    def _account_id(self) -> int | None:
        """None for the legacy path (no cookie, or one that doesn't resolve
        -- same handling api_server.py's operator() dependency gives an
        absent/invalid bp_session). Stdlib http.cookies rather than a hand
        rolled split on ';', for the same reason api_server.py leans on
        FastAPI's Cookie() instead of parsing the header itself: cookie
        syntax has enough edge cases (quoting, extra attributes) that a
        one-line split silently mis-parses some of them."""
        import http.cookies

        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            return None
        morsel = jar.get("bp_session")
        if morsel is None:
            return None
        return accounts_auth.resolve_session(morsel.value)

    _ASSET_CTYPES = {
        ".js": "application/javascript", ".css": "text/css",
        ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
        ".json": "application/json", ".woff2": "font/woff2",
    }

    def _serve_spa_index(self) -> None:
        index = os.path.join(SPA_DIST_DIR, "index.html")
        if not os.path.isfile(index):
            self._send(404, b"migration-webui not built -- run "
                             b"'npm run build' in migration-webui/ first",
                       "text/plain; charset=utf-8")
            return
        with open(index, "rb") as f:
            self._send(200, f.read(), "text/html; charset=utf-8")

    def _serve_spa_asset(self, path: str) -> None:
        # path looks like "/app/assets/index-XXXX.js" -- strip the "/app"
        # prefix the SPA is mounted under, then resolve strictly inside
        # SPA_DIST_DIR so ".." can never escape it onto the rest of the
        # filesystem this process can read (it holds tenant credentials).
        rel = path[len("/app/"):]
        target = os.path.normpath(os.path.join(SPA_DIST_DIR, rel))
        if os.path.commonpath([target, SPA_DIST_DIR]) != SPA_DIST_DIR or not os.path.isfile(target):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ext = os.path.splitext(target)[1]
        ctype = self._ASSET_CTYPES.get(ext, "application/octet-stream")
        with open(target, "rb") as f:
            self._send(200, f.read(), ctype)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        query = urllib.parse.parse_qs(self.path.partition("?")[2])
        if path == "/":
            # Bitport (the SPA at /app) is the only UI now. This used to
            # 302 here and also serve an inline dashboard at /legacy from
            # before accounts, login, and the SaaS pivot existed -- Quick
            # Setup + Seed Wizard in the SPA cover that flow now, and the
            # legacy dashboard (PAGE) has been deleted outright rather than
            # kept around as a fallback nobody was using.
            self.send_response(302)
            self.send_header("Location", "/app")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/api/status":
            self._json(status_payload(self._on_screen()))
        elif path == "/api/actions":
            self._json({k: {"label": v["label"], "blurb": v["blurb"],
                            "destructive": v.get("destructive", False),
                            "confirm": v.get("confirm", "")}
                        for k, v in ACTIONS.items()})
        elif path == "/api/snapshot":
            self._json(_cached_payload("snapshot_payload", snapshot_payload, self._on_screen()))
        elif path == "/api/spa/users":
            self._json(_cached_payload("spa_users_payload", spa_users_payload, self._on_screen()))
        elif path == "/api/spa/activity":
            self._json(spa_activity_payload(self._on_screen()))
        elif path == "/api/spa/metrics":
            self._json(_cached_payload("spa_metrics_payload", spa_metrics_payload, self._on_screen()))
        elif path == "/api/spa/stages":
            self._json(_cached_payload("spa_stages_payload", spa_stages_payload, self._on_screen()))
        elif path == "/api/spa/verification":
            self._json(_cached_payload("spa_verification_payload", spa_verification_payload, self._on_screen()))
        elif path == "/api/spa/report":
            self._json(_cached_payload("spa_report_payload", spa_report_payload, self._on_screen()))
        elif path == "/api/toggles":
            # Readable, not only settable. set_toggles returns the state,
            # but a UI that can only POST has to mutate something to find
            # out what it is -- so the switches rendered from whatever the
            # page happened to assume rather than from the server.
            self._json({"ok": True, "toggles": dict(_RUN_STATE)})
        elif path == "/api/scope":
            self._json(scope_payload(self._on_screen()))
        elif path == "/api/logs":
            self._json(logs_payload(query.get("job", [""])[0],
                                    query.get("account", [""])[0]))
        elif path == "/api/groq":
            # The panel needs to know whether a key is saved (masked) so it
            # can prompt to enter one, without ever round-tripping the real
            # key back to the browser.
            key = groq_api_key()
            self._json({
                "configured": bool(key),
                "key": (key[:4] + "\u2022" * 12) if key else "",
                "model": GROQ_MODEL,
            })
        elif path == "/api/oauth/status":
            self._json(oauth_status())
        elif path == "/api/dwd":
            self._json(dwd_payload())
        elif path == "/api/identities":
            self._json(identities_payload())
        elif path == "/api/config":
            from config import Settings as _S
            self._json({"config": read_config(), "env_path": ENV_PATH,
                        "uploads": uploads_status(),
                        "auth_modes": AUTH_MODES,
                        "auth_mode": _S().auth_mode,
                        "run_mode": _S().run_mode,
                        "run_modes": _RUN_MODES(),
                        "seed_scales": list(SEED_SCALES),
                        "deploy": read_deploy_config(),
                        "host": host_info()})
        elif path == "/oauth/callback":
            # Google redirects the admin's browser back here after consent.
            tenant = "source" if "source" in (self.headers.get("Referer") or "") else None
            tenant = tenant or _PENDING.get("tenant", "source")
            full = f"http://localhost:{self.server.server_address[1]}{self.path}"
            res = oauth_finish(tenant, full)
            msg = (f"<h2>{'Connected' if res.get('ok') else 'Could not connect'}</h2>"
                   f"<p>{html.escape(res.get('account') or res.get('error',''))}</p>"
                   f"<p>You can close this tab and return to the migration UI.</p>")
            self._send(200, f"<html><body style='font:15px sans-serif;padding:40px'>"
                            f"{msg}</body></html>".encode(), "text/html; charset=utf-8")
        elif path == "/api/job":
            since = 0
            if "since=" in self.path:
                try:
                    since = int(self.path.split("since=")[1].split("&")[0])
                except ValueError:
                    since = 0
            # An operator can start a job on another account's tenant (the
            # wipe and reset buttons take an account id), so they have to be
            # able to watch it -- otherwise the UI launches work it cannot
            # then show, and the only way to see the output is the box.
            watching, scope_err = resolve_target_account(
                self._account_id(), query.get("account", [""])[0] or None)
            if scope_err:
                self._json({"error": scope_err}, 403)
                return
            self._json(_job_snapshot(watching, since))
        elif path == "/api/dms_status":
            # The DMS import runs on its own Job (parallel to the migration),
            # so it has its own status feed rather than /api/job's.
            since = 0
            if "since=" in self.path:
                try:
                    since = int(self.path.split("since=")[1].split("&")[0])
                except ValueError:
                    since = 0
            self._json(get_parallel_job(self._account_id(),
                                        "dms_import").snapshot(since))
        elif path == "/api/dms_metrics":
            # DMS counters live only in Google's console, so they are scraped
            # into a cache file (dms_migrate --status) and served from there;
            # a refresh is a separate browser job the UI can trigger.
            self._json(_dms_metrics_payload())
        elif path == "/api/job_history":
            # The last COMPLETED run of a given job name, read back from
            # disk -- covers exactly the gap /api/job can't: a browser tab
            # opened (or refreshed) after the server itself already
            # restarted, where nothing is running and nothing is left in
            # memory to report. See Job._save_result().
            name = ""
            if "name=" in self.path:
                name = urllib.parse.unquote(self.path.split("name=")[1].split("&")[0])
            result = load_job_result(self._account_id(), name) if name else None
            self._json({"result": result})
        elif path == "/api/deploy_history":
            self._json({"history": load_deploy_history()})
        elif path.startswith("/app/assets/"):
            self._serve_spa_asset(path)
        elif path == "/app" or path == "/app/" or path.startswith("/app/"):
            # Client-side routes (e.g. /app/mission-control) all resolve to
            # the same index.html -- the SPA's own router takes it from
            # there, same as any other client-routed single-page app.
            self._serve_spa_index()
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2 * MAX_UPLOAD:
            self._json({"ok": False, "error": "request body too large"}, 413)
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"ok": False, "error": "bad json"}, 400)
            return

        if self.path == "/api/oauth/begin":
            tenant = body.get("tenant", "")
            if tenant not in ("source", "target"):
                self._json({"ok": False, "error": "tenant must be source or target"}, 400)
                return
            _PENDING["tenant"] = tenant
            self._json(oauth_begin(tenant, self.server.server_address[1]))
            return

        if self.path == "/api/oauth/disconnect":
            from config import Settings
            import oauth_store

            tenant = body.get("tenant", "")
            if tenant not in ("source", "target"):
                self._json({"ok": False, "error": "tenant must be source or target"}, 400)
                return
            oauth_store.TokenStore(Settings().oauth_token_dir).clear(tenant)
            # Deliberately worded: the local token is gone, but the grant still
            # exists at Google until an admin revokes it in their own console.
            self._json({"ok": True,
                        "msg": f"{tenant} token removed locally; access is still "
                               f"granted at Google until revoked in the admin console"})
            return

        if self.path == "/api/seed":
            account_id = self._account_id()
            if not _subscription_ok(account_id):
                self._json({"ok": False, "error": "subscription inactive"}, 402)
                return
            if not _seed_ok(account_id):
                self._json({"ok": False, "error": "seeding is not enabled on this account"}, 403)
                return
            argv, env, err = seed_argv(body, account_id)
            if err:
                self._json({"ok": False, "error": err}, 400)
                return
            admitted, admit_msg = job_admission.try_admit(account_id, "seed")
            if not admitted:
                self._json({"ok": False, "error": admit_msg}, 503)
                return
            ok, msg = get_job(account_id).start(
                "seed", argv, env=env,
                cwd=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "data-generator"),
                on_finish=lambda rc: job_admission.release(account_id, "seed"))
            if not ok:
                # Job.start() refused (e.g. already running) before ever
                # spawning a process -- _drain() (and so on_finish) never
                # runs, so nothing else will free the slot just reserved.
                job_admission.release(account_id, "seed")
            self._json({"ok": ok, "error": "" if ok else msg})
            return

        if self.path == "/api/reset_target":
            # Same targeting as reset_drive_ledger: an operator cleaning up
            # somebody else's tenant is the normal case here, and resolving
            # from the session alone silently aims at their own empty one.
            account_id, scope_err = resolve_target_account(
                self._account_id(), body.get("account_id"))
            if scope_err:
                self._json({"ok": False, "error": scope_err}, 403)
                return
            if not _subscription_ok(account_id):
                self._json({"ok": False, "error": "subscription inactive"}, 402)
                return
            argv, env, err = reset_target_argv(body, account_id)
            if err:
                self._json({"ok": False, "error": err}, 400)
                return
            admitted, admit_msg = job_admission.try_admit(account_id, "reset target")
            if not admitted:
                self._json({"ok": False, "error": admit_msg}, 503)
                return
            # reset_target.py lives at the repo root, unlike the seeder.
            ok, msg = get_job(account_id).start(
                "reset target", argv, env=env,
                on_finish=lambda rc: job_admission.release(account_id, "reset target"))
            if not ok:
                job_admission.release(account_id, "reset target")
            self._json({"ok": ok, "error": "" if ok else msg})
            return

        if self.path == "/api/wipe_source":
            account_id, scope_err = resolve_target_account(
                self._account_id(), body.get("account_id"))
            if scope_err:
                self._json({"ok": False, "error": scope_err}, 403)
                return
            if not _subscription_ok(account_id):
                self._json({"ok": False, "error": "subscription inactive"}, 402)
                return
            argv, env, err = wipe_source_argv(body, account_id)
            if err:
                self._json({"ok": False, "error": err}, 400)
                return
            admitted, admit_msg = job_admission.try_admit(account_id, "wipe source")
            if not admitted:
                self._json({"ok": False, "error": admit_msg}, 503)
                return
            ok, msg = get_job(account_id).start(
                "wipe source", argv, env=env,
                on_finish=lambda rc: job_admission.release(account_id, "wipe source"))
            if not ok:
                job_admission.release(account_id, "wipe source")
            self._json({"ok": ok, "error": "" if ok else msg})
            return

        if self.path == "/api/wipe_target":
            # Same targeting as reset_drive_ledger: an operator cleaning up
            # somebody else's tenant is the normal case here, and resolving
            # from the session alone silently aims at their own empty one.
            account_id, scope_err = resolve_target_account(
                self._account_id(), body.get("account_id"))
            if scope_err:
                self._json({"ok": False, "error": scope_err}, 403)
                return
            if not _subscription_ok(account_id):
                self._json({"ok": False, "error": "subscription inactive"}, 402)
                return
            argv, env, err = wipe_target_argv(body, account_id)
            if err:
                self._json({"ok": False, "error": err}, 400)
                return
            admitted, admit_msg = job_admission.try_admit(account_id, "wipe target")
            if not admitted:
                self._json({"ok": False, "error": admit_msg}, 503)
                return
            ok, msg = get_job(account_id).start(
                "wipe target", argv, env=env,
                on_finish=lambda rc: job_admission.release(account_id, "wipe target"))
            if not ok:
                job_admission.release(account_id, "wipe target")
            self._json({"ok": ok, "error": "" if ok else msg})
            return

        if self.path == "/api/identities/save":
            self._json(save_identity_pair(
                body.get("source_email", ""), body.get("target_email", "")))
            return

        if self.path == "/api/reset_drive_ledger":
            account_id, scope_err = resolve_target_account(
                self._account_id(), body.get("account_id"))
            if scope_err:
                self._json({"ok": False, "error": scope_err}, 403)
                return
            if not _subscription_ok(account_id):
                self._json({"ok": False, "error": "subscription inactive"}, 402)
                return
            argv, env, err = reset_drive_ledger_argv(body, account_id)
            if err:
                self._json({"ok": False, "error": err}, 400)
                return
            admitted, admit_msg = job_admission.try_admit(account_id, "reset drive ledger")
            if not admitted:
                self._json({"ok": False, "error": admit_msg}, 503)
                return
            ok, msg = get_job(account_id).start(
                "reset drive ledger", argv, env=env,
                on_finish=lambda rc: job_admission.release(account_id, "reset drive ledger"))
            if not ok:
                job_admission.release(account_id, "reset drive ledger")
            self._json({"ok": ok, "error": "" if ok else msg})
            return

        if self.path == "/api/check":
            self._json(live_check(body.get("kind", "")))
            return

        if self.path == "/api/check_dwd":
            invalidate_status()
            _refresh_snapshot()
            with _snap_lock:
                entry = _snaps.get(None)
                data = (entry or {}).get("data") or {}
            self._json({"ok": True, "status": data})
            return

        if self.path == "/api/scope_diagnosis":
            tenant = (body.get("tenant") or "").strip().lower()
            if tenant not in ("source", "target"):
                self._json({"ok": False, "error": "tenant must be 'source' or 'target'"}, 400)
                return
            self._json({"ok": True, "diagnosis": scope_diagnosis(tenant)})
            return

        if self.path == "/api/checkstep":
            try:
                n = int(body.get("step", 0))
            except (TypeError, ValueError):
                self._json({"ok": False, "error": "step must be a number"}, 400)
                return
            self._json(check_step(n))
            return

        if self.path == "/api/runmode":
            self._json(set_run_mode(body.get("mode", "")))
            return

        if self.path == "/api/toggles":
            self._json(set_toggles(body))
            return

        if self.path == "/api/dwd/automate":
            tenant = body.get("tenant", "")
            if tenant not in ("source", "target"):
                self._json({"ok": False,
                            "error": "tenant must be source or target"}, 400)
                return
            from config import Settings
            import dwd_helper

            st = Settings()
            base = os.path.dirname(os.path.abspath(__file__))
            key = st.source_sa_key if tenant == "source" else st.target_sa_key
            client_id = ""
            try:
                with open(key, encoding="utf-8") as fh:
                    client_id = json.load(fh).get("client_id", "")
            except Exception:  # noqa: BLE001 - absent key is an early state
                pass
            scopes = ""
            try:
                data = dwd_helper._load_payload(tenant)
                scopes = data.get("scopes", "")
            except Exception:  # noqa: BLE001 - never break the panel
                pass
            if not client_id:
                self._json({"ok": False,
                            "error": f"upload the {tenant} service-account "
                                     f"key first (no client ID yet)"}, 400)
                return
            cmd = (f"cd {base} && "
                   f"{sys.executable} dwd_helper.py --client-id {client_id} "
                   f"--scopes {scopes!r}")
            # This server is headless (VPS), so it cannot open the browser
            # itself: hand the operator the exact command to run on a machine
            # with a display. --headful is the default inside the helper.
            self._json({"ok": True, "command": cmd, "client_id": client_id,
                        "scopes": scopes, "tenant": tenant,
                        "note": "run this on a machine with a browser"})
            return

        if self.path == "/api/authmode":
            self._json(set_auth_mode(body.get("mode", "")))
            return

        if self.path == "/api/upload":
            res = upload_credential(body.get("kind", ""), body.get("content", ""))
            self._json(res, 200 if res.get("ok") else 400)
            return

        if self.path == "/api/config":
            clean, err = validate_config(body)
            if err:
                self._json({"ok": False, "error": err}, 400)
                return
            write_config(clean)
            self._json({"ok": True, "msg": f"saved to {ENV_PATH}",
                        "config": read_config()})
            return

        if self.path == "/api/groq":
            # Save the Groq API key (case-preserved -- it is not a domain) or
            # clear it with an empty string.
            if "key" in body:
                err = save_groq_key(body.get("key", ""))
                if err:
                    self._json({"ok": False, "error": err}, 400)
                    return
                self._json({"ok": True,
                            "msg": "saved to " + ENV_PATH,
                            "configured": bool(groq_api_key())})
                return
            self._json({"ok": False, "error": "no key field"}, 400)
            return

        if self.path == "/api/groq_log":
            # Ask Groq to summarise the current log tail + headline metrics.
            key = groq_api_key()
            if not key:
                self._json({"ok": False,
                            "error": "no Groq API key — add one in the Logs panel"},
                           400)
                return
            prompt = (body.get("prompt") or "").strip()[:2000]
            if not prompt:
                prompt = ("Summarise this migration's current state for "
                          "benchmarking and error reporting.")
            tail = "\n".join(logs_payload()["lines"][-500:])
            summary = _groq_run_summary()
            text, err = _groq_analyze_log(
                f"{summary}\n\n{prompt}" if summary else prompt, prompt, key)
            if err:
                self._json({"ok": False, "error": err}, 502)
                return
            self._json({"ok": True, "text": text})
            return

        if self.path == "/api/setup":
            cfg = read_config()
            missing = [k for k, v in cfg.items() if not v]
            if missing:
                self._json({"ok": False,
                            "error": "save the domains and admins first (missing: "
                                     + ", ".join(missing) + ")"}, 400)
                return
            argv = ["bash", "setup.sh",
                    "--source-domain", cfg["source_domain"],
                    "--target-domain", cfg["target_domain"],
                    "--source-admin", cfg["source_admin"],
                    "--target-admin", cfg["target_admin"]]
            if body.get("keyless"):
                argv.append("--keyless")
            ok, msg = JOB.start("setup", argv, env=gcloud_env())
            self._json({"ok": ok, "error": "" if ok else msg})
            return

        if self.path == "/api/deploy_config":
            # Save-only: lets the VPS connection be entered once, from either
            # UI, before a Deploy is ever run -- see read_deploy_config().
            clean, err = validate_deploy_config(body)
            if err:
                self._json({"ok": False, "error": err}, 400)
                return
            write_config_raw(clean)
            self._json({"ok": True, "msg": f"saved to {ENV_PATH}"})
            return

        if self.path == "/api/deploy":
            clean, err = validate_deploy_config(body)
            if err:
                self._json({"ok": False, "error": err}, 400)
                return
            host, user = clean["DEPLOY_HOST"], clean["DEPLOY_USER"]
            port, ui_port = clean["DEPLOY_PORT"], clean["DEPLOY_UI_PORT"]
            key = clean["DEPLOY_KEY"]
            # Copying credentials to another machine is outward-facing and not
            # undoable, so it takes the same typed confirmation as a migration.
            creds = bool(body.get("include_credentials"))
            if creds and body.get("confirm") != "DEPLOY":
                self._json({"ok": False,
                            "error": "sending credentials to a host needs the "
                                     "confirmation phrase DEPLOY"}, 400)
                return
            # Remembered for next time regardless of how this run turns out --
            # a failed deploy (bad password prompt, network blip) still means
            # the operator typed a real host worth keeping.
            write_config_raw(clean)
            argv = [PY, "deploy_remote.py", "--host", host, "--user", user,
                    "--port", port, "--ui-port", ui_port]
            if key:
                argv += ["--key", key]
            if creds:
                argv.append("--include-credentials")
            rec_id = record_deploy_start(host, user, port, ui_port, creds,
                                         host_info().get("commit", ""))
            ok, msg = JOB.start("deploy", argv,
                                on_finish=lambda rc: record_deploy_finish(rec_id, rc))
            if not ok:
                # Never started -- e.g. another job already running -- so
                # there is no process to ever call on_finish. Recording it
                # as its own immediate failure keeps the history honest
                # instead of leaving a permanently "in progress" ghost.
                record_deploy_finish(rec_id, None)
            self._json({"ok": ok, "error": "" if ok else msg})
            return

        if self.path == "/api/stop":
            # The account whose job this is. /api/run starts jobs under
            # get_job(account_id); stopping the global JOB instead meant a
            # tenant could start work it could not then stop -- pressed
            # live, Stop reported success and the run carried on.
            account_id, scope_err = resolve_target_account(
                self._account_id(), body.get("account_id"))
            if scope_err:
                self._json({"ok": False, "msg": scope_err}, 403)
                return
            force = bool(body.get("force"))
            job = get_job(account_id)
            if job.running:
                self._json({"ok": True, "msg": job.stop(force)})
            else:
                jobs = _external_processes()
                if not jobs:
                    self._json({"ok": True, "msg": "nothing running"})
                else:
                    sent = []
                    for j in jobs:
                        try:
                            # Same cooperative SIGINT the webui's own Stop uses:
                            # the engine finishes in-flight items, then resumes.
                            os.kill(j["pid"], signal.SIGKILL if force
                                    else signal.SIGINT)
                            sent.append(str(j["pid"]))
                        except (ProcessLookupError, PermissionError):
                            pass
                    verb = "kill" if force else "interrupt"
                    msg = (f"{verb} sent to {len(sent)} external "
                           f"process(es): {', '.join(sent)}") if sent \
                        else "external process(es) already gone"
                    self._json({"ok": True, "msg": msg})
            return

        if self.path != "/api/run":
            self._json({"ok": False, "error": "not found"}, 404)
            return

        name = body.get("action", "")
        spec = ACTIONS.get(name)
        if not spec:
            # The whitelist is the security boundary: an unknown name is
            # simply not runnable, whatever it contains.
            self._json({"ok": False, "error": f"unknown action {name!r}"}, 400)
            return
        if spec.get("destructive") and body.get("confirm") != spec.get("confirm"):
            self._json({"ok": False,
                        "error": f"{spec['label']} needs confirmation"}, 400)
            return

        # Which tenant this action is about. Without it every button ran
        # against env.sh -- see _account_env.
        account_id, scope_err = resolve_target_account(
            self._account_id(), body.get("account_id"))
        if scope_err:
            self._json({"ok": False, "error": scope_err}, 403)
            return
        if not _subscription_ok(account_id):
            self._json({"ok": False, "error": "subscription inactive"}, 402)
            return

        # count-only verifies; phased_migrate performs. Only the verifier
        # widens itself from the ledger -- a migration must still do exactly
        # what its checkboxes say, or a toggle would mean nothing.
        # The DMS import only drives a browser to press Start; Google does the
        # copying server-side, so it neither needs env.sh's service accounts
        # nor the one-heavy-job slot -- it runs in parallel with the engine
        # migration, which is the point of handing mail to Google.
        if spec.get("browser"):
            env = _dms_env(account_id)
        elif name in _PHASE_GATED_ACTIONS:
            env = _account_env(account_id,
                               _service_env(account_id,
                                            from_ledger=(name == "phased_count_only")))
        else:
            env = _account_env(account_id, gcloud_env())

        if spec.get("parallel"):
            # A dedicated Job instance so it does not collide with the
            # account's primary migration Job (one process each). /api/job
            # keeps showing the migration; this job has its own status feed.
            job = get_parallel_job(account_id, name)
            ok, msg = job.start(spec["label"], _action_argv(name), env=env)
            self._json({"ok": ok, "error": None if ok else msg})
            return

        admitted, admit_msg = job_admission.try_admit(account_id, spec["label"])
        if not admitted:
            self._json({"ok": False, "error": admit_msg}, 503)
            return
        ok, msg = get_job(account_id).start(
            spec["label"], _action_argv(name), env=env,
            on_finish=lambda rc: job_admission.release(account_id, spec["label"]))
        if not ok:
            job_admission.release(account_id, spec["label"])
        self._json({"ok": ok, "error": None if ok else msg})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Local web UI for the migration.")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1",
                    help="leave this alone unless you know exactly why")
    args = ap.parse_args(argv)

    # Load env.sh before serving anything.
    #
    # Settings reads os.environ at construction. Previously the environment
    # only got populated as a side effect of the first status poll (State()
    # does it), so any request arriving before that -- a credential test, a
    # seed -- saw defaults instead of the configured domains and admins, and
    # reported the admin address as unset while it sat in env.sh all along.
    # Configuration must not depend on which endpoint happened to be hit first.
    try:
        from wizard import load_env

        loaded = load_env(ENV_PATH)
        for key, value in loaded.items():
            os.environ.setdefault(key, value)
        if loaded:
            print(f"loaded {len(loaded)} setting(s) from {ENV_PATH}")
    except Exception as exc:  # noqa: BLE001 - the UI should still start
        print(f"could not read {ENV_PATH}: {exc}")

    _reconcile_active_jobs()

    if args.host not in ("127.0.0.1", "localhost"):
        print("\n*** WARNING ***")
        print(f"Binding to {args.host} exposes command execution on this host to")
        print("anyone who can reach that address. There is no authentication here")
        print("by design -- the loopback bind IS the authentication. Use an SSH")
        print("tunnel instead:\n")
        print(f"    ssh -L {args.port}:localhost:{args.port} root@this-host\n")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Migration UI on http://{args.host}:{args.port}")
    print(f"\nFrom your laptop:\n    ssh -L {args.port}:localhost:{args.port} "
          f"root@<this-host>\nthen open http://localhost:{args.port}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
