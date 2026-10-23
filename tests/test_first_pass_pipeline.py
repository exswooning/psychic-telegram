from types import SimpleNamespace as NS
import copy
import pytest
import migration_acceptance as A
import verification_cache as C
from transfer_policy import choose, SANDBOX
from tests.conftest import SRC_USER,TGT_USER


def request(value=None,error=None):
    def execute():
        if error:raise error
        return copy.deepcopy(value)
    return NS(execute=execute)


class Mail:
    def __init__(self,history='10',records=None,error=None):self.anchor=history;self.records=records or [];self.error=error;self.reads=0
    def users(self):return self
    def history(self):return self
    def getProfile(self,**kw):return request({'historyId':self.anchor})
    def list(self,**kw):
        self.reads+=1
        return request({'historyId':self.anchor,'history':self.records},self.error)


def verifier(db,settings):
    return NS(db=db,settings=settings,src_user=SRC_USER,tgt_user=TGT_USER,limit=None,_x=lambda f:f())


def seed(db,settings):
    s,t=Mail(),Mail();c=C.MailCache(verifier(db,settings),s,t,({},{}))
    for i in range(1000):c.put(str(i),'t'+str(i),{'messageId':'m'+str(i),'sourceSha256':'a'*64,'targetSha256':'b'*64})
    c.finish();return s,t


def test_unchanged_history_reuses_thousand_comparisons_without_history_walk(db,settings):
    s,t=seed(db,settings);c=C.MailCache(verifier(db,settings),s,t,({},{}))
    assert all(c.get(str(i),'t'+str(i)) for i in range(1000))
    assert s.reads==t.reads==0 and c.hits==1000


@pytest.mark.parametrize('side,key', [('source','4'),('target','t4')])
def test_only_changed_message_is_invalidated(db,settings,side,key):
    s,t=seed(db,settings);svc=s if side=='source' else t
    svc.anchor='12';svc.records=[{'id':'11','labelsAdded':[{'message':{'id':key},'labelIds':['UNREAD']}]}]
    c=C.MailCache(verifier(db,settings),s,t,({},{}))
    assert c.get('4','t4') is None
    assert c.get('5','t5') and c.hits==1


@pytest.mark.parametrize('failure',['expired','unread','backwards','unknown','no_id','truncated'])
def test_unsafe_history_falls_back_to_full_reads(db,settings,failure):
    s,t=seed(db,settings);s.anchor='12'
    if failure=='expired':s.error=RuntimeError('HTTP 404 history expired')
    if failure=='unread':s.error=RuntimeError('503')
    if failure=='backwards':s.anchor='9'
    if failure=='unknown':s.records=[{'id':'11','messages':[{'id':'4'}],'newKind':[]}]
    if failure=='no_id':s.records=[{'id':'11','labelsRemoved':[{}]}]
    if failure=='truncated':s.records=[{'id':'11'}]
    c=C.MailCache(verifier(db,settings),s,t,({},{}))
    assert c.get('4','t4') is None and c.get('5','t5') is None
    assert 'fallback' in c.reason


def test_target_mapping_and_label_names_change_invalidates(db,settings):
    s,t=seed(db,settings)
    c=C.MailCache(verifier(db,settings),s,t,({},{}));assert c.get('1','wrong') is None
    c=C.MailCache(verifier(db,settings),s,t,({'label':'Renamed'},{}));assert c.get('1','t1') is None


def test_new_mail_mapping_does_not_invalidate_other_bodies(db,settings):
    s,t=seed(db,settings);db.record_mapping(SRC_USER,'new','new-target','message')
    c=C.MailCache(verifier(db,settings),s,t,({},{}));assert c.get('1','t1')
    db.record_mapping(SRC_USER,'drive','new-drive','file')
    c=C.MailCache(verifier(db,settings),s,t,({},{}));assert c.get('1','t1') is None


