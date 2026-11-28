"""Source write-boundary regression tests over a recording fake transport."""

from __future__ import annotations

from contextlib import ExitStack
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from googleapiclient.http import MediaInMemoryUpload

import auth
import source_attack_probe as probe


@pytest.fixture(scope="module")
def fake_source():
    settings = SimpleNamespace(
        source_domain="dharan.gov.np",
        target_domain="sandbox.invalid",
        source_admin="admin@dharan.gov.np",
        target_admin="admin@sandbox.invalid",
        source_sa_key="/tmp/source-attack-probe-source-key.json",
        target_sa_key="/tmp/source-attack-probe-target-key.json",
        oauth_token_dir="/tmp/source-attack-probe-test-oauth",
    )
    recorder = probe.RecorderTransport()
    with ExitStack() as stack:
        stack.enter_context(patch.object(auth, "_http", lambda *a, **k: recorder))
        stack.enter_context(patch.object(
            auth.google_auth_httplib2, "AuthorizedHttp",
            lambda _credentials, http: http))
        stack.enter_context(patch.object(
            auth.service_account.Credentials, "from_service_account_file",
            lambda *a, **k: probe.DummyCredentials()))
        manager = auth.AuthManager(settings)
        manager._credentials = lambda _tenant, _user: probe.DummyCredentials()
        manager._scopes = lambda _tenant: ["https://www.googleapis.com/auth/drive.readonly"]
        manager._settle_optional_passes = lambda: None
        yield manager, recorder


@pytest.fixture(autouse=True)
def clear_recorded_requests(fake_source):
    fake_source[1].requests.clear()


@pytest.mark.parametrize(
    "case",
    [pytest.param(case, id=case.id.replace(" ", "_")) for case in probe.attack_matrix()],
)
def test_discovery_write_matrix_refuses_or_uses_only_the_staging_exception(fake_source, case):
    manager, recorder = fake_source
    before = len(recorder.requests)
    client = None
    locally_refused = False
    try:
        client = probe._service_route(manager, case, "operator@dharan.gov.np")
        is_staging_route = case.route.startswith("allow_copy_into(")
        request = probe.generated_request(client, case, staging=is_staging_route)
        request.execute()
    except auth.SourceWriteRefused:
        # Local refusal is the desired result for the government source.
        locally_refused = True
    except ValueError as exc:
        assert case.route in {
            'directory("source", writable=True)',
            'directory("source", groups=True)',
        }
        assert "government domain" in str(exc).lower()
        locally_refused = True
    except Exception as exc:  # noqa: BLE001 - request may have been sent before a fake response parser errors
        if len(recorder.requests) == before:
            pytest.fail(f"{case.id}: failed before making a recorded request: {type(exc).__name__}: {exc}")

    outgoing = recorder.requests[before:]
    if locally_refused:
        assert not outgoing, f"{case.id} refused locally after sending {outgoing}"
        return
    assert outgoing, f"{case.id}: neither refused locally nor recorded a request"
    leaks = probe.leaking_requests(outgoing)
    assert not leaks, f"{case.id} leaked {[(r.method, r.url, r.body) for r in leaks]}"


@pytest.mark.parametrize("route", ["source_drive()", 'api("source", "drive", user)'])
def test_batch_of_gets_is_allowed_even_though_google_batches_use_post(fake_source, route):
    manager, recorder = fake_source
    source = manager.source_drive("operator@dharan.gov.np") if route == "source_drive()" else manager.api(
        "source", "drive", "operator@dharan.gov.np")
    raw = source._svc if isinstance(source, auth.ReadOnlyDrive) else source
    request = raw.files().get(fileId="SOURCE_ATTACK_FILE")
    batch = source.new_batch_http_request()
    batch.add(request)
    try:
        batch.execute()
    except auth.SourceWriteRefused:
        assert recorder.requests == []
        pytest.fail(f"{route} refused a batch containing only GET requests")
    except Exception:  # the response parser is irrelevant; the request is already recorded
        pass
    assert recorder.requests
    assert "/batch/" in recorder.requests[-1].url
    assert not probe.leaking_requests(recorder.requests)


