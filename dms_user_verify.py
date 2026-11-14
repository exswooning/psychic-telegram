"""Reconcile and verify individual mailboxes during a DMS import.

A stable near-source target count is a scheduling hint. Each worker reconciles
the mailbox before checking Gmail and records a separate verification report.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys
import time
from uuid import uuid4

ROOT = "/opt/bitport"
MAX_PARALLEL = 3
SHARE = 0.99
LOG = f"{ROOT}/logs/jobs/3/dms_user_verify.log"


def say(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} {msg}"
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line, flush=True)


def landed(prev, now, source_total, target_total):
    """Require two matching counts before scheduling the mailbox."""
    return prev == now == target_total and target_total >= SHARE * source_total


def verify_mailbox(auth, db, settings, user):
    from resilience import retry_on_google_error
    from split_mail import reconcile_dms
    from verify_sample import run_and_save, verify_dir

    def progress(message):
        print(message, flush=True)

    result = reconcile_dms(auth, db, settings, users=[user], progress=progress)
    if result["blocked"] or result["errors"]:
        progress(f"Reconciliation unresolved for {user}: {result['blocked']} blocked, "
                 f"{len(result['errors'])} error(s). Verification held.")
        return 1

    # Parallel workers must not replace one another's report or latest pointer.
    report_dir = os.path.join(verify_dir(3), "dms", uuid4().hex)
    report = run_and_save(
        auth, db, settings, [user], ("gmail",),
        retry=retry_on_google_error(
            max_retries=settings.max_retries,
            max_elapsed=float(os.getenv("VERIFY_RETRY_MAX_ELAPSED_SEC", "180")),
        ),
        progress=progress, base=report_dir,
    )
    complete = report["coverage"]["completion"] == "COMPLETE"
    progress(f"Gmail check for {user}: {report['coverage']['completion']}, "
             f"verdict {report['verdict']} ({report_dir})")
    return 0 if complete else 1


def dispatch(user):
    """Return the exit status of the worker, including launch errors."""
    with open(LOG, "a", encoding="utf-8") as log:
        result = subprocess.run(
            [sys.executable, "-u", os.path.abspath(__file__), f"--verify-user={user}"],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
        )
    return result.returncode


def watch_mailboxes(auth, users):
    last, finished, running = {}, set(), {}
    say(f"Watching {len(users)} mailboxes for DMS landings")

    def collect_finished():
        for user, future in list(running.items()):
            if not future.done():
                continue
            try:
                code = future.result()
            except OSError as exc:
                say(f"Could not launch Gmail check for {user}: {exc}")
                return False
            if code:
                say(f"Gmail worker for {user} exited with status {code}; see its log")
                return False
            del running[user]
            finished.add(user)
        return True

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as workers:
        while len(finished) < len(users):
            for src, tgt in users:
                if not collect_finished():
                    return 1
                if len(running) == MAX_PARALLEL:
                    break
                if src in running or src in finished:
                    continue
                try:
                    source = auth.source_gmail(src).users().getProfile(userId="me").execute()["messagesTotal"]
                    target = auth.target_gmail(tgt).users().getProfile(userId="me").execute()["messagesTotal"]
                    if any(type(count) is not int or count < 0 for count in (source, target)):
                        raise ValueError("invalid messagesTotal")
                except Exception as exc:  # A failed observation cannot establish readiness.
                    say(f"Could not read mailbox counts for {src}: {type(exc).__name__}")
                    return 1
                if landed(last.get(src), target, source, target):
                    if not collect_finished():
                        return 1
                    running[src] = workers.submit(dispatch, src)
                    say(f"Scheduled reconcile and Gmail check for {src}")
                last[src] = target
            if not collect_finished():
                return 1
            if len(finished) < len(users):
                time.sleep(60)
    say(f"Gmail checks finished for {len(finished)} mailboxes; see each report for its verdict")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-user", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    os.environ.setdefault("MIGRATION_DB", f"{ROOT}/data/accounts/3/migration.db")
    os.environ.setdefault("GOOGLE_HTTP_TIMEOUT_SEC", os.getenv("VERIFY_HTTP_TIMEOUT_SEC", "90"))
    from auth import AuthManager
    from config import Settings
    from db import MigrationDB

    settings = Settings(account_id=3)
    db = MigrationDB(settings.db_path)
    try:
        auth = AuthManager(settings)
        users = [(r["source_email"], r["target_email"]) for r in db.all_identities()
                 if r["entity_type"] == "user"]
        if args.verify_user:
            if args.verify_user not in dict(users):
                parser.error("mailbox is outside account 3's identity map")
            return verify_mailbox(auth, db, settings, args.verify_user)
        return watch_mailboxes(auth, users)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
