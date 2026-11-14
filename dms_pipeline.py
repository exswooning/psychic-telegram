#!/usr/bin/env python3
"""The DMS after a split run, end to end -- the `dms` job api_server._start_dms launches.

1. decide who it may take (dms_handoff.assess) and RECORD it before anything starts:
   logs/dms/<account>/decision.json; a held user also opens an incident (dms_user_held) with its reason;
2. import for every ready user (dms_migrate.py --apply ... with an identity file of the ready users only);
3. CONFIRM the import started: this launch's own output must read "import status:" within
   DMS_START_CONFIRM_MIN (30) minutes -- a wait for the source admin's approval extends the window
   while it lasts. Never assumed, never read from an earlier run. Otherwise: stopped, incident;
4. delta: re-run the import (same map, no fresh import) while the console still reports failed emails,
   at most DMS_DELTA_ATTEMPTS (5) times, stopping with an incident when the count stops falling;
5. align the labels of delivered mail whose content the reconciliation proved (dms_label_alignment:
   no body is read; DMS_ALIGN_LABELS=0 switches it off).
Exit 0 once the import is finished -- zero failed, or a remainder a delta no longer lowers (an incident
names it) -- and api_server then starts the full verify with repair. An unknown result is never a finish.

    python dms_pipeline.py --account-id 3 --identities logs/dms/3/identities-45u-ab12.csv \
        [--watch 720 --until-done 4320 --timeout 200 --source-domain ... --target-admin ... --source-admin ...]
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
CONFIRMED = "import status:"
REMAINDER = 4      # the import finished and a delta no longer lowers its failed count: named, not unknown
STILL_WAITING = ("waiting for the source admin to approve", "another import is driving the console")


def _incident(**kw) -> None:
    try:
        import run_watch
        run_watch.open_incident(**kw)
    except Exception as exc:      # noqa: BLE001 - the decision stands even if it cannot be recorded
        print(f"could not record an incident: {exc}", flush=True)


def ready_identities(path: str, held: dict[str, str], out_path: str) -> int:
    """Copy the identity file without the held users; returns how many users it keeps."""
    lines = [l for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]
    header = lines[0] if lines and lines[0].lower().startswith("source_email") else None
    rows = [l for l in (lines[1:] if header else lines) if l.split(",")[0].strip().lower() not in held]
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(([header] if header else []) + rows) + "\n")
    return len(rows)


def run_confirmed(argv: list[str], confirm_sec: float, *, env=None, cwd=HERE) -> int:
    """Run the import, streaming its output; stop it unless it reports an import status in time.
    0 = it ran to its end after confirming; 3 = never confirmed (stopped); otherwise its own exit code."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env=env, cwd=cwd, bufsize=1)
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: [lines.put(l) for l in proc.stdout] and None, daemon=True).start()
    deadline, confirmed = time.monotonic() + confirm_sec, False
    while True:
        try:
            line = lines.get(timeout=0.5)
            print(line, end="", flush=True)
            if CONFIRMED in line:
                confirmed = True
            elif any(w in line for w in STILL_WAITING):
                deadline = time.monotonic() + confirm_sec      # a person or another import is the wait
        except queue.Empty:
            if proc.poll() is not None and lines.empty():
                break
        if not confirmed and time.monotonic() > deadline and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
            return 3
    rc = proc.wait()
    return 3 if not confirmed and rc == 0 else rc


def failed_count(metrics_path: str) -> tuple[int, str]:
    try:
        m = json.load(open(metrics_path))
        return int((m.get("metrics") or {}).get("Emails failed", -1)), str(m.get("status", "?"))
    except Exception:      # noqa: BLE001 - unknown is not zero
        return -1, "?"


