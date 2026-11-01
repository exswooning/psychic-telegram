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
    ap.add_argument("--account-id", type=int, required=True)
    ap.add_argument("--user", action="append", help="default: " + ", ".join(DEFAULT_USERS))
    args = ap.parse_args(argv)
    from config import Settings
    domain = Settings(account_id=args.account_id).source_domain
    users = [u if "@" in u else f"{u}@{domain}" for u in (args.user or DEFAULT_USERS)]
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    folder = os.path.join(OUT, stamp)
    os.makedirs(folder, exist_ok=True)
    started = time.time()
    with ThreadPoolExecutor(len(users)) as ex:
        results = dict(zip(users, ex.map(lambda u: run_one(args.account_id, u, folder), users)))
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
