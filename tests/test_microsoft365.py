"""Isolated Microsoft fixtures. No Microsoft credentials or live HTTP calls."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from microsoft365.discovery import Discovery
from microsoft365.graph import GraphClient, GraphError, Stopped, retry_delay, validate_config
from microsoft365.operations import SandboxOperations, sandbox_gate
from microsoft365.planner import build_plan, profile_diff
from microsoft365.routes import router
from microsoft365.store import Store

SOURCE = "10000000-0000-0000-0000-000000000001"
TARGET = "20000000-0000-0000-0000-000000000001"
APP = "30000000-0000-0000-0000-000000000001"
ALICE = "40000000-0000-0000-0000-000000000001"
BOB = "50000000-0000-0000-0000-000000000001"
OTHER = "60000000-0000-0000-0000-000000000001"


def configuration():
    return {"source": {"tenantId": SOURCE, "clientId": APP, "cloud": "commercial", "region": ""},
            "target": {"tenantId": TARGET, "clientId": APP, "cloud": "commercial", "region": "Europe"},
            "mode": "preserve_source", "sandbox": True}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("BITPORT_MICROSOFT_DATA", str(tmp_path))
    value = Store(7)
    value.save_config(configuration())
    return value


def inventoried(store):
    rid = store.new_run("discover")
    store.object(rid, "source", "directory", "", {"id": ALICE, "userPrincipalName": "info@old.example", "displayName": "दीप Shahi", "givenName": "दीप", "surname": "Shahi", "userType": "Member"})
    store.object(rid, "target", "directory", "", {"id": BOB, "userPrincipalName": "info@new.example", "displayName": "Info User", "userType": "Member"})
    for side in ("source", "target"):
        store.coverage(rid, side, "directory", "users", "COMPLETE", 1)
    store.update_run(rid, "DISCOVERED")
    return rid


class Response:
    def __init__(self, data=None, status=200, headers=None):
        self.data, self.status_code, self.headers = data, status, headers or {}
        self.content = json.dumps(data).encode() if data is not None else b""

    def json(self):
        return self.data

    def close(self):
        pass


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def close(self):
        pass


class TestGraphBoundary:
    @pytest.mark.parametrize("path", ["https://evil.example/v1.0/users", "https://graph.microsoft.com:444/v1.0/users", "https://secret@graph.microsoft.com/v1.0/users", "https://graph.microsoft.com/beta/users", "https://graph.microsoft.com/v1.0/../beta/users", "https://graph.microsoft.com/v1.0/%2e%2e/users", "https://graph.microsoft.com/v1.0/users#token"])
    def test_rejects_untrusted_continuations(self, path):
        client = GraphClient("commercial", lambda: "test-token", side="source")
        with pytest.raises(ValueError):
            client.url(path)

    @pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE", "PUT"])
    def test_source_never_writes(self, method):
        session = Session()
        client = GraphClient("commercial", lambda: "test-token", side="source", session=session)
        with pytest.raises(ValueError, match="read-only"):
            client.request(method, "/users")
        assert session.calls == []

    def test_source_cannot_be_constructed_writable(self):
        with pytest.raises(ValueError, match="source Graph writes"):
            GraphClient("commercial", lambda: "test-token", side="source", writable=True)

    def test_pagination_follows_all_pages(self):
        session = Session(Response({"value": [{"id": "1"}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?$skiptoken=next"}), Response({"value": [{"id": "2"}]}))
        client = GraphClient("commercial", lambda: "test-token", side="source", session=session)
        assert [r["id"] for r in client.items("/users")] == ["1", "2"]

    def test_pagination_cycle_fails(self):
        session = Session(Response({"value": [], "@odata.nextLink": "https://graph.microsoft.com/v1.0/users"}))
        with pytest.raises(GraphError, match="pagination_cycle"):
            list(GraphClient("commercial", lambda: "x", side="source", session=session).items("/users"))

    def test_missing_value_is_not_empty(self):
        session = Session(Response({}))
        with pytest.raises(GraphError, match="malformed_collection"):
            list(GraphClient("commercial", lambda: "x", side="source", session=session).items("/users"))

    def test_throttle_honors_retry_after(self):
        session = Session(Response({}, 429, {"Retry-After": "17"}), Response({"value": []}))
        client = GraphClient("commercial", lambda: "x", side="source", session=session)
        waits = []
        client.wait = waits.append
        assert list(client.items("/users")) == []
        assert waits == [17]

    def test_writes_are_not_retried_when_result_is_ambiguous(self):
        session = Session(Response({"error": {"code": "ServiceUnavailable"}}, 503))
        client = GraphClient("commercial", lambda: "x", side="target", writable=True, session=session)
        with pytest.raises(GraphError):
            client.json_write("POST", "/users", {})
        assert len(session.calls) == 1

    def test_storage_urls_are_pinned(self):
        client = GraphClient("commercial", lambda: "x", side="source")
        for url in ("http://tenant.sharepoint.com/a", "https://tenant.sharepoint.com.evil.example/a", "https://127.0.0.1/a", "https://user:secret@tenant.sharepoint.com/a"):
            with pytest.raises(ValueError):
                client._storage_url(url)

    def test_retry_delay_numeric(self):
        assert retry_delay("120", 0) == 120


class TestEvidence:
    def test_zero_and_unread_are_different(self, store):
        rid = store.new_run("discover")
        discovery = Discovery(store, rid)
        discovery.client = SimpleNamespace(items=lambda _: iter([]))
        assert discovery.collect("source", "mail", "empty", "/messages") == []
        def denied(_):
            raise GraphError(403, "AccessDenied")
        discovery.client = SimpleNamespace(items=denied)
        assert discovery.collect("source", "calendar", "denied", "/events") is None
        by = {r["scope"]: r for r in store.coverage_rows(rid)}
        assert by["empty"]["state"] == "COMPLETE" and by["empty"]["count"] == 0
        assert by["denied"]["state"] == "AUTH_REQUIRED"
        assert build_plan(store)["productionReady"] is False

    def test_partial_pages_remain_incomplete(self, store):
        rid = store.new_run("discover")
        discovery = Discovery(store, rid)
        def interrupted(_):
            yield {"id": "first"}
            raise GraphError(503, "failed_second_page")
        discovery.client = SimpleNamespace(items=interrupted)
        assert discovery.collect("source", "onedrive", "items", "/items") is None
        assert store.coverage_rows(rid)[0]["state"] == "ERROR"
        assert store.coverage_rows(rid)[0]["count"] == 1
        assert len(store.objects(rid, "source", "onedrive")) == 1

    def test_never_discovered_workload_is_not_verified(self, store):
        plan = build_plan(store)
        assert all(w["verification"] == "NOT_RUN" and not w["ready"] for w in plan["workloads"])
        assert all(w["inventory"]["source"]["state"] in ("NOT_CHECKED", "NOT_IMPLEMENTED") for w in plan["workloads"])

    def test_mapping_suggestion_is_not_confirmation(self, store):
        inventoried(store)
        person = build_plan(store)["identities"][0]
        assert person["suggestedTargetId"] == BOB
        assert person["targetId"] is None and not person["confirmed"]

    def test_names_are_compared_exactly(self, store):
        inventoried(store)
        store.map_user(ALICE, BOB)
        differences = build_plan(store)["identities"][0]["differences"]
        assert next(d for d in differences if d["field"] == "displayName")["source"] == "दीप Shahi"

    def test_account_isolation(self, store):
        inventoried(store)
        assert Store(8).latest_inventory() is None
        assert Store(8).config()["source"] == {}

    def test_same_target_cannot_map_twice(self, store):
        inventoried(store)
        store.map_user(ALICE, BOB)
        with pytest.raises(ValueError, match="cannot be mapped twice"):
            store.map_user(OTHER, BOB)

    def test_save_does_not_drop_unchanged_mapping(self, store):
        inventoried(store)
        store.map_user(ALICE, BOB)
        store.save_config(configuration())
        assert len(store.mappings()) == 1

    def test_changed_connection_invalidates_inventory(self, store):
        inventoried(store)
        config = configuration(); config["target"]["tenantId"] = OTHER
        store.save_config(config)
        assert not build_plan(store)["inventoryCurrent"]

    def test_preserve_source_excludes_native_moves(self, store):
        mail = next(w for w in build_plan(store)["workloads"] if w["id"] == "mail")
        assert any("excluded" in b for b in mail["blockers"])

    def test_active_run_locks_connections(self, store):
        store.new_run("discover")
        with pytest.raises(ValueError, match="stop"):
            store.save_config(configuration())
        with pytest.raises(ValueError, match="already active"):
            store.new_run("discover")

    def test_stop_is_seen_between_items(self, store):
        rid = store.new_run("discover")
        store.stop(rid)
        with pytest.raises(Stopped):
            Discovery(store, rid).check()

    def test_temporary_download_urls_are_not_persisted(self, store):
        rid = store.new_run("discover")
        store.object(rid, "source", "onedrive", "drive", {"id": "item", "@microsoft.graph.downloadUrl": "secret", "child": {"uploadUrl": "secret"}})
        assert store.objects(rid, "source", "onedrive") == [{"id": "item", "child": {}}]

    def test_recent_run_cannot_be_declared_interrupted(self, store):
        rid = store.new_run("discover")
        with pytest.raises(ValueError, match="two minutes"):
            store.recover_interrupted(rid)

    def test_exited_run_can_be_reconciled_without_restart(self, store):
        rid = store.new_run("discover")
        with store.connect() as c:
            c.execute("UPDATE runs SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?", (rid,))
        store.recover_interrupted(rid)
        assert store.run(rid)["state"] == "INTERRUPTED"
        assert store.new_run("discover") != rid

    def test_large_finding_list_is_bounded_without_hiding_totals(self, store):
        rid = inventoried(store)
        for number in range(215):
            store.coverage(rid, "source", "mail", str(number), "AUTH_REQUIRED", None, "fixture denial")
        plan = build_plan(store)
        assert plan["findingCount"] == 215
        assert len(plan["findings"]) == 200
        assert plan["findingsTruncated"] is True

    def test_receipt_failure_opens_repair_and_verified_resolves(self, store):
        store.receipt("x", "profile", ALICE, BOB, "hash", "ERROR", {"error": "readback failed"})
        assert store.snapshot()["repairs"][0]["state"] == "OPEN"
        store.receipt("x", "profile", ALICE, BOB, "hash", "VERIFIED", {})
        assert store.snapshot()["repairs"] == []
        with store.connect() as c:
            events = [dict(r) for r in c.execute("SELECT * FROM receipt_events ORDER BY id")]
        assert [e["state"] for e in events] == ["ERROR", "VERIFIED"]
        assert json.loads(events[0]["detail"])["error"] == "readback failed"

    def test_reject_same_tenant_and_unknown_cloud(self):
        config = configuration(); config["target"]["tenantId"] = SOURCE
        with pytest.raises(ValueError, match="different"):
            validate_config(config)
        config = configuration(); config["source"]["cloud"] = "attacker.example"
        with pytest.raises(ValueError, match="unknown"):
            validate_config(config)


class FakeProfiles:
    def __init__(self, person):
        self.person = dict(person)
        self.writes = []

    def get(self, _):
        return dict(self.person)

    def json_write(self, method, path, payload):
        self.writes.append((method, path, payload))
        self.person.update(payload)


class TestSandboxProfiles:
    def prepare(self, store, monkeypatch):
        inventoried(store); store.map_user(ALICE, BOB)
        monkeypatch.setenv("BITPORT_MICROSOFT_SANDBOX_WRITES", "1")
        return build_plan(store)["planHash"]

    def test_default_write_gate_is_closed(self, store):
        inventoried(store); store.map_user(ALICE, BOB)
        with pytest.raises(ValueError, match="Sandbox writes require"):
            sandbox_gate(store, build_plan(store)["planHash"], ALICE)

    def test_stale_approval_is_rejected(self, store, monkeypatch):
        self.prepare(store, monkeypatch)
        with pytest.raises(ValueError, match="plan changed"):
            sandbox_gate(store, "old-hash", ALICE)

    def test_profile_changes_only_target_and_verifies(self, store, monkeypatch):
        plan_hash = self.prepare(store, monkeypatch)
        rid = store.new_run("profile-pilot", {"sourceId": ALICE, "planHash": plan_hash})
        op = SandboxOperations(store, rid)
        op.source = FakeProfiles({"id": ALICE, "displayName": "दीप Shahi", "givenName": "दीप", "surname": "Shahi"})
        op.target = FakeProfiles({"id": BOB, "displayName": "Info User"})
        op.profile({"source_id": ALICE, "target_id": BOB})
        assert not op.source.writes
        assert op.target.person["displayName"] == "दीप Shahi"
        assert store.run(rid)["state"] == "VERIFIED"
        assert store.snapshot()["repairs"] == []
        op.profile({"source_id": ALICE, "target_id": BOB})
        assert len(op.target.writes) == 1

    def test_synced_target_is_blocked(self, store, monkeypatch):
        plan_hash = self.prepare(store, monkeypatch)
        rid = store.new_run("profile-pilot", {"sourceId": ALICE, "planHash": plan_hash})
        op = SandboxOperations(store, rid)
        op.source = FakeProfiles({"displayName": "Source"})
        op.target = FakeProfiles({"displayName": "Target", "onPremisesSyncEnabled": True})
        with pytest.raises(ValueError, match="authoritative"):
            op.profile({"source_id": ALICE, "target_id": BOB})
        assert not op.target.writes

    def test_profile_null_is_not_fabricated_name(self):
        assert profile_diff({"surname": None}, {"surname": "User"}) == [{"field": "surname", "source": None, "target": "User"}]


class TestFiles:
    def test_remote_file_facet_is_reported_not_copied(self, store):
        rid = store.new_run("drive-pilot")
        op = SandboxOperations(store, rid)
        op.source = SimpleNamespace(get=lambda _: {"id": "source-drive"}, items=lambda _: iter([
            {"id": "root", "root": {}, "folder": {}},
            {"id": "external", "name": "remote.docx", "file": {}, "remoteItem": {"id": "outside"}, "parentReference": {"id": "root"}},
        ]))
        op.target = SimpleNamespace(get=lambda path: {"id": "target-root" if path.endswith('/root') else "target-drive"})
        op.folder = lambda *_: "sandbox-folder"
        def forbidden(*_):
            raise AssertionError("remote file must not reach the copy adapter")
        op.file = forbidden
        op.drive({"source_id": ALICE, "target_id": BOB})
        assert store.receipts()[0]["state"] == "NEEDS_ATTENTION"
        assert "remote" in store.receipts()[0]["detail"]["error"]

    def test_file_byte_readback_and_no_duplicate_upload(self, store):
        rid = store.new_run("drive-pilot")
        op = SandboxOperations(store, rid)
        content = b"synthetic-content"
        class Source:
            def download_into(self, path, sink, **_):
                sink.write(content); return len(content)
            def get(self, _):
                return {"eTag": "version-1"}
        class Target:
            uploads = 0
            exists = False
            def get(self, _):
                if not self.exists:
                    raise GraphError(404, "notFound")
                return {"id": "new-file"}
            def upload_file(self, _, stream, size):
                assert stream.read() == content and size == len(content)
                self.uploads += 1; self.exists = True
                return {"id": "new-file"}
            def download_into(self, path, sink, **_):
                sink.write(content); return len(content)
        op.source, op.target = Source(), Target()
        item = {"id": "old-file", "name": "x.txt", "size": len(content), "eTag": "version-1"}
        op.file("old-drive", item, "new-drive", "parent")
        op.file("old-drive", item, "new-drive", "parent")
        assert op.target.uploads == 1
        assert store.receipts()[0]["state"] == "VERIFIED"

    def test_unowned_target_conflict_is_not_overwritten(self, store):
        rid = store.new_run("drive-pilot")
        op = SandboxOperations(store, rid)
        def download(path, sink, **_):
            sink.write(b"x"); return 1
        op.source = SimpleNamespace(download_into=download)
        op.target = SimpleNamespace(get=lambda _: {"id": "foreign"})
        with pytest.raises(ValueError, match="without a migration intent"):
            op.file("a", {"id": "b", "name": "x", "size": 1, "eTag": "v"}, "c", "d")

    def test_bad_target_hash_opens_repair(self, store):
        rid = store.new_run("drive-pilot")
        op = SandboxOperations(store, rid)
        def source_download(path, sink, **_):
            sink.write(b"x"); return 1
        def wrong_download(path, sink, **_):
            sink.write(b"y"); return 1
        def missing(_):
            raise GraphError(404, "notFound")
        op.source = SimpleNamespace(download_into=source_download, get=lambda _: {"eTag": "v"})
        op.target = SimpleNamespace(get=missing, upload_file=lambda *_: {"id": "new"}, download_into=wrong_download)
        with pytest.raises(ValueError, match="SHA-256"):
            op.file("a", {"id": "b", "name": "x", "size": 1, "eTag": "v"}, "c", "d")
        assert store.snapshot()["repairs"][0]["state"] == "OPEN"


class TestCertificateVault:
    def test_certificate_is_encrypted_and_bound_to_connection(self, store, tmp_path):
        from datetime import datetime, timedelta, timezone
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        from microsoft365.graph import credential_path, credential_status, install_certificate, read_credential
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Bitport synthetic test")])
        now = datetime.now(timezone.utc)
        cert = x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1)).sign(key, hashes.SHA256())
        key_path, cert_path = tmp_path / "test.key", tmp_path / "test.crt"
        key_path.write_bytes(private); cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        install_certificate(7, "source", SOURCE, APP, key_path, cert_path)
        ciphertext = credential_path(7, "source").read_bytes()
        assert b"PRIVATE KEY" not in ciphertext
        assert read_credential(7, "source", configuration()["source"])["private_key"].encode() == private
        assert credential_status(7, "source", configuration()["source"])["ready"]
        wrong = {**configuration()["source"], "tenantId": OTHER}
        with pytest.raises(ValueError, match="different connection"):
            read_credential(7, "source", wrong)
        assert not credential_status(7, "source", wrong)["ready"]


@pytest.fixture
def api(store):
    op = SimpleNamespace(account_id=7, name="Sandbox operator", role="admin")
    def operator():
        return op
    def login(user):
        if user.account_id is None:
            raise HTTPException(401, "sign in required")
    def admin(user):
        if user.role != "admin":
            raise HTTPException(403, "admin required")
    async def off_loop(fn, *args):
        return fn(*args)
    app = FastAPI()
    app.include_router(router(operator=operator, require_login=login, require_admin=admin,
                              require_active_subscription=lambda _: None, launch=lambda *_: (False, "capacity is full"), off_loop=off_loop))
    with TestClient(app) as client:
        yield client, op


class TestAPI:
    def test_anonymous_has_no_inventory(self, api):
        client, op = api; op.account_id = None
        assert client.get("/api/v2/microsoft/workspace").status_code == 401

    def test_viewer_cannot_change_connections(self, api):
        client, op = api; op.role = "viewer"
        response = client.post("/api/v2/microsoft/connections", json={**configuration(), "reason": "fixture setup"})
        assert response.status_code == 403

    def test_config_roundtrip_contains_no_credentials(self, api):
        client, _ = api
        assert client.post("/api/v2/microsoft/connections", json={**configuration(), "reason": "fixture setup"}).status_code == 200
        payload = client.get("/api/v2/microsoft/workspace").json()
        assert payload["config"]["source"]["tenantId"] == SOURCE
        assert "private_key" not in json.dumps(payload)

    def test_foreign_user_mapping_rejected(self, api, store):
        inventoried(store)
        client, _ = api
        response = client.post("/api/v2/microsoft/mappings", json={"sourceId": OTHER, "targetId": BOB, "reason": "fixture mapping"})
        assert response.status_code == 400

    def test_account_header_cannot_select_other_store(self, api):
        client, _ = api
        payload = client.get("/api/v2/microsoft/workspace", headers={"X-Bitport-Migration": "999"}).json()
        # The enclosing control-plane operator dependency owns header resolution.
        assert payload["accountId"] == 7

    def test_empty_reason_rejected(self, api):
        client, _ = api
        assert client.post("/api/v2/microsoft/discover", json={"reason": ""}).status_code == 422

    def test_missing_certificate_blocks_discovery(self, api):
        client, _ = api
        assert client.post("/api/v2/microsoft/discover", json={"reason": "fixture discovery"}).status_code == 400

    def test_full_report_contains_every_scope(self, api, store):
        rid = inventoried(store)
        store.coverage(rid, "source", "calendar", "fixture", "ERROR", None, "fixture failure")
        client, _ = api
        response = client.get("/api/v2/microsoft/report")
        assert response.status_code == 200
        assert len(response.json()["allCoverageScopes"]) == 3
