import copy
import json
from types import SimpleNamespace
import pytest
from tests.conftest import SRC_USER,TGT_USER
from tests.test_split_mail_mode import PLAIN
from config import DEFERRED_TO_DMS
from split_mail import reconcile_dms


def test_label_difference_maps_proven_identity_without_repair(db,auth,settings,identity):
    sid=auth.source_gmail(SRC_USER).add_message(PLAIN,['INBOX'])
    tid=auth.target_gmail(TGT_USER).add_message(PLAIN,['INBOX','CATEGORY_UPDATES'])
    db.log_audit(SRC_USER,sid,'message',DEFERRED_TO_DMS)
    out=reconcile_dms(auth,db,settings,progress=lambda _:None)
    assert out['blocked'] and db.get_target_id(SRC_USER,sid,'message')==tid
    assert db.get_audit(SRC_USER,sid,'message')['status']==DEFERRED_TO_DMS
    assert auth.target_gmail(TGT_USER).call_count('messages.modify')==0
    proof=json.loads(db.conn.execute('SELECT evidence FROM migration_item_receipts').fetchone()[0])
    assert proof['contentProven'] and proof['status']=='NEEDS_RECHECK'


def test_unseen_source_mail_is_reconciled_even_without_engine_audit(db,auth,settings,identity):
    sid=auth.source_gmail(SRC_USER).add_message(PLAIN,['INBOX'])
    tid=auth.target_gmail(TGT_USER).add_message(PLAIN,['INBOX'])
    out=reconcile_dms(auth,db,settings,progress=lambda _:None)
    assert out['verified']==1
    assert db.get_target_id(SRC_USER,sid,'message')==tid


def test_body_conflict_never_maps_or_changes_target(db,auth,settings,identity):
    sid=auth.source_gmail(SRC_USER).add_message(PLAIN,['INBOX'])
    target=auth.target_gmail(TGT_USER)
    target.add_message(PLAIN+b' changed',['INBOX'])
    before=copy.deepcopy(target.messages)
    out=reconcile_dms(auth,db,settings,progress=lambda _:None)
    assert out['blocked'] and not db.get_target_id(SRC_USER,sid,'message')
    assert target.messages==before


def test_ledger_deep_audit_does_not_write(db,identity):
    from parity_deep_audit import assess
    before=db.conn.total_changes
    report=assess(db.conn)
    assert report['parityEstablished'] is False
    assert any(x['code']=='MISSING_SERVICE_RESULTS' for x in report['findings'])
    assert before==db.conn.total_changes


class Sheets:
    def __init__(self, fid, value=1, height=201):
        self.fid=fid;self.value=value;self.calls=[];self.height=height
    def spreadsheets(self):return self
    def get(self,**kw):
        self.calls.append(kw)
        props={'sheetId':self.fid,'title':'Data','gridProperties':{'rowCount':self.height,'columnCount':100}}
        if 'ranges' not in kw:return SimpleNamespace(execute=lambda:{'sheets':[{'properties':props}]})
        # The same cell appears in each requested fake window, sufficient to
        # test bounded requests, normalisation and complete traversal after failure.
        return SimpleNamespace(execute=lambda:{'sheets':[{'properties':props,'data':[{'rowData':[{'values':[{'userEnteredValue':{'numberValue':self.value}}]}]}]}]})


def test_streamed_sheet_reads_bounded_windows_and_finishes_after_mismatch():
    from native_repair import compare_sheets_streamed
    a,b=Sheets(1),Sheets(2,value=2)
    diffs=compare_sheets_streamed(a,b,'a','b')
    assert diffs and len(a.calls)==len(b.calls)==4
    assert all('ranges' in c for c in a.calls[1:])
    assert all('numberValue' not in d for d in diffs)


def test_streamed_sheet_matches_generated_sheet_ids_and_budget_is_explicit():
    from native_repair import compare_sheets_streamed
    assert compare_sheets_streamed(Sheets(1),Sheets(2),'a','b')==[]
    with pytest.raises(RuntimeError,match='budget'):
        compare_sheets_streamed(Sheets(1),Sheets(2),'a','b',max_cells=10)


def test_checkpoint_reuses_only_stable_verified_mapping(gmail_migrator,auth,db,settings):
    from mail_reconciliation import Checkpoint
    from migration_checks import verify_written_mail
    gm=gmail_migrator
    sid=auth.source_gmail(SRC_USER).add_message(PLAIN,['INBOX'])
    tid=auth.target_gmail(TGT_USER).add_message(PLAIN,['INBOX'])
    import base64
    raw=base64.urlsafe_b64encode(PLAIN).decode()
    assert verify_written_mail(gm,sid,tid,raw,['INBOX'])
    db.record_mapping(SRC_USER,sid,tid,'message')
    cp=Checkpoint(gm)
    assert cp.context
    cp.save(sid,tid,'VERIFIED',{})
    assert Checkpoint(gm).get(sid)==tid
    auth.target_gmail(TGT_USER).add_message(PLAIN+b' new')
    assert Checkpoint(gm).get(sid) is None


def test_checkpoint_never_reuses_unresolved_outcome(gmail_migrator):
    from mail_reconciliation import Checkpoint
    cp=Checkpoint(gmail_migrator)
    cp.save('s','t','COMPARISON_DIFFERENCE',{})
    assert cp.get('s') is None


def test_delta_uses_unique_units_without_forcing_existing_runs(monkeypatch):
    import dms_delta_loop as D
    calls=[]
    monkeypatch.setattr(D.subprocess,'run',lambda args,**kw:calls.append(args))
    monkeypatch.setattr(D,'wait_done',lambda _:None)
    D.run_delta(1);D.run_delta(1)
    units=[next(x for x in c if x.startswith('--unit=')) for c in calls if c[0]=='systemd-run']
    assert len(set(units))==2


def test_unknown_dms_state_does_not_launch_a_retry(monkeypatch):
    import dms_delta_loop as D
    monkeypatch.setattr(D,'say',lambda _:None)
    monkeypatch.setattr(D,'wait_done',lambda _:None)
    monkeypatch.setattr(D,'failed_count',lambda:(-1,'?'))
    monkeypatch.setattr(D,'run_delta',lambda _:pytest.fail('unknown state must hold'))
    assert D._main()==2


def test_atomic_adoption_refuses_destination_collision_and_mapping_replacement(db):
    db.adopt_message_mapping(SRC_USER,'s1','t1')
    with pytest.raises(RuntimeError):db.adopt_message_mapping(SRC_USER,'s2','t1')
    with pytest.raises(RuntimeError):db.adopt_message_mapping(SRC_USER,'s1','t2')
    assert db.get_target_id(SRC_USER,'s1','message')=='t1'


def test_unmapped_custom_labels_cannot_silently_pass(db,auth,settings,identity):
    auth.source_gmail(SRC_USER).add_message(PLAIN,['INBOX','Label_unknown'])
    auth.target_gmail(TGT_USER).add_message(PLAIN,['INBOX'])
    out=reconcile_dms(auth,db,settings,progress=lambda _:None)
    assert out['blocked'] and out['verified']==0
    assert 'label mapping' in out['errors'][0]['error']
