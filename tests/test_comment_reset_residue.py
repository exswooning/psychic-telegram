from types import SimpleNamespace
import pytest


def test_drive_reset_forgets_reply_mappings_and_delivery_receipts_only_for_that_user(db):
    from reset_drive_ledger import reset_service_ledger
    with db.write() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS comment_delivery_attempts (
            source_user TEXT,item_key TEXT,target_file TEXT,state TEXT,updated_at TEXT,
            PRIMARY KEY(source_user,item_key))''')
        for user in ('one@s.example','other@s.example'):
            c.execute('INSERT INTO comment_delivery_attempts VALUES(?,?,?,?,?)',
                      (user,'file::comment::reply','old-copy','CONFIRMED','before-wipe'))
    for user in ('one@s.example','other@s.example'):
        db.record_mapping(user,'file::comment::reply','old-reply','comment_reply')
    db.record_mapping('one@s.example','mail','keep-mail','message')
    db.preload_mappings('one@s.example')
    reset_service_ledger(db,'one@s.example',('drive',))
    assert db.get_target_id('one@s.example','file::comment::reply','comment_reply') is None
    assert db.get_target_id('other@s.example','file::comment::reply','comment_reply')=='old-reply'
    assert db.get_target_id('one@s.example','mail','message')=='keep-mail'
    assert db.conn.execute('SELECT source_user FROM comment_delivery_attempts').fetchall()[0][0]=='other@s.example'


@pytest.mark.parametrize('old_file,state,allowed', [
    ('old-target','CONFIRMED',True),('old-target','UNCERTAIN',True),
    ('new-target','CONFIRMED',False),('new-target','UNCERTAIN',False),
    (None,None,False),('','CONFIRMED',False),('  ','CONFIRMED',False),
])
def test_stale_reply_mapping_requires_proof_it_belongs_to_another_target_copy(
    db,settings,monkeypatch,old_file,state,allowed
):
    import migration_comments as comments
    from tests.conftest import SRC_USER,TGT_USER
    db.record_mapping(SRC_USER,'source-file','new-target','file')
    db.record_mapping(SRC_USER,'source-file::c','current-comment','comment')
    db.record_mapping(SRC_USER,'source-file::c::r','old-reply','comment_reply')
    with db.write() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS comment_delivery_attempts (
            source_user TEXT,item_key TEXT,target_file TEXT,state TEXT,updated_at TEXT,
            PRIMARY KEY(source_user,item_key))''')
        if old_file is not None:
            c.execute('INSERT INTO comment_delivery_attempts VALUES(?,?,?,?,?)',
                      (SRC_USER,'source-file::c::r',old_file,state,'before-copy'))
    src={'id':'c','content':'thread','author':{'displayName':'A'},'createdTime':'2026-01-01T00:00:00Z',
         'replies':[{'id':'r','content':'reply','author':{'displayName':'B'},'createdTime':'2026-01-01T00:00:00Z'}]}
    target={'id':'current-comment','content':'[A, 2026-01-01] thread','replies':[]}
    monkeypatch.setattr(comments,'list_threads',lambda svc,fid,execute=None: [src] if fid=='source-file' else [target])
    writes=[]
    def create(**kw):
        assert kw['fileId']=='new-target' and kw['commentId']=='current-comment'
        def execute():
            writes.append(kw)
            target['replies'].append({'id':'new-reply',**kw['body']})
            return {'id':'new-reply'}
        return SimpleNamespace(execute=execute)
    migrator=SimpleNamespace(db=db,settings=settings,source_user=SRC_USER,target_user=TGT_USER,
                             src=None,tgt=SimpleNamespace(replies=lambda: SimpleNamespace(create=create)))
    if allowed:
        assert comments.reconcile(migrator,'source-file','new-target')==1
        assert comments.reconcile(migrator,'source-file','new-target')==0
        assert len(writes)==1
        assert db.get_target_id(SRC_USER,'source-file::c::r','comment_reply')=='new-reply'
        row=db.conn.execute("SELECT target_file,state FROM comment_delivery_attempts WHERE item_key='source-file::c::r'").fetchone()
        assert tuple(row)==('new-target','CONFIRMED')
    else:
        with pytest.raises(RuntimeError,match='Mapped comment/reply is missing'):
            comments.reconcile(migrator,'source-file','new-target')
        assert writes==[]
