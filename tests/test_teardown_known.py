"""Teardown asked for identifiers nothing in the UI ever showed.

/api/v2/teardown/start refuses without a project id or a DWD client id,
and neither is in tenant_configs -- both are fields of the service-account
key. So the documented way to fill that form was to SSH in and cat a JSON
file, which is the one thing the UI is supposed to replace.
"""
import json

import pytest
from fastapi.testclient import TestClient

import api_server


@pytest.fixture
def client():
    return TestClient(api_server.app)


def _key(tmp_path, name, project, client_id):
    p = tmp_path / name
    p.write_text(json.dumps({
        "type": "service_account", "project_id": project,
        "client_id": client_id,
        "private_key": "-----BEGIN PRIVATE KEY-----\nSECRET\n-----END PRIVATE KEY-----\n",
        "client_email": f"sa@{project}.iam.gserviceaccount.com"}))
    return str(p)


@pytest.fixture
def one_account(tmp_path, monkeypatch):
    key = _key(tmp_path, "source-sa.json", "wsmig-src-96030", "10747993343463666")
    monkeypatch.setattr(api_server.accounts_auth, "list_accounts",
                        lambda: [{"id": 7, "email": "someone@example.com"}])
    monkeypatch.setattr(
        api_server.accounts_auth, "get_tenant_config",
        lambda aid, side: ({"domain": "source.example", "admin_email": "a@source.example",
                            "sa_key_path": key, "db_path": "/x.db"}
                           if side == "source" else
                           {"domain": "", "admin_email": "", "sa_key_path": "",
                            "db_path": ""}))
    return key


def _as_superadmin(monkeypatch):
    op = api_server.Operator(name="boss", role="admin", account_id=66,
                             is_superadmin=True)
    api_server.app.dependency_overrides[api_server.operator] = lambda: op
    return op


class TestItSurfacesWhatTeardownNeeds:
    def test_it_reports_the_project_and_client_id_from_the_key(
            self, client, one_account, monkeypatch):
        _as_superadmin(monkeypatch)
        try:
            rows = client.get("/api/v2/teardown/known").json()["tenants"]
        finally:
            api_server.app.dependency_overrides.clear()
        src = [r for r in rows if r["side"] == "source"][0]
        assert src["projectId"] == "wsmig-src-96030"
        assert src["clientId"] == "10747993343463666"
        assert src["accountId"] == 7

    def test_it_never_returns_the_private_key(self, client, one_account,
                                              monkeypatch):
        _as_superadmin(monkeypatch)
        try:
            body = client.get("/api/v2/teardown/known").text
        finally:
            api_server.app.dependency_overrides.clear()
        assert "SECRET" not in body
        assert "private_key" not in body

    def test_an_unconfigured_side_is_left_out(self, client, one_account,
                                              monkeypatch):
        # A row with no domain and no key is not something to tear down.
        _as_superadmin(monkeypatch)
        try:
            rows = client.get("/api/v2/teardown/known").json()["tenants"]
        finally:
            api_server.app.dependency_overrides.clear()
        assert [r["side"] for r in rows] == ["source"]

    def test_a_missing_key_file_is_reported_not_fatal(self, client, monkeypatch):
        _as_superadmin(monkeypatch)
        monkeypatch.setattr(api_server.accounts_auth, "list_accounts",
                            lambda: [{"id": 7, "email": "x@y"}])
        monkeypatch.setattr(
            api_server.accounts_auth, "get_tenant_config",
            lambda aid, side: {"domain": "d.example", "admin_email": "a@d.example",
                               "sa_key_path": "/nope/missing.json", "db_path": ""})
        try:
            rows = client.get("/api/v2/teardown/known").json()["tenants"]
        finally:
            api_server.app.dependency_overrides.clear()
        assert rows and rows[0]["keyPresent"] is False
        assert rows[0]["projectId"] == ""


class TestScope:
    def test_a_plain_account_sees_only_itself(self, client, one_account,
                                              monkeypatch):
        op = api_server.Operator(name="t", role="admin", account_id=7)
        api_server.app.dependency_overrides[api_server.operator] = lambda: op
        called = []
        monkeypatch.setattr(api_server.accounts_auth, "list_accounts",
                            lambda: called.append(1) or [])
        try:
            rows = client.get("/api/v2/teardown/known").json()["tenants"]
        finally:
            api_server.app.dependency_overrides.clear()
        assert not called, "listed every account for a non-superadmin"
        assert {r["accountId"] for r in rows} == {7}

    def test_it_refuses_an_anonymous_caller(self, client, monkeypatch):
        monkeypatch.delenv("CP_OPERATORS", raising=False)
        op = api_server.Operator(name="anonymous", role="viewer", account_id=None)
        api_server.app.dependency_overrides[api_server.operator] = lambda: op
        try:
            assert client.get("/api/v2/teardown/known").status_code == 401
        finally:
            api_server.app.dependency_overrides.clear()