def test_unsaved_or_failed_comparisons_never_acquire_new_checkpoint(db,settings):
    s,t=seed(db,settings);c=C.MailCache(verifier(db,settings),s,t,({},{}));assert c.get('1','t1')
    c.finish();again=C.MailCache(verifier(db,settings),s,t,({},{}))
    assert again.get('1','t1') and again.get('2','t2') is None


@pytest.mark.parametrize('mime,expected', [('application/vnd.google-apps.document','server_side'),('application/vnd.google-apps.spreadsheet','server_side'),('application/vnd.google-apps.presentation','server_side'),('application/pdf','download_upload'),('image/jpeg','download_upload'),('application/octet-stream','download_upload')])
def test_sandbox_route_by_type(settings,mime,expected):
    settings.transfer_mode='adaptive';settings.source_domain,settings.target_domain=SANDBOX;settings.source_read_only=False
    decision=choose(settings,{'mimeType':mime,'size':10},['server_side','download_upload','native_api'])
    assert decision['order'][0]==expected
    assert 'link_flip' not in decision['order'] and 'move' not in decision['order']
    if mime.startswith('application/vnd.google-apps.'):assert decision['order']==['server_side']


def test_source_policy_removes_native_route_instead_of_export_fallback(settings):
    settings.transfer_mode='adaptive';settings.source_domain,settings.target_domain=SANDBOX;settings.source_read_only=True
    assert choose(settings,{'mimeType':'application/vnd.google-apps.document'},['server_side','download_upload'])['order']==[]


def test_adaptive_refuses_other_tenants(settings):
    settings.transfer_mode='adaptive'
    with pytest.raises(ValueError):choose(settings,{},['server_side'])


def test_binary_threshold_is_explicit_and_bounded_by_policy(settings):
    settings.transfer_mode='adaptive';settings.source_domain,settings.target_domain=SANDBOX;settings.source_read_only=False;settings.adaptive_binary_server_threshold=10
    assert choose(settings,{'mimeType':'video/mp4','size':11},['server_side','download_upload'])['order'][0]=='server_side'
    settings.source_read_only=True
    assert choose(settings,{'mimeType':'video/mp4','size':11},['server_side','download_upload'])['order']==['download_upload']


def good_result():
    import verify_sample as V
    r=V.Verifier._blank();r.update(checked=1,identical=1,scopeVersion=2,coverageComplete=True)
    return r


def setup_acceptance(monkeypatch,auth,db,settings,identity):
    entries={('drive','file','f'):'{"version":"1"}',('directory','user',SRC_USER):'{}'}
    db.record_mapping(SRC_USER,'f','t','file')
    monkeypatch.setattr(A,'inventory',lambda *a:(copy.deepcopy(entries),{}))
    return A.Acceptance(auth,db,settings,SRC_USER,TGT_USER,{'drive'}),entries


def test_manifest_exists_before_any_copy(auth,db,settings,identity,monkeypatch):
    a,_=setup_acceptance(monkeypatch,auth,db,settings,identity)
    assert a.ready()
    assert db.conn.execute("SELECT count(*) FROM migration_source_manifest WHERE state='PENDING'").fetchone()[0]==2
    assert db.conn.execute('SELECT state FROM migration_acceptance_runs').fetchone()[0]=='COPYING'


def test_inventory_failure_is_blocked_not_an_empty_success(auth,db,settings,identity,monkeypatch):
    monkeypatch.setattr(A,'inventory',lambda *a:({}, {'drive':'403 unread'}))
    a=A.Acceptance(auth,db,settings,SRC_USER,TGT_USER,{'drive'})
    assert not a.ready() and a.errors['drive']=='403 unread'


