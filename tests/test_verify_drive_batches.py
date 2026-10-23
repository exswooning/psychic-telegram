from types import SimpleNamespace

import pytest

import verify_sample as V


class ReadError(Exception):
    def __init__(self, status):
        self.resp=SimpleNamespace(status=status)
        super().__init__(str(status))


class Service:
    def __init__(self, errors=None):
        self.errors=errors or {}
        self.direct=[]
        self.batches=[]
    def files(self):
        return self
    def permissions(self):
        pytest.fail('combined response already provides permissions')
    def get(self, fileId, **kwargs):
        def execute():
            self.direct.append(fileId)
            return {'id':fileId,'name':fileId,'permissions':[{'type':'user','role':'reader','emailAddress':'x@tgt'}]}
        return SimpleNamespace(execute=execute)
    def new_batch_http_request(self, callback):
        requests=[]
        self.batches.append(requests)
        def add(request, request_id):
            requests.append((request_id,request))
        def execute():
            # A batch may complete in any order. IDs, not callback positions,
            # must select the file to compare and the associated permissions.
            for fid,request in reversed(requests):
                error=self.errors.get(fid)
                callback(fid,None if error else {'id':fid,'name':fid,'permissions':[]},error)
        return SimpleNamespace(add=add,execute=execute)


def test_out_of_order_batch_keeps_file_identity_and_uses_inline_permissions(db,settings):
    svc=Service()
    v=V.Verifier(None,db,settings,'u@src','u@tgt')
    data,errors=v._drive_meta_many(svc,['a','b'],'source')
    assert not errors
    assert data['a']['id']=='a' and data['b']['id']=='b'
    assert v._perms(svc,'a')==[]
    assert v._perms(svc,'b')==[]
    assert not svc.direct


def test_per_item_errors_are_not_dropped_and_transient_reads_retry(db,settings):
    svc=Service({'gone':ReadError(404),'retry':ReadError(503)})
    v=V.Verifier(None,db,settings,'u@src','u@tgt')
    data,errors=v._drive_meta_many(svc,['ok','gone','retry'],'source')
    assert v._drive_meta_from_batch(svc,'ok',data,errors)['id']=='ok'
    with pytest.raises(ReadError):
        v._drive_meta_from_batch(svc,'gone',data,errors)
    assert v._drive_meta_from_batch(svc,'retry',data,errors)['id']=='retry'
    assert svc.direct==['retry']


def test_missing_callback_falls_back_to_normal_read(db,settings):
    svc=Service()
    v=V.Verifier(None,db,settings,'u@src','u@tgt')
    assert v._drive_meta_from_batch(svc,'missing',{},{} )['id']=='missing'
    assert svc.direct==['missing']


def test_drive_windows_are_bounded_and_every_mapping_is_checked(db,settings,monkeypatch):
    src,tgt=object(),object()
    auth=SimpleNamespace(source_drive=lambda u:src,target_drive=lambda u:tgt)
    for i in range(31):
        db.record_mapping('u@src',f's{i}',f't{i}','file')
    v=V.Verifier(auth,db,settings,'u@src','u@tgt')
    calls=[]
    def many(svc,ids,side):
        calls.append((side,ids))
        return {fid:{'id':fid,'name':fid[1:],'mimeType':'application/pdf','ownedByMe':True} for fid in ids},{}
    monkeypatch.setattr(v,'_drive_meta_many',many)
    monkeypatch.setattr(v,'_drive_path',lambda svc,fid,cache:'/'+fid[1:])
    monkeypatch.setattr(v,'_content',lambda *a:[])
    monkeypatch.setattr(v,'_sharing',lambda *a:[])
    monkeypatch.setattr(v,'_target_files',lambda svc:[])
    result=v.drive()
    assert result['checked']==31 and result['identical']==31
    assert max(len(ids) for _,ids in calls)==V.DRIVE_METADATA_BATCH_SIZE
    assert sum(len(ids) for side,ids in calls if side=='source')==31
    assert sum(len(ids) for side,ids in calls if side=='target')==31


def test_acl_fallback_reads_every_page(db,settings):
    pages=[]
    class Paged:
        def permissions(self):return self
        def list(self,**kwargs):
            token=kwargs['pageToken'];pages.append(token)
            response=({'permissions':[{'id':'a'}],'nextPageToken':'next'} if token is None else {'permissions':[{'id':'b'}]})
            return SimpleNamespace(execute=lambda:response)
    v=V.Verifier(None,db,settings,'u@src','u@tgt')
    assert v._perms(Paged(),'file')==[{'id':'a'},{'id':'b'}]
    assert pages==[None,'next']
