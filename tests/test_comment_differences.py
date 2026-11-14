"""Two comment threads with the same words on different spots of one file are told apart by
their anchors (live, finance@s.: a PDF carrying "[FINANCE, 2023-01-13]" twice read as
"missing or ambiguous" plus an "extra" thread, though both had been copied exactly)."""
import pytest

from migration_comments import differences

A1, A2 = "[null,null,[[[5,[[0.67,0.84,0.91,0.86]]]]]]", "[null,null,[[[5,[[0.67,0.84,0.74,0.86]]]]]]"


def _src(anchor, t):
    return {"content": "[FINANCE, 2023-01-13]", "anchor": anchor, "createdTime": t,
            "author": {"displayName": "Finance"}, "replies": []}


def _tgt(anchor):
    return {"content": "[Finance, 2026-10-25] [FINANCE, 2023-01-13]", "anchor": anchor, "replies": []}


def test_same_words_different_anchors_are_identical():
    source = [_src(A1, "2026-10-25T01:44:41Z"), _src(A2, "2026-10-25T01:44:38Z")]
    assert differences(source, [_tgt(A2), _tgt(A1)], lambda fid: None) == []


def test_a_missing_twin_is_still_found():
    source = [_src(A1, "2026-10-25T01:44:41Z"), _src(A2, "2026-10-25T01:44:38Z")]
    assert differences(source, [_tgt(A1)], lambda fid: None) == ["comment thread missing or ambiguous"]


def test_an_action_only_reply_with_drive_trimmed_space_is_not_a_difference():
    """Live, info@s. (2026-11-03): 6 action-only replies were refused as 'differs; preserve' because
    the engine wrote '[Author, date] ' and Drive kept '[Author, date]'. Nothing had changed."""
    from migration_comments import _text
    assert _text("[Author, 2020-11-11] ") == _text("[Author, 2020-11-11]") == "[Author, 2020-11-11]"
    author = {"displayName": "Author"}
    src_reply = {"id": "r1", "content": "", "action": "RESOLVE", "createdTime": "2020-11-11T09:00:00Z", "author": author}
    src = {"content": "[Finance, 2023-01-13] x", "anchor": A1, "replies": [src_reply]}
    tgt = {"content": "[Finance, 2023-01-13] x", "anchor": A1,
           "replies": [{"id": "t1", "content": "[Author, 2020-11-11]", "action": "RESOLVE"}]}
    assert differences([src], [tgt], lambda fid: None) == []


def test_a_real_reply_edit_on_the_target_is_still_a_difference():
    author = {"displayName": "Author"}
    src_reply = {"id": "r1", "content": "please check", "createdTime": "2020-11-11T09:00:00Z", "author": author}
    src = {"content": "[Finance, 2023-01-13] x", "anchor": A1, "replies": [src_reply]}
    tgt = {"content": "[Finance, 2023-01-13] x", "anchor": A1,
           "replies": [{"id": "t1", "content": "[Author, 2020-11-11] please check, edited by a person"}]}
    assert differences([src], [tgt], lambda fid: None) == ["comment replies differ"]


def test_reconcile_does_not_refuse_an_action_only_reply_already_copied(db, settings, monkeypatch):
    """The live path: reconcile -> deliver refused the reply with 'differs; preserve'."""
    from types import SimpleNamespace
    import migration_comments as MC
    from tests.conftest import SRC_USER, TGT_USER
    settings.source_domain = "s.example"
    db.record_mapping(SRC_USER, "src-file", "tgt-file", "file")
    author = {"displayName": "Author"}
    src_reply = {"id": "r1", "content": "", "action": "RESOLVE", "createdTime": "2020-11-11T09:00:00Z", "author": author}
    src_thread = {"id": "c1", "content": "x", "anchor": A1, "replies": [src_reply],
                  "author": {"displayName": "Finance"}, "createdTime": "2023-01-13T10:00:00Z"}
    tgt_thread = {"id": "tc1", "content": "[Finance, 2023-01-13] x", "anchor": A1,
                  "replies": [{"id": "tr1", "content": "[Author, 2020-11-11]", "action": "RESOLVE"}]}
    db.record_mapping(SRC_USER, "src-file::c1", "tc1", "comment")
    db.record_mapping(SRC_USER, "src-file::c1::r1", "tr1", "comment_reply")
    answers = iter([[src_thread], [tgt_thread]])
    monkeypatch.setattr(MC, "list_threads", lambda svc, fid, execute=None: next(answers))
    migrator = SimpleNamespace(db=db, settings=settings, source_user=SRC_USER, target_user=TGT_USER,
                               src=None, tgt=None, _retry=lambda fn, **k: fn(), _tgt_write_limiter=None,
                               _project_limiter=None)
    migrator.target_user = TGT_USER
    assert MC.reconcile(migrator, "src-file", "tgt-file") == 0