def test_same_job_repair_then_accepts_and_rechecks_only_changed_service(auth,db,settings,identity,monkeypatch):
    import verify_sample as V,verification_repairs as R
    a,_=setup_acceptance(monkeypatch,auth,db,settings,identity);calls=[]
    def run(*args,**kw):
        calls.append(tuple(args[4]));per={s:good_result() for s in args[4]}
        if len(calls)==1:per['drive']['differences']=[{'source':'f','target':'t','diffs':['grant missing']}]
        return {'users':{SRC_USER:per}}
    monkeypatch.setattr(V,'run',run);monkeypatch.setattr(R,'scoped_repair',lambda *a:{'drive_findings':{'changed':1}})
    result=a.finish(progress=lambda _:None)
    assert result['complete'] and result['repairRounds']==1
    assert calls==[('directory','drive'),('drive',)]
    assert db.conn.execute("SELECT COUNT(*) FROM migration_source_manifest WHERE state='VALIDATED'").fetchone()[0]==2


def test_source_change_and_unreached_item_cannot_pass(auth,db,settings,identity,monkeypatch):
    import verify_sample as V
    a,entries=setup_acceptance(monkeypatch,auth,db,settings,identity)
    entries[('drive','file','f')]='{"version":"2"}'
    entries[('drive','file','never-reached')]='{}'
    monkeypatch.setattr(V,'run',lambda *args,**kw:{'users':{SRC_USER:{s:good_result() for s in args[4]}}})
    result=a.finish(progress=lambda _:None)
    assert not result['complete'] and len(result['sourceChanges'])==2
    assert db.conn.execute("SELECT state FROM migration_source_manifest WHERE source_id='never-reached'").fetchone()[0]=='RECHECK_REQUIRED'


def test_unsupported_features_stop_without_repair_loop(auth,db,settings,identity,monkeypatch):
    import verify_sample as V,verification_repairs as R
    a,_=setup_acceptance(monkeypatch,auth,db,settings,identity)
    def run(*args,**kw):
        per={s:good_result() for s in args[4]};per['drive']['coverageComplete']=False
        return {'users':{SRC_USER:per}}
    monkeypatch.setattr(V,'run',run);monkeypatch.setattr(R,'scoped_repair',lambda *a:{})
    assert a.finish(progress=lambda _:None)['state']=='NEEDS_ATTENTION'


def test_repair_loop_is_bounded(auth,db,settings,identity,monkeypatch):
    import verify_sample as V,verification_repairs as R
    settings.acceptance_repair_rounds=2;a,_=setup_acceptance(monkeypatch,auth,db,settings,identity)
    def run(*args,**kw):
        per={s:good_result() for s in args[4]};per['drive']['missing']=[{'source':'f','target':'gone'}]
        return {'users':{SRC_USER:per}}
    monkeypatch.setattr(V,'run',run);monkeypatch.setattr(R,'scoped_repair',lambda *a:{'drive_findings':{'changed':1}})
    result=a.finish(progress=lambda _:None)
    assert not result['complete'] and result['repairRounds']==2


def test_interruption_retains_manifest_without_certifying(auth,db,settings,identity,monkeypatch):
    import resilience
    a,_=setup_acceptance(monkeypatch,auth,db,settings,identity)
    monkeypatch.setattr(resilience,'shutdown_requested',lambda:True)
    result=a.finish(progress=lambda _:None)
    assert not result['complete'] and result['state']=='INTERRUPTED'


def test_native_id_normalization_preserves_text_styles_and_reference_graph():
    from native_fidelity import canonical_native
    source={'documentId':'a','body':{'content':[{'startIndex':1,'endIndex':5,'paragraph':{'elements':[{'textRun':{'content':'word','textStyle':{'bold':True}},'startIndex':1,'endIndex':5}],'bullet':{'listId':'listA'}}}]},'lists':{'listA':{'x':1}}}
    target=copy.deepcopy(source);target['documentId']='b';target['body']['content'][0]['paragraph']['bullet']['listId']='listB';target['lists']={'listB':{'x':1}}
    assert canonical_native(source)==canonical_native(target)
    target['body']['content'][0]['paragraph']['elements'][0]['textRun']['textStyle']['bold']=False
    assert canonical_native(source)!=canonical_native(target)


