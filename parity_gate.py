#!/usr/bin/env python3
"""The parity gate: one command, one verdict for a migration (the 10/10 plan's acceptance test).

    python parity_gate.py --account-id 3 [--json]      (or --db path/to/migration.db)

PARITY       every user's every service was checked and is identical, or what differs is ACCEPTED
             (no API can carry it, or the operator accepted it in known_differences.json);
NOT_PARITY   something differs that is not accepted -- each one named, with whether Bitport repairs it
             on its own (auto) or a person must act;
UNVERIFIED   a user/service was never checked, or its check could not finish. Never a pass.
Exit 0 / 1 / 2 in that order. Reads the ledger only; nothing is written anywhere.

What was not migrated on purpose is listed with its reason (the explained remainder) and does not
fail the gate: Google's own import refusals, files no API can copy, grants to deleted accounts.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICES = ("drive", "gmail", "calendar", "contacts", "tasks", "directory")

# Kinds no API can carry, each named in the final report (known_issues says why for every one).
ACCEPTED_KINDS = {"chat.attributed", "chat.space_type", "chat.members", "chat.sender", "chat.not_migrated",
                  "drive.form_relinked", "drive.ab_test_residue", "gmail.link_scan_encrypted"}
# Audit statuses that are a decision with a reason, not a failure.
REMAINDER = {
    "SKIPPED_UNEXPORTABLE": "no API can copy it (Sites, My Maps, Jamboards); listed for a person",
    "SKIPPED_UNMAPPED_IDENTITY": "a sharing grant to a deleted account (no email to map)",
    "SKIPPED_GRANTEE_NOT_ON_GOOGLE": "a share with someone who has no Google account",
    "LINK_SCAN_ENCRYPTED": "an encrypted email: its links cannot be read, so it went through Google's import as it is",
}


def _rows(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return None


def _accepted_by_operator(finding: dict, accepted: list[dict]) -> dict | None:
    for a in accepted:
        if a.get("sourceUser") in (None, finding["user"]) and a.get("item") and a["item"] in finding["detail"]:
            return a
    return None


def assess(conn, accepted: list[dict] | None = None, dms_failed: int | None = None) -> dict:
    import known_issues
    conn.row_factory = sqlite3.Row
    accepted = accepted or []
    auto = {i[0] for i in known_issues.ISSUES if i[6]}
    users = [r[0] for r in (_rows(conn, "SELECT source_email FROM identity_map WHERE entity_type='user'") or [])]
    checks = {(r["source_user"], r["service"]): r for r in
              (_rows(conn, "SELECT source_user, service, verdict, verified_at FROM user_verification") or [])}
    opened = {}
    for r in _rows(conn, "SELECT source_user, service, category, item_id, detail, issue FROM verification_repair_queue "
                         "WHERE state!='RESOLVED'") or []:
        kind = r["issue"] if r["issue"] and r["issue"] != "unclassified" else known_issues.classify(r["service"], r["detail"])
        opened.setdefault((r["source_user"], r["service"]), []).append(
            {"user": r["source_user"], "service": r["service"], "category": r["category"], "item": r["item_id"],
             "kind": kind, "detail": str(r["detail"])[:400]})

    unverified, differs, accepted_out, per_user = [], [], [], {}
    for user in sorted(users):
        state = "PARITY"
        for svc in SERVICES:
            row = checks.get((user, svc))
            verdict = row["verdict"] if row else "NOT_VERIFIED"
            if verdict == "IDENTICAL":
                continue
            if verdict not in ("DIFFERENCES",):
                unverified.append({"user": user, "service": svc, "verdict": verdict})
                state = "UNVERIFIED" if state == "PARITY" else state
                continue
            found = opened.get((user, svc), [])
            if not found:       # a difference with no named finding is not explained
                differs.append({"user": user, "service": svc, "kind": "unnamed", "how": "person",
                                "detail": "the check found differences but recorded no finding"})
                state = "NOT_PARITY"
            for f in found:
                op = _accepted_by_operator(f, accepted)
                if f["kind"] in ACCEPTED_KINDS or op:
                    accepted_out.append({**f, "why": (op or {}).get("why") or "no API can carry it (known_issues)"})
                    continue
                differs.append({**f, "how": "auto" if f["kind"] in auto else "person"})
                state = "NOT_PARITY"
        per_user[user] = state

    remainder = [{"status": r[0], "count": r[1], "why": REMAINDER[r[0]]} for r in
                 _rows(conn, "SELECT status, COUNT(*) FROM audit_log WHERE status IN (%s) GROUP BY status"
                       % ",".join("?" * len(REMAINDER)), tuple(REMAINDER)) or []]
    if dms_failed:
        remainder.append({"status": "DMS_FAILED", "count": dms_failed,
                          "why": "Google's Data Migration Service refused these emails; the console names no ids"})
    failed = [dict(r) for r in _rows(conn, "SELECT source_user, item_type, status, substr(error_message,1,160) AS error "
                                           "FROM audit_log WHERE status LIKE 'FAIL%' LIMIT 200") or []]
    for f in failed:
        differs.append({"user": f["source_user"], "service": f["item_type"], "kind": "run.failed", "how": "auto",
                        "detail": f["error"] or f["status"]})

    verdict = ("UNVERIFIED" if unverified or not users else "NOT_PARITY" if differs else "PARITY")
    return {"verdict": verdict, "users": len(users),
            "usersAtParity": sum(1 for s in per_user.values() if s == "PARITY"),
            "perUser": per_user, "unverified": unverified, "differences": differs,
            "accepted": accepted_out, "remainder": remainder}


def _dms_failed(account_id) -> int | None:
    try:
        with open(HERE / "logs" / "dms" / str(account_id) / "metrics.json") as f:
            return int((json.load(f).get("metrics") or {}).get("Emails failed") or 0)
    except (OSError, ValueError, TypeError):
        return None


def report(result: dict) -> str:
    out = [f"PARITY GATE: {result['verdict']}  ({result['usersAtParity']}/{result['users']} users at parity)"]
    if result["unverified"]:
        out.append(f"\nNot verified ({len(result['unverified'])}):")
        out += [f"  {u['user']} {u['service']}: {u['verdict']}" for u in result["unverified"][:60]]
    if result["differences"]:
        by = {}
        for d in result["differences"]:
            by.setdefault((d["kind"], d["how"]), []).append(d)
        out.append(f"\nDifferences that fail the gate ({len(result['differences'])}), by kind:")
        for (kind, how), ds in sorted(by.items(), key=lambda x: -len(x[1])):
            users = sorted({d["user"].split("@")[0] for d in ds})
            out.append(f"  {len(ds):>6}  {kind}  [{'Bitport repairs it' if how == 'auto' else 'a person must act'}]  "
                       f"users: {', '.join(users[:8])}{' ...' if len(users) > 8 else ''}")
    if result["accepted"]:
        out.append(f"\nAccepted differences ({len(result['accepted'])}):")
        out += [f"  {a['user'].split('@')[0]} {a['service']} {a['kind']}: {a['why'][:120]}" for a in result["accepted"][:30]]
    if result["remainder"]:
        out.append("\nNot migrated, with the reason:")
        out += [f"  {r['count']:>6}  {r['status']}: {r['why']}" for r in result["remainder"]]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--db")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    db = args.db or (HERE / "data" / "accounts" / str(args.account_id) / "migration.db" if args.account_id
                     else os.getenv("MIGRATION_DB", "migration.db"))
    try:
        accepted = json.loads((HERE / "known_differences.json").read_text()).get("accepted", [])
    except (OSError, ValueError):
        accepted = []
    conn = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        result = assess(conn, accepted, _dms_failed(args.account_id) if args.account_id else None)
    finally:
        conn.close()
    print(json.dumps(result, indent=1, ensure_ascii=False) if args.json else report(result))
    return {"PARITY": 0, "NOT_PARITY": 1}.get(result["verdict"], 2)


if __name__ == "__main__":
    sys.exit(main())