@pytest.mark.parametrize(
    "route",
    ["source_drive().new_batch_http_request()",
     'api("source", "drive", user).new_batch_http_request()'],
)
def test_batch_with_a_write_is_refused_before_leaving_the_box(fake_source, route):
    manager, recorder = fake_source
    source = manager.source_drive("operator@dharan.gov.np") if route.startswith("source_drive") else manager.api(
        "source", "drive", "operator@dharan.gov.np")
    raw = source._svc if isinstance(source, auth.ReadOnlyDrive) else source
    request = raw.files().delete(fileId="SOURCE_ATTACK_FILE")
    batch = source.new_batch_http_request()
    batch.add(request)
    try:
        batch.execute()
    except auth.SourceWriteRefused:
        assert recorder.requests == []
        return
    except Exception:
        pass
    leaks = probe.leaking_requests(recorder.requests)
    assert not leaks, f"{route} leaked {[(r.method, r.url, r.body) for r in leaks]}"


def test_source_client_raw_http_request_cannot_write_source_file(fake_source):
    manager, recorder = fake_source
    service = manager.api("source", "drive", "operator@dharan.gov.np")
    http = service._http
    try:
        http.request(
            "https://www.googleapis.com/drive/v3/files/SOURCE_ATTACK_FILE",
            method="PATCH", body='{"description":"probe"}',
            headers={"content-type": "application/json"},
        )
    except auth.SourceWriteRefused:
        assert recorder.requests == []
        return
    leaks = probe.leaking_requests(recorder.requests)
    assert not leaks, f"source client _http.request leaked {[(r.method, r.url, r.body) for r in leaks]}"


def test_mutated_request_is_checked_at_execute_not_only_at_creation(fake_source):
    manager, recorder = fake_source
    source = manager.source_drive("operator@dharan.gov.np")
    request = source.files().get(fileId="SOURCE_ATTACK_FILE")
    request.method = "PATCH"
    request.uri = "https://www.googleapis.com/drive/v3/files/SOURCE_ATTACK_FILE"
    request.body = '{"description":"probe"}'
    request.headers = {"content-type": "application/json"}
    try:
        request.execute()
    except auth.SourceWriteRefused:
        assert recorder.requests == []
        return
    leaks = probe.leaking_requests(recorder.requests)
    assert not leaks, f"mutated HttpRequest leaked {[(r.method, r.url, r.body) for r in leaks]}"


@pytest.mark.parametrize("resumable", [False, True], ids=["multipart-MediaUpload", "resumable-upload"])
def test_media_upload_paths_are_source_write_guarded(fake_source, resumable):
    manager, recorder = fake_source
    source = manager.api("source", "drive", "operator@dharan.gov.np")
    media = MediaInMemoryUpload(b"probe-bytes", "text/plain", resumable=resumable)
    try:
        # The source Drive facade refuses at method construction, before an
        # HttpRequest exists or can reach the transport.
        request = source.files().create(body={"name": "source attack probe"}, media_body=media)
    except auth.SourceWriteRefused:
        assert recorder.requests == []
        return
    request.execute()
    leaks = probe.leaking_requests(recorder.requests)
    assert not leaks, f"MediaUpload leaked {[(r.method, r.url, r.body) for r in leaks]}"


def test_source_drive_wrapper_refuses_media_upload_locally(fake_source):
    manager, recorder = fake_source
    source = manager.source_drive("operator@dharan.gov.np")
    media = MediaInMemoryUpload(b"probe-bytes", "text/plain", resumable=True)
    with pytest.raises(auth.SourceWriteRefused):
        source.files().create(body={"name": "source attack probe"}, media_body=media)
    assert recorder.requests == []


def test_copy_and_move_exceptions_are_narrowly_limited_to_the_run_staging_drive(fake_source):
    manager, recorder = fake_source
    base = manager.source_drive("operator@dharan.gov.np")
    staged = auth.allow_copy_into(base, probe.STAGING_ID)
    staged.files().copy(fileId="SOURCE_ATTACK_FILE", body={"parents": [probe.STAGING_ID]}).execute()
    assert not probe.leaking_requests(recorder.requests)
    assert recorder.requests[-1].method == "POST"

    moved = auth.allow_copy_into(base, probe.STAGING_ID, moves=True)
    moved.files().update(fileId="SOURCE_ATTACK_FILE", addParents=probe.STAGING_ID,
                         removeParents="SOURCE_PARENT", supportsAllDrives=True,
                         fields="id").execute()
    assert not probe.leaking_requests(recorder.requests)
    assert recorder.requests[-1].method == "PATCH"

    with pytest.raises(auth.SourceWriteRefused):
        staged.files().copy(fileId="SOURCE_ATTACK_FILE", body={"parents": [probe.OUTSIDE_ID]})
    with pytest.raises(auth.SourceWriteRefused):
        moved.files().update(fileId="SOURCE_ATTACK_FILE", body={"description": "not a move"})
    assert len(recorder.requests) == 2