def test_comment_comparison_checks_duplicate_multiplicity_resolution_and_replies():
    from migration_comments import differences
    source=[{'id':'s','content':'body','createdTime':'2026-01-01','author':{'displayName':'Name'},'resolved':True,'replies':[{'content':'yes','createdTime':'2026-01-02','author':{'displayName':'Other'}}]}]
    target=[{'content':'[Name, 2026-01-01] body','resolved':True,'replies':[{'content':'[Other, 2026-01-02] yes'}]}]
    assert differences(source,target,lambda _:None)==[]
    assert differences(source,target+target,lambda _:None)
    target[0]['resolved']=False
    assert 'comment resolution differs' in differences(source,target,lambda _:None)


def test_interrupted_mail_is_recovered_inside_original_job(auth,db,settings,identity,monkeypatch):
    import main,gmail_engine,user_profiles
    settings.first_pass_acceptance=True;settings.migration_checks_enabled=True
    settings.source_read_only=True;settings.migrate_groups=False
    settings.acceptance_repair_rounds=1
    monkeypatch.setattr(user_profiles,'sync_profiles',lambda *a,**kw:{'profiles':[{'status':'MATCHED'}]})
    def directory(side):
        address=SRC_USER if side=='source' else TGT_USER
        profile={'id':side,'primaryEmail':address,'name':{'givenName':'Exact','familyName':'Name'},'emails':[{'address':address,'primary':True}]}
        return NS(users=lambda:NS(get=lambda **kw:request(profile)))
    monkeypatch.setattr(auth,'directory',directory,raising=False)
    src=auth.source_gmail(SRC_USER)
    for i in range(5):src.add_message(f'Message-ID: <firstpass-{i}@test>\r\nFrom: sender@test\r\nTo: {SRC_USER}\r\nSubject: item{i}\r\n\r\nbody{i}'.encode())
    before=copy.deepcopy(src.messages)
    original=gmail_engine.GmailMigrator._iter_messages
    def partial(self,query):yield next(iter(original(self,query)))
    monkeypatch.setattr(gmail_engine.GmailMigrator,'_iter_messages',partial)
    monkeypatch.setattr(main.VERIFY,'submit',lambda *a,**kw:pytest.fail('No standalone verification job should be queued'))
    result=main.migrate_user(auth,db,settings,SRC_USER,TGT_USER,{'gmail'},False,0)
    assert result['status']=='DONE',result
    assert result['acceptance']['complete'] and result['acceptance']['repairRounds']==1
    assert db.conn.execute("SELECT COUNT(*) FROM id_mapping WHERE type='message'").fetchone()[0]==5
    assert src.messages==before
    assert db.conn.execute("SELECT COUNT(*) FROM migration_source_manifest WHERE state='VALIDATED'").fetchone()[0]==6


def test_chunked_sheet_read_spans_large_grid_and_every_tab():
    from native_repair import read_sheet
    calls=[]
    meta={'properties':{'title':'Big'},'sheets':[{'properties':{'sheetId':1,'title':'One','gridProperties':{'rowCount':6000,'columnCount':50}}},{'properties':{'sheetId':2,'title':'Two','gridProperties':{'rowCount':3,'columnCount':4}}}]}
    class Sheets:
        def spreadsheets(self):return self
        def get(self,**kw):return request(meta)
        def getByDataFilter(self,**kw):
            grid=kw['body']['dataFilters'][0]['gridRange'];calls.append(grid)
            return request({'sheets':[{'properties':{'sheetId':grid['sheetId']},'data':[{'startRow':grid['startRowIndex'],'startColumn':grid['startColumnIndex'],'rowData':[]}]}]})
    result=read_sheet(Sheets(),'id')
    assert len(result['sheets'])==2 and {c['sheetId'] for c in calls}=={1,2}
    assert max((c['endRowIndex']-c['startRowIndex'])*(c['endColumnIndex']-c['startColumnIndex']) for c in calls)<=10000
    assert max(c['endRowIndex'] for c in calls if c['sheetId']==1)==6000


