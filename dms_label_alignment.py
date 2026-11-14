#!/usr/bin/env python3
"""Bring the labels of DMS-delivered mail into line with the source, without reading a message body.

Google's DMS copies a message's bytes faithfully but files it by its own rules: account 3, 14 Nov 2026 --
of 19,080 delivered messages reconciled so far, 7,133 (37%) had their content proven identical and only
their labels differed (INBOX missing on 3,446; CATEGORY_PERSONAL, CATEGORY_UPDATES or IMPORTANT added on
the rest). The general repair re-reads both full copies before it touches a label; on that box's 3 MB/s
line that is days. Here the content proof is the reconciliation's own receipt (contentProven), so only
labels are read (two small metadata reads) and one modify is written per message.

Writes labels on the TARGET only, and only for a message whose receipt proves its content, whose ledger
mapping still names that target copy, and whose source labels all map. Never on a source domain, never
in a dry run. A message it aligns gets its receipt set VERIFIED; anything else is left for the verify.

    python dms_label_alignment.py --account-id 3 [--user someone@source ...] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

NEVER_MOVED = {"SENT", "DRAFT", "CHAT"}      # Gmail owns these; the inline alignment leaves them too


def candidates(conn, users=None) -> dict[str, list[tuple[str, str]]]:
    """{source_user: [(source_id, target_id)]} whose receipt proves content and names only label drift."""
    out: dict[str, list[tuple[str, str]]] = {}
    wanted = {u.lower() for u in users} if users else None
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='migration_item_receipts'").fetchone():
        return out
    for user, sid, tid, evidence in conn.execute(
            "SELECT source_user, source_id, target_id, evidence FROM migration_item_receipts "
            "WHERE service='gmail' AND status='NEEDS_RECHECK'"):
        if wanted is not None and user.lower() not in wanted:
            continue
        try:
            proof = json.loads(evidence)
        except ValueError:
            continue
        if (proof.get("contentProven") is True and proof.get("labelDifferences")
                and not (proof.get("findings") or {}).get("errors")):
            out.setdefault(user, []).append((sid, tid))
    return out


def align_one(gm, source_id: str, target_id: str, dry_run: bool = False) -> tuple[str, dict]:
    """('aligned' | 'already' | 'would' | 'skipped' | 'differs', detail) for one message."""
    import verify_sample as V
    from gmail_engine import SYSTEM_LABELS
    if gm.db.get_target_id(gm.source_user, source_id, "message") != target_id:
        return "skipped", {"why": "the ledger no longer maps this message to that target copy"}
    read = lambda svc, mid, label: set(gm._retry(lambda: svc.users().messages().get(      # noqa: E731
        userId="me", id=mid, format="metadata", fields="id,labelIds").execute(), label=label).get("labelIds") or [])
    gm.limiter.acquire()
    source = read(gm.src, source_id, "gmail.dms_labels.source")
    label_map = gm.db.get_label_map(gm.source_user)
    if source - SYSTEM_LABELS - set(label_map):
        return "skipped", {"why": "a source label has no label on the target yet"}
    desired = set(gm._map_label_ids(sorted(source)))
    gm.limiter.acquire()
    current = read(gm.tgt, target_id, "gmail.dms_labels.target")
    if not V.compare_labels(desired, current):
        return "already", {}
    manageable = (SYSTEM_LABELS | set(label_map.values())) - NEVER_MOVED
    add, remove = sorted((desired - current) & manageable), sorted((current - desired) & manageable)
    if not (add or remove):
        return "differs", {"why": "the difference is in labels Gmail owns", "labels": V.compare_labels(desired, current)}
    if dry_run:
        return "would", {"added": add, "removed": remove}
    gm.limiter.acquire()
    gm._retry(lambda: gm.tgt.users().messages().modify(
        userId="me", id=target_id, body={"addLabelIds": add, "removeLabelIds": remove}).execute(),
        label="gmail.dms_labels.align")
    left = V.compare_labels(desired, read(gm.tgt, target_id, "gmail.dms_labels.target"))
    return ("differs" if left else "aligned"), {"added": add, "removed": remove, "labels": left}


def _record(db, user: str, source_id: str, detail: dict) -> None:
    from db import utc_now
    with db.write() as c:
        row = c.execute("SELECT evidence FROM migration_item_receipts WHERE source_user=? AND service='gmail' "
                        "AND source_id=?", (user, source_id)).fetchone()
        proof = json.loads(row[0]) if row else {}
        proof.update(labelDifferences=[], labelsAligned={**detail, "at": utc_now()})
        c.execute("UPDATE migration_item_receipts SET status='VERIFIED', evidence=? WHERE source_user=? "
                  "AND service='gmail' AND source_id=?", (json.dumps(proof), user, source_id))


def run(auth, db, settings, users=None, progress=print, workers: int = 8) -> dict:
    from domain_guard import require_not_source
    from gmail_engine import GmailMigrator
    from resilience import RateLimiter
    from split_mail import reconcile_tuning
    totals: dict = {"aligned": 0, "already": 0, "would": 0, "skipped": 0, "differs": 0, "errors": 0, "users": 0}
    dry = bool(getattr(settings, "dry_run", False))
    todo = candidates(db.conn, users)
    for user, rows in sorted(todo.items()):
        target = db.resolve_identity(user)
        if not target:
            continue
        require_not_source(target.rsplit("@", 1)[-1], settings.source_domain)
        base = GmailMigrator(auth, db, settings, user, target)
        base.limiter = RateLimiter(reconcile_tuning()[1])
        local = threading.local()

        def one(pair, base=base, user=user, target=target, local=local):
            if not hasattr(local, "gm"):      # one migrator per thread: its Gmail clients are per thread
                local.gm = GmailMigrator(auth, db, settings, user, target)
                local.gm.limiter = base.limiter
            try:
                outcome, detail = align_one(local.gm, pair[0], pair[1], dry)
                if outcome in ("aligned", "already"):
                    _record(db, user, pair[0], detail)
                return outcome
            except Exception as exc:      # noqa: BLE001 - one message never stops a mailbox
                progress(f"DMS labels: {user} {pair[0]}: {str(exc)[:160]}")
                return "errors"
        totals["users"] += 1
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="dms-labels") as pool:
            for outcome in pool.map(one, rows):
                totals[outcome] += 1
                done += 1
                if done % 250 == 0 or done == len(rows):
                    progress(f"DMS labels: {user} | items={done}/{len(rows)} | {json.dumps(totals)}")
    return totals


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-id", type=int, required=True)
    ap.add_argument("--user", action="append")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    from auth import AuthManager
    from config import Settings
    from db import MigrationDB
    st = Settings(account_id=args.account_id)
    if args.dry_run:
        st.dry_run = True
    print(f"DMS labels: done {json.dumps(run(AuthManager(st), MigrationDB(st.db_path), st, users=args.user, progress=lambda m: print(m, flush=True)))}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