def test_move_exception_requires_a_complete_move_out_of_the_source(fake_source):
    manager, recorder = fake_source
    moved = auth.allow_copy_into(
        manager.source_drive("operator@dharan.gov.np"), probe.STAGING_ID, moves=True)
    try:
        moved.files().update(fileId="SOURCE_ATTACK_FILE", addParents=probe.STAGING_ID).execute()
    except auth.SourceWriteRefused:
        assert recorder.requests == []
        return
    leaks = probe.leaking_requests(recorder.requests)
    assert not leaks, f"incomplete move leaked {[(r.method, r.url, r.body) for r in leaks]}"


def test_government_domain_guard_refuses_explicitly_writable_source_routes(fake_source):
    manager, recorder = fake_source
    with pytest.raises(ValueError, match="government domain"):
        manager.source_drive("admin@dharan.gov.np", writable=True)
    with pytest.raises(ValueError, match="government domain"):
        manager.directory("source", writable=True)
    with pytest.raises(ValueError, match="government domain"):
        manager.directory("source", groups=True)
    assert recorder.requests == []


def test_live_cli_requires_exact_sandbox_domain_and_explicit_file():
    args = SimpleNamespace(domain="dharan.gov.np", user="admin@dharan.gov.np",
                           live=True, dry_run=False,
                           i_understand=True, file="f")
    with pytest.raises(ValueError, match="only a.sarafgloabalexim.com"):
        probe.validate_cli_args(args)
    args.domain = probe.SAFE_LIVE_DOMAIN
    args.user = "operator@a.sarafgloabalexim.com"
    args.i_understand = False
    with pytest.raises(ValueError, match="--i-understand"):
        probe.validate_cli_args(args)
    args.i_understand = True
    args.file = None
    with pytest.raises(ValueError, match="--file"):
        probe.validate_cli_args(args)
    args.file = "f"
    args.user = "operator@dharan.gov.np"
    with pytest.raises(ValueError, match="requires --user in"):
        probe.validate_cli_args(args)


def test_live_request_rewrite_removes_original_method_body_and_upload_state():
    request = SimpleNamespace(method="DELETE", uri="https://example.invalid/delete",
                              body=b"unsafe", headers={"content-type": "multipart/mixed"},
                              resumable=object())
    rewritten = probe.mutate_request_to_description_update(
        request, "file id/1", "probe-marker", "etag-1")
    assert rewritten.method == "PATCH"
    assert "/file%20id%2F1?" in rewritten.uri
    assert rewritten.body == '{"description":"probe-marker"}'
    assert rewritten.headers["content-type"] == "application/json"
    assert rewritten.headers["if-match"] == "etag-1"
    assert rewritten.resumable is None


def test_only_real_oauth_token_refresh_requests_are_exempt_from_write_detection():
    refresh = probe.RequestRecord("POST", "https://oauth2.googleapis.com/token", "grant_type=x", {})
    token_named_file = probe.RequestRecord(
        "POST", "https://www.googleapis.com/drive/v3/files/token/copy",
        '{"parents":["outside"]}', {})
    assert probe.leaking_requests([refresh]) == []
    assert probe.leaking_requests([token_named_file]) == [token_named_file]


def test_dry_run_constructs_the_full_matrix_without_executing_requests(fake_source):
    manager, recorder = fake_source
    output = StringIO()
    result = probe.run_dry_run(manager, "operator@dharan.gov.np", stream=output)
    construction_errors = [line for line in output.getvalue().splitlines()
                           if "request construction failed" in line]
    assert result == 0, construction_errors
    assert f"DRY-RUN: {len(probe.attack_matrix())} discovery write methods" in output.getvalue()
    assert "MediaUpload" in output.getvalue()
    assert recorder.requests == []