def test_large_manifest_retains_duplicate_names_and_distinct_folder_ids(auth,db,settings,identity,monkeypatch):
    import verify_sample as V
    entries={('drive','file',str(i)):'{"name":"same.txt","parent":"p'+str(i%200)+'"}' for i in range(5000)}
    entries.update({('drive','folder','p'+str(i)):'{"name":"same folder"}' for i in range(200)})
    entries[('directory','user',SRC_USER)]='{}'
    monkeypatch.setattr(A,'inventory',lambda *a:(copy.deepcopy(entries),{}))
    a=A.Acceptance(auth,db,settings,SRC_USER,TGT_USER,{'drive'})
    assert db.conn.execute('SELECT COUNT(*) FROM migration_source_manifest').fetchone()[0]==5201
    assert len(a.before)==5201
    monkeypatch.setattr(V,'run',lambda *args,**kw:{'users':{SRC_USER:{s:good_result() for s in args[4]}}})
    missing=a.finish(progress=lambda _:None)
    assert not missing['complete'] and len(missing['mappingGaps'])==5200
    assert db.conn.execute("SELECT COUNT(*) FROM migration_source_manifest WHERE state='VALIDATED' AND service='drive'").fetchone()[0]==0
    for service,kind,sid in entries:
        if service=='drive':db.record_mapping(SRC_USER,sid,'target-'+sid,kind)
    accepted=a.finish(progress=lambda _:None)
    assert accepted['complete'] and accepted['mappingGaps']==[]


def test_cancelled_verifier_retains_pending_items(auth,db,settings,identity,monkeypatch):
    import verify_sample as V
    a,_=setup_acceptance(monkeypatch,auth,db,settings,identity)
    def cancel(*args,**kwargs):raise V.VerificationCancelled('stopped')
    monkeypatch.setattr(V,'run',cancel)
    result=a.finish(progress=lambda _:None)
    assert not result['complete'] and result['state']=='INTERRUPTED'
    assert db.conn.execute("SELECT COUNT(*) FROM migration_source_manifest WHERE state='VALIDATED'").fetchone()[0]==0


def test_ambiguous_comment_create_never_retries_into_a_duplicate(db,settings,identity):
    from migration_comments import reconcile
    db.record_mapping(SRC_USER,'sfile','tfile','file')
    source_comment={'id':'comment','content':'text','author':{'displayName':'Name'},'createdTime':'2026-01-01'}
    creates=[]
    class Drive:
        def __init__(self,source):self.source=source
        def comments(self):return self
        def list(self,**kw):return request({'comments':[source_comment] if self.source else []})
        def create(self,**kw):
            creates.append(kw)
            return request(error=TimeoutError('response lost'))
    dm=NS(source_user=SRC_USER,target_user=TGT_USER,settings=settings,db=db,src=Drive(True),tgt=Drive(False),_retry=lambda fn,**kw:fn())
    with pytest.raises(TimeoutError):reconcile(dm,'sfile','tfile')
    with pytest.raises(RuntimeError,match='uncertain'):reconcile(dm,'sfile','tfile')
    assert len(creates)==1


def test_directory_managed_aliases_are_separate_from_profile_email_fields():
    import user_profiles as P
    source={"name":{"givenName":"Exact नाम","familyName":"Surname"},"primaryEmail":SRC_USER,
            "nonEditableAliases":[SRC_USER+".test-google-a.com"],"aliases":["alias@tenanta.com"],
            "emails":[{"address":SRC_USER,"primary":True},{"address":SRC_USER+".test-google-a.com"},
                      {"address":"alias@tenanta.com"},{"address":"contact@external.com","type":"home"}]}
    target=copy.deepcopy(source);target["primaryEmail"]=TGT_USER
    target["nonEditableAliases"]=[TGT_USER+".test-google-a.com"];target["aliases"]=["alias@tenantb.com"]
    target["emails"][0]["address"]=TGT_USER;target["emails"][1]["address"]=TGT_USER+".test-google-a.com"
    target["emails"][2]["address"]="alias@tenantb.com"
    body=P.profile_body(source,TGT_USER,{SRC_USER:TGT_USER})
    assert len(body["emails"])==2 and body["name"]["givenName"]=="Exact नाम"
    assert P.differences(body,target)==[]
    target["emails"][-1]["address"]="changed@external.com"
    assert P.differences(body,target)==["emails"]


