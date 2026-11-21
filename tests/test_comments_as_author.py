"""Each migrated comment is posted as its author's target account (domain-wide delegation); the owner posts it,
naming the author, only when Google refuses that (operator, 21 Nov 2026)."""
from types import SimpleNamespace

import migration_comments as mc


class _Exec(SimpleNamespace):
    def execute(self):
        return self.fn()


class _Refused(Exception):
    resp = SimpleNamespace(status=403)


class Drive:
    """One file's comments, shared by every account's client, each posting under its own name."""
    def __init__(self, store, who, perms=(), allowed=True):
        self.store, self.who, self.perms, self.allowed = store, who, list(perms), allowed

    def comments(self):
        drive = self

        class C:
            def list(self, **kw):
                return _Exec(fn=lambda: {"comments": [dict(c) for c in drive.store]})

            def create(self, fileId, body, **kw):
                def go():
                    if not drive.allowed:
                        raise _Refused()
                    c = {"id": f"c{len(drive.store)}", "content": body["content"], "author": {"displayName": drive.who},
                         "replies": [], **{k: body[k] for k in ("anchor",) if k in body}}
                    drive.store.append(c)
                    return {"id": c["id"]}
                return _Exec(fn=go)
        return C()

    def replies(self):
        drive = self

        class R:
            def create(self, fileId, commentId, body, **kw):
                def go():
                    if not drive.allowed:
                        raise _Refused()
                    c = next(c for c in drive.store if c["id"] == commentId)
                    c["replies"].append({"id": f"r{len(c['replies'])}", "content": body["content"],
                                         "action": body.get("action", ""), "author": {"displayName": drive.who}})
                    return {"id": c["replies"][-1]["id"]}
                return _Exec(fn=go)
        return R()

    def permissions(self):
        drive = self
        return SimpleNamespace(list=lambda **kw: _Exec(fn=lambda: {"permissions": drive.perms}))


def _migrator(db, settings, source_comments, allowed=("carol@b.test",)):
    settings.source_domain = "a.test"
    db.conn.execute("INSERT OR REPLACE INTO identity_map (source_email, target_email) VALUES ('carol@a.test', 'carol@b.test')")
    db.record_mapping("dave@a.test", "S1", "T1", "file")
    src = Drive(source_comments, "src", perms=[{"id": "p-carol", "type": "user", "emailAddress": "carol@a.test"},
                                               {"id": "p-out", "type": "user", "emailAddress": "x@elsewhere.test"}])
    shared = []
    tgt = Drive(shared, "Dave B")
    auth = SimpleNamespace(target_drive=lambda who: Drive(shared, who, allowed=who in allowed))
    return SimpleNamespace(settings=settings, db=db, src=src, tgt=tgt, auth=auth, source_user="dave@a.test",
                           target_user="dave@b.test", _retry=lambda fn, **kw: fn()), shared


def _comment(cid, text, author, when="2026-11-17T13:58:40Z", replies=()):
    return {"id": cid, "content": text, "createdTime": when, "author": author, "replies": list(replies)}


def test_a_colleagues_comment_is_posted_as_them_with_its_date(db, settings):
    src = [_comment("a", "Numbers in row 12 look off.", {"displayName": "Carol", "permissionId": "p-carol"},
                    replies=[{"id": "r", "content": "Fixed.", "createdTime": "2026-11-18T09:00:00Z", "action": "resolve",
                              "author": {"displayName": "Dave", "me": True}}])]
    m, target = _migrator(db, settings, src)
    mc.reconcile(m, "S1", "T1")
    (c,) = target
    assert c["author"]["displayName"] == "carol@b.test" and c["content"] == "Numbers in row 12 look off.\n\n(originally 2026-11-17)"
    assert c["replies"][0]["author"]["displayName"] == "Dave B" and c["replies"][0]["action"] == "resolve"
    assert mc.differences(src, target, lambda _: None) == []
    mc.reconcile(m, "S1", "T1")          # a second pass adopts what is there
    assert len(target) == 1 and len(target[0]["replies"]) == 1


def test_an_author_without_access_or_account_is_named_by_the_owner(db, settings):
    src = [_comment("a", "Who approved this?", {"displayName": "Carol", "permissionId": "p-carol"}),
           _comment("b", "From outside.", {"displayName": "X", "permissionId": "p-out"})]
    m, target = _migrator(db, settings, src, allowed=())
    mc.reconcile(m, "S1", "T1")
    assert [c["author"]["displayName"] for c in target] == ["Dave B", "Dave B"]
    assert target[0]["content"] == "[Carol, 2026-11-17] Who approved this?"
    assert mc.differences(src, target, lambda _: None) == []


def test_switched_off_it_posts_as_the_owner(db, settings):
    settings.comments_as_author = False
    src = [_comment("a", "Hi.", {"displayName": "Carol", "permissionId": "p-carol"})]
    m, target = _migrator(db, settings, src)
    mc.reconcile(m, "S1", "T1")
    assert target[0]["author"]["displayName"] == "Dave B" and target[0]["content"].startswith("[Carol, ")