@pytest.mark.parametrize("mapped", [False, True])
@pytest.mark.parametrize("changed_quote", [False, True])
def test_reconcile_preserves_structured_quote_and_target_edits(
    db, settings, monkeypatch, mapped, changed_quote
):
    """A quote is an object, not text; matching must not crash or lose its fields."""
    from copy import deepcopy
    from types import SimpleNamespace
    import migration_comments as MC
    from tests.conftest import SRC_USER, TGT_USER

    db.record_mapping(SRC_USER, "quoted-file", "target-file", "file")
    source = {"id": "c1", "content": "review", "author": {"displayName": "Author"},
              "createdTime": "2026-11-01T00:00:00Z", "anchor": A1,
              "quotedFileContent": {"mimeType": "text/plain", "value": "quote "}}
    target = deepcopy(source)
    target.update(id="tc1", content="[Author, 2026-11-01] review ")
    if changed_quote:
        target["quotedFileContent"]["value"] = "quote"
    if mapped:
        db.record_mapping(SRC_USER, "quoted-file::c1", "tc1", "comment")
    answers = iter([[source], [target]])
    monkeypatch.setattr(MC, "list_threads", lambda *a, **k: next(answers))
    class NoWrites:
        def comments(self):
            raise AssertionError("Must not write a duplicate or overwrite a target edit")
    migrator = SimpleNamespace(db=db, settings=settings, source_user=SRC_USER,
                              target_user=TGT_USER, src=None, tgt=NoWrites())
    if changed_quote and not mapped:
        # An uncertain previous delivery must still block any duplicate create.
        with db.write() as conn:
            conn.execute("CREATE TABLE comment_delivery_attempts (source_user TEXT,item_key TEXT,target_file TEXT,state TEXT,updated_at TEXT,PRIMARY KEY(source_user,item_key))")
            conn.execute("INSERT INTO comment_delivery_attempts VALUES(?,?,?,'UNCERTAIN','now')",
                         (SRC_USER, "quoted-file::c1", "target-file"))
    if changed_quote:
        with pytest.raises(RuntimeError, match="differs|uncertain"):
            MC.reconcile(migrator, "quoted-file", "target-file")
    else:
        assert MC.reconcile(migrator, "quoted-file", "target-file") == 0
        assert db.get_target_id(SRC_USER, "quoted-file::c1", "comment") == "tc1"


def test_a_comment_quoting_file_content_is_compared_not_crashed():
    """Live (health@, ward15@, ward4@, 7 Nov 2026): quotedFileContent is an object, and trimming it as text
    raised "'dict' object has no attribute 'rstrip'" -- the comment failed instead of matching."""
    from migration_comments import _text
    quoted = {"mimeType": "text/html", "value": "the quoted words"}
    assert _text(quoted) == quoted and _text(None) == "" and _text("a  ") == "a"
    src = {"content": "x", "anchor": A1, "quotedFileContent": quoted, "replies": [],
           "author": {"displayName": "Finance"}, "createdTime": "2023-01-13T10:00:00Z"}
    tgt = {"content": "[Finance, 2023-01-13] x", "anchor": A1, "quotedFileContent": dict(quoted), "replies": []}
    assert differences([src], [tgt], lambda fid: None) == []


