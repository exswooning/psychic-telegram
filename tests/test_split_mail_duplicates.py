"""Two source messages may share one target copy only when they are the same email byte for byte."""
import base64
from types import SimpleNamespace

import split_mail


def _gm(raw: bytes):
    class Call:
        def execute(self):
            return {"raw": base64.urlsafe_b64encode(raw).decode().rstrip("=")}
    msgs = SimpleNamespace(get=lambda **kw: Call())
    src = SimpleNamespace(users=lambda: SimpleNamespace(messages=lambda: msgs))
    return SimpleNamespace(src=src, _retry=lambda f: f())


def test_the_same_email_twice_on_the_source_may_share_one_copy():
    raw = b"Message-ID: <twice@seed.test>\r\nSubject: One Message-ID, twice\r\n\r\nsame id\r\n"
    assert split_mail.same_source_email(_gm(raw), "other", base64.urlsafe_b64encode(raw).decode())


def test_copies_that_differ_only_in_gmails_received_stamp_are_one_email():
    a = (b"Received: by 2002:a05:7000::1; Tue, 17 Nov 2026 11:51:21 -0800\r\nMessage-ID: <t@seed.test>\r\n"
         b"Subject: One Message-ID, twice\r\n\r\nsame id\r\n")
    b = a.replace(b"11:51:21", b"11:51:20")
    assert split_mail.same_source_email(_gm(b), "other", base64.urlsafe_b64encode(a).decode())


def test_a_different_email_never_does():
    a = b"Message-ID: <x@seed.test>\r\n\r\none\r\n"
    b = b"Message-ID: <x@seed.test>\r\n\r\ntwo\r\n"
    assert not split_mail.same_source_email(_gm(b), "other", base64.urlsafe_b64encode(a).decode())


def test_only_the_proven_twin_may_share_a_target(tmp_path):
    import pytest
    from db import MigrationDB
    d = MigrationDB(str(tmp_path / "l.db"))
    d.adopt_message_mapping("u@a", "first", "T")
    d.adopt_message_mapping("u@a", "second", "T", shares_with="first")
    with pytest.raises(RuntimeError):
        d.adopt_message_mapping("u@a", "third", "T", shares_with="nobody")


def test_two_copies_are_two_even_when_gmail_sends_a_page_token():
    """Gmail tokens a full page with nothing after it: maxResults=2 over two copies read as 'more' (18 Nov 2026)."""
    import split_mail
    pages = {None: {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "t"}, "t": {"messages": []}}

    class Msgs:
        def list(self, userId, q, includeSpamTrash, maxResults, pageToken=None):
            page = dict(pages[pageToken])
            page["messages"] = page["messages"][:maxResults]
            return type("R", (), {"execute": lambda s: page})()

    gm = type("G", (), {"_retry": staticmethod(lambda fn, **k: fn()),
                        "tgt": type("T", (), {"users": lambda s: type("U", (), {"messages": lambda s: Msgs()})()})()})()
    assert [m["id"] for m in split_mail.target_copies(gm, "<x@y>")] == ["a", "b"]


def _draft(subject, sender, body="hello"):
    raw = f"From: {sender}\r\nTo: x@y.test\r\nSubject: {subject}\r\n\r\n{body}\r\n".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _draft_gm(tmp_path, target_drafts, mapped):
    """target_drafts {draft id: (message id, raw)}; mapped = draft ids the ledger knows."""
    from db import MigrationDB
    db = MigrationDB(str(tmp_path / "l.db"))
    for sid, tid in mapped.items():
        db.record_mapping("u@a.test", sid, tid, "draft")
    trashed = []
    drafts = SimpleNamespace(
        list=lambda **kw: SimpleNamespace(execute=lambda: {"drafts": [{"id": i, "message": {"id": m}} for i, (m, _) in target_drafts.items()]}),
        get=lambda **kw: SimpleNamespace(execute=lambda: {"message": {"raw": target_drafts[kw["id"]][1]}}))
    msgs = SimpleNamespace(trash=lambda **kw: SimpleNamespace(execute=lambda: trashed.append(kw["id"]) or {}))
    tgt = SimpleNamespace(users=lambda: SimpleNamespace(drafts=lambda: drafts, messages=lambda: msgs))
    gm = SimpleNamespace(tgt=tgt, _retry=lambda f: f(), limiter=SimpleNamespace(acquire=lambda: None),
                         target_user="u@b.test", settings=SimpleNamespace(dry_run=False, source_domain="a.test", target_domain="b.test"))
    return gm, db, trashed


def test_the_dms_copy_of_a_draft_is_trashed_beside_the_engines(tmp_path):
    gm, db, trashed = _draft_gm(tmp_path, {
        "d-engine": ("m1", _draft("Plan", "U <u@b.test>")), "d-dms": ("m2", _draft("Plan", "u@a.test"))},
        mapped={"src-draft": "d-engine"})
    assert split_mail.trash_dms_drafts(gm, db, "u@a.test") == 1 and trashed == ["m2"]


def test_a_draft_with_no_engine_twin_is_left_alone(tmp_path):
    gm, db, trashed = _draft_gm(tmp_path, {
        "d-engine": ("m1", _draft("Plan", "U <u@b.test>")), "d-own": ("m3", _draft("Something else", "u@b.test"))},
        mapped={"src-draft": "d-engine"})
    assert split_mail.trash_dms_drafts(gm, db, "u@a.test") == 0 and trashed == []


def test_one_engine_draft_absorbs_one_dms_copy_not_two(tmp_path):
    gm, db, trashed = _draft_gm(tmp_path, {
        "d-engine": ("m1", _draft("Plan", "u@b.test")), "x1": ("m2", _draft("Plan", "u@a.test")), "x2": ("m3", _draft("Plan", "u@a.test"))},
        mapped={"src-draft": "d-engine"})
    assert split_mail.trash_dms_drafts(gm, db, "u@a.test") == 1 and len(trashed) == 1
