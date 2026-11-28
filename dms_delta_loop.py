"""Keep re-running the DMS as a DELTA until its failed-email count reaches zero.

After the fresh import finishes, this runs `dms_migrate.py --apply` WITHOUT --fresh-import on the same identity
map (a delta: the console keeps the map, so it only retries what did not land). It reads the console's failed count
after each attempt and stops when it is zero. Limits: 5 attempts; if the count stops falling it escalates to
Claude (logs/autopilot/INBOX.md) and stops, rather than repeating the same failure. Never deletes an import.
"""
import argparse, json, os, subprocess, sys, time
import fcntl
import uuid

ROOT = os.path.dirname(os.path.abspath(__file__))        # /opt/bitport on the box
ACCOUNT = int(os.getenv("BITPORT_ACCOUNT_ID") or 0)      # set by --account-id; 0 = none chosen
CSV = LOG = INBOX = LOCK = ""
MAX_ATTEMPTS = 5


def configure(account_id: int, identities: str | None = None) -> None:
    """Point every path at ONE account's folders (it was account 3's, written in)."""
    global ACCOUNT, CSV, LOG, INBOX, LOCK
    ACCOUNT = int(account_id)
    CSV = identities or f"{ROOT}/logs/dms/{ACCOUNT}/identities.csv"
    LOG = f"{ROOT}/logs/jobs/{ACCOUNT}/dms_delta_loop.log"
    INBOX = f"{ROOT}/logs/autopilot/INBOX.md"
    LOCK = f"{ROOT}/logs/jobs/{ACCOUNT}/dms-delta-scheduler.lock"


configure(ACCOUNT)


def say(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} {msg}"
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line, flush=True)


def unit_active(name):
    return subprocess.run(["systemctl", "is-active", "--quiet", name]).returncode == 0


def failed_count():
    try:
        m = json.load(open(f"{ROOT}/logs/dms/{ACCOUNT}/metrics.json"))
        return int(m["metrics"].get("Emails failed", -1)), m.get("status", "?")
    except Exception:      # noqa: BLE001 - unknown is not zero
        return -1, "?"


def wait_done(unit):
    while unit_active(unit):
        time.sleep(60)


def run_delta(attempt):
    unit = f"bitport-dms-delta-{attempt}-{uuid.uuid4().hex[:10]}"
    code = (f"import sys,subprocess,webui;env=webui._dms_env({ACCOUNT});env['MIGRATION_DB']='{ROOT}/data/accounts/{ACCOUNT}/migration.db';"
            f"sys.exit(subprocess.call(['{ROOT}/.venv/bin/python','dms_migrate.py','--apply','--watch','120',"
            f"'--until-done','4320','--timeout','200','--identities','{CSV}','--no-tally'],env=env,cwd='{ROOT}'))")
    subprocess.run(["systemctl", "reset-failed", unit], capture_output=True)
    subprocess.run(["systemd-run", f"--unit={unit}", f"--working-directory={ROOT}", "/bin/sh", "-c",
                    f"{ROOT}/.venv/bin/python -c \"{code}\" >> {LOG} 2>&1"], check=True, capture_output=True)
    wait_done(unit)


def _main():
    say("delta loop started: waiting for the fresh import to finish")
    wait_done("bitport-dms-fresh")
    prev = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        n, status = failed_count()
        say(f"after attempt {attempt - 1}: console status={status} failed={n}")
        if n == 0 and status.lower().startswith("complete"):
            say("DONE: the DMS reports zero failed emails")
            return 0
        if prev is not None and n >= prev and attempt > 2:
            msg = f"DMS delta is not reducing failures ({prev} -> {n}) after {attempt - 1} attempts; needs Claude"
            say(msg)
            with open(INBOX, "a", encoding="utf-8") as fh:
                fh.write(f"- {time.strftime('%Y-%m-%d %H:%MZ', time.gmtime())} NEEDS CLAUDE: {msg}\n")
            return 2
        if n < 0 or not status.lower().startswith("complete"):
            say("HOLD: DMS completion/failure count is unknown; no retry admitted")
            return 2
        prev = n
        say(f"delta attempt {attempt}")
        run_delta(attempt)
    n, status = failed_count()
    say(f"stopped after {MAX_ATTEMPTS} attempts: failed={n} status={status}")
    with open(INBOX, "a", encoding="utf-8") as fh:
        fh.write(f"- {time.strftime('%Y-%m-%d %H:%MZ', time.gmtime())} NEEDS CLAUDE: DMS still has {n} failed after {MAX_ATTEMPTS} deltas\n")
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-id", type=int, default=ACCOUNT or None, required=not ACCOUNT)
    ap.add_argument("--identities", help="the identity CSV the fresh import used "
                                         "(default: logs/dms/<id>/identities.csv)")
    args = ap.parse_args(argv)
    configure(args.account_id, args.identities)
    # A scheduler restart cannot create a second loop beside this one.
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    with open(LOCK, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            say("HOLD: another DMS delta scheduler owns the account")
            return 2
        return _main()


if __name__ == "__main__":
    sys.exit(main())
