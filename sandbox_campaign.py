"""Operator-started fresh sandbox run with durable, bounded recovery.

Never schedules another wipe. Interrupted destructive work is held for review.
The timer resumes quota-paused copies; code defects remain explicit blockers.
"""
from __future__ import annotations
import argparse
import csv
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
SERVICES = ('drive', 'gmail', 'calendar', 'contacts', 'tasks')
PAIR = ('s.sarafgloabalexim.com', 't.sarafgloabalexim.com')


def now():
    return datetime.now(timezone.utc)


def manifest():
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(ROOT.glob('*.py'))}


def save(path, state):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def validate(settings, rows):
    if (settings.source_domain, settings.target_domain) != PAIR:
        raise RuntimeError('Wrong tenant pair; nothing may run')
    pairs = [(r['source_email'], r['target_email']) for r in rows if r['entity_type'] == 'user']
    if (len(pairs) != 45 or len({x for x, _ in pairs}) != 45
            or len({y for _, y in pairs}) != 45
            or any(not x.endswith('@'+PAIR[0]) or not y.endswith('@'+PAIR[1]) for x, y in pairs)):
        raise RuntimeError('Expected exactly 45 unique source/target user mappings')
    return sorted(pairs)


def quota_resume_at(conn, targets, at=None):
    """Hourly ledger buckets expire conservatively at start +25h, never midnight.

    With no usable copy record, wait a full day after the observed refusal.
    Minute-level API throttling never enters this PAUSED_QUOTA path.
    """
    at = at or now()
    waits = []
    for target in targets:
        rows = conn.execute('SELECT day_utc FROM upload_ledger WHERE target_user=? AND bytes_sent>0',
                            (target,)).fetchall()
        valid = []
        for row in rows:
            try:
                expires = datetime.strptime(row[0], '%Y-%m-%dT%H').replace(tzinfo=timezone.utc) + timedelta(hours=25)
            except ValueError:
                continue
            if expires > at:
                valid.append(expires)
        waits.append(max(valid) if valid else at + timedelta(hours=24))
    return max(waits) if waits else at


def idle():
    """Fail closed if unrelated tenant work or unfinished repair records exist."""
    result = subprocess.run(['ps', '-eo', 'pid=,args='], capture_output=True, text=True, check=True)
    scripts = ('main.py', 'verify_sample.py', 'wipe_tenant.py', 'reset_target.py',
               'dms_migrate.py', 'dms_delta_loop.py', 'parity_loop.py')
    for line in result.stdout.splitlines():
        if 'python' in line and any(s in line for s in scripts) and int(line.split()[0]) != os.getpid():
            raise RuntimeError('Conflicting tenant process: '+line.strip()[:180])
    for db in (ROOT/'data/accounts').glob('*/migration.db'):
        with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('repair_runs', 'verification_repairs'):
                if table in tables and c.execute(f'SELECT 1 FROM {table} WHERE finished_at IS NULL LIMIT 1').fetchone():
                    raise RuntimeError(f'Unfinished {table} in account {db.parent.name}')


def child(args, directory, log, env):
    import run_watch
    started = now().isoformat()
    job = 'migrate' if args[0] == 'main.py' else ('verify' if args[0] == 'verify_sample.py' else Path(args[0]).stem)
    with (directory/log).open('a') as output:
        p = subprocess.Popen([sys.executable, '-u', *args], cwd=ROOT, env=env,
                             stdout=output, stderr=subprocess.STDOUT)
        run_watch.record_started(3, job, pid=p.pid, started_at=started)
        rc = p.wait()
        run_watch.record_finished(3, job, rc, pid=p.pid, started_at=started)
    if rc:
        raise RuntimeError(f'{args[0]} exited {rc}; inspect {directory/log}')


