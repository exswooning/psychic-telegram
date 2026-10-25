"""Live, dharan -> s.: 94 of 194 failures were 404s for older link-shared files reached through
a shared folder; with X-Goog-Drive-Resource-Keys the same read worked at once."""
import auth
import resource_keys


class _Req:
    def __init__(self, response):
        self.headers, self._response = {}, response

    def execute(self):
        return self._response


class _Files:
    def __init__(self):
        self.calls = []

    def list(self, **kw):
        self.calls.append(("list", kw))
        return _Req({"files": [{"id": "F1", "resourceKey": "0-key1"},
                               {"id": "S1", "shortcutDetails": {"targetId": "T1", "targetResourceKey": "0-tk"}}]})

    def get_media(self, **kw):
        self.calls.append(("get_media", kw))
        return _Req(b"bytes")

    def delete(self, **kw):
        raise AssertionError("reached the source")

    def get(self, **kw):
        self.calls.append(("get", kw))
        return _Req({"id": kw["fileId"]})


class _Svc:
    def __init__(self):
        self.f = _Files()

    def files(self):
        return self.f


def test_a_listed_key_is_sent_on_the_next_read():
    resource_keys.KEYS.clear()
    svc = _Svc()
    src = auth.ReadOnlyDrive(svc)
    src.files().list(q="'P' in parents", fields="nextPageToken, files(id,name)").execute()
    assert svc.f.calls[0][1]["fields"] == "nextPageToken, files(resourceKey,id,name)"   # asked for
    req = src.files().get_media(fileId="F1")
    assert req.headers[resource_keys.HEADER] == "F1/0-key1"
    assert src.files().get(fileId="T1", fields="id").headers[resource_keys.HEADER] == "T1/0-tk"   # a shortcut target
    assert svc.f.calls[-1][1]["fields"] == "id,resourceKey"
    assert resource_keys.HEADER not in src.files().get_media(fileId="OTHER").headers         # no key, no header


def test_the_source_stays_read_only():
    import pytest
    src = auth.ReadOnlyDrive(_Svc())
    with pytest.raises(auth.SourceWriteRefused):
        src.files().delete(fileId="F1")


def test_a_failed_write_reports_its_own_error_not_a_rollback_one(db):
    """Live: 'cannot rollback - no transaction is active' hid 'database or disk is full'."""
    import sqlite3
    import pytest
    with pytest.raises(sqlite3.OperationalError, match="disk is full"):
        with db.write() as c:
            c.execute("ROLLBACK")                       # what SQLite does on a full disk
            raise sqlite3.OperationalError("database or disk is full")


def test_a_key_outlives_the_process_that_listed_it(tmp_path):
    """A repair or a re-check reads a file it never listed: the key comes from the ledger."""
    path = str(tmp_path / "ledger.db")
    resource_keys.KEYS.clear()
    src = auth.ReadOnlyDrive(_Svc(), keys_db=path)
    src.files().list(fields="files(id)").execute()
    resource_keys.KEYS.clear()                                   # a new process
    req = auth.ReadOnlyDrive(_Svc(), keys_db=path).files().get_media(fileId="F1")
    assert req.headers[resource_keys.HEADER] == "F1/0-key1"


def test_every_dharan_failure_has_a_kind(db):
    """The 194 'not yet classified' of dharan -> s., by kind; a later success reads as fixed."""
    import known_issues
    from db import bulk_seed_identities
    bulk_seed_identities(db, [("u@src", "u@tgt")])
    for item, kind, msg in (
            ("f1", "file", 'HTTP 404 (notFound): returned "File not found: f1."'),
            ("f2:(list-failed)", "acl", 'could not read source permissions: HTTP 404 "File not found: f2."'),
            ("f3", "file", "download_upload: OSError: [Errno 28] No space left on device"),
            ("f4", "file", "cannot rollback - no transaction is active"),
            ("f5", "file", "HTTP 403 (cannotDownloadAbusiveFile)"),
            ("f6", "file", "something new")):
        db.log_audit("u@src", item, kind, "FAILED", msg)
    db.log_audit("u@src", "f1", "file", "SUCCESS", "")
    s = known_issues.run_failure_summary(db.conn)
    by = {k["id"]: (k["open"], k["fixed"]) for k in s["kinds"]}
    assert by["run.resource_key"] == (1, 1) and by["run.disk_full"] == (1, 0)
    assert by["run.db_rollback"] == (1, 0) and by["run.malware_flag"] == (1, 0)
    assert s["unclassified"] == 1 and s["open"] == 5


def test_the_failures_panel_names_each_kind(db):
    import api_server
    from db import bulk_seed_identities
    bulk_seed_identities(db, [("u@src", "u@tgt")])
    db.log_audit("u@src", "f1", "file", "FAILED", 'returned "File not found: f1."')
    db.log_audit("u@src", "f9", "file", "FAILED", "a cause nobody has seen")
    out = api_server._repair_payload(db, 7)
    fam = {f["key"]: f for f in out["families"]}
    assert fam["run.resource_key"]["count"] == 1 and fam["run.resource_key"]["auto"] is True
    assert out["unclassified"] == 1


def test_two_files_with_the_same_comment_id_both_get_their_comment(migrator, auth, db, settings):
    """Live, ss@dharan: two copies of one Doc both carried comment AAABghWPKe8; the second
    found the first's unscoped mapping and stopped ("Mapped comment/reply is missing")."""
    from tests.conftest import SRC_USER, TGT_USER
    settings.migrate_comments = True
    src = auth.source_drive(SRC_USER)
    a, b = src.add_binary("one.pdf"), src.add_binary("two.pdf")
    for fid in (a, b):
        src.comment_store[fid].append({"id": "AAABghWPKe8", "content": "same id", "author": {"displayName": "Bob"},
                                       "createdTime": "2024-03-04T05:06:07Z", "resolved": False, "replies": []})
    migrator.run()
    tgt = auth.target_drive(TGT_USER)
    for fid in (a, b):
        tid = db.get_target_id(SRC_USER, fid, "file")
        assert [c["content"] for c in tgt.comment_store[tid]][-1].endswith("same id")
    assert not db.conn.execute("SELECT 1 FROM audit_log WHERE item_type='comment' AND status='FAILED'").fetchone()


def test_a_failed_acceptance_names_its_findings():
    import sqlite3
    import json
    import known_issues
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE verification_repair_queue (source_user TEXT, service TEXT, category TEXT, "
              "detail TEXT, state TEXT, issue TEXT)")
    for _ in range(3):
        c.execute("INSERT INTO verification_repair_queue VALUES ('a@src','drive','differences',?, 'QUEUED', NULL)",
                  (json.dumps({"diffs": ["native document content, formatting or structure differs"]}),))
    c.execute("INSERT INTO verification_repair_queue VALUES ('a@src','drive','notCopied',?, 'QUEUED', NULL)",
              (json.dumps({"status": "UNMAPPED_SOURCE"}),))
    assert known_issues.user_reason(c, "a@src") == \
        "3 Doc/Slide structure differs from the source; 1 A source file has no copy"
    assert known_issues.user_reason(c, "b@src") == ""
