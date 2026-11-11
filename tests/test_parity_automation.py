import base64
import json
from types import SimpleNamespace
import pytest
import db as D
import migration_checks as M
import parity_diagnostics as P
import verify_sample as V
import verification_repairs as R
from gmail_engine import GmailMigrator
from tests.conftest import SRC_USER, TGT_USER


def test_item_failure_preserves_full_mailbox_result_and_other_findings(auth, db, settings):
    full = V.Verifier._blank()
    full.update(checked=100, identical=100, scopeVersion=2, coverageComplete=True)
    V.save_user_results(db, {'users': {SRC_USER: {'gmail': full}}})
    R.enqueue_findings(db, SRC_USER, {'gmail': {'missing': [{'source': 'other', 'type': 'draft'}], 'itemScope': True}})
    original = db.conn.execute('select payload from user_verification').fetchone()[0]
    raw = b'Message-ID: <one@example>\r\nSubject: unchanged\r\n\r\nbody'
    target = auth.target_gmail(TGT_USER)
    tid = target.add_message(raw, ['INBOX'])
    db.record_mapping(SRC_USER, 'one', tid, 'message')
    gm = GmailMigrator(auth, db, settings, SRC_USER, TGT_USER)
    assert not M.verify_written_mail(gm, 'one', tid, base64.urlsafe_b64encode(raw).decode(), [])
    assert db.conn.execute('select payload from user_verification').fetchone()[0] == original
    receipt = json.loads(db.conn.execute('select evidence from migration_item_receipts').fetchone()[0])
    assert receipt['verificationKind'] == 'item' and 'sampled' not in receipt
    assert receipt['findings']['differences']
    assert 'MAIL_LABELS' in {d['code'] for d in D.pending_mail_diagnostics(db.conn)[SRC_USER]}
    assert db.conn.execute("select state from verification_repair_queue where item_id='other'").fetchone()[0] != 'RESOLVED'
    users = [{'source_email': SRC_USER, 'target_email': TGT_USER, 'status': 'DONE', 'services_done': 'gmail'}]
    result = D.verification_rollup(users, db.user_verifications(), D.pending_mail_checks(db.conn))
    assert result['users'][0]['verdict'] == 'INCOMPLETE'
    assert result['users'][0]['services'][0]['checked'] == 100
    assert result['users'][0]['services'][0]['itemChecksPending'] == 1


def test_full_verification_retires_only_older_pending_receipts(auth, db, settings):
    raw = b'Message-ID: <one@example>\r\n\r\nbody'
    tid = auth.target_gmail(TGT_USER).add_message(raw, ['INBOX'])
    db.record_mapping(SRC_USER, 'one', tid, 'message')
    gm = GmailMigrator(auth, db, settings, SRC_USER, TGT_USER)
    M.verify_written_mail(gm, 'one', tid, base64.urlsafe_b64encode(raw).decode(), [])
    db.conn.execute("update migration_item_receipts set verified_at='2026-01-01T00:00:00Z'"); db.conn.commit()
    full = V.Verifier._blank()
    full.update(checked=1, identical=1, scopeVersion=2, sourceCoverage=True, coverageComplete=True,
                provenance={'serviceStartedAt': '2026-02-01T00:00:00Z'})
    V.save_user_results(db, {'users': {SRC_USER: {'gmail': {**full, 'sampled': {'checked':1,'of':2}}}}})
    assert D.pending_mail_checks(db.conn)[SRC_USER] == 1
    V.save_user_results(db, {'users': {SRC_USER: {'gmail': full}}})
    assert D.pending_mail_checks(db.conn) == {}
    assert db.conn.execute('select status from migration_item_receipts').fetchone()[0] == 'RECHECKED_BY_FULL_VERIFICATION'


def test_historical_identical_with_extras_is_never_green():
    row = {'source_user': 'u', 'service': 'gmail', 'verified_at': 't', 'verdict': 'IDENTICAL',
           'checked': 20, 'identical': 20, 'sampled_of': None,
           'payload': json.dumps({'scopeVersion':2, 'counts':{'extras':99}})}
    assert D.parse_user_verification_rows([row])[0]['verdict'] == 'DIFFERENCES'


@pytest.mark.parametrize('service,result,code', [
    ('gmail', {'differences':[{'diffs':['message differs: header x-forwarded-encrypted: old -> new']}]}, 'MAIL_STORAGE_HEADER'),
    ('gmail', {'missing':[{'type':'draft'}]}, 'MISSING_DRAFT'),
    ('gmail', {'notCopied':[{'id':'x'}]}, 'MAIL_MAPPING_GAP'),
    ('drive', {'errors':['Native sheet allocated cells exceed budget']}, 'SHEET_READ_BUDGET'),
    ('drive', {'errors':['HTTP 503 unavailable']}, 'READ_RETRY_REQUIRED'),
    ('drive', {'differences':[{'diffs':['comment thread missing or ambiguous']}]}, 'COMMENT_FIDELITY'),
])
def test_diagnosis_is_specific_and_advisory(service, result, code):
    diagnoses = P.diagnose(service, result)
    assert code in {x['code'] for x in diagnoses}
    assert all(x['nextAction'] for x in diagnoses)


def test_failed_attempt_cannot_disappear_from_user_scope(db):
    db.log_audit('u@s', 'message1', 'message', 'FAILED', 'copy failed')
    expected = D.expected_verification_services(db.conn)
    user = {'source_email':'u@s','target_email':'u@t','status':'DONE','services_done':'drive'}
    result = D.verification_rollup([user], [{'user':'u@s','service':'drive','verdict':'IDENTICAL','verifiedAt':'t'}],
                                   expected_services=expected)
    assert result['users'][0]['verdict'] == 'NOT_VERIFIED'
    assert any(s['service']=='gmail' and s['verdict']=='NOT_VERIFIED' for s in result['users'][0]['services'])


def test_older_full_run_does_not_clear_a_newer_item_failure(auth, db, settings):
    raw = b'Message-ID: <one@example>\r\n\r\nbody'
    tid = auth.target_gmail(TGT_USER).add_message(raw, ['INBOX'])
    db.record_mapping(SRC_USER, 'one', tid, 'message')
    gm = GmailMigrator(auth, db, settings, SRC_USER, TGT_USER)
    M.verify_written_mail(gm, 'one', tid, base64.urlsafe_b64encode(raw).decode(), [])
    full = V.Verifier._blank()
    full.update(checked=1, identical=1, scopeVersion=2, sourceCoverage=True, coverageComplete=True,
                provenance={'serviceStartedAt':'2000-01-01T00:00:00Z'})
    V.save_user_results(db, {'users':{SRC_USER:{'gmail':full}}})
    assert D.pending_mail_checks(db.conn)[SRC_USER] == 1
    assert db.conn.execute("select count(*) from verification_repair_queue where state!='RESOLVED'").fetchone()[0] > 0


def test_identical_label_attempt_is_not_repeated_across_connections(db):
    import repair
    assert repair._claim_label_attempt(db, 'u', 's', 'fingerprint-1')
    other = D.MigrationDB(db.path if hasattr(db,'path') else db.conn.execute('pragma database_list').fetchone()[2])
    try:
        assert not repair._claim_label_attempt(other, 'u', 's', 'fingerprint-1')
        assert repair._claim_label_attempt(other, 'u', 's', 'changed-source-or-target-evidence')
    finally:
        other.close()