def prepare(directory, state, settings, db, pairs):
    import reset_target
    import source_guard
    import wipe_tenant
    from auth import AuthManager
    os.environ['SANDBOX_MODE'] = 'true'
    reset_target.assert_sandbox(settings, PAIR[1], 'target')
    for svc in (*SERVICES, 'groups'):
        if not wipe_tenant.granted(settings.target_sa_key, settings.target_admin, svc):
            raise RuntimeError(f'Target {svc} wipe scope unavailable; no wipe started')
    # A local process can be gone while Google's import is still running.
    from webui import _dms_env
    prior = subprocess.run([sys.executable, 'dms_migrate.py', '--status', '--json', '--timeout', '120'],
                           cwd=ROOT, env=_dms_env(3), capture_output=True, text=True, timeout=180)
    status = json.loads(prior.stdout.strip().splitlines()[-1]) if prior.returncode == 0 else {}
    (directory/'dms-before.json').write_text(json.dumps(status, indent=2))
    if not status.get('ok') or status.get('status') not in ('complete', 'Stopped'):
        raise RuntimeError('DMS idle state not established; target has not been wiped')
    # A consistent SQLite copy preserves old evidence and real quota expenditure.
    with sqlite3.connect(directory/'before.db') as archive:
        db.conn.backup(archive)
    with (directory/'identities.csv').open('w') as f:
        w = csv.writer(f); w.writerow(['source_email', 'target_email']); w.writerows(pairs)
    source_guard.snapshot(AuthManager(settings), settings, db)
    bad = db.conn.execute("SELECT source_user,state FROM source_guard_snapshot").fetchall()
    if len(bad) != 45 or any(json.loads(r[1]).get('errors') for r in bad):
        raise RuntimeError('Full source baseline incomplete; target has not been wiped')
    # Commit BEFORE first delete. A kill or power loss cannot repeat this phase.
    state['wipeStartedAt'] = now().isoformat()
    save(directory/'state.json', state)
    result = wipe_tenant.wipe([t for _, t in pairs], wipe_tenant.key_clients(settings.target_sa_key),
                             settings.target_admin, services=SERVICES, workers=4,
                             shared_drives=True, groups_domain=PAIR[1])
    (directory/'wipe.json').write_text(json.dumps(result, indent=2))
    # Reset only services/users actually emptied, even after a partial wipe.
    wipe_tenant.reset_ledger(db, result['emptied'], result['shared_drives'] is not None,
                            groups_gone=result['groups'] is not None, failed=result['failed'])
    if any(result['failed'].values()) or result['shared_drives'] is None or result['groups'] is None:
        raise RuntimeError('Partial wipe; inspect wipe.json. Never automatically wipe again')
    with db.write() as c:
        c.executemany("UPDATE identity_map SET status='PENDING',services_done='' WHERE source_email=?",
                      [(s,) for s, _ in pairs])
        # Directory checks and old repair fingerprints also refer to the prior target.
        for s, _ in pairs:
            c.execute('DELETE FROM user_verification WHERE source_user=?', (s,))
            c.execute('DELETE FROM verification_repairs WHERE source_user=?', (s,))
    state['wipeCompletedAt'] = now().isoformat()
    state['phase'] = 'migrate'


