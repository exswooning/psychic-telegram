"""
regression.py
=============
The fixed regression set: a few source users who between them carry every problem kind seen
live, each migrated into a brand-new target account the way a full run moves them
(server_side + split mail + Google's import), then checked one to one and the differences
counted by kind. Each run is compared with the last, so a change that breaks parity -- in the
copy or in the checker -- shows up the day it is made, not on a customer's tenant.

    python regression.py --account-id 3                # the default set
    python regression.py --account-id 3 --user a@... --user b@...

Each user is one `ab_transfer.py` run (its own report and progress file); they run side by
side, and Google's mail imports queue behind one another on dms_migrate's own lock. A user
never mapped before is left unmapped afterwards (ab_transfer's own rule). Always exits 0:
whether the check RAN, never what it found -- run_watch reads non-zero as a crash.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "logs", "regression")

# Who carries what (sandbox s.sarafgloabalexim.com, 2026-11-01):
#   esc.mrc   Forms (rebuilt and compared by body; a Form has no Drive comments)
#   audit     bounce reports (multipart with no text part) among link mail
#   finance   two comments with the same text on one PDF; Sheets with links (per-user read quota)
#   livestock small, plain -- a control: anything here is a regression in the common path
DEFAULT_USERS = ("esc.mrc", "audit", "finance", "livestock")


def _account_from_ledger() -> int | None:
    """A web UI launch points MIGRATION_DB at data/accounts/<id>/migration.db (webui._account_env)."""
    import re
    m = re.search(r"accounts/(\d+)/migration\.db$", os.getenv("MIGRATION_DB", ""))
    return int(m.group(1)) if m else None


# Three real steps per user, read from its own ab_transfer progress file -- never a guess from
# time. Stages before the migration finishes count 0; Google's import running means the
# migration is done (1); the snapshot and the 1:1 check mean the import is done (2); a finished
# run is 3.
STEPS_DONE = {"dms import": 1, "snapshot target": 2, "verify 1:1": 2}
STEPS_PER_USER = 3
HEARTBEAT_SEC = 300      # well inside job_supervisor.STALL_SECONDS (900)


def user_steps(progress: dict | None) -> tuple[int, str]:
    """(steps finished, what it is doing) for one user's run."""
    if not progress:
        return 0, "waiting to start"
    if not progress.get("running") and progress.get("finishedAt"):
        return STEPS_PER_USER, "finished"
    stage = ((progress.get("current") or {}).get("stage")) or progress.get("phase") or "starting"
    return STEPS_DONE.get(stage, 0), stage


def progress_line(states: dict[str, dict | None]) -> str:
    """'[5/12] steps · esc.mrc: verify 1:1 · audit: dms import · ...' -- webui turns the counter
    into the card's progress bar."""
    parts, done = [], 0
    for user, st in states.items():
        n, doing = user_steps(st)
        done += n
        parts.append(f"{user.partition('@')[0]}: {doing}")
    return f"[{done}/{STEPS_PER_USER * len(states)}] steps · " + " · ".join(parts)


def summarise(report: dict) -> dict:
    """One user's ab_transfer report -> verdict per service, failures, seconds, kinds counted."""
    runs = report.get("runs") or []
    if not runs:
        return {"ran": False}
    r = runs[-1]
    kinds: dict[str, int] = {}
    for d in (r.get("differences") or {}).values():
        for k in d.get("kinds") or []:
            kinds[k["kind"]] = kinds.get(k["kind"], 0) + int(k.get("count") or 0)
    return {"ran": True, "verdicts": {k.split("/", 1)[-1]: v for k, v in (r.get("verdicts") or {}).items()},
            "failed": r.get("failed"), "seconds": r.get("seconds"), "dms": r.get("dms"),
            "exit": r.get("exit"), "kinds": kinds}


def compare(now: dict, before: dict | None) -> list[str]:
    """What got worse since the last run: a verdict that was IDENTICAL and is not, a kind that is
    new or grew, more failures, or a user that no longer ran. Better results are not listed."""
    if not before:
        return []
    out = []
    for user, s in now.items():
        b = (before.get(user) or {})
        if not b.get("ran"):
            continue
        if not s.get("ran"):
            out.append(f"{user}: did not run")
            continue
        for svc, v in (b.get("verdicts") or {}).items():
            if v == "IDENTICAL" and s["verdicts"].get(svc) != "IDENTICAL":
                out.append(f"{user}/{svc}: IDENTICAL -> {s['verdicts'].get(svc)}")
        for kind, n in s["kinds"].items():
            was = (b.get("kinds") or {}).get(kind, 0)
            if n > was:
                out.append(f"{user}: {kind} {was} -> {n}")
        if (s.get("failed") or 0) > (b.get("failed") or 0):
            out.append(f"{user}: failed items {b.get('failed') or 0} -> {s['failed']}")
    return out


