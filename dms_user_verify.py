"""Verify each user as soon as its mail has landed from the DMS, while the rest of the import runs.

The DMS console reports only a total. A user counts as landed when its target mailbox holds at least 99% of the
source's messages AND that count has not moved between two polls a minute apart. Then one process per user runs
`verify_sample.py --repair-after-user --scope-to-findings --user X --services gmail`, which verifies that user and
repairs only that user if findings remain. At most MAX_PARALLEL run at once. Each user is dispatched once.
"""
import os, subprocess, sys, time

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
    """True when the target has nearly all of the source and did not move since the last poll."""
    return target_total >= SHARE * source_total and prev is not None and prev == target_total and source_total > 0


def running_verifies():
    out = subprocess.run(["systemctl", "list-units", "--state=active", "--plain", "--no-legend", "bitport-verify-*"],
                         capture_output=True, text=True).stdout
    return len([l for l in out.splitlines() if l.strip()])


def dispatch(user, n):
    """Reconcile this user's DMS deliveries into the ledger (match by Message-ID), then verify and repair it.
    Without the reconcile the ledger has no mapping for DMS mail, and every DMS message reads as 'extra'
    (10 Nov 2026: admin@ read INCOMPLETE with 29 of 29 Message-IDs on the target, all matching the source)."""
    unit = f"bitport-verify-{n}"
    recon = (f"{ROOT}/.venv/bin/python -c \"import sys;sys.path.insert(0,'{ROOT}');import split_mail,"
             f"auth,config,db;st=config.Settings(account_id=3);d=db.MigrationDB(st.db_path);"
             f"split_mail.reconcile_dms(auth.AuthManager(st),d,st,users=['{user}'])\" >> {LOG} 2>&1")
    cmd = (f"{recon}; {ROOT}/.venv/bin/python -u verify_sample.py --account-id 3 --repair-after-user --scope-to-findings "
           f"--user {user} --services gmail >> {LOG} 2>&1")
    subprocess.run(["systemctl", "reset-failed", unit], capture_output=True)
    subprocess.run(["systemd-run", f"--unit={unit}", f"--working-directory={ROOT}", "/bin/sh", "-c", cmd],
                   capture_output=True)
    say(f"verify + repair dispatched for {user} ({unit})")


def main():
    sys.path.insert(0, ROOT)
    os.chdir(ROOT)
    os.environ.setdefault("MIGRATION_DB", f"{ROOT}/data/accounts/3/migration.db")
    from auth import AuthManager
    from config import Settings
    from db import MigrationDB
    st = Settings(account_id=3)
    db, a = MigrationDB(st.db_path), AuthManager(st)
    users = [(r["source_email"], r["target_email"]) for r in db.all_identities() if r["entity_type"] == "user"]
    last, done, n = {}, set(), 0
    say(f"watching {len(users)} mailboxes for DMS landings")
    while len(done) < len(users):
        for src, tgt in users:
            if src in done:
                continue
            try:
                s = a.source_gmail(src).users().getProfile(userId="me").execute().get("messagesTotal", 0)
                t = a.target_gmail(tgt).users().getProfile(userId="me").execute().get("messagesTotal", 0)
            except Exception as exc:      # noqa: BLE001 - a poll that fails is retried next minute
                say(f"poll failed for {src}: {str(exc)[:120]}")
                continue
            if landed(last.get(src), t, s, t) and running_verifies() < MAX_PARALLEL:
                n += 1
                dispatch(src, n)
                done.add(src)
            last[src] = t
        time.sleep(60)
    say("every user has been verified and repaired where needed")


if __name__ == "__main__":
    sys.exit(main())