def step(directory, state):
    from config import Settings
    from db import MigrationDB
    env = dict(os.environ, TRANSFER_MODE='server_side', MAIL_ONLY_WITH_LINKS='true',
               MAIL_LINK_SCAN='search', MIGRATE_CHAT='false', MIGRATE_GROUPS='true',
               MIGRATE_CONTACTS='true', MIGRATE_TASKS='true', MIGRATE_CALENDAR_ACLS='false',
               VERIFY_ON_COMPLETE='false', FIRST_PASS_ACCEPTANCE='false',
               AUTO_REPAIR_ON_VERIFICATION='false', USER_WORKERS='2', MIGRATE_PROCESSES='2',
               DRIVE_FILE_WORKERS='4', PYTHONUNBUFFERED='1')
    os.environ.update(env)
    settings = Settings(account_id=3)
    db = MigrationDB(settings.db_path)
    try:
        pairs = validate(settings, db.all_identities())
        prior = state.get('pairs')
        if prior and prior != [list(p) for p in pairs]:
            raise RuntimeError('User mappings changed during the campaign')
        state['pairs'] = pairs
        idle()
        if state['phase'] == 'prepare':
            if state.get('wipeStartedAt'):
                raise RuntimeError('Wipe already started; manual recovery required')
            prepare(directory, state, settings, db, pairs)
        elif state['phase'] == 'migrate':
            attempts = state.get('migrationAttempts', 0)
            if attempts >= 4:
                raise RuntimeError('Four migration attempts exhausted; manual error review required')
            state['migrationAttempts'] = attempts + 1
            save(directory/'state.json', state)
            child(['main.py', '--account-id', '3', 'migrate', '--ordered', '--services',
                   ','.join(SERVICES)], directory, 'migrate.log', env)
            users = [r for r in db.all_identities() if r['entity_type'] == 'user']
            pending = [r for r in users if not set(SERVICES) <= set((r['services_done'] or '').split(','))]
            paused = [r for r in pending if r['status'] == 'PAUSED_QUOTA']
            if pending and len(paused) != len(pending):
                raise RuntimeError('Migration has non-quota unfinished users; inspect migrate.log before retrying')
            if paused:
                state['nextEligibleAt'] = quota_resume_at(db.conn, [r['target_email'] for r in paused]).isoformat()
                state['quotaUsers'] = len(paused)
            else:
                state['phase'] = 'dms'
        elif state['phase'] == 'dms':
            from webui import _dms_env
            scoped = _dms_env(3); scoped.update({k: v for k, v in env.items() if k not in scoped})
            if state.get('dmsStartedAt'):
                raise RuntimeError('Fresh DMS already initiated; inspect its actual state before recovery')
            state['dmsStartedAt'] = now().isoformat()
            save(directory/'state.json', state)
            child(['dms_migrate.py', '--apply', '--fresh-import', '--identities', str(directory/'identities.csv'),
                   '--watch', '120', '--until-done', '4320', '--timeout', '200', '--no-tally'],
                  directory, 'dms.log', scoped)
            state['phase'] = 'verify'
        elif state['phase'] == 'verify':
            import verification_repairs
            verification_repairs.enable(db, operator=True)
            child(['verify_sample.py', '--account-id', '3', '--services', ','.join((*SERVICES, 'directory')),
                   '--repair-after-user'], directory, 'verify.log', env)
            state['phase'] = 'deep-check'
        elif state['phase'] == 'deep-check':
            child(['tenant_verify.py', '--account-id', '3'], directory, 'tenant.log', env)
            import parity_deep_audit
            import source_guard
            from auth import AuthManager
            report = parity_deep_audit.assess(db.conn)
            (directory/'deep-audit.json').write_text(json.dumps(report, indent=2, default=str))
            settings.migrate_contacts = settings.migrate_tasks = True
            client_id = json.loads(Path(settings.source_sa_key).read_text()).get('client_id')
            guard = source_guard.check(AuthManager(settings), settings, db, client_id)
            (directory/'source-after.json').write_text(json.dumps(guard, indent=2, default=str))
            import verify_sample
            import readiness_report
            latest = Path(verify_sample.verify_dir(3))/'latest.json'
            evidence = readiness_report.assess(json.loads(latest.read_text()), [s for s, _ in pairs])
            (directory/'readiness.json').write_text(json.dumps(evidence, indent=2, default=str))
            # Completion of a check is not a parity claim. An operator reviews
            # feature coverage, tenant findings and source stability together.
            state['phase'] = 'REVIEW_REQUIRED'
            state['outcome'] = 'Full checks finished; inspect current findings and feature coverage before claiming parity'
        else:
            raise RuntimeError('Unknown campaign phase')
    finally:
        db.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--directory', type=Path, required=True)
    ap.add_argument('--start', action='store_true', help='Operator authorization for one fresh account-3 wipe')
    a = ap.parse_args(argv)
    a.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = a.directory/'state.json'
    with (a.directory/'lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        if not path.exists():
            if not a.start:
                raise SystemExit('Campaign must be explicitly started once')
            state = dict(phase='prepare', createdAt=now().isoformat(), codeHash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), manifest=manifest())
            save(path, state)
        else:
            state = json.loads(path.read_text())
        if state.get('codeHash') != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
            raise SystemExit('Campaign code changed; review and explicitly admit the new version before resuming')
        if state.get('manifest') and state['manifest'] != manifest():
            raise SystemExit('Installed Python baseline changed; campaign held for review')
        if state['phase'] in ('BLOCKED', 'REVIEW_REQUIRED'):
            return 0
        if state.get('inProgress'):
            state.update(phase='BLOCKED', error='Interrupted phase; inspect before resuming. Wipe will never repeat')
            save(path, state)
            return 1
        if state.get('nextEligibleAt') and now() < datetime.fromisoformat(state['nextEligibleAt']):
            return 0
        state.pop('nextEligibleAt', None)
        state['inProgress'] = True
        save(path, state)
        try:
            step(a.directory, state)
        except Exception as exc:
            state.update(phase='BLOCKED', error=f'{type(exc).__name__}: {exc}')
            print(state['error'], flush=True)
        finally:
            state['inProgress'] = False
            state['updatedAt'] = now().isoformat()
            save(path, state)
        return int(state['phase'] == 'BLOCKED')


if __name__ == '__main__':
    sys.exit(main())
