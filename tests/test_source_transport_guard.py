"""27 Nov 2026: the source guard was only ever tested on the methods ReadOnlyDrive lists. Every side door is attacked
here against a real googleapiclient Drive client (the bundled discovery document) over a recording transport: an
unlisted collection, the raw transport, a batch, a request altered after it was built, an upload, auth.api(). The
only requests that may leave the box are reads, a batch of reads, and a copy into the run's own staging drive."""
import httplib2
import pytest
from googleapiclient.discovery import build_from_document
from googleapiclient.http import MediaInMemoryUpload

import auth
from auth import SourceReadOnlyHttp, SourceWriteRefused, check_source_request


class Recorder:
    def __init__(self):
        self.sent = []

    def request(self, uri, method="GET", body=None, headers=None, *a, **k):
        self.sent.append((method, uri))
        return httplib2.Response({"status": "200", "content-type": "application/json"}), b"{}"


def _drive():
    rec = Recorder()
    svc = build_from_document(auth.shared_discovery_doc("drive", "v3"), http=SourceReadOnlyHttp(rec))
    return svc, rec


WRITES = [
    lambda s: s.files().delete(fileId="f").execute(),
    lambda s: s.files().update(fileId="f", body={"name": "x"}).execute(),
    lambda s: s.files().create(body={"name": "x"}).execute(),
    lambda s: s.files().emptyTrash().execute(),
    lambda s: s.files().copy(fileId="f", body={"parents": ["source-folder"]}).execute(),
    lambda s: s.files().copy(fileId="f", body={}).execute(),
    lambda s: s.permissions().create(fileId="f", body={"role": "reader", "type": "anyone"}).execute(),
    lambda s: s.permissions().delete(fileId="f", permissionId="p").execute(),
    lambda s: s.comments().create(fileId="f", body={"content": "x"}, fields="id").execute(),
    lambda s: s.revisions().delete(fileId="f", revisionId="r").execute(),
    lambda s: s.drives().create(requestId="r", body={"name": "x"}).execute(),
    lambda s: s.accessproposals().resolve(fileId="f", proposalId="p", body={"action": "ACCEPT"}).execute(),
    lambda s: s.files().create(body={"name": "x"}, media_body=MediaInMemoryUpload(b"x", "text/plain")).execute(),
    lambda s: s._http.request("https://www.googleapis.com/drive/v3/files/f", "DELETE"),
    lambda s: s._http.request("https://www.googleapis.com/drive/v3/files/f", method="PATCH", body='{"name":"x"}'),
]


@pytest.mark.parametrize("attack", WRITES, ids=range(len(WRITES)))
def test_no_write_leaves_the_box_by_any_route(attack):
    svc, rec = _drive()
    with pytest.raises(SourceWriteRefused):
        attack(svc)
    assert rec.sent == []


def test_a_read_request_altered_into_a_write_is_refused():
    svc, rec = _drive()
    req = svc.files().get(fileId="f")
    req.method, req.body = "PATCH", '{"name": "x"}'
    with pytest.raises(SourceWriteRefused):
        req.execute()
    assert rec.sent == []


def test_a_batch_may_only_read():
    svc, rec = _drive()
    batch = svc.new_batch_http_request()
    batch.add(svc.files().get(fileId="a"))
    batch.add(svc.files().update(fileId="b", body={"name": "x"}))
    with pytest.raises(SourceWriteRefused):
        batch.execute()
    assert rec.sent == []
    ok = svc.new_batch_http_request()
    ok.add(svc.files().get(fileId="a"))
    ok.add(svc.files().list())
    try:
        ok.execute()
    except Exception as exc:      # noqa: BLE001 - the recorder's reply is not a multipart answer
        assert not isinstance(exc, SourceWriteRefused)
    assert rec.sent and rec.sent[0][0] == "POST" and "/batch" in rec.sent[0][1]


def test_reads_and_the_staging_copy_go_through():
    svc, rec = _drive()
    svc.files().get(fileId="f").execute()
    svc.files().list().execute()
    svc.files().export(fileId="f", mimeType="text/plain").execute()
    auth._COPY_TARGETS.add("staging-1")
    try:
        svc.files().copy(fileId="f", body={"parents": ["staging-1"]}).execute()
    finally:
        auth._COPY_TARGETS.discard("staging-1")
    assert [m for m, _ in rec.sent] == ["GET", "GET", "GET", "POST"]


