"""fableplan2 P1.4: wipe, reset, seed, remove-setup and "Run the DMS again" refuse while a tenant is not ready.

On 16 Nov 2026 incidents #77/#78 (tenant not ready) had been open an hour when a wipe started on both
tenants and deleted only the mail."""
import tempfile

import pytest

import oversight
import webui

OPEN = {"id": 77, "kind": "watch_tenant_ready", "fingerprint": "oversight:watch_tenant_ready:3:target",
        "title": "b.example (target) is not ready any more"}


class TestTheQuestion:
    def test_an_open_incident_for_that_side_is_a_refusal_naming_it(self):
        why = oversight.tenant_not_ready(3, "target", incidents=[OPEN])
        assert "target tenant is not ready" in why and "#77" in why

    def test_the_other_side_and_other_kinds_are_not(self):
        assert oversight.tenant_not_ready(3, "source", incidents=[OPEN]) is None
        assert oversight.tenant_not_ready(3, "target", incidents=[dict(OPEN, kind="watch_box_busy")]) is None
        assert oversight.tenant_not_ready(3, "target", incidents=[]) is None

    def test_an_unreadable_incident_list_does_not_block_the_hard_guard_is_the_wipe_preflight(self, monkeypatch):
        import run_watch
        monkeypatch.setattr(run_watch, "list_incidents", lambda *a, **k: (_ for _ in ()).throw(OSError("db")))
        assert oversight.tenant_not_ready(3, "target") is None


@pytest.fixture
def handler(monkeypatch):
    sent, launched = [], []
    h = webui.Handler.__new__(webui.Handler)
    h._json = lambda payload, code=200: sent.append((code, payload))
    h._account_id = lambda: 7
    monkeypatch.setattr(webui, "resolve_target_account", lambda a, b: (7, None))
    monkeypatch.setattr(webui, "_subscription_ok", lambda a: True)
    monkeypatch.setattr(webui, "_seed_ok", lambda a: True)
    monkeypatch.setattr(webui, "launch_or_queue", lambda *a, **k: launched.append(a) or ("started", "ok"))
    h.sent, h.launched = sent, launched
    return h


@pytest.mark.parametrize("method,side,body", [
    ("_post_wipe_target", "target", {}),
    ("_post_reset_target", "target", {}),
    ("_post_wipe_source", "source", {}),
    ("_post_seed", "source", {}),
    ("_post_remove_tenant_setup", "target", {"side": "target", "mode": "wipe"}),
])
def test_each_destructive_launcher_refuses_with_409_and_starts_nothing(handler, monkeypatch, method, side, body):
    monkeypatch.setattr(webui, "_not_ready", lambda account_id, s: "the %s tenant is not ready -- incident #77" % s if s == side else None)
    getattr(handler, method)(body)
    code, payload = handler.sent[0]
    assert code == 409 and payload["ok"] is False and "#77" in payload["error"]
    assert handler.launched == []


def test_a_ready_tenant_is_not_blocked_by_the_gate(handler, monkeypatch):
    monkeypatch.setattr(webui, "_not_ready", lambda account_id, s: None)
    monkeypatch.setattr(webui, "wipe_target_argv", lambda body, aid: (["x"], {}, ""))
    handler._post_wipe_target({})
    assert handler.launched and handler.sent[0][0] == 200


def test_run_the_dms_again_refuses_while_either_tenant_is_not_ready(monkeypatch):
    from fastapi.testclient import TestClient

    import accounts_auth
    import api_server
    import control_plane_db as cpdb
    from db import MigrationDB
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    started = []
    monkeypatch.setattr(api_server, "_start_dms", lambda *a, **k: started.append(a) or (True, "ok"))
    monkeypatch.setattr(oversight, "tenant_not_ready", lambda account_id, side: "the source tenant is not ready -- #78" if side == "source" else None)
    with TestClient(api_server.app) as client:
        aid = accounts_auth.create_account("dms@one.example", "a-long-password", "Someone")
        client.cookies.set("bp_session", accounts_auth.create_session(aid))
        r = client.post("/api/v2/dms/start", json={"reason": "again"})
    assert r.status_code == 409 and "#78" in r.text and started == []
