from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest
import sandbox_campaign as campaign
import wipe_tenant


def test_pair_guard_rejects_duplicates_and_another_tenant():
    rows = [dict(entity_type='user', source_email=f'u{i}@{campaign.PAIR[0]}',
                 target_email=f'u{i}@{campaign.PAIR[1]}') for i in range(45)]
    settings = SimpleNamespace(source_domain=campaign.PAIR[0], target_domain=campaign.PAIR[1])
    assert len(campaign.validate(settings, rows)) == 45
    rows[-1]['target_email'] = rows[0]['target_email']
    with pytest.raises(RuntimeError, match='45 unique'):
        campaign.validate(settings, rows)
    settings.source_domain = 'production.example'
    with pytest.raises(RuntimeError, match='Wrong tenant'):
        campaign.validate(settings, rows)


def test_quota_wait_preserves_rolling_window_not_local_midnight():
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE upload_ledger(target_user,day_utc,bytes_sent)')
    c.executemany('INSERT INTO upload_ledger VALUES(?,?,?)',
                  [('a', '2026-11-12T21', 1), ('a', '2026-11-12T22', 2),
                   ('other', '2026-11-13T01', 100)])
    at = datetime(2026, 11, 12, 23, tzinfo=timezone.utc)
    assert campaign.quota_resume_at(c, ['a'], at).isoformat() == '2026-11-13T23:00:00+00:00'
    assert campaign.quota_resume_at(c, ['unknown'], at).isoformat() == '2026-11-13T23:00:00+00:00'
    assert c.execute('SELECT SUM(bytes_sent) FROM upload_ledger').fetchone()[0] == 103


def test_interrupted_campaign_never_repeats_wipe(tmp_path, monkeypatch):
    import hashlib
    state = dict(phase='prepare', inProgress=True, wipeStartedAt='before-delete',
                 codeHash=hashlib.sha256(campaign.Path(campaign.__file__).read_bytes()).hexdigest())
    (tmp_path/'state.json').write_text(json.dumps(state))
    monkeypatch.setattr(campaign, 'step', lambda *a: pytest.fail('must not run another wipe'))
    assert campaign.main(['--directory', str(tmp_path)]) == 1
    assert json.loads((tmp_path/'state.json').read_text())['phase'] == 'BLOCKED'
    assert campaign.main(['--directory', str(tmp_path), '--start']) == 0


def test_wipe_page_tokens_are_collected_before_mutation_and_cycles_fail():
    class Resource:
        def list(self, **kw):
            page = {'items': [{'id': 'second'}]} if kw.get('pageToken') else {
                'items': [{'id': 'first'}], 'nextPageToken': 'next'}
            return SimpleNamespace(execute=lambda: page)
    assert [x['id'] for x in wipe_tenant._all_pages(Resource(), 'items')] == ['first', 'second']
    r = SimpleNamespace(list=lambda **kw: SimpleNamespace(execute=lambda: {'nextPageToken': 'loop'}))
    with pytest.raises(RuntimeError, match='Repeated'):
        wipe_tenant._all_pages(r, 'items')


def test_drive_wipe_removes_trashed_owned_items_without_emptying_foreign_trash():
    store = {'mine': 'owned', 'trashed': 'owned', 'foreign': 'foreign'}
    class Files:
        def list(self, **kw):
            ids = [k for k, owner in store.items() if owner == 'owned'
                   and (k != 'trashed' or 'trashed = false' not in kw['q'])]
            return SimpleNamespace(execute=lambda: {'files': [{'id': k} for k in ids]})
        def delete(self, fileId, **kw):
            return SimpleNamespace(execute=lambda: store.pop(fileId))
        def emptyTrash(self):
            pytest.fail('blanket emptyTrash may delete items not owned by the target user')
    assert wipe_tenant.wipe_drive(SimpleNamespace(files=Files)) == 2
    assert store == {'foreign': 'foreign'}


def test_service_reset_discards_old_receipts_but_keeps_other_users_and_real_quota(db):
    import reset_drive_ledger
    from db import bulk_seed_identities
    user, other = 'a@s.example', 'b@s.example'
    bulk_seed_identities(db, [(user, 'a@t.example'), (other, 'b@t.example')])
    with db.write() as c:
        for table, extra in [('migration_item_receipts', ',service TEXT'),
                             ('verification_repair_queue', ',service TEXT'),
                             ('verification_mail_cache', ''), ('verification_mail_checkpoints', ''),
                             ('mail_reconciliation_items', '')]:
            c.execute(f'CREATE TABLE IF NOT EXISTS {table}(source_user TEXT{extra})')
            for u in (user, other):
                if extra:
                    # Existing schemas may have additional required columns.
                    cols = {r[1] for r in c.execute(f'PRAGMA table_info({table})')}
                    values = {k: 'x' for k in cols}; values.update(source_user=u, service='gmail')
                    names = list(values)
                    c.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?'*len(names))})", list(values.values()))
                else:
                    c.execute(f'INSERT INTO {table}(source_user) VALUES (?)', (u,))
        c.execute('INSERT INTO upload_ledger VALUES(?,?,?)', ('a@t.example', '2026-11-12T21', 123))
    reset_drive_ledger.reset_service_ledger(db, user, ('gmail',))
    for table in ('migration_item_receipts','verification_repair_queue','verification_mail_cache',
                  'verification_mail_checkpoints','mail_reconciliation_items'):
        assert db.conn.execute(f'SELECT COUNT(*) FROM {table} WHERE source_user=?',(user,)).fetchone()[0] == 0
        assert db.conn.execute(f'SELECT COUNT(*) FROM {table} WHERE source_user=?',(other,)).fetchone()[0] == 1
    assert db.conn.execute('SELECT bytes_sent FROM upload_ledger').fetchone()[0] == 123


def test_observer_cannot_launch_a_competing_recovery(monkeypatch):
    import autopilot
    monkeypatch.setenv('AUTOPILOT_OBSERVE_ONLY', '1')
    monkeypatch.setattr(autopilot, '_api', lambda *a: pytest.fail('must not launch'))
    assert autopilot.act({'do':'resume'}, {}, 3).startswith('held:')
    assert autopilot.act({'do':'fix_mtime'}, {}, 3).startswith('held:')


def test_phase_failure_is_durable_and_does_not_retry_on_timer(tmp_path, monkeypatch):
    calls = []
    def fail(*args):
        calls.append('called')
        raise RuntimeError('authorization missing')
    monkeypatch.setattr(campaign, 'step', fail)
    assert campaign.main(['--directory', str(tmp_path), '--start']) == 1
    assert 'authorization missing' in json.loads((tmp_path/'state.json').read_text())['error']
    assert campaign.main(['--directory', str(tmp_path)]) == 0
    assert calls == ['called']


def test_timer_waits_until_quota_expiry_without_spending_an_attempt(tmp_path, monkeypatch):
    import hashlib
    state = dict(phase='migrate', nextEligibleAt='2999-01-01T00:00:00+00:00', migrationAttempts=1,
                 codeHash=hashlib.sha256(campaign.Path(campaign.__file__).read_bytes()).hexdigest())
    (tmp_path/'state.json').write_text(json.dumps(state))
    monkeypatch.setattr(campaign, 'step', lambda *a: pytest.fail('quota has not expired'))
    assert campaign.main(['--directory', str(tmp_path)]) == 0
    assert json.loads((tmp_path/'state.json').read_text())['migrationAttempts'] == 1