def delta_until_zero(run, metrics_path: str, attempts: int, account_id) -> int:
    """`run()` runs one delta import and returns its exit code."""
    prev = None
    for attempt in range(1, attempts + 1):
        n, status = failed_count(metrics_path)
        print(f"dms: after attempt {attempt - 1}: status={status} failed={n}", flush=True)
        if n == 0 and status.lower().startswith("complete"):
            print("dms: DONE -- the DMS reports zero failed emails", flush=True)
            return 0
        if n < 0 or not status.lower().startswith("complete"):
            _incident(kind="dms_not_started", title="The DMS result could not be read",
                      summary=f"status={status} failed={n}; no further delta started for account {account_id}",
                      account_id=account_id, job_name="dms", fingerprint=f"dms_unknown:{account_id}", severity="warn")
            return 2
        if prev is not None and n >= prev:
            _incident(kind="dms_not_started", title="The DMS delta is not reducing failures",
                      summary=f"{prev} -> {n} failed emails after {attempt - 1} delta(s) for account {account_id}",
                      account_id=account_id, job_name="dms", fingerprint=f"dms_stuck:{account_id}", severity="warn")
            return REMAINDER
        prev = n
        print(f"dms: delta attempt {attempt} for {n} failed email(s)", flush=True)
        run()
    n, status = failed_count(metrics_path)
    if n == 0 and status.lower().startswith("complete"):
        return 0
    _incident(kind="dms_not_started", title="The DMS still has failed emails",
              summary=f"{n} failed after {attempts} deltas for account {account_id}", account_id=account_id,
              job_name="dms", fingerprint=f"dms_stuck:{account_id}", severity="warn")
    return REMAINDER if status.lower().startswith("complete") else 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-id", type=int, required=True)
    ap.add_argument("--identities", required=True)
    args, passthrough = ap.parse_known_args(argv)
    from auth import AuthManager
    from config import Settings
    import dms_handoff
    import dms_migrate
    st = Settings(account_id=args.account_id)
    out_dir = os.path.join(HERE, "logs", "dms", str(args.account_id))
    os.makedirs(out_dir, exist_ok=True)

    auth = AuthManager(st)
    with sqlite3.connect(f"file:{st.db_path}?mode=ro", uri=True) as conn:
        held, notes = dms_handoff.assess(conn, auth.source_gmail)
    ready_path = os.path.join(out_dir, "identities-ready.csv")
    ready = ready_identities(args.identities, held, ready_path)
    decision = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ready": ready,
                "held": held, "notes": notes, "identities": ready_path}
    with open(os.path.join(out_dir, "decision.json"), "w", encoding="utf-8") as fh:
        json.dump(decision, fh, indent=1)
    print(f"dms: DECISION ready={ready} held={held or 'none'} notes={notes or 'none'}", flush=True)
    for user, why in held.items():
        _incident(kind="dms_user_held", title=f"{user} held back from the DMS", summary=why,
                  account_id=args.account_id, job_name="dms", fingerprint=f"dms_user_held:{args.account_id}:{user}",
                  severity="warn")
    if not ready:
        print("dms: no user is ready; nothing started", flush=True)
        return 2

    # The account's own ledger, named outright: dms_migrate finds its account (and its metrics file) from it.
    env = {**os.environ, "MIGRATION_DB": st.db_path}
    base = [PY, "dms_migrate.py", "--apply", "--identities", ready_path, *passthrough]
    rc = run_confirmed(base, float(os.getenv("DMS_START_CONFIRM_MIN", "30")) * 60, env=env)
    if rc == 3:
        _incident(kind="dms_not_started", title="The DMS did not start",
                  summary=f"no import status within {os.getenv('DMS_START_CONFIRM_MIN', '30')} min; stopped",
                  account_id=args.account_id, job_name="dms", fingerprint=f"dms_not_started:{args.account_id}",
                  severity="warn")
        return 3
    if rc != 0:
        return rc
    delta = base + ([] if "--no-tally" in base else ["--no-tally"])
    rc = delta_until_zero(lambda: subprocess.call(delta, cwd=HERE, env=env),
                          dms_migrate.metrics_path(args.account_id),
                          int(os.getenv("DMS_DELTA_ATTEMPTS", "5")), args.account_id)
    if rc not in (0, REMAINDER):
        return rc
    # The import is finished. A few emails Google will not import (17 of 141,977 on account 3) are an open
    # incident and show as missing in the verify that follows; they must not hold that verify back for ever.
    if os.getenv("DMS_ALIGN_LABELS", "1").lower() not in ("0", "false", "no"):
        import dms_label_alignment
        from db import MigrationDB
        totals = dms_label_alignment.run(auth, MigrationDB(st.db_path), st,
                                         progress=lambda m: print(m, flush=True))
        print(f"dms: labels of delivered mail aligned: {json.dumps(totals)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
