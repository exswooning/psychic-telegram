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
from config import Settings
from db import MigrationDB
from discovery import print_report, scan_user
from drive_engine import DriveMigrator
from gmail_engine import GmailMigrator
from resilience import DailyQuotaGuard, QuotaExhausted

log = logging.getLogger("migrate")

# Cooperative shutdown flag, flipped by SIGINT/SIGTERM.
SHUTDOWN = threading.Event()


def setup_logging(settings: Settings) -> None:
    fmt = "%(asctime)s %(levelname)-7s [%(threadName)-14s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
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

        status = "INTERRUPTED" if SHUTDOWN.is_set() else "DONE"
        db.set_identity_status(source_user, status)
        result["status"] = status

    except Exception as exc:  # noqa: BLE001 - worker must not propagate
        log.exception("[%s] user migration failed", source_user)
        db.set_identity_status(source_user, "FAILED", str(exc))
        db.log_audit(source_user, source_user, "user", "FAILED", str(exc))
        result["status"] = "FAILED"
        result["error"] = str(exc)

    result["elapsed_sec"] = round(time.time() - started, 1)
    log.info("[%s] finished in %.1fs: %s", source_user,
             result["elapsed_sec"], json.dumps(result.get("services", {})))
    return result


def run_batch(auth: AuthManager, db: MigrationDB, settings: Settings,
              services: set[str], delta: bool, delta_days: int,
              only: list[str] | None = None) -> list[dict]:
    """Fan out across users with a bounded thread pool."""
    rows = [r for r in db.all_identities() if r["entity_type"] == "user"]
    # On a full run, skip users already marked DONE so restarts are cheap.
    # On a delta run, every user is a candidate — that is the point of the pass.
    pairs = [
        (r["source_email"], r["target_email"])
        for r in rows
        if delta or r["status"] != "DONE"
    ]

    if only:
        wanted = {u.lower() for u in only}
        pairs = [p for p in pairs if p[0] in wanted]

    if not pairs:
        log.warning("no users to process — check identity_map")
        return []

    log.info("dispatching %d users across %d workers (services=%s, delta=%s)",
             len(pairs), settings.user_workers, ",".join(sorted(services)), delta)

    results: list[dict] = []
    with futures.ThreadPoolExecutor(
        max_workers=settings.user_workers, thread_name_prefix="user"
    ) as pool:
        pending = {
            pool.submit(migrate_user, auth, db, settings, s, t,
                        services, delta, delta_days): s
            for s, t in pairs
        }
        for fut in futures.as_completed(pending):
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                log.exception("worker for %s crashed: %s", pending[fut], exc)
    return results


# ======================================================================
# Commands
# ======================================================================
def cmd_init_db(args, settings: Settings, db: MigrationDB, auth: AuthManager):
    if args.identities:
        n = db.load_identity_csv(args.identities)
        print(f"Loaded {n} identity mappings.")
    elif args.auto_map:
        # Same-localpart auto-mapping: convenient for lift-and-shift merges
        # where nobody is being renamed. Always review the output before use.
        src_users = list_domain_users(auth, "source", settings.source_domain)
        tgt_users = set(list_domain_users(auth, "target", settings.target_domain))
        pairs = []
        for s in src_users:
            candidate = s.split("@")[0] + "@" + settings.target_domain
            if candidate in tgt_users:
                pairs.append((s, candidate))
            else:
                log.warning("no target account for %s (expected %s)", s, candidate)
        from db import bulk_seed_identities
        bulk_seed_identities(db, pairs)
        print(f"Auto-mapped {len(pairs)} of {len(src_users)} source users.")
    print(f"Schema initialised at {settings.db_path}")


def cmd_preflight(args, settings: Settings, db: MigrationDB, auth: AuthManager):
    """Verify DWD on both tenants for every mapped user before doing real work."""
    rows = db.all_identities()
    if not rows:
        print("identity_map is empty — run init-db first.")
        return
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


def cmd_migrate(args, settings: Settings, db: MigrationDB, auth: AuthManager):
    services = {s.strip().lower() for s in args.services.split(",") if s.strip()}
    unknown = services - {"drive", "gmail", "calendar"}
    if unknown:
        sys.exit(f"unknown service(s): {', '.join(sorted(unknown))}")
    results = run_batch(auth, db, settings, services, delta=False,
                        delta_days=0, only=args.user)
    _print_batch_summary(results)


def cmd_delta(args, settings: Settings, db: MigrationDB, auth: AuthManager):
    """
    Module 6: incremental catch-up pass.

    Run this repeatedly in the days between the bulk copy and the cutover, and
    once more immediately after the cutover window closes.
    """
    services = {s.strip().lower() for s in args.services.split(",") if s.strip()}
    results = run_batch(auth, db, settings, services, delta=True,
                        delta_days=args.days, only=args.user)
    _print_batch_summary(results)


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
    for tenant, scopes in scope_mod.oauth_scopes().items():
        print(f"\n  {tenant.upper()} tenant service account "
              f"(paste as one comma-separated line in Admin Console):")
        for s in scopes:
            print(f"    {s}")


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
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init-db", help="create schema and load identity_map")
    s.add_argument("--identities", help="CSV: source_email,target_email")
    s.add_argument("--auto-map", action="store_true",
                   help="derive mappings by matching localparts across tenants")
    s.set_defaults(func=cmd_init_db)

    s = sub.add_parser("preflight", help="verify DWD for every mapped user")
    s.set_defaults(func=cmd_preflight)

    s = sub.add_parser("discover", help="read-only pre-migration scan")
    s.add_argument("--user", action="append", help="limit to specific user(s)")
    s.add_argument("--include-mail", action="store_true")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("migrate", help="run the full bulk copy")
    s.add_argument("--services", default="drive,gmail,calendar")
    s.add_argument("--user", action="append", help="limit to specific user(s)")
    s.set_defaults(func=cmd_migrate)

    s = sub.add_parser("delta", help="incremental catch-up pass")
    s.add_argument("--services", default="drive,gmail,calendar")
    s.add_argument("--days", type=int, default=2,
                   help="look-back window for Gmail/Calendar delta queries")
    s.add_argument("--user", action="append")
    s.set_defaults(func=cmd_delta)

    s = sub.add_parser("report", help="print migration status and failures")
    s.add_argument("--max-failures", type=int, default=20)
    s.set_defaults(func=cmd_report)

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

    settings = Settings()
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