def test_rewrite_receipt_status_change_invalidates_cached_message(db,settings):
    s,t=seed(db,settings)
    db.log_audit(SRC_USER,'1','link_rewrite','SUCCESS','link rewritten')
    cache=C.MailCache(verifier(db,settings),s,t,({},{}))
    assert cache.get('1','t1') is None


def test_missing_manifest_mapping_is_saved_and_queued(auth,db,settings,identity,monkeypatch):
    import verify_sample as V
    a,_=setup_acceptance(monkeypatch,auth,db,settings,identity)
    db.forget_mapping(SRC_USER,'f','file')
    monkeypatch.setattr(V,'run',lambda *args,**kw:{'users':{SRC_USER:{s:good_result() for s in args[4]}}})
    result=a.finish(repair=False,progress=lambda _:None)
    assert not result['complete'] and result['mappingGaps'][0]['id']=='f'
    assert db.conn.execute("SELECT COUNT(*) FROM verification_repair_queue WHERE state='QUEUED'").fetchone()[0]>=1
    assert next(row for row in db.user_verifications() if row['service']=='drive')['verdict']!='IDENTICAL'


def test_api_reports_latest_acceptance_without_mutating_ledger(auth,db,settings,identity,monkeypatch):
    import api_server as api
    a,_=setup_acceptance(monkeypatch,auth,db,settings,identity)
    # Runs can have the same second timestamp. The later row alone counts.
    later=A.Acceptance(auth,db,settings,SRC_USER,TGT_USER,{'drive'})
    monkeypatch.setattr(api,'_ledger_path',lambda _:settings.db_path)
    before=db.conn.total_changes
    result=api._migration_checks_view(6)
    assert len(result['acceptanceRuns'])==2
    assert sum(row['count'] for row in result['manifestTotals'])==2
    assert db.conn.total_changes==before


def test_staging_copy_repair_uses_new_read_only_source_context(db,settings,identity,monkeypatch):
    import auth as auth_module, verification_repairs as R
    import repair,retry_failed,finding_repairs
    contexts=[]
    class Context:
        def __init__(self,st):self.settings=st;contexts.append(self)
    monkeypatch.setattr(auth_module,'AuthManager',Context)
    settings.source_read_only=False;settings.transfer_mode='server_side'
    original=Context(settings)
    monkeypatch.setattr(repair,'repair_verified_gmail_labels',lambda a,d,st,*args,**kw: {'context':a,'readOnly':st.source_read_only})
    monkeypatch.setattr(retry_failed,'retry',lambda *a,**kw:{})
    result=R.scoped_repair(original,db,settings,SRC_USER,{'gmail':good_result()})
    context=result['gmail_labels']['context']
    assert context is not original and context.settings.source_read_only
    assert context.settings.transfer_mode=='download_upload'
    assert settings.source_read_only is False and settings.transfer_mode=='server_side'


@pytest.mark.parametrize('change', ['sample','split','delta','untracked','disabled'])
def test_inline_acceptance_never_expands_a_sample_or_dms_handoff(settings,change):
    import main
    settings.first_pass_acceptance=True;settings.sample_limit=None;settings.mail_only_with_links=False
    assert main.inline_acceptance_enabled(settings)
    kwargs={}
    if change=='sample':settings.sample_limit=5
    if change=='split':settings.mail_only_with_links=True
    if change=='delta':kwargs['delta']=True
    if change=='untracked':kwargs['track_status']=False
    if change=='disabled':settings.first_pass_acceptance=False
    assert not main.inline_acceptance_enabled(settings,**kwargs)
