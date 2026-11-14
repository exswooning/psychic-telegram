"""Drive links in mail that cannot be repointed are listed -- with what the source answers for each --
not only counted."""
import mail_dead_links as M
from tests.conftest import SRC_USER
import concurrent.futures
import pytest

GONE = "1GoneFileIdGoneFileIdGoneFileId0001"
MOVED = "1CopiedFileIdCopiedFileIdCopied0002"


def _mail(tag, *ids):
    body = "".join(f"see https://drive.google.com/file/d/{i}/view\r\n" for i in ids)
    return (f"Message-ID: <{tag}@a.com>\r\nFrom: a@b.com\r\nTo: c@d.com\r\nSubject: s\r\n\r\n{body}").encode()


def test_only_links_with_no_target_copy_are_listed_with_the_sources_answer(gmail_migrator, auth, db):
    db.record_mapping(SRC_USER, MOVED, "t-copy", "file")                      # copied: the engine rewrites it
    g = auth.source_gmail(SRC_USER)
    g.add_message(_mail("one", GONE, MOVED), ["INBOX"])
    g.add_message(_mail("two", GONE), ["INBOX"])
    r = M.inventory_user(gmail_migrator, workers=2, say=lambda *_: None)
    rep = M.report(db.conn)
    rows = {x["fileId"]: x for x in rep["links"]}
    assert MOVED not in rows                                                  # not a dead link
    assert rows[GONE]["status"] == "NOT_FOUND" and rows[GONE]["emails"] == 2 and rows[GONE]["exampleMessage"]
    assert r["links"] == 1 and rep["totals"]["NOT_FOUND"] == {"label": M.LABEL["NOT_FOUND"], "links": 1, "emails": 2}


def test_a_second_run_replaces_the_users_rows_and_the_report_filters(gmail_migrator, auth, db):
    auth.source_gmail(SRC_USER).add_message(_mail("one", GONE), ["INBOX"])
    M.inventory_user(gmail_migrator, workers=1, say=lambda *_: None)
    M.inventory_user(gmail_migrator, workers=1, say=lambda *_: None)
    assert M.report(db.conn)["matched"] == 1
    assert M.report(db.conn, status="NO_ACCESS")["matched"] == 0
    assert M.report(db.conn, q=GONE[:8].lower())["matched"] == 1


def test_what_the_source_answers_is_named():
    assert M.classify("HTTP 404 (notFound): x", None, "s.x")[0] == "NOT_FOUND"
    assert M.classify("HTTP 403 (insufficientFilePermissions)", None, "s.x")[0] == "NO_ACCESS"
    assert M.classify(None, ["a@other.org"], "s.x") == ("OUTSIDE", "owned by other.org")
    assert M.classify(None, ["a@s.x"], "s.x") == ("NOT_MIGRATED", "owned by s.x")
    assert M.classify(None, [], "s.x")[0] == "OUTSIDE"            # another organisation's shared drive
    assert M.classify("HTTP 500", None, "s.x")[0] == "OTHER"


def test_link_emails_are_recorded_with_whether_gmail_search_named_them(gmail_migrator, auth, db):
    """The search's miss rate: emails a full read finds carrying a link, that the search did not name."""
    g = auth.source_gmail(SRC_USER)
    named = g.add_message(_mail("named", GONE), ["INBOX"])
    hidden = g.add_message(_mail("hidden", MOVED), ["INBOX"])
    g.chip_hits = {"has:drive": {named}}
    import gmail_engine
    gmail_engine_terms = gmail_engine.LINK_SEARCHES
    try:
        gmail_engine.LINK_SEARCHES = ("has:drive",)          # the quoted-URL term would find both
        r = M.inventory_user(gmail_migrator, workers=1, say=lambda *_: None)
    finally:
        gmail_engine.LINK_SEARCHES = gmail_engine_terms
    rows = dict(db.conn.execute("SELECT message_id, by_search FROM mail_link_messages").fetchall())
    assert rows == {named: 1, hidden: 0}
    assert (r["linkMessages"], r["searchMissed"]) == (2, 1)


def test_stop_inside_a_mailbox_does_not_drain_queued_reads_or_replace_complete_evidence(gmail_migrator, auth, db, monkeypatch):
    """Live account 3: a daemon inventory kept its non-daemon pool alive after Stop."""
    import main
    g=auth.source_gmail(SRC_USER)
    g.add_message(_mail('old',GONE),['INBOX'])
    M.inventory_user(gmail_migrator,workers=1,say=lambda *_: None)
    before=M.report(db.conn)
    for i in range(30):g.add_message(_mail(str(i),MOVED),['INBOX'])
    retry=gmail_migrator._retry;reads=[]
    def stopping_read(call,**kw):
        result=retry(call,**kw)
        if kw.get('label')=='gmail.dead_links.get':
            reads.append(1);main.SHUTDOWN.set()
        return result
    monkeypatch.setattr(gmail_migrator,'_retry',stopping_read)
    try:
        with pytest.raises(concurrent.futures.CancelledError):
            M.inventory_user(gmail_migrator,workers=1,say=lambda *_: None)
        assert len(reads)==1
        assert M.report(db.conn)==before
    finally:main.SHUTDOWN.clear()
