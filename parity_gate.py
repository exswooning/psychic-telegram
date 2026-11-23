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

# Which service an audit row's item type belongs to (account 3's ledger, 15 Nov 2026). Unknown types make
# every service of that user stale: a change nobody can place is a change to all of it.
SERVICE_OF = {**dict.fromkeys(("file", "folder", "acl", "comment", "shortcut", "fallback", "transfer_route",
                               "link_rewrite", "shared_drive", "shared_drive_member", "form_link", "drive",
                               "acl_pass", "links_pass", "carried", "flagged_file", "source_change",
                               "source_link_reference", "staging_cleanup"), "drive"),
              # A type missing here reads as a change to EVERY service: the DMS check's own duplicate trash made
              # five users' older directory checks STALE (a. -> b., 18 Nov 2026). tests/test_parity_gate.py
              # fails on a type the code writes that is in neither this map nor EVERY_SERVICE.
              **dict.fromkeys(("message", "message_link", "draft", "filter", "signature", "gmail", "label",
                               "dms_duplicate", "dms_draft_duplicate", "link_repair", "delegate", "forwarding", "reset_link_recovery"), "gmail"),
              **dict.fromkeys(("event", "calendar", "calendar_acl", "calendar_subscription", "resource"), "calendar"),
              **dict.fromkeys(("contact", "contact_group"), "contacts"),
              **dict.fromkeys(("task", "tasklist", "task_list"), "tasks"),
              **dict.fromkeys(("group", "group_member", "profile", "profile_photo", "sso_assignment", "sso_profile"),
                              "directory"),
              **dict.fromkeys(("chat_member", "chat_message", "chat_space"), "chat")}
# A whole-user failure (main.migrate_user): it does touch every service.
EVERY_SERVICE = {"user"}
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
    import domain_guard
    users = [r[0] for r in (_rows(conn, "SELECT source_email FROM identity_map WHERE entity_type='user'") or [])]
    aid = domain_guard.ledger_account((_rows(conn, "PRAGMA database_list") or [(0, "", "")])[0][2])
    users = [u for u in users if not domain_guard.hidden(u, aid)]      # another account's protected users
    checks = {(r["source_user"], r["service"]): r for r in
              (_rows(conn, "SELECT source_user, service, verdict, verified_at, payload FROM user_verification") or [])}
    # As of: a check older than the user's last write by the run or the DMS reconciliation describes a state
    # that no longer exists, so it cannot pass (the source is live; a verdict needs its moment).
    last_write = {}       # (user, service) -> last change; service None = an item type that names no service

    def note(user, service, t):
        if t and t > last_write.get((user, service), ""):
            last_write[(user, service)] = t
    for u, kind, t in _rows(conn, "SELECT source_user, item_type, MAX(timestamp) FROM audit_log "
                                  "GROUP BY source_user, item_type") or []:
        note(u, SERVICE_OF.get(kind), t)
    for u, t in _rows(conn, "SELECT source_user, MAX(checked_at) FROM mail_reconciliation_items GROUP BY source_user") or []:
        note(u, "gmail", t)
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
            changed = max(last_write.get((user, svc), ""), last_write.get((user, None), ""))
            if row and (row["verified_at"] or "") < changed:
                verdict = f"STALE (checked {row['verified_at']}, changed {changed})"
            if verdict == "IDENTICAL":
                continue
            if verdict != "DIFFERENCES":
                unverified.append({"user": user, "service": svc, "verdict": verdict})
                state = "UNVERIFIED" if state == "PARITY" else state
                continue
            found = opened.get((user, svc), [])
            if not found:       # no queued finding: name it from the check's own result, or call it unexplained
                try:
                    saved = json.loads(row["payload"] or "{}")
                except (TypeError, ValueError):
                    saved = {}
                named = [k for k in ("missing", "extras", "differences", "duplicates", "notCopied", "errors")
                         if saved.get(k)]
                for k in named or ["unnamed"]:
                    n = len(saved[k]) if k in saved and isinstance(saved[k], list) else saved.get(k)
                    differs.append({"user": user, "service": svc, "kind": f"{svc}.{k}" if named else "unnamed",
                                    "how": "person", "detail": f"{n} in the saved check" if named else
                                    "the check found differences but recorded no finding"})
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


def _accepted_list() -> list[dict]:
    try:
        return json.loads((HERE / "known_differences.json").read_text()).get("accepted", [])
    except (OSError, ValueError):
        return []


def record(account_id: int, db_path: str) -> dict:
    """Run the gate for an account and put the verdict where an operator looks: the report folder
    (parity-gate-<time>.json and parity-gate-latest.json) and an incident while it is not PARITY, closed by
    the first PARITY verdict. Called by api_server once the verify after the DMS exits cleanly."""
    import datetime
    import run_watch
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        result = assess(conn, _accepted_list(), _dms_failed(account_id))
    finally:
        conn.close()
    result["asOf"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = HERE / "logs" / "reports" / str(account_id)
    out.mkdir(parents=True, exist_ok=True)
    text = json.dumps(result, indent=1, ensure_ascii=False)
    (out / f"parity-gate-{result['asOf'].replace(':', '')}.json").write_text(text)
    (out / "parity-gate-latest.json").write_text(text)
    fingerprint = f"parity_gate:{account_id}"
    if result["verdict"] == "PARITY":
        for inc in run_watch.list_incidents("open", account_id=account_id):
            if inc.get("fingerprint") == fingerprint:
                run_watch.resolve(inc["id"], f"the parity gate read PARITY at {result['asOf']}")
    else:
        run_watch.open_incident(
            kind="parity_gate", title=f"Not at parity: {result['verdict']}",
            summary=report(result)[:1500], account_id=account_id, job_name="verify",
            fingerprint=fingerprint, severity="warn", brief_text=report(result))
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--db")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    db = args.db or (HERE / "data" / "accounts" / str(args.account_id) / "migration.db" if args.account_id
                     else os.getenv("MIGRATION_DB", "migration.db"))
    accepted = _accepted_list()
    conn = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        result = assess(conn, accepted, _dms_failed(args.account_id) if args.account_id else None)
    finally:
        conn.close()
    print(json.dumps(result, indent=1, ensure_ascii=False) if args.json else report(result))
    return {"PARITY": 0, "NOT_PARITY": 1}.get(result["verdict"], 2)


if __name__ == "__main__":
    sys.exit(main())
