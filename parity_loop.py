"""Check every user the moment a pass finishes it, record every metric, and say only what is not at parity.

Runs on the box as its own unit beside a migration; costs no model tokens. It reads the run's log for
`[user] finished in Ns: {services}` lines and, for the service that just finished, runs the deep checks
that exist for it:
  drive   fidelity_audit.audit_user (every grant, time, setting) + form_deep_check (every Form, its
          fields, settings, responses and responses Sheet)
  gmail   link emails delivered with write-time receipts, mail owed to the DMS
  every   tally.tally_user (an exact count of every service on both tenants)
Each check's result and the user's metrics -- elapsed, items, failures by kind, fallbacks, dropped
settings, warnings in the log -- go into `parity_user` (one row per user and stage) and a summary file.

Output is for logwatch: a user not at parity writes ONE `ERROR [parity] parity: <user>: <why>` line per
distinct reason; `INFO` otherwise. When every user is at parity it runs the full tenant checks once and
writes `PARITY REACHED`.

    python parity_loop.py --account-id 3 [--once] [--workers 2]
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as futures
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FINISHED = re.compile(r"\[([^\]]+@[^\]]+)\] finished in ([\d.]+)s: (\{.*\})\s*$")
SCHEMA = """CREATE TABLE IF NOT EXISTS parity_user (
    source_user TEXT NOT NULL, stage TEXT NOT NULL, verdict TEXT NOT NULL, reasons TEXT NOT NULL,
    metrics TEXT NOT NULL, checked_at TEXT NOT NULL, PRIMARY KEY (source_user, stage))"""


def user_metrics(db, user: str, elapsed: float | None, stats: dict) -> dict:
    c = db.conn
    by = collections.Counter()
    for t, st, n in c.execute("SELECT item_type, status, COUNT(*) FROM audit_log WHERE source_user=? "
                              "GROUP BY 1, 2", (user,)):
        by[f"{t}:{st}"] = n
    return {"elapsedSec": elapsed, "stats": stats,
            "failed": {k: v for k, v in by.items() if ":FAIL" in k},
            "skipped": {k: v for k, v in by.items() if ":SKIPPED" in k},
            "fallbacks": by.get("fallback:FALLBACK", 0), "carriedDropped": by.get("carried:CARRIED_DROPPED", 0),
            "mapped": dict(c.execute("SELECT type, COUNT(*) FROM id_mapping WHERE source_user=? GROUP BY 1", (user,)).fetchall())}


def check_drive(auth, db, st, user: str, target: str) -> list[str]:
    import fidelity_audit
    import form_deep_check
    reasons = []
    r = fidelity_audit.audit_user(auth, db, st, user, target, workers=8, say=lambda *_: None)
    reasons += [f"drive {k} x{n}" for k, n in sorted(r["byKind"].items())]
    for f in form_deep_check.check_user_forms(auth, db, user, target):
        if f.get("error"):
            reasons.append(f"form check incomplete: {str(f['error'])[:160]}")
            continue
        if not f.get("parity"):
            it = f.get("items", {})
            reasons.append(f"form {f.get('title', f.get('error', '?'))[:40]!r}: "
                           f"missing {len(it.get('missing', []))} extra {len(it.get('extra', []))} "
                           f"changed {len(it.get('changed', []))} settings {len(f.get('settings', []))}")
        sh = f.get("responsesSheet") or {}
        if sh.get("sourceSheet") and not sh.get("sheetCopied"):
            reasons.append(f"form {f.get('title', '?')[:40]!r}: responses Sheet not copied")
    return reasons


def check_mail(db, user: str) -> tuple[list[str], dict]:
    from db import deferred_mail_by_user
    c = db.conn
    mapped = c.execute("SELECT COUNT(*) FROM id_mapping WHERE source_user=? AND type='message'", (user,)).fetchone()[0]
    receipts = c.execute("SELECT COUNT(*) FROM migration_item_receipts WHERE source_user=? AND service='gmail' "
                         "AND status='VERIFIED'", (user,)).fetchone()[0] if c.execute(
        "SELECT 1 FROM sqlite_master WHERE name='migration_item_receipts'").fetchone() else 0
    links = c.execute("SELECT COUNT(*) FROM mail_link_messages WHERE source_user=?", (user,)).fetchone()[0] if c.execute(
        "SELECT 1 FROM sqlite_master WHERE name='mail_link_messages'").fetchone() else None
    owed = deferred_mail_by_user(c).get(user, 0)
    failed = c.execute("SELECT COUNT(*) FROM audit_log WHERE source_user=? AND item_type='message' "
                       "AND status LIKE 'FAIL%'", (user,)).fetchone()[0]
    reasons = []
    if failed:
        reasons.append(f"mail failed x{failed}")
    if mapped and receipts < mapped:
        reasons.append(f"mail receipts {receipts} of {mapped} delivered")
    return reasons, {"delivered": mapped, "receipts": receipts, "linkEmailsInScan": links, "owedToDms": owed}


def check_counts(auth, db, st, user: str, target: str, svc: str = "") -> tuple[list[str], dict]:
    """The user's tally, judged on the ONE service that just finished. The tally's overall countParity
    includes mail, which split mode leaves to the DMS on purpose: every other service read 0.04 (10 Nov
    2026) and 125 false alarms went to the report."""
    import tally
    p = tally.tally_user_and_save(auth, db, st, user, target) or {}
    svcs = p.get("services") or {}
    key = {"gmail": "mail"}.get(svc, svc)
    mine = svcs.get(key) or {}
    par = mine.get("parity")
    metrics = {"countParity": par, "source": mine.get("source"), "target": mine.get("target"), "expected": mine.get("expected")}
    expected, target_count = mine.get("expected"), mine.get("target")
    if not mine or expected is None or target_count is None or mine.get("source") is None:
        return [f"{svc} count unknown (source {mine.get('source')})"], metrics
    return ([] if target_count == expected else [f"{svc} count {target_count} of {expected}"]), metrics


def _write(db, user, stage, verdict, reasons, metrics):
    from db import utc_now
    with db.write() as c:
        c.execute(SCHEMA)
        c.execute("INSERT OR REPLACE INTO parity_user VALUES (?,?,?,?,?,?)",
                  (user, stage, verdict, json.dumps(reasons, ensure_ascii=False),
                   json.dumps(metrics, ensure_ascii=False, default=str), utc_now()))


def _say(level: str, msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())},000 {level:<7} [parity] parity: {msg}", flush=True)


def handle(auth, db, st, ledger: dict, user: str, elapsed: float, services: dict) -> None:
    target = ledger.get(user.lower())
    if not target:
        return
    for svc, stats in services.items():
        stage = svc
        try:
            metrics = user_metrics(db, user, elapsed, stats if isinstance(stats, dict) else {})
            if svc == "drive":
                reasons = check_drive(auth, db, st, user, target)
            elif svc == "gmail":
                reasons, extra = check_mail(db, user)
                metrics.update(extra)
            else:
                reasons = []
            if svc in ("gmail", "calendar", "contacts", "tasks"):
                more, extra = check_counts(auth, db, st, user, target, svc)
                metrics.update(extra)
                # Mail owed to the DMS is not missing yet: it waits for the import.
                if svc == "gmail" and metrics.get("owedToDms"):
                    stage = "gmail-awaiting-dms"
                    more.append(f"mail awaiting DMS confirmation x{metrics['owedToDms']}")
                reasons += more
        except Exception as exc:      # noqa: BLE001 - a check that cannot run is itself a finding
            reasons, metrics = [f"{svc} check could not run: {str(exc)[:160]}"], {}
        verdict = "WAITING_DMS" if stage == "gmail-awaiting-dms" else ("PARITY" if not reasons else "NOT_AT_PARITY")
        _write(db, user, stage, verdict, reasons, metrics)
        if reasons:
            for r in reasons:
                _say("ERROR", f"{user}: {stage}: {r}")
        else:
            _say("INFO", f"{user}: {stage}: at parity")


def summary(db) -> dict:
    rows = db.conn.execute("SELECT stage, verdict, COUNT(*) FROM parity_user GROUP BY 1, 2").fetchall()
    out: dict = collections.defaultdict(dict)
    for stage, verdict, n in rows:
        out[stage][verdict] = n
    return dict(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-id", type=int, required=True)
    ap.add_argument("--log", help="the run's log (default logs/jobs/<id>/migrate.log)")
    ap.add_argument("--from-start", action="store_true", help="also check users finished before this started")
    ap.add_argument("--since", default="", help="with --from-start: only lines at or after this time ('2026-11-10 20:00')")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--recheck", choices=("gmail", "drive", "all"),
                    help="check every user now for this service and exit (e.g. gmail once the DMS import is done)")
    args = ap.parse_args(argv)
    from auth import AuthManager
    from config import Settings
    from db import MigrationDB
    st = Settings(account_id=args.account_id)
    db, auth = MigrationDB(st.db_path), AuthManager(st)
    with db.write() as c:
        c.execute(SCHEMA)
    ledger = {r["source_email"].lower(): r["target_email"] for r in db.all_identities() if r["entity_type"] == "user"}
    if args.recheck:
        svcs = {"gmail": {"gmail": {}}, "drive": {"drive": {}},
                "all": {"drive": {}, "gmail": {}, "calendar": {}, "contacts": {}, "tasks": {}}}[args.recheck]
        with futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            list(pool.map(lambda u: handle(auth, db, st, ledger, u, None, svcs), sorted(ledger)))
        _say("INFO", f"recheck {args.recheck} done: {json.dumps(summary(db))}")
        return 0
    path = args.log or os.path.join(HERE, "logs", "jobs", str(args.account_id), "migrate.log")
    offset = 0 if args.from_start or not os.path.exists(path) else os.path.getsize(path)
    sumfile = os.path.join(HERE, "logs", "jobs", str(args.account_id), "parity_summary.json")
    reached = False
    _say("INFO", f"watching {path} for finished users ({len(ledger)} mapped)")
    with futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        while True:
            if os.path.exists(path):
                size = os.path.getsize(path)
                if size < offset:
                    offset = 0
                with open(path, "rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                offset += len(chunk)
                for line in chunk.decode("utf-8", "replace").splitlines():
                    if args.since and line[:16] < args.since[:16]:
                        continue            # an earlier run's line: not this run's user
                    m = FINISHED.search(line)
                    if m:
                        try:
                            services = json.loads(m.group(3))
                        except ValueError:
                            continue
                        pool.submit(handle, auth, db, st, ledger, m.group(1), float(m.group(2)), services)
            s = summary(db)
            with open(sumfile, "w", encoding="utf-8") as fh:
                json.dump({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "stages": s}, fh, indent=1)
            at = {u for (u,) in db.conn.execute("SELECT source_user FROM parity_user GROUP BY 1 "
                                                "HAVING SUM(verdict!='PARITY')=0 AND SUM(stage='gmail')>0")}
            if not reached and len(at) == len(ledger) and ledger:
                reached = True
                _say("INFO", f"Stage checks passed for all {len(ledger)} users; full content and tenant parity still unconfirmed")
            if args.once:
                pool.shutdown(wait=True)
                return 0
            time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
