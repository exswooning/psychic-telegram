"""Offline ledger audit. Opens read-only; does not call providers or resolve findings."""
from __future__ import annotations
from contextlib import closing
import argparse
import datetime
import json
import sqlite3
from pathlib import Path


def assess(conn):
    conn.row_factory = sqlite3.Row
    findings = []
    def add(code, count, action, severity='BLOCKER', detail=None):
        if count:
            findings.append(dict(code=code, count=count, action=action, severity=severity, detail=detail))
    def rows(sql, args=()):
        try:
            return conn.execute(sql, args).fetchall()
        except sqlite3.Error as exc:
            add('AUDIT_EVIDENCE_UNAVAILABLE', 1, 'Inspect the ledger schema; unknown is not a pass.', detail=type(exc).__name__)
            return []
    integrity = rows('PRAGMA quick_check')
    if not integrity or any(r[0] != 'ok' for r in integrity):
        add('DATABASE_CHECK_FAILED', 1, 'Investigate database consistency before writes.')
    failed = rows("SELECT status,COUNT(*) n FROM audit_log WHERE status LIKE 'FAIL%' GROUP BY status")
    add('FAILED_AUDIT_ITEMS', sum(r['n'] for r in failed), 'Inspect per-item causes and fresh provider evidence.',
        detail=[dict(r) for r in failed])
    queue = rows("SELECT service,category,COUNT(*) n FROM verification_repair_queue WHERE state!='RESOLVED' GROUP BY service,category")
    add('OPEN_FINDING_QUEUE', sum(r['n'] for r in queue), 'Recheck by item and cause; historical queue rows are not distinct new defects.',
        detail=[dict(r) for r in queue])
    pending = rows("SELECT COUNT(*) FROM migration_item_receipts WHERE status='NEEDS_RECHECK'")
    add('PENDING_ITEM_RECEIPTS', pending[0][0] if pending else 0, 'Finish fresh content/label checks or retain an explicit blocker.')
    orphan = rows("SELECT COUNT(*) FROM migration_item_receipts r LEFT JOIN id_mapping m ON m.source_user=r.source_user AND m.source_id=r.source_id AND m.target_id=r.target_id AND m.type='message' WHERE r.service='gmail' AND r.status='VERIFIED' AND m.source_id IS NULL")
    add('VERIFIED_RECEIPT_MAPPING_MISMATCH', orphan[0][0] if orphan else 0, 'Do not reuse receipts that no longer identify the mapped destination.')
    duplicates = rows("SELECT source_user,target_id,COUNT(*) n FROM id_mapping WHERE type='message' GROUP BY source_user,target_id HAVING COUNT(*)>1")
    add('DESTINATION_IDENTITY_COLLISION', len(duplicates), 'Disambiguate multiple source items mapped to one destination; preserve copies.')
    active = rows('SELECT COUNT(*) FROM verification_repairs WHERE finished_at IS NULL')
    add('UNFINISHED_REPAIR_RECORDS', active[0][0] if active else 0, 'Observe repair completion before deployment or overlapping work.')
    users = rows("SELECT source_email FROM identity_map WHERE entity_type='user'")
    expected = {r[0] for r in users}
    recorded = rows('SELECT source_user,service,checked,verdict,verified_at,payload FROM user_verification')
    totals = {}; zero = []; legacy = []; services = ('drive','gmail','calendar','contacts','tasks','directory')
    present = {(r['source_user'], r['service']) for r in recorded}
    for r in recorded:
        try:
            p = json.loads(r['payload'])
        except (ValueError, TypeError):
            add('INVALID_RESULT_PAYLOAD',1,'Rerun the affected check with valid saved evidence.'); continue
        for key, n in (p.get('counts') or {}).items():
            totals[f"{r['service']}.{key}"] = totals.get(f"{r['service']}.{key}",0) + n
        if r['checked']==0 and not p.get('emptySource'):
            zero.append(dict(user=r['source_user'],service=r['service']))
        if not p.get('provenance') or p.get('scopeVersion',0)<2:
            legacy.append(dict(user=r['source_user'],service=r['service']))
    missing = [dict(user=u,service=s) for u in sorted(expected) for s in services if (u,s) not in present]
    add('MISSING_SERVICE_RESULTS',len(missing),'Complete each required user/service check.',detail=missing)
    add('ZERO_ITEM_COVERAGE_UNPROVEN',len(zero),'Prove an empty source or reconcile and compare all source items.',detail=zero)
    add('LEGACY_OR_UNVERSIONED_RESULTS',len(legacy),'Fresh affected-scope verification must record the actual comparison version.',detail=legacy)
    for key, n in totals.items():
        if n:
            add('SAVED_'+key.upper().replace('.','_'), n,
                'Reconcile current item evidence; this mixed-age count is not proof of current absence or data loss.')
    return {'observedAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'status':'BLOCKED' if findings else 'LEDGER_CHECKS_CLEAR', 'parityEstablished':False,
            'scope':'Saved ledger evidence only; no live provider reads, source stability or feature-fidelity certification',
            'users':len(expected),'findings':findings}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db',required=True);ap.add_argument('--out')
    args=ap.parse_args()
    with closing(sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as conn:
        conn.execute('PRAGMA query_only=ON')
        result=assess(conn)
    text=json.dumps(result,indent=2)+'\n'
    if args.out:Path(args.out).write_text(text)
    else:print(text,end='')
    return 1 if result['findings'] else 0


if __name__=='__main__':raise SystemExit(main())
