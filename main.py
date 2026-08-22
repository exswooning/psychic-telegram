"""
main.py
=======
Module 5 orchestration + CLI entry point.

Concurrency model
-----------------
Parallelism is applied **across users**, not within a user. This is deliberate:
Google's binding quotas (Drive queries/min/user, Gmail units/sec/user, and the
750 GB/day upload cap) are all *per-user*. Ten threads on one mailbox spend
most of their life in exponential backoff; ten threads on ten mailboxes run at
close to ten times the throughput.

Each worker thread owns:
  * its own `googleapiclient` service objects (httplib2 is not thread-safe),
  * its own `sqlite3` connection (handed out by `MigrationDB`),
  * its own token buckets and quota guard.

Failure isolation: one user blowing up must never abort the batch. Every
worker is wrapped, and per-user status lands in `identity_map.status` so the
next run resumes exactly where this one stopped.

Usage
-----
    python main.py init-db --identities users.csv
    python main.py preflight
    python main.py discover
    python main.py migrate --services drive,gmail,calendar
    python main.py delta   --services drive,gmail --days 2
    python main.py report
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from auth import AuthManager, list_domain_users
from calendar_engine import CalendarMigrator
from chat_engine import ChatMigrator
from contacts_engine import ContactsMigrator
import user_claims
from config import Settings
from db import MigrationDB
from discovery import print_report, scan_user
from drive_engine import DriveMigrator
from gmail_engine import GmailMigrator
from tasks_engine import TasksMigrator
from resilience import DailyQuotaGuard, QuotaExhausted
from resources import cached_probe, pressure_severe

log = logging.getLogger("migrate")

# Cooperative shutdown flag, flipped by SIGINT/SIGTERM.
SHUTDOWN = threading.Event()

# Memory watchdog's own pause flag. Distinct from SHUTDOWN so an exit caused by
# sustained memory pressure can be reported (and code-pathed) separately from
# an operator's Ctrl-C, which also sets SHUTDOWN.
MEMORY_PAUSE = threading.Event()

# Exit code for a run stopped by memory pressure. 0 is success, 1 is a reported
# error, 2 is argparse usage -- 3 is the first free slot.
EXIT_PAUSED = 3

# Watchdog cadence: poll a cached probe every 2 s and require 3 consecutive
# severe samples (~6 s) before draining, so one transient stall never pauses a
# migration. While draining, remind the operator every 5 minutes.
WATCHDOG_POLL_SEC = 2.0
WATCHDOG_SUSTAINED_SAMPLES = 3
MEMORY_REMINDER_SEC = 300.0


def setup_logging(settings: Settings) -> None:
    fmt = "%(asctime)s %(levelname)-7s [%(threadName)-14s] %(name)s: %(message)s"
    # stderr, not stdout: `scope --format json`/`--format markdown` and any
    # future machine-readable output must be pipeable without log noise mixed in.
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if settings.log_file:
        os.makedirs(os.path.dirname(os.path.abspath(settings.log_file)) or ".",
                    exist_ok=True)
        handlers.append(logging.FileHandler(settings.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format=fmt, handlers=handlers,
    )
    # The discovery-cache warning from googleapiclient is noise; silence it.
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
    logging.getLogger("googleapiclient.http").setLevel(logging.WARNING)


def _install_signal_handlers() -> None:
    def handler(signum, _frame):
        log.warning("signal %s received — finishing in-flight items then "
                    "stopping. Re-run to resume.", signum)
        SHUTDOWN.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, AttributeError):
            pass  # not in main thread / unsupported platform


# ======================================================================
# Per-user worker
# ======================================================================
# Google says "failedPrecondition ... Mail service not enabled" for an
# account that has no Workspace licence. Nothing in that sentence contains
# the word licence, so it reads like a service outage or a scope problem --
# and it is neither. Seen live on both tenants at once: 201 accounts, 200
# licences, one unlicensed account on each side. Two users failed with an
# HTTP 400 that named no cause anyone could act on.
_NO_MAILBOX = ("mail service not enabled", "failedprecondition")


# Which ledger item types prove a service actually did something.
_SERVICE_ITEMS = {
    "gmail": ("message", "draft"),
    "drive": ("file", "folder"),
    "calendar": ("event", "calendar"),
    "contacts": ("contact", "contact_group"),
    "tasks": ("task", "task_list"),
    "chat": ("space", "chat_message"),
}


def reconcile_service_markers(db) -> list[tuple]:
    """Re-open services that are marked done but migrated nothing and failed.

    A service marked done is skipped forever, so a bug that failed one
    outright made itself permanent -- the fix could never be applied because
    the user was never looked at again. That is fixed going forward
    (_services_that_succeeded), but ledgers written BEFORE the fix still
    carry the bad markers, and those users are stranded exactly as they were.

    This clears them: a service claiming completion with zero items of its
    own types and at least one failure recorded did not complete. Both
    conditions together, deliberately -- zero items alone is the ordinary
    state of a user with no tasks, and clearing that would re-check every
    empty mailbox on every run forever.

    Returns what it re-opened, so a run says it rather than silently
    behaving differently from the last one.
    """
    reopened: list[tuple] = []
    try:
        rows = [r for r in db.all_identities() if r["entity_type"] == "user"]
    except Exception:      # noqa: BLE001 - never block a migration
        return reopened

    for row in rows:
        user = row["source_email"]
        try:
            done = db.services_done(user)
            if not done:
                continue
            keep = set(done)
            for svc in done:
                types = _SERVICE_ITEMS.get(svc)
                if not types:
                    continue
                marks = db.conn.execute(
                    "SELECT COUNT(*) n FROM id_mapping WHERE source_user=? "
                    f"AND type IN ({','.join('?' * len(types))})",
                    (user, *types)).fetchone()["n"]
                if marks:
                    continue
                fails = db.conn.execute(
                    "SELECT COUNT(*) n FROM audit_log WHERE source_user=? "
                    "AND status='FAILED'", (user,)).fetchone()["n"]
                if fails:
                    keep.discard(svc)
                    reopened.append((user, svc))
            if keep != set(done):
                with db.write() as conn:
                    conn.execute(
                        "UPDATE identity_map SET services_done=? "
                        "WHERE source_email=?",
                        (",".join(sorted(keep)), user))
        except Exception:      # noqa: BLE001 - one bad row must not stop the run
            continue
    return reopened


def _services_that_succeeded(services: dict) -> list[str]:
    """Which services may be recorded as done for this user.

    A service is skipped on the next run once it is marked done, so marking
    one that failed outright makes the failure permanent: the fix can never
    be applied, because the user is never looked at again.

    Confirmed live and immediately: every contact of a canary user failed
    with "Fields with source ids are not allowed", contacts was marked done
    anyway, and re-running after fixing the bug reported "no users to
    process". The data was recoverable; the ledger said otherwise.

    A service that migrated SOMETHING and failed some items still counts as
    done -- the per-item ledger already skips what landed and retries what
    did not, so those users are not stranded. It is the all-or-nothing
    failure that has to stay un-marked.
    """
    done = []
    for name, stats in (services or {}).items():
        if not isinstance(stats, dict):
            done.append(name)
            continue
        failed = sum(v for k, v in stats.items()
                     if k.endswith("failed") and isinstance(v, int))
        moved = sum(v for k, v in stats.items()
                    if isinstance(v, int) and not k.endswith("failed"))
        if failed and not moved:
            continue
        done.append(name)
    return done


def is_blocked_externally(exc: Exception) -> bool:
    """Is this obstacle outside the tool's reach entirely?

    Distinguished from a failure because the two need opposite responses: a
    failure is investigated, a block is waited on. Conflating them means a
    list that says "2 failed" every run forever, about something no re-run
    can change -- and a failure count nobody trusts is a failure count
    nobody reads.
    """
    return all(k in str(exc).lower() for k in _NO_MAILBOX)


def explain_user_failure(exc: Exception, source_user: str,
                         target_user: str) -> str:
    """Turn an engine exception into something an operator can act on.

    Only rewrites what it recognises. Anything else keeps its original text
    verbatim -- a diagnosis layer that paraphrases errors it does not
    understand is worse than none, because it hides the one detail that
    would have identified them.
    """
    raw = str(exc)
    low = raw.lower()
    if all(k in low for k in _NO_MAILBOX):
        return (f"{raw}\n\nThis almost always means the account has no "
                f"Workspace licence, so Gmail does not exist for it -- the "
                f"error names no cause, which is why it reads like an "
                f"outage. Check {source_user} and {target_user} in the Admin "
                f"Console under Billing > Licences; assign one and re-run "
                f"this user. Nothing was migrated for them.")
    return raw


def migrate_user(auth: AuthManager, db: MigrationDB, settings: Settings,
                 source_user: str, target_user: str, services: set[str],
                 delta: bool, delta_days: int) -> dict:
    """
    Run the requested services for one user pair. Executed inside a worker
    thread; must never raise.
    """
    threading.current_thread().name = source_user.split("@")[0][:14]
    result: dict = {"source": source_user, "target": target_user, "services": {}}
    started = time.time()

    # A dry run must be a true no-op, including against the resume-tracking
    # state -- otherwise running --dry-run before the real migrate (exactly
    # the sequence this tool's own docs recommend) marks every user DONE and
    # the real run then skips all of them as "already done".
    track_status = not settings.dry_run
    if track_status:
        db.set_identity_status(source_user, "RUNNING")
    quota = DailyQuotaGuard(db, target_user, settings.effective_upload_cap())

    try:
        if "drive" in services and not SHUTDOWN.is_set():
            try:
                dm = DriveMigrator(auth, db, settings, source_user,
                                   target_user, quota)
                result["services"]["drive"] = dm.run(delta=delta)
            except QuotaExhausted as exc:
                log.warning("[%s] %s", source_user, exc)
                result["services"]["drive"] = {"status": "PAUSED_QUOTA",
                                               "detail": str(exc)}
                if track_status:
                    db.set_identity_status(source_user, "PAUSED_QUOTA", str(exc))
                return result

        if "gmail" in services and not SHUTDOWN.is_set():
            gm = GmailMigrator(auth, db, settings, source_user, target_user)
            result["services"]["gmail"] = gm.run(
                delta=delta, since_epoch_days=delta_days
            )

        if "calendar" in services and not SHUTDOWN.is_set():
            updated_min = None
            if delta:
                updated_min = (
                    datetime.now(timezone.utc) - timedelta(days=delta_days)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            cm = CalendarMigrator(auth, db, settings, source_user, target_user)
            result["services"]["calendar"] = cm.run(delta=delta,
                                                    updated_min=updated_min)

        # Chat last: it is the only pass that can leave a half-built artefact
        # (a space stuck in import mode), so it runs after everything that
        # cannot.
        if "chat" in services and settings.migrate_chat and not SHUTDOWN.is_set():
            cm = ChatMigrator(auth, db, settings, source_user, target_user)
            result["services"]["chat"] = cm.run()

        if ("contacts" in services and settings.migrate_contacts
                and not SHUTDOWN.is_set()):
            com = ContactsMigrator(auth, db, settings, source_user, target_user)
            result["services"]["contacts"] = com.run()

        if "tasks" in services and settings.migrate_tasks and not SHUTDOWN.is_set():
            tm = TasksMigrator(auth, db, settings, source_user, target_user)
            result["services"]["tasks"] = tm.run()

        status = "INTERRUPTED" if SHUTDOWN.is_set() else "DONE"
        if track_status:
            db.set_identity_status(source_user, status)
            if status == "DONE":
                db.mark_services_done(source_user,
                                      _services_that_succeeded(result["services"]))
        result["status"] = status

    except Exception as exc:  # noqa: BLE001 - worker must not propagate
        log.exception("[%s] user migration failed", source_user)
        detail = explain_user_failure(exc, source_user, target_user)
        # BLOCKED, not FAILED, when the obstacle is outside this tool.
        #
        # An account with no Workspace licence has no Gmail at all. No
        # retry, scope, quota or code change reaches it -- confirmed against
        # the live tenants, where the Licensing API answers HTTP 412 "There
        # aren't enough available licenses" because both hold 201 accounts
        # against 200 seats. Reporting that beside genuine errors trains
        # people to skim a failure list that is supposed to demand
        # attention.
        #
        # Still not DONE, so it retries the moment a seat is freed --
        # _already_done skips only DONE, and this state is deliberately not
        # that. It is "waiting on you", not "finished" and not "broken".
        blocked = is_blocked_externally(exc)
        status = "BLOCKED" if blocked else "FAILED"
        if track_status:
            db.set_identity_status(source_user, status, detail)
        db.log_audit(source_user, source_user, "user", status, detail)
        result["status"] = status
        result["error"] = detail

    result["elapsed_sec"] = round(time.time() - started, 1)
    log.info("[%s] finished in %.1fs: %s", source_user,
             result["elapsed_sec"], json.dumps(result.get("services", {})))
    return result


def _coordination_enabled() -> bool:
    """Is this run sharing a user list with other machines?

    Off unless a coordinator is configured, so every existing single-box
    install behaves exactly as before -- no claims table, no extra calls, no
    new way to fail. Turning it on is one environment variable.
    """
    return bool(user_claims.coordinator_url())


def _renew_until(stop: threading.Event, account_id, source_user: str) -> None:
    """Hold this node's lease for as long as the user is in flight.

    A user can take an hour; the lease is five minutes. Without this the
    claim would lapse mid-migration and the user would read as abandoned
    while it was actively being worked on.

    A renewal that returns False means the claim is no longer ours -- an
    operator forced it to another node -- and there is nothing useful this
    thread can do about it, so it stops renewing and lets the log say so.
    The migration itself is deliberately NOT interrupted: killing it in the
    middle would leave a half-migrated user, which is worse than finishing
    work whose ownership record has moved.
    """
    while not stop.wait(user_claims.RENEW_EVERY):
        try:
            if not user_claims.renew(account_id, source_user):
                log.warning("lease for %s is no longer held by this node; "
                            "another node may have been forced onto it",
                            source_user)
                return
        except Exception as exc:      # noqa: BLE001 - never kill the migration
            # A coordinator blip must not end a run that is otherwise fine.
            # The lease may lapse, which surfaces as a stale claim an
            # operator can see, rather than as a failed migration.
            log.warning("could not renew lease for %s: %s", source_user, exc)


def run_batch(auth: AuthManager, db: MigrationDB, settings: Settings,
              services: set[str], delta: bool, delta_days: int,
              only: list[str] | None = None) -> list[dict]:
    """Fan out across users with a bounded thread pool."""
    rows = [r for r in db.all_identities() if r["entity_type"] == "user"]

    def _already_done(r) -> bool:
        """
        Has this user finished *the services being asked for*?

        `status` is per-user, not per-service. A phased run that completed
        Drive marked every user DONE, so the Gmail phase that followed skipped
        all of them -- migrating nothing, recording nothing, and reporting a
        98.8% shortfall it could not explain. Restarts still need to be cheap,
        so the check is now per-service rather than removed.
        """
        if r["status"] != "DONE":
            return False
        done = db.services_done(r["source_email"])
        # A ledger written before services_done existed has an empty set; fall
        # back to the old behaviour rather than re-migrating everything.
        #
        # Say so, loudly. This fallback silently skipped all five users of a
        # Gmail/Calendar/Chat run against a ledger the Drive A/B had left
        # DONE, and the run reported a clean batch summary having migrated
        # nothing at all. Skipping on an assumption is defensible; skipping
        # without saying which assumption is not.
        if not done:
            log.warning(
                "%s is DONE in a ledger with no per-service record — assuming "
                "%s already ran and skipping. If they did not, run "
                "`backfill-services` or reset this user to PENDING.",
                r["source_email"], ",".join(sorted(services)))
            return True
        return set(services) <= done

    pairs = [
        (r["source_email"], r["target_email"])
        for r in rows
        if delta or not _already_done(r)
    ]

    if only:
        wanted = {u.lower() for u in only}
        pairs = [p for p in pairs if p[0] in wanted]

    if not pairs:
        log.warning("no users to process — check identity_map")
        return []

    _warn_if_ledger_is_stale(db, auth, pairs)

    log.info("dispatching %d users across %d workers (services=%s, delta=%s)",
             len(pairs), settings.user_workers, ",".join(sorted(services)), delta)

    results: list[dict] = []
    coordinated = _coordination_enabled()
    account_id = getattr(settings, "account_id", None)
    svc_label = ",".join(sorted(services))

    def _one(src: str, tgt: str) -> dict | None:
        """Migrate one user, holding its claim for the whole time.

        Returns None when another node owns the user -- as_completed then
        simply has nothing to add, which is the correct outcome: this node
        did not do that work and must not report on it.
        """
        if not coordinated:
            return migrate_user(auth, db, settings, src, tgt,
                                services, delta, delta_days)

        claimed, why = user_claims.acquire(account_id, src, services=svc_label)
        if not claimed:
            log.info("skipping %s: %s", src, why)
            return None

        stop = threading.Event()
        renewer = threading.Thread(
            target=_renew_until, args=(stop, account_id, src),
            name=f"lease-{src[:12]}", daemon=True)
        renewer.start()
        try:
            out = migrate_user(auth, db, settings, src, tgt,
                               services, delta, delta_days)
            user_claims.finish(account_id, src, status="DONE")
            return out
        except BaseException as exc:
            # Record the failure against the claim before re-raising, so the
            # user shows as FAILED rather than sitting CLAIMED until the
            # lease lapses and looking like a live node still working on it.
            user_claims.finish(account_id, src, status="FAILED",
                               detail=str(exc)[:400])
            raise
        finally:
            stop.set()
            renewer.join(timeout=2)

    with futures.ThreadPoolExecutor(
        max_workers=settings.user_workers, thread_name_prefix="user"
    ) as pool:
        pending = {pool.submit(_one, s, t): s for s, t in pairs}
        for fut in futures.as_completed(pending):
            try:
                out = fut.result()
                if out is not None:
                    results.append(out)
            except Exception as exc:  # noqa: BLE001
                log.exception("worker for %s crashed: %s", pending[fut], exc)
    return results


# ======================================================================
# Memory watchdog
#
# Convert catastrophic memory pressure into a clean, resumable pause. The
# executor, submit-all submission, worker lifecycle and ledger are all
# untouched: the watchdog just flips SHUTDOWN once sustained severe pressure is
# confirmed, workers finish their CURRENT service (they already check SHUTDOWN
# between services), and the run exits PAUSED to resume from the ledger.
#
# There is deliberately no admission gate. Queued tasks are ~2.2 KB each (heavy
# per-user state is built inside migrate_user, not at submit time), so the
# active working set is bounded by ThreadPoolExecutor(max_workers=N) either
# way; an admission gate would protect memory that was never in use.
# ======================================================================
def _memory_watchdog(stop_event: threading.Event, shutdown=SHUTDOWN,
                     pause_event=MEMORY_PAUSE, probe_fn=None,
                     poll: float = WATCHDOG_POLL_SEC,
                     samples: int = WATCHDOG_SUSTAINED_SAMPLES,
                     reminder: float = MEMORY_REMINDER_SEC) -> None:
    """
    Daemon thread that runs for the whole migration.

    Before a drain it polls a cached probe and counts consecutive severe
    samples; after `samples` of them it logs the transition and flips SHUTDOWN
    exactly once. It then stops probing and only emits a periodic reminder
    while in-flight services finish. Exits when the main thread sets
    `stop_event` (run_batch returned) or the process dies (daemon).

    Must never raise and never busy-loop: every path through the loop sleeps at
    least `poll` seconds via stop_event.wait(), and a failing probe is logged
    and skipped rather than propagated.
    """
    probe_fn = probe_fn or cached_probe
    consecutive = 0
    last_reminder = 0.0
    while not stop_event.wait(poll):
        if shutdown.is_set():
            # Draining: workers are still finishing their current service.
            if pause_event.is_set() and time.monotonic() - last_reminder >= reminder:
                log.warning("Still waiting for current services to finish — "
                            "the migration will pause. Re-run to resume.")
                last_reminder = time.monotonic()
            continue
        try:
            snapshot = probe_fn()
        except Exception as exc:  # noqa: BLE001 - a broken probe must not kill the run
            log.warning("Memory probe failed: %s — retrying", exc)
            consecutive = 0
            continue
        if pressure_severe(snapshot):
            consecutive += 1
            if consecutive == 1:
                log.warning("Memory pressure detected — confirming before pausing.")
            elif consecutive >= samples:
                log.warning("Entering drain mode — waiting for current services "
                            "to finish.")
                pause_event.set()
                shutdown.set()
                last_reminder = time.monotonic()
        else:
            if consecutive:
                log.warning("Memory pressure subsided.")
                consecutive = 0


def _gate_on_delegation(settings: Settings) -> None:
    """Stop before the batch if a tenant cannot mint the token it will need.

    Every per-user failure this prevents used to arrive eight minutes into a
    run as a raw `unauthorized_client` naming neither the tenant nor the
    scope, and left a FAILED ledger row against a user nothing was wrong
    with. One combined token mint per tenant answers it up front; only a
    failure pays for the per-scope walk that produces the diagnosis.

    Skippable via MIGRATE_SKIP_SCOPE_CHECK=1 -- an offline rehearsal against
    a fixture ledger has no tenant to probe, and this must never be the
    reason such a run cannot start.
    """
    if os.getenv("MIGRATE_SKIP_SCOPE_CHECK", "").strip() in ("1", "true", "yes"):
        log.info("scope preflight skipped (MIGRATE_SKIP_SCOPE_CHECK)")
        return
    try:
        import scope_guard
    except Exception as exc:      # noqa: BLE001 - never block on the guard
        log.warning("scope preflight unavailable: %s", exc)
        return
    try:
        repaired = scope_guard.ensure(settings)
    except scope_guard.ScopeGapError as gap:
        # Deliberately not a traceback: this is an operator-facing
        # instruction, and the stack tells them nothing they can act on.
        sys.exit("\nDelegation is incomplete — nothing has been migrated.\n"
                 f"{scope_guard.describe(gap.gaps)}\n\n"
                 "Re-run this command once the grant is in place. "
                 "Set MIGRATE_SKIP_SCOPE_CHECK=1 to bypass this check.")
    except Exception as exc:      # noqa: BLE001 - advisory, never blocking
        # A probe that itself fails (network, clock skew) must not stop a
        # migration that would otherwise work. The run will surface any real
        # scope problem the old way.
        log.warning("scope preflight could not complete: %s", exc)
        return
    for gap in repaired:
        log.warning("scope preflight repaired %s: granted %s",
                    gap.tenant, ", ".join(gap.missing))


def _run_with_memory_pause(auth, db, settings, services, delta, delta_days,
                           only=None) -> list[dict]:
    """run_batch under the memory watchdog; exits PAUSED if it fires."""
    _gate_on_delegation(settings)
    try:
        reopened = reconcile_service_markers(db)
        for user, svc in reopened[:20]:
            log.warning("re-opening %s for %s: marked done but migrated "
                        "nothing and recorded failures", svc, user)
        if len(reopened) > 20:
            log.warning("... and %d more re-opened", len(reopened) - 20)
    except Exception as exc:      # noqa: BLE001 - advisory, never blocking
        log.warning("could not reconcile service markers: %s", exc)
    MEMORY_PAUSE.clear()
    stop = threading.Event()
    watchdog = threading.Thread(target=_memory_watchdog, args=(stop,),
                                name="watchdog", daemon=True)
    watchdog.start()
    flusher = threading.Thread(target=_metrics_flusher, args=(stop, db),
                               name="metrics", daemon=True)
    flusher.start()
    try:
        results = run_batch(auth, db, settings, services, delta=delta,
                            delta_days=delta_days, only=only)
    finally:
        stop.set()
        watchdog.join(timeout=WATCHDOG_POLL_SEC * 2 + 1)
    if MEMORY_PAUSE.is_set():
        log.warning("Migration paused — current services completed "
                    "successfully; re-run to resume.")
        print("\nMigration paused due to sustained memory pressure.")
        print("Current services completed successfully.")
        print("Re-run the same command to resume.")
        print("Progress is preserved in the migration ledger.")
        raise SystemExit(EXIT_PAUSED)
    return results


# ======================================================================
# Commands
# ======================================================================
def read_identity_csv_domains(path: str) -> list[dict]:
    """The source/target addresses in a CSV, without loading it."""
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


def cmd_init_db(args, settings: Settings, db: MigrationDB, auth: AuthManager):
    if args.identities:
        # Check before loading, not after. identities.csv is regenerated by
        # the seeder and otherwise sits in the working directory indefinitely,
        # so pointing a new tenant pair at an old checkout loads the previous
        # migration's users and reports success -- the command did what it was
        # told, with input nobody re-read.
        rows = read_identity_csv_domains(args.identities)
        mismatch = identity_domain_mismatch(rows, settings) if rows else ""
        if mismatch and not getattr(args, "force", False):
            print(f"REFUSING to load {args.identities}: it describes different "
                  f"tenants.\n  {mismatch}\n\n"
                  f"That file is probably left over from an earlier migration. "
                  f"Re-run the seeder (it writes a fresh one), point\n"
                  f"--identities at the right file, or pass --force if you "
                  f"really mean it.")
            sys.exit(1)

        n = db.load_identity_csv(args.identities)
        print(f"Loaded {n} identity mappings.")
    elif args.auto_map:
        # Same-localpart auto-mapping: convenient for lift-and-shift merges
        # where nobody is being renamed. Always review the output before use.
        src_users = list_domain_users(auth, "source", settings.source_domain)
        tgt_users = set(list_domain_users(auth, "target", settings.target_domain))
        pairs = []
        missing = []
        for s in src_users:
            candidate = s.split("@")[0] + "@" + settings.target_domain
            if candidate in tgt_users:
                pairs.append((s, candidate))
            elif getattr(args, "include_missing", False):
                # Map it anyway, so provision-users has something to create.
                #
                # Without this the two commands cannot start each other:
                # auto-map only pairs accounts that already exist on the
                # target, and provision-users only creates accounts already
                # in identity_map. A fresh target therefore maps 1 user of
                # 201 and then reports "nothing to create" -- correctly, and
                # uselessly. Seen exactly that: a target holding only
                # info@, mapped to source's info@, with 200 source accounts
                # invisible to both commands.
                pairs.append((s, candidate))
                missing.append(candidate)
            else:
                log.warning("no target account for %s (expected %s)", s, candidate)
        from db import bulk_seed_identities
        bulk_seed_identities(db, pairs)
        print(f"Auto-mapped {len(pairs)} of {len(src_users)} source users.")
        if missing:
            print(f"  {len(missing)} of them have no target account yet. "
                  f"They are mapped so `provision-users` can create them; "
                  f"until it does, migrating those users will fail.")
            print(f"  Creating them consumes a licence each -- run "
                  f"`provision-users --tenant target --dry-run` first to see "
                  f"the exact list.")
    print(f"Schema initialised at {settings.db_path}")


METRICS_FLUSH_SEC = 15.0


def _metrics_flusher(stop_event: threading.Event, db,
                     interval: float = METRICS_FLUSH_SEC) -> None:
    """Copy this process's metrics into the ledger, so other processes can
    read them.

    Metrics live in the migrating process and every reader lives in another
    one -- webui_spa called METRICS.snapshot() from inside api_server, a
    process that issues no Drive calls, and rendered the resulting empty
    reservoir as though it were the run. Nothing was wrong with the metrics;
    they were simply being asked for in the wrong address space.

    Daemon, and must never raise: a failure to report progress is not a
    reason to stop making it.
    """
    import metrics as metrics_mod

    while not stop_event.wait(interval):
        try:
            payload = metrics_mod.METRICS.snapshot()
            try:
                import drive_engine
                payload["limiters"] = drive_engine.limiter_stats()
            except Exception:      # noqa: BLE001
                pass
            payload["workers_configured"] = getattr(
                db, "_workers_configured", None) or 0
            db.record_metrics(payload)
        except Exception as exc:      # noqa: BLE001
            log.debug("metrics flush skipped: %s", exc)


def _warn_if_ledger_is_stale(db, auth, pairs) -> None:
    """Refuse to start a run that would skip everything and call it success.

    id_mapping is authoritative, so a target account recreated since the
    ledger was written leaves every mapping naming something that no longer
    exists -- and the run skips all of it in seconds and reports done. That
    happened live on 2026-08-21: 200 accounts deleted, 200 recreated on the
    same addresses, 462,048 mappings left pointing at deleted items.

    One Directory call per user, once per run. Loud rather than automatic:
    forgetting 462,048 mappings means re-copying them, which is hours of
    someone's bandwidth and quota, and is not a decision to take silently on
    their behalf.
    """
    import ledger_verify

    try:
        report = ledger_verify.verify(db, auth.directory("target"), pairs)
    except Exception as exc:      # noqa: BLE001 - never block a run on the check
        log.warning("could not verify the ledger against the target (%s); "
                    "continuing", str(exc)[:160])
        return
    if not report.stale:
        return
    log.error(
        "%d of %d user(s) have %s mapping(s) that no longer exist on the "
        "target -- their accounts were recreated after the ledger was "
        "written. Those items will be SKIPPED as already-migrated and this "
        "run will report success without copying them. Run "
        "`main.py --account-id N verify-ledger --reopen` first.",
        len(report.stale), report.checked, f"{report.stale_mappings:,}")
    for verdict in report.stale[:3]:
        log.error("  %s: %s", verdict.source_user, verdict.reason)
    raise SystemExit(
        "refusing to start against a ledger that does not match the target; "
        "see verify-ledger")


def cmd_verify_ledger(args, settings: Settings, db: MigrationDB,
                      auth: "AuthManager") -> int:
    """Does the ledger still describe the tenant?

    id_mapping is authoritative -- anything with a mapping is skipped on a
    resume -- and nothing ever checked that the target item still exists.
    See ledger_verify for the incident that made this necessary.
    """
    import ledger_verify

    identities = [(r["source_email"], r["target_email"])
                  for r in db.identity_pairs()]
    if not identities:
        log.error("no identity mappings; run init-db --auto-map first")
        return 2

    directory = auth.directory("target")
    spot = None
    if args.spot_check:
        def spot(target_user: str, target_id: str) -> bool:
            try:
                auth.target_drive(target_user).files().get(
                    fileId=target_id, fields="id",
                    supportsAllDrives=True).execute()
                return True
            except Exception as exc:      # noqa: BLE001
                return "404" not in str(exc)

    report = ledger_verify.verify(db, directory, identities, spot_check=spot)
    print(report.as_text())

    # Separate from the mapping check, and invisible to it: a user whose
    # mappings were already forgotten has nothing left for verify() to
    # compare, so a stale DONE status reads as perfectly healthy while
    # dropping the user from every dispatch.
    orphaned = ledger_verify.orphans(db)
    if orphaned:
        total = sum(r["migrated"] for r in orphaned)
        print(f"\n{len(orphaned)} user(s) are marked DONE with no mappings "
              f"left, but their audit records {total:,} migrated item(s) -- "
              f"they will be skipped by every run until reopened:")
        for r in orphaned[:10]:
            print(f"  {r['source_email']}: {r['migrated']:,} item(s) "
                  f"in the audit, 0 mappings")
        if len(orphaned) > 10:
            print(f"  ... and {len(orphaned) - 10} more")

    if not report.stale and not orphaned:
        return 0

    n = ledger_verify.reopen(db, report, dry_run=not args.reopen)
    ledger_verify.reopen_orphans(db, orphaned, dry_run=not args.reopen)
    if args.reopen and orphaned:
        print(f"Re-opened {len(orphaned)} user(s) whose status claimed they "
              f"were finished.")
    if args.reopen:
        print(f"\nForgot {n:,} stale mapping(s). The next migrate run will "
              f"copy those items again. Audit history was left intact.")
    else:
        print(f"\n{n:,} mapping(s) would be forgotten. Re-run with --reopen "
              f"to apply, after confirming the target accounts are the ones "
              f"you intend to migrate into.")
    return 1


def cmd_provision_users(args, settings: Settings, db: MigrationDB,
                        auth: AuthManager):
    """
    Create missing accounts for the identities already in identity_map.

    Separate from `migrate` on purpose. This is the only command that can
    create licensed accounts, so it must be something you run deliberately,
    never a side effect of copying files. It only ever creates -- an address
    that already exists is left exactly as it is.
    """
    import provision

    rows = [r for r in db.all_identities() if r["entity_type"] == "user"]
    if not rows:
        print("identity_map is empty — run init-db first.")
        return

    which = args.tenant
    emails = sorted({r["source_email"] if which == "source" else r["target_email"]
                     for r in rows})
    domain = (settings.source_domain if which == "source"
              else settings.target_domain)

    wrong = [e for e in emails if not e.endswith("@" + domain)]
    if wrong:
        # A typo in identity_map should not become an account in a domain
        # nobody meant to touch.
        sys.exit(f"REFUSING: these addresses are not in {domain}: {wrong[:5]}")

    print(f"Tenant : {which} ({domain})")
    print(f"Accounts in identity_map: {len(emails)}")
    if args.dry_run:
        print("DRY RUN — nothing will be created\n")
    else:
        print("\nThis creates real accounts, which consume licences.")
        if not args.yes and input(f"Type the domain to confirm: ").strip() != domain:
            print("Aborted.")
            return

    directory = auth.directory(which, writable=not args.dry_run)
    result = provision.ensure_users(directory, emails, dry_run=args.dry_run)
    provision.report(result, dry_run=args.dry_run)

    if result["failed"]:
        sys.exit(1)


def identity_domain_mismatch(rows, settings: Settings) -> str:
    """
    Does identity_map describe the tenants we are configured for?

    A migration.db outlives the run that created it. Point the same directory
    at a second tenant pair and the old identity_map is still there, so every
    command silently operates on the *previous* migration's users -- preflight
    impersonates accounts in a tenant nobody is migrating any more and reports
    a wall of authentication failures that have nothing to do with the current
    setup. Cheap to detect, and invisible if you do not.
    """
    src = (settings.source_domain or "").strip().lower()
    tgt = (settings.target_domain or "").strip().lower()
    if not src or not tgt:
        return ""

    def domains(field):
        return {r[field].split("@")[-1].lower() for r in rows if r[field]}

    have_src, have_tgt = domains("source_email"), domains("target_email")
    bad_src = have_src - {src}
    bad_tgt = have_tgt - {tgt}
    if not bad_src and not bad_tgt:
        return ""

    parts = []
    if bad_src:
        parts.append(f"source addresses are in {', '.join(sorted(bad_src))} "
                     f"but SOURCE_DOMAIN is {src}")
    if bad_tgt:
        parts.append(f"target addresses are in {', '.join(sorted(bad_tgt))} "
                     f"but TARGET_DOMAIN is {tgt}")
    return "; ".join(parts)


def cmd_preflight(args, settings: Settings, db: MigrationDB, auth: AuthManager):
    """Verify DWD on both tenants for every mapped user before doing real work."""
    rows = db.all_identities()
    if not rows:
        print("identity_map is empty — run init-db first.")
        return

    mismatch = identity_domain_mismatch(rows, settings)
    if mismatch:
        print(f"REFUSING: identity_map does not match this configuration.\n"
              f"  {mismatch}\n\n"
              f"This database was built for a different tenant pair. Testing "
              f"those accounts would tell you nothing about the tenants you\n"
              f"are set up for now. Either point MIGRATION_DB at a fresh file, "
              f"or reload the map:\n"
              f"    python3 main.py init-db --identities identities.csv\n"
              f"    python3 main.py init-db --auto-map")
        sys.exit(1)

    # API enablement before delegation. They are separate gates in separate
    # consoles and a granted scope does not switch an API on -- this project
    # ran a full seeding pass with 17/17 DWD scopes live and People/Tasks
    # never enabled, producing zero contacts and zero tasks while reporting
    # success. Checking here costs one call per API and catches it before a
    # multi-hour run rather than inside one.
    try:
        import ensure_apis

        for tenant in ("source", "target"):
            res = ensure_apis.ensure(settings, tenant, do_enable=False)
            if res.get("disabled"):
                print(f"\nWARNING: {tenant} project {res['project']} has "
                      f"DISABLED API(s): {', '.join(res['disabled'])}")
                print("Calls to these fail with SERVICE_DISABLED no matter "
                      "what DWD grants.")
                print(ensure_apis.advice(res["project"], res["disabled"]))
                print()
    except Exception as exc:  # noqa: BLE001 - advisory; never block preflight
        log.debug("API enablement check skipped: %s", exc)

    failures = 0
    for r in rows:
        ok_s, msg_s = auth.verify_delegation("source", r["source_email"])
        ok_t, msg_t = auth.verify_delegation("target", r["target_email"])
        flag = "OK  " if (ok_s and ok_t) else "FAIL"
        print(f"[{flag}] {r['source_email']} -> {r['target_email']}")
        if not ok_s:
            print(f"        source: {msg_s}")
            failures += 1
        if not ok_t:
            print(f"        target: {msg_t}")
            failures += 1
    print(f"\nPreflight complete: {failures} problem(s).")
    if failures:
        sys.exit(1)


def cmd_discover(args, settings: Settings, db: MigrationDB, auth: AuthManager):
    rows = db.all_identities()
    users = [r["source_email"] for r in rows]
    if args.user:
        users = [u for u in users if u in {x.lower() for x in args.user}]

    totals = {"files": 0, "folders": 0, "bytes": 0}
    with futures.ThreadPoolExecutor(max_workers=settings.user_workers) as pool:
        jobs = {
            pool.submit(scan_user, auth, db, settings, u, args.include_mail): u
            for u in users
        }
        for fut in futures.as_completed(jobs):
            u = jobs[fut]
            try:
                stats = fut.result()
                print_report(stats)
                totals["files"] += stats["file_count"]
                totals["folders"] += stats["folder_count"]
                totals["bytes"] += stats["total_bytes"]
            except Exception as exc:  # noqa: BLE001
                log.error("discovery failed for %s: %s", u, exc)

    print("\n=== Tenant totals ===")
    print(f"  Files   : {totals['files']:,}")
    print(f"  Folders : {totals['folders']:,}")
    print(f"  Size    : {totals['bytes'] / 1024**4:.3f} TB")
    days = totals["bytes"] / (750 * 1024**3 * max(1, settings.user_workers))
    print(f"  Floor on wall-clock from the 750 GB/user/day cap: "
          f"~{days:.1f} day(s) at {settings.user_workers} concurrent users")


# Everything `migrate` can run per user. Shared Drives are absent on purpose:
# they belong to no user, are driven as an admin, and are run by
# shared_drives.py -- putting them here would make them run 141 times.
PER_USER_SERVICES = ("drive", "gmail", "calendar", "chat", "contacts", "tasks")


def resolve_services(raw: str) -> set[str]:
    """
    Parse --services, expanding `all`.

    `all` means every per-user service, and selecting a service turns its
    feature flag on. That is the point: someone asking for the full scope
    should not then discover that two of the six silently did nothing because
    a MIGRATE_* variable was unset. The scopes still have to be granted, and
    preflight says so by name when they are not.
    """
    services = {s.strip().lower() for s in raw.split(",") if s.strip()}
    if "all" in services:
        services.discard("all")
        services |= set(PER_USER_SERVICES)
    unknown = services - set(PER_USER_SERVICES)
    if unknown:
        sys.exit(f"unknown service(s): {', '.join(sorted(unknown))}. "
                 f"Valid: {', '.join(PER_USER_SERVICES)}, or 'all'.")
    return services


def cmd_migrate(args, settings: Settings, db: MigrationDB, auth: AuthManager):
    services = resolve_services(args.services)
    if "chat" in services:
        # Chat is a first-class service: selecting it opts the run in. That
        # widens the scopes (chat.spaces/chat.messages) and enables the
        # engine's import pass. See config.py for the fidelity caveat.
        settings.migrate_chat = True
    if "contacts" in services:
        settings.migrate_contacts = True
    if "tasks" in services:
        settings.migrate_tasks = True
    results = _run_with_memory_pause(
        auth, db, settings, services, delta=False, delta_days=0, only=args.user)
    _print_batch_summary(results)


def cmd_delta(args, settings: Settings, db: MigrationDB, auth: AuthManager):
    """
    Module 6: incremental catch-up pass.

    Run this repeatedly in the days between the bulk copy and the cutover, and
    once more immediately after the cutover window closes.
    """
    services = {s.strip().lower() for s in args.services.split(",") if s.strip()}
    if "chat" in services:
        settings.migrate_chat = True
    results = _run_with_memory_pause(
        auth, db, settings, services, delta=True, delta_days=args.days,
        only=args.user)
    _print_batch_summary(results)


def cmd_syncacls(args, settings: Settings, db: MigrationDB,
                 auth: AuthManager) -> int:
    """
    Recreate share access on every already-migrated file and folder.

    The copy pass preserves a file's direct grants and, for inherited ones,
    relies on the target tree recreating the parent folder's permission. This
    pass makes the access explicit on the item itself (per-file model), so a
    document keeps its collaborators even if it is later moved or the folder
    unshared. Idempotent: creating a grant that already exists is a no-op.
    """
    from drive_engine import DriveMigrator
    from resilience import DailyQuotaGuard

    rows = [r for r in db.all_identities() if r["entity_type"] == "user"]
    if args.user:
        want = {u.lower() for u in args.user}
        rows = [r for r in rows if r["source_email"] in want]

    print("Recreating per-file share access on the target "
          f"(recreate_inherited_acls={settings.recreate_inherited_acls})")
    applied = synced = 0
    for r in rows:
        src, tgt = r["source_email"], r["target_email"]
        mapped = db.conn.execute(
            "SELECT source_id, target_id FROM id_mapping "
            "WHERE source_user=? AND type IN ('file','folder') ORDER BY source_id",
            (src,),
        ).fetchall()
        if not mapped:
            print(f"  {src:<40} nothing mapped")
            continue
        if settings.dry_run:
            print(f"  {src:<40} WOULD sync {len(mapped)} items")
            synced += len(mapped)
            continue
        quota = DailyQuotaGuard(db, tgt, settings.effective_upload_cap())
        migrator = DriveMigrator(auth, db, settings, src, tgt, quota)
        per = 0
        for i, row in enumerate(mapped, start=1):
            try:
                per += migrator._sync_acls(row["source_id"], row["target_id"])
            except Exception as exc:  # noqa: BLE001
                print(f"    ! {row['source_id']}: {exc}")
            if i % 100 == 0:
                print(f"    {src} {i}/{len(mapped)} items ...", flush=True)
        applied += per
        synced += len(mapped)
        print(f"  {src:<14} {len(mapped):>5} items, {per} grants applied")
    print(f"\nApplied {applied} grants across {synced} mapped items.")
    return 0


def cmd_report(args, settings: Settings, db: MigrationDB, auth: AuthManager):
    rows = db.all_identities()
    print(f"{'SOURCE':<38}{'TARGET':<38}{'STATUS':<16}")
    print("-" * 92)
    for r in rows:
        print(f"{r['source_email']:<38}{r['target_email']:<38}{r['status']:<16}")

    print("\n=== Per-user item counts ===")
    for r in rows:
        s = db.summary(r["source_email"])
        if not s:
            continue
        line = ", ".join(f"{k}={v['count']}" for k, v in sorted(s.items()))
        print(f"  {r['source_email']}: {line}")

    print("\n=== Failures ===")
    total_failed = 0
    for r in rows:
        fails = db.failed_items(r["source_email"])
        total_failed += len(fails)
        for f in fails[: args.max_failures]:
            print(f"  {r['source_email']} {f['item_type']} {f['item_id']}: "
                  f"{(f['error_message'] or '')[:120]}")
        if len(fails) > args.max_failures:
            print(f"  ... and {len(fails) - args.max_failures} more for "
                  f"{r['source_email']}")
    print(f"\nTotal failed items: {total_failed}")


def cmd_backfill_services(args, settings: Settings, db: MigrationDB,
                          auth: AuthManager):
    """
    Record which services a pre-`services_done` ledger actually completed.

    Needed because `status` used to be per-user: a ledger left DONE by a
    Drive-only run cannot say so, and the per-service skip check then has to
    assume *everything* ran. Stating the truth once here unblocks the services
    that genuinely have not run, without re-migrating the one that has.

    Verified against the ledger rather than trusted: a service is only
    backfilled for a user who has SUCCESS rows of the matching item types, so
    a wrong --services cannot mark work done that never happened.
    """
    evidence = {
        "drive": ("file", "folder"),
        "gmail": ("message", "label", "draft"),
        "calendar": ("event", "calendar"),
        "chat": ("space", "chat_message"),
    }
    changed = skipped = 0
    for r in db.all_identities():
        if r["entity_type"] != "user" or r["status"] != "DONE":
            continue
        # summary() keys are "<item_type>:<status>", so a bare item type never
        # matches. Only SUCCESS counts as evidence -- a user whose entire Drive
        # phase failed must not be recorded as having completed it.
        have = db.summary(r["source_email"]) or {}
        done_types = {k.split(":", 1)[0] for k, v in have.items()
                      if k.endswith(":SUCCESS") and v["count"] > 0}
        confirmed = [s for s in args.services
                     if done_types & set(evidence.get(s, ()))]
        missing = sorted(set(args.services) - set(confirmed))
        if missing:
            print(f"  {r['source_email']}: no ledger evidence for "
                  f"{','.join(missing)} — not backfilled")
            skipped += 1
        if confirmed:
            db.mark_services_done(r["source_email"], confirmed)
            print(f"  {r['source_email']}: recorded {','.join(sorted(confirmed))}")
            changed += 1
    print(f"\nBackfilled {changed} user(s); {skipped} had gaps left alone.")


def cmd_coverage(args, settings: Settings, db: MigrationDB, auth: AuthManager):
    """Which supported data types does the source actually contain?

    `scope` says what the engine can migrate; this says which of those the
    source has instances of. A supported category with zero instances is a
    path the migration will report success on without ever running.
    """
    import coverage_audit

    users = args.user or [r["source_email"] for r in db.all_identities()
                          if r["entity_type"] == "user"]
    if not users:
        print("no users in identity_map — run init-db first")
        return 2
    totals = coverage_audit.collect(auth, settings, users)
    rows = coverage_audit.assess(totals)
    if args.format == "json":
        import json as _json
        print(_json.dumps({"rows": rows, "totals": totals}, indent=2, default=str))
    else:
        print(coverage_audit.render(rows, totals))
    absent = [r for r in rows if r["verdict"] == coverage_audit.ABSENT]
    return 1 if (absent and not args.allow_absent) else 0


def cmd_scope(args, settings: Settings, db: MigrationDB, auth: AuthManager):
    """Print the migration scope manifest — what moves and what does not."""
    import scope as scope_mod

    if args.format == "json":
        print(scope_mod.as_json())
        return
    if args.format == "markdown":
        md = scope_mod.as_markdown()
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(md)
            print(f"Wrote {args.out}")
        else:
            print(md)
        return

    services = args.service or None
    statuses = args.status or None
    tally = scope_mod.counts()

    print("MIGRATION SCOPE")
    print("  [+] FULL     high fidelity")
    print("  [~] PARTIAL  migrated with a named fidelity loss")
    print("  [-] NONE     not migrated by this engine\n")
    print(f"  {'service':<10}{'full':>6}{'partial':>9}{'none':>7}")
    for svc in scope_mod.SERVICES:
        t = tally.get(svc, {})
        print(f"  {svc:<10}{t.get('FULL',0):>6}{t.get('PARTIAL',0):>9}"
              f"{t.get('NONE',0):>7}")

    for line in scope_mod.as_text(services, statuses):
        print(line)

    vol = scope_mod.planned_volume(db)
    print("\n== DISCOVERED VOLUME " + "=" * 78)
    if vol["users"]:
        print(f"  Users scanned : {vol['users']}")
        print(f"  Files         : {vol['files']:,} "
              f"({vol['native']:,} native Google docs)")
        print(f"  Folders       : {vol['folders']:,}  (max depth {vol['max_depth']})")
        print(f"  Messages      : {vol['messages']:,}")
        print(f"  Total bytes   : {vol['bytes'] / 1024**4:.3f} TB")
    else:
        print("  No discovery data yet — run 'python main.py discover'.")

    print("\n== OAUTH SCOPES REQUIRED " + "=" * 74)
    # Reported for THIS configuration, not the baseline: server_side mode and
    # the optional passes each widen the grant, and pasting the wrong list
    # into the Admin Console fails every call with unauthorized_client.
    baseline = scope_mod.oauth_scopes()
    effective = scope_mod.oauth_scopes(settings)
    for tenant, scopes in effective.items():
        print(f"\n  {tenant.upper()} tenant service account "
              f"(paste as one comma-separated line in Admin Console):")
        for s in scopes:
            extra = "" if s in baseline[tenant] else "   <- added by your config"
            print(f"    {s}{extra}")

    widened = {t: set(effective[t]) - set(baseline[t]) for t in effective}
    if any(widened.values()):
        print("\n  NOTE: your current settings widen the default grant. The "
              "options responsible:")
        if settings.transfer_mode == "server_side":
            print("    TRANSFER_MODE=server_side      -> source needs write "
                  "'drive' instead of 'drive.readonly'")
        if settings.migrate_calendar_acls:
            print("    MIGRATE_CALENDAR_ACLS=true     -> source needs write "
                  "'calendar' (acl.list rejects calendar.readonly)")
        if settings.migrate_gmail_settings:
            print("    MIGRATE_GMAIL_SETTINGS=true    -> both tenants need "
                  "'gmail.settings.basic'")


def cmd_ui(args, settings: Settings, db: MigrationDB, auth: AuthManager):
    """Launch the curses dashboard."""
    import tui
    db.close()
    sys.exit(tui.main(["--db", settings.db_path, "--refresh", str(args.refresh)]))


def _print_batch_summary(results: list[dict]) -> None:
    print("\n=== Batch summary ===")
    for r in sorted(results, key=lambda x: x["source"]):
        print(f"  {r['source']:<38}{r.get('status', '?'):<14}"
              f"{r.get('elapsed_sec', 0):>8.1f}s")
        for svc, st in (r.get("services") or {}).items():
            print(f"      {svc}: {st}")

    # Latency and throughput, from the run that just happened. Printed
    # unconditionally because it is the first thing anyone asks when a
    # migration is slower than expected, and reproducing a six-hour run to
    # collect it is not an option.
    from metrics import METRICS

    print("\n=== API timing ===")
    print(METRICS.report())


# ======================================================================
# CLI
# ======================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="migrate",
        description="Tenant-to-tenant Google Workspace migration engine.",
    )
    p.add_argument("--db", help="override migration.db path")
    p.add_argument("--dry-run", action="store_true",
                   help="log every intended write without performing it")
    p.add_argument("--workers", type=int, help="override concurrent user count")
    # Internal: set by api_server.py when a request came from a signed-in
    # SaaS account, never by a human. Makes Settings() resolve that
    # account's own domains/keys/db_path from tenant_configs instead of
    # env.sh -- see config.py's Settings._load_account_tenant_config --
    # and doubles as the marker api_server.py's status polling greps
    # `ps -eo args=` for, so two accounts' concurrent runs never look like
    # the same process to it.
    p.add_argument("--account-id", type=int, default=None, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init-db", help="create schema and load identity_map")
    s.add_argument("--identities", help="CSV: source_email,target_email")
    s.add_argument("--force", action="store_true",
                   help="load the CSV even when its domains do not match "
                        "SOURCE_DOMAIN/TARGET_DOMAIN")
    s.add_argument("--auto-map", action="store_true",
                   help="derive mappings by matching localparts across tenants")
    s.add_argument("--include-missing", action="store_true",
                   help="with --auto-map, also map source users that have NO "
                        "target account yet, so provision-users can create "
                        "them. Without this a fresh target maps almost "
                        "nothing: auto-map only pairs accounts that already "
                        "exist, and provision-users only creates accounts "
                        "already mapped, so neither can start the other. "
                        "Every account created consumes a licence.")
    s.set_defaults(func=cmd_init_db)

    s = sub.add_parser("preflight", help="verify DWD for every mapped user")
    s.set_defaults(func=cmd_preflight)

    s = sub.add_parser("verify-ledger",
                       help="check that mapped target items still exist "
                            "(a deleted account leaves the ledger claiming "
                            "work that is gone)")
    s.add_argument("--reopen", action="store_true",
                   help="forget mappings that no longer resolve, so the next "
                        "migrate run copies them again")
    s.add_argument("--spot-check", action="store_true",
                   help="also sample one real file per user (a few extra API "
                        "calls each; catches contents removed from an account "
                        "that itself survived)")
    s.set_defaults(func=cmd_verify_ledger)

    s = sub.add_parser("provision-users",
                       help="create missing accounts for identity_map entries "
                            "(never runs as part of migrate)")
    s.add_argument("--tenant", choices=["source", "target"], required=True)
    s.add_argument("--dry-run", action="store_true",
                   help="report what would be created, create nothing")
    s.add_argument("--yes", action="store_true",
                   help="skip the interactive domain confirmation "
                        "(for non-interactive callers like the web UI)")
    s.set_defaults(func=cmd_provision_users)

    s = sub.add_parser("discover", help="read-only pre-migration scan")
    s.add_argument("--user", action="append", help="limit to specific user(s)")
    s.add_argument("--include-mail", action="store_true")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("migrate", help="run the full bulk copy")
    s.add_argument("--services", default="all",
                   help="drive,gmail,calendar,chat,contacts,tasks — or 'all' "
                        "(the default: everything the tenant has). "
                        "for every per-user service. Shared Drives are not a "
                        "per-user service; run shared_drives.py, or use "
                        "phases.py which sequences both.")
    s.add_argument("--user", action="append", help="limit to specific user(s)")
    s.set_defaults(func=cmd_migrate)

    s = sub.add_parser("delta", help="incremental catch-up pass")
    s.add_argument("--services", default="all")
    s.add_argument("--days", type=int, default=2,
                   help="look-back window for Gmail/Calendar delta queries")
    s.add_argument("--user", action="append")
    s.set_defaults(func=cmd_delta)

    s = sub.add_parser("syncacls", help="recreate per-file ACLs on migrated items")
    s.add_argument("--user", action="append")
    s.set_defaults(func=cmd_syncacls)

    s = sub.add_parser("report", help="print migration status and failures")
    s.add_argument("--max-failures", type=int, default=20)
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("backfill-services",
                       help="record which services an older ledger completed")
    s.add_argument("--services", required=True,
                   type=lambda v: [x.strip() for x in v.split(",") if x.strip()],
                   help="comma-separated, e.g. drive")
    s.set_defaults(func=cmd_backfill_services)

    s = sub.add_parser("coverage",
                       help="which supported data types the source actually has")
    s.add_argument("--user", action="append", help="limit to specific source user(s)")
    s.add_argument("--format", default="text", choices=["text", "json"])
    s.add_argument("--allow-absent", action="store_true",
                   help="report gaps but still exit 0")
    s.set_defaults(func=cmd_coverage)

    s = sub.add_parser("scope", help="print exactly what will and will not migrate")
    s.add_argument("--service", action="append",
                   choices=["drive", "gmail", "calendar", "identity", "other"])
    s.add_argument("--status", action="append",
                   choices=["FULL", "PARTIAL", "NONE"],
                   help="e.g. --status NONE to review only what is left behind")
    s.add_argument("--format", default="text",
                   choices=["text", "markdown", "json"])
    s.add_argument("--out", help="write to a file instead of stdout")
    s.set_defaults(func=cmd_scope)

    s = sub.add_parser("ui", help="launch the terminal dashboard")
    s.add_argument("--refresh", type=float, default=2.0)
    s.set_defaults(func=cmd_ui)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    settings = Settings(account_id=args.account_id)
    if args.db:
        settings.db_path = args.db
    if args.dry_run:
        settings.dry_run = True
    if args.workers:
        settings.user_workers = args.workers

    setup_logging(settings)
    _install_signal_handlers()
    os.makedirs(settings.scratch_dir, exist_ok=True)

    if settings.dry_run:
        log.warning("DRY RUN — no writes will be made to %s",
                    settings.target_domain)

    db = MigrationDB(settings.db_path)
    auth = AuthManager(settings)

    try:
        args.func(args, settings, db, auth)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