@pytest.mark.parametrize("method,uri,body,ok", [
    ("GET", "https://www.googleapis.com/gmail/v1/users/me/messages", None, True),
    ("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages", b"{}", False),
    ("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/m/modify", b"{}", False),
    ("DELETE", "https://www.googleapis.com/calendar/v3/calendars/c/events/e", None, False),
    ("POST", "https://sheets.googleapis.com/v4/spreadsheets/s:getByDataFilter", b"{}", True),
    ("POST", "https://sheets.googleapis.com/v4/spreadsheets/s:batchUpdate", b"{}", False),
    ("PUT", "https://admin.googleapis.com/admin/directory/v1/users/u", b"{}", False),
    ("POST", "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable", b"{}", False),
    ("PATCH", "https://www.googleapis.com/drive/v3/files/f?addParents=x", b"", False),
])
def test_every_api_is_held_to_reads(method, uri, body, ok):
    if ok:
        check_source_request(method, uri, body)
    else:
        with pytest.raises(SourceWriteRefused):
            check_source_request(method, uri, body)


class _Creds:
    valid = True

    def __init__(self, subject):
        self._subject = subject

    def before_request(self, *a, **k):
        pass


def _hooked(subject, monkeypatch, protected=("src.gov.np",)):
    import google_auth_httplib2
    import domain_guard
    monkeypatch.setattr(domain_guard, "source_refuse_reason",
                        lambda d, s="": "a protected source" if d in protected else "")
    auth._PROTECTED_SUBJECT_CACHE.clear()
    rec = Recorder()
    return google_auth_httplib2.AuthorizedHttp(_Creds(subject), http=rec), rec


def test_a_module_that_builds_its_own_client_cannot_write_as_a_protected_source_user(monkeypatch):
    """12 modules load the source key themselves; the hook on AuthorizedHttp holds them all."""
    http, rec = _hooked("ward1@src.gov.np", monkeypatch)
    with pytest.raises(SourceWriteRefused):
        http.request("https://www.googleapis.com/drive/v3/files/f", "DELETE")
    http.request("https://www.googleapis.com/drive/v3/files/f", "GET")
    assert rec.sent == [("GET", "https://www.googleapis.com/drive/v3/files/f")]


def test_registry_failure_does_not_disable_the_direct_client_source_guard(monkeypatch):
    import domain_guard
    import google_auth_httplib2

    def unavailable(*args, **kwargs):
        raise RuntimeError("domain registry unavailable")

    monkeypatch.setattr(domain_guard, "source_refuse_reason", unavailable)
    monkeypatch.setattr(domain_guard, "is_government", unavailable)
    auth._PROTECTED_SUBJECT_CACHE.clear()
    rec = Recorder()
    http = google_auth_httplib2.AuthorizedHttp(_Creds("alice@source.example"), http=rec)

    with pytest.raises(SourceWriteRefused):
        http.request("https://www.googleapis.com/drive/v3/files/f", "DELETE")
    assert rec.sent == []


def test_the_hook_leaves_the_target_a_sandbox_and_an_explicit_writer_alone(monkeypatch):
    http, rec = _hooked("u@tgt.test", monkeypatch)
    http.request("https://www.googleapis.com/drive/v3/files/f", "DELETE")
    marked, rec2 = _hooked("ward1@src.gov.np", monkeypatch)
    marked._bitport_tenant = "target"      # after a rename the target can carry a protected name
    marked.request("https://www.googleapis.com/drive/v3/files/f", "DELETE")
    writer, rec3 = _hooked("u@src.gov.np", monkeypatch)
    writer._bitport_source_writes_ok = True
    writer.request("https://www.googleapis.com/drive/v3/files/f", "DELETE")
    assert len(rec.sent) == len(rec2.sent) == len(rec3.sent) == 1


def test_api_source_drive_is_the_guarded_client(settings):
    a = auth.AuthManager.__new__(auth.AuthManager)
    a.settings = settings
    a._service = lambda tenant, api, user, writable=False: object()
    assert isinstance(a.api("source", "drive", "u@x"), auth.ReadOnlyDrive)


def test_a_move_run_may_only_make_a_complete_move_into_its_staging_drive():
    auth._MOVE_TARGETS.add("stage")
    try:
        check_source_request("PATCH", "https://www.googleapis.com/drive/v3/files/f?addParents=stage&removeParents=p", b"")
        for uri in ("https://www.googleapis.com/drive/v3/files/f?addParents=stage",
                    "https://www.googleapis.com/drive/v3/files/f?addParents=other&removeParents=p"):
            with pytest.raises(SourceWriteRefused):
                check_source_request("PATCH", uri, b"")
        with pytest.raises(SourceWriteRefused):
            check_source_request("PATCH", "https://www.googleapis.com/drive/v3/files/f?addParents=stage&removeParents=p",
                                 b'{"name": "x"}')
    finally:
        auth._MOVE_TARGETS.discard("stage")