def test_reconcile_accepts_a_thread_that_quotes_file_content(db, settings, monkeypatch):
    from types import SimpleNamespace
    import migration_comments as MC
    from tests.conftest import SRC_USER, TGT_USER
    settings.source_domain = "s.example"
    db.record_mapping(SRC_USER, "src-file", "tgt-file", "file")
    quoted = {"mimeType": "text/html", "value": "quoted"}
    src_thread = {"id": "c1", "content": "x", "anchor": A1, "quotedFileContent": quoted, "replies": [],
                  "author": {"displayName": "Finance"}, "createdTime": "2023-01-13T10:00:00Z"}
    tgt_thread = {"id": "tc1", "content": "[Finance, 2023-01-13] x", "anchor": A1,
                  "quotedFileContent": dict(quoted), "replies": []}
    db.record_mapping(SRC_USER, "src-file::c1", "tc1", "comment")
    answers = iter([[src_thread], [tgt_thread]])
    monkeypatch.setattr(MC, "list_threads", lambda svc, fid, execute=None: next(answers))
    migrator = SimpleNamespace(db=db, settings=settings, source_user=SRC_USER, target_user=TGT_USER,
                               src=None, tgt=None, _retry=lambda fn, **k: fn(), _tgt_write_limiter=None,
                               _project_limiter=None)
    assert MC.reconcile(migrator, "src-file", "tgt-file") == 0


def _reconcile_with_attempt(db, settings, monkeypatch, attempt_file):
    """One Doc re-copied to tgt-file-new; an earlier attempt for its comment is on record against `attempt_file`."""
    from types import SimpleNamespace
    import migration_comments as MC
    from tests.conftest import SRC_USER, TGT_USER
    settings.source_domain = "s.example"
    db.record_mapping(SRC_USER, "src-file", "tgt-file-new", "file")
    with db.write() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS comment_delivery_attempts (
            source_user TEXT,item_key TEXT,target_file TEXT,state TEXT,updated_at TEXT,
            PRIMARY KEY(source_user,item_key))""")
        c.execute("INSERT INTO comment_delivery_attempts VALUES(?,?,?,'CONFIRMED','2026-11-01T00:00:00Z')",
                  (SRC_USER, "src-file::c1", attempt_file))
    src_thread = {"id": "c1", "content": "x", "anchor": A1, "replies": [],
                  "author": {"displayName": "Finance"}, "createdTime": "2023-01-13T10:00:00Z"}
    answers = iter([[src_thread], []])
    monkeypatch.setattr(MC, "list_threads", lambda svc, fid, execute=None: next(answers))
    created = []

    class Create:
        def execute(self):
            created.append(1)
            return {"id": "tc-new"}
    migrator = SimpleNamespace(db=db, settings=settings, source_user=SRC_USER, target_user=TGT_USER,
                               src=None, tgt=SimpleNamespace(comments=lambda: SimpleNamespace(
                                   create=lambda **kw: Create())),
                               _retry=lambda fn, **k: fn(), _tgt_write_limiter=None, _project_limiter=None)
    return MC, migrator, created


def test_an_attempt_on_an_old_copy_does_not_block_the_new_copys_comments(db, settings, monkeypatch):
    MC, migrator, created = _reconcile_with_attempt(db, settings, monkeypatch, "tgt-file-OLD")
    assert MC.reconcile(migrator, "src-file", "tgt-file-new") == 1 and created == [1]
    row = db.conn.execute("SELECT target_file, state FROM comment_delivery_attempts").fetchone()
    assert tuple(row) == ("tgt-file-new", "CONFIRMED")


def test_an_attempt_on_the_same_file_still_waits_for_a_person(db, settings, monkeypatch):
    MC, migrator, created = _reconcile_with_attempt(db, settings, monkeypatch, "tgt-file-new")
    with pytest.raises(RuntimeError, match="Prior uncertain"):
        MC.reconcile(migrator, "src-file", "tgt-file-new")
    assert created == []
