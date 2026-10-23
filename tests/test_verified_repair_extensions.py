from types import SimpleNamespace

import pytest
import repair
import verification_repairs as R


def test_unread_only_difference_is_eligible_but_content_damage_is_not(monkeypatch):
    import gmail_engine
    changed=[]
    class Mail:
        def __init__(self,labels):self.labels=labels
        def users(self):return self
        def messages(self):return self
        def get(self,**kw):return SimpleNamespace(execute=lambda:{'labelIds':self.labels})
        def batchModify(self,**kw):
            def apply():
                changed.append(kw['body']);self.labels=list((set(self.labels)|set(kw['body']['addLabelIds']))-set(kw['body']['removeLabelIds']))
            return SimpleNamespace(execute=apply)
    gm=SimpleNamespace(src=Mail(['UNREAD']),tgt=Mail([]),sync_labels=lambda:None,
                       _retry=lambda fn,**kw:fn(),_map_label_ids=lambda labels:labels,
                       limiter=SimpleNamespace(acquire=lambda:None))
    monkeypatch.setattr(gmail_engine,'GmailMigrator',lambda *a:gm)
    import finding_repairs
    monkeypatch.setattr(finding_repairs,'record',lambda *a,**kw:None)
    db=SimpleNamespace(get_target_id=lambda *a:'tid',resolve_identity=lambda u:'u@target',get_label_map=lambda u:{})
    item={'source':'sid','target':'tid','diffs':['labels missing on the target: UNREAD','unread state differs']}
    report={'users':{'u@source':{'gmail':{'differences':[item]}}}}
    result=repair.repair_verified_gmail_labels(None,db,SimpleNamespace(dry_run=False),report,apply=True)
    assert result['changed']==1 and changed[0]['addLabelIds']==['UNREAD']
    item['diffs'].append('message differs: body differs')
    assert repair.repair_verified_gmail_labels(None,db,SimpleNamespace(dry_run=False),report,apply=True)['candidates']==0


def setup_native(monkeypatch, mapping='tid', diffs=None, same_id=False):
    import link_rewrite
    import domain_guard
    monkeypatch.setattr(domain_guard,'source_domains',lambda:set())
    calls=[]
    mime='application/vnd.google-apps.document'
    def drive():return SimpleNamespace(files=lambda:SimpleNamespace(get=lambda **kw:SimpleNamespace(execute=lambda:{'id':kw['fileId'],'mimeType':mime})))
    auth=SimpleNamespace(source_drive=lambda u:drive(),target_drive=lambda u:drive(),api=lambda side,kind,user:calls.append((side,kind,user)) or 'target-api')
    db=SimpleNamespace(get_target_id=lambda *a:mapping,resolve_identity=lambda u:'u@target',target_for_source_id=lambda sid:'mapped')
    def rewrite(kind,api,tid,lookup,**kw):calls.append((api,tid,kw['apply']));return 1
    monkeypatch.setattr(link_rewrite,'rewrite_native',rewrite)
    item={'source':'sid','target':'sid' if same_id else 'tid','diffs':diffs or ['links inside still point at the source (1 rewrite(s) due)']}
    report={'users':{'u@source':{'drive':{'differences':[item]}}}}
    return auth,db,report,calls


def test_native_link_repair_only_writes_exact_mapped_target(monkeypatch):
    auth,db,report,calls=setup_native(monkeypatch)
    r=repair.repair_verified_native_links(auth,db,SimpleNamespace(dry_run=False),report,apply=True)
    assert r['changed']==1 and r['errors']==[]
    assert calls==[('target','docs','u@target'),('target-api','tid',True)]


@pytest.mark.parametrize('kind',['mapping','content','same-id','dry-run'])
def test_native_link_repair_refuses_unsafe_scope(monkeypatch,kind):
    auth,db,report,calls=setup_native(monkeypatch,mapping='other' if kind=='mapping' else 'tid',
        diffs=['content differs when exported (1 -> 2 bytes)'] if kind=='content' else None,same_id=kind=='same-id')
    r=repair.repair_verified_native_links(auth,db,SimpleNamespace(dry_run=kind=='dry-run'),report,apply=True)
    assert r['changed']==0 and not calls


def test_native_link_repair_never_targets_a_source_domain(monkeypatch):
    auth,db,report,calls=setup_native(monkeypatch)
    r=repair.repair_verified_native_links(auth,db,SimpleNamespace(dry_run=False,source_domain='target'),report,apply=True)
    assert r['changed']==0 and r['errors'] and not calls


def test_new_capability_revisits_only_eligible_terminal_findings_once(db,monkeypatch):
    import verify_sample as V
    R.enable(db)
    r=V.Verifier._blank();r.update(checked=1)
    r['differences']=[{'source':'sid','target':'tid','diffs':['unread state differs']}]
    V.save_user_results(db,{'users':{'u@source':{'gmail':r}}})
    monkeypatch.setattr(R,'REPAIR_CAPABILITY_VERSION',1)
    old=R._key(db,'u@source');R._finish(db,'u@source',[old],'NEEDS_ATTENTION','unsupported')
    assert R.due_users(db)==[]
    monkeypatch.setattr(R,'REPAIR_CAPABILITY_VERSION',2)
    assert R.due_users(db)==['u@source']
    current=R._key(db,'u@source');R._finish(db,'u@source',[current],'NEEDS_ATTENTION','still unresolved')
    assert R.due_users(db)==[]


def test_capability_upgrade_revisits_new_native_content_inspection(monkeypatch):
    rows=[{'service':'drive','differences':[{'diffs':['content differs when exported (1 -> 2 bytes)']}]}]
    monkeypatch.setattr(R,'REPAIR_CAPABILITY_VERSION',1);before=R.fingerprint(rows)
    monkeypatch.setattr(R,'REPAIR_CAPABILITY_VERSION',2)
    assert R.fingerprint(rows)!=before
    # Chat content has no new repair handler and must not be retried on upgrade.
    rows[0]["service"]="chat"
    before=R.fingerprint(rows)
    monkeypatch.setattr(R,"REPAIR_CAPABILITY_VERSION",3)
    assert R.fingerprint(rows)==before


def test_inspection_failure_is_error_instead_of_unsupported_noop(db,settings,monkeypatch):
    import verify_sample as V
    r=V.Verifier._blank();r.update(checked=1)
    r['differences']=[{'source':'sid','target':'tid','diffs':['unread state differs']}]
    monkeypatch.setattr(R,'scoped_repair',lambda *args:{'gmail_labels':{'candidates':0,'errors':['API unavailable']}})
    monkeypatch.setattr(V,'run',lambda *args:pytest.fail('failed inspection must not trigger another full scan'))
    R.finished_user(None,db,settings,'u@source',{'gmail':r},None,lambda f:f,lambda m:None)
    row=db.conn.execute('SELECT state,summary FROM verification_repairs').fetchone()
    assert row['state']=='ERROR' and 'API unavailable' in row['summary']
