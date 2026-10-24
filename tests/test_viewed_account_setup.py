"""A superadmin reads another account's Setup (tenant-config, readiness); a client cannot."""
import pytest

pytest.importorskip("fastapi", reason="control plane deps not installed")
pytest.importorskip("httpx", reason="starlette TestClient needs httpx")
from fastapi.testclient import TestClient  # noqa: E402

import api_server  # noqa: E402
import accounts_auth  # noqa: E402
import readiness  # noqa: E402

PATHS = ["/api/v2/setup/tenant-config?side=source", "/api/v2/setup/readiness?side=source"]


@pytest.fixture
def client(monkeypatch):
    cfgs = {1: {"domain": "mine.com", "admin_email": "a@mine.com"},
            7: {"domain": "theirs.com", "admin_email": "a@theirs.com"}}
    monkeypatch.setattr(accounts_auth, "get_tenant_config", lambda aid, side: cfgs.get(aid))
    import config
    monkeypatch.setattr(readiness, "check", lambda s, side, purpose: {"domain": cfgs[s.account_id]["domain"]})
    who = {}
    app = api_server.app
    app.dependency_overrides[api_server.operator] = lambda: api_server.Operator(
        name="t", role="admin", account_id=1, login_id=1, is_superadmin=who["super"])
    with TestClient(app) as c:
        monkeypatch.setattr(config, "Settings", lambda account_id=None: type("S", (), {"account_id": account_id})())
        yield c, who
    app.dependency_overrides.clear()


@pytest.mark.parametrize("path", PATHS)
def test_default_is_own_account_and_superadmin_may_name_another(client, path):
    c, who = client
    who["super"] = True
    own = c.get(path).json()
    other = c.get(path + "&account_id=7")
    assert other.status_code == 200 and own["domain"] == "mine.com" and other.json()["domain"] == "theirs.com"


@pytest.mark.parametrize("path", PATHS)
def test_a_client_naming_another_account_is_refused(client, path):
    c, who = client
    who["super"] = False
    assert c.get(path).json()["domain"] == "mine.com"
    assert c.get(path + "&account_id=7").status_code == 403