def run_one(account_id: int, user: str, folder: str) -> dict:
    name = user.partition("@")[0]
    report = os.path.join(folder, f"{name}.md")
    env = {**os.environ, "AB_PROGRESS": os.path.join(folder, f"{name}.progress.json")}
    argv = [sys.executable, "ab_transfer.py", "--account-id", str(account_id), "--mode", "server_side",
            "--mail-mode", "split", "--user", user, "--report", report]
    with open(os.path.join(folder, f"{name}.log"), "w") as log:
        rc = subprocess.call(argv, cwd=HERE, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        with open(report + ".json") as fh:
            got = summarise(json.load(fh))
    except (OSError, ValueError):
        got = {"ran": False}
    got["rc"] = rc
    return got


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--account-id", type=int, default=_account_from_ledger(),
                    help="default: the account whose ledger MIGRATION_DB names (a web UI launch)")
    ap.add_argument("--user", action="append", help="default: " + ", ".join(DEFAULT_USERS))
    args = ap.parse_args(argv)
    if args.account_id is None:
        ap.error("which account? pass --account-id (a web UI launch names it through MIGRATION_DB)")
    from config import Settings
    domain = Settings(account_id=args.account_id).source_domain
    users = [u if "@" in u else f"{u}@{domain}" for u in (args.user or DEFAULT_USERS)]
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    folder = os.path.join(OUT, stamp)
    os.makedirs(folder, exist_ok=True)
    started = time.time()
    print(f"Regression set: {len(users)} users, server_side + split, into new target accounts", flush=True)
    import threading
    finished = threading.Event()

    def report_progress():
        # Printed when anything moves, and at least every HEARTBEAT_SEC regardless: Google's
        # import can hold one stage for an hour, and a job that says nothing for 15 minutes while
        # using no CPU of its own is ended as wedged (job_supervisor; incident #8, 2026-11-01).
        last, said = None, 0.0
        while True:
            states = {}
            for u in users:
                try:
                    with open(os.path.join(folder, f"{u.partition('@')[0]}.progress.json")) as fh:
                        states[u] = json.load(fh)
                except (OSError, ValueError):
                    states[u] = None
            line = progress_line(states)
            if line != last or time.time() - said >= HEARTBEAT_SEC:
                print(line, flush=True)
                last, said = line, time.time()
            if finished.wait(20):
                return

    watcher = threading.Thread(target=report_progress, daemon=True)
    watcher.start()

    def one(u):
        got = run_one(args.account_id, u, folder)
        print(f"{u}: finished, {'ran' if got.get('ran') else 'did not run'}"
              f"{'' if got.get('seconds') is None else f' in {got['seconds'] / 60:.0f} min'}"
              f", failed={got.get('failed')}, verdicts={got.get('verdicts')}", flush=True)
        return got

    with ThreadPoolExecutor(len(users)) as ex:
        results = dict(zip(users, ex.map(one, users)))
    finished.set()
    watcher.join(5)
    latest = os.path.join(OUT, "latest.json")
    try:
        with open(latest) as fh:
            before = json.load(fh).get("users")
    except (OSError, ValueError):
        before = None
    worse = compare(results, before)
    summary = {"startedAt": stamp, "seconds": round(time.time() - started), "users": results,
               "regressions": worse, "comparedWith": (before is not None)}
    for path in (os.path.join(folder, "summary.json"), latest):
        with open(path, "w") as fh:
            json.dump(summary, fh, indent=2)
    for u, s in results.items():
        print(f"{u}: {'ran' if s.get('ran') else 'DID NOT RUN'} failed={s.get('failed')} "
              f"seconds={s.get('seconds')} verdicts={s.get('verdicts')} kinds={s.get('kinds')}", flush=True)
    print("REGRESSIONS: " + ("; ".join(worse) if worse else "none" if before else "none (first run, nothing to compare)"),
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
