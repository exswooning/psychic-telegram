"""readiness: a tenant is ready only when Google says so, asked as that domain's own
admin -- never from setup's result (gorkhatweed.com read "18/18 scopes live" on a key
it refused, checked as another tenant's admin)."""
import json
from types import SimpleNamespace

import pytest

import readiness
import verify_scopes

G = "https://www.googleapis.com/auth/"


@pytest.fixture
def tenant(tmp_path, monkeypatch):
    key = tmp_path / "k.json"
    key.write_text(json.dumps({"project_id": "p1", "client_id": "c1"}))
    st = SimpleNamespace(source_domain="a.com", source_admin="info@a.com", source_sa_key=str(key),
                         target_domain="b.com", target_admin="info@b.com", target_sa_key=str(key),
                         db_path=str(tmp_path / "none.db"), dry_run=False)
    monkeypatch.setattr(verify_scopes, "migrate_source_scopes", lambda s: [G + "drive"])
    monkeypatch.setattr(verify_scopes, "required_scopes", lambda s, side: [G + "drive"])
    import separate_credentials
    monkeypatch.setattr(separate_credentials, "seed_scopes", lambda: [G + "drive", G + "gmail.insert"])
    state = {"mints": lambda key, who, scope: True, "call": lambda name: {}}
    monkeypatch.setattr(verify_scopes, "probe_scope",
                        lambda key, who, scope: (state["mints"](key, who, scope), "" if state["mints"](key, who, scope) else "not delegated"))

    def fake_build(api, version, credentials=None, cache_discovery=False):
        name = {"drive": "Drive"}.get(api, api)

        class Req:
            def execute(self):
                return state["call"](name)

        return SimpleNamespace(about=lambda: SimpleNamespace(get=lambda **k: Req()))
    import googleapiclient.discovery as disc
    monkeypatch.setattr(disc, "build", fake_build)
    monkeypatch.setattr(readiness, "_creds", lambda *a: None)
    return st, state


def names(out, ok):
    return [r["name"] for r in out["checks"] if r["ok"] is ok]


def test_ready_when_everything_answers(tenant):
    st, _ = tenant
    out = readiness.check(st, "target", "migrate")
    assert out["ready"] is True, out


def test_an_admin_off_the_domain_is_not_ready(tenant):
    st, _ = tenant
    st.source_admin = "info@source.old.com"
    out = readiness.check(st, "source", "migrate")
    assert out["ready"] is False and names(out, False) == ["configured"]


def test_a_scope_that_does_not_mint_is_not_ready(tenant):
    st, state = tenant
    state["mints"] = lambda key, who, scope: False
    out = readiness.check(st, "target", "migrate")
    assert "delegation" in names(out, False)


def test_a_source_key_that_can_write_is_not_ready(tenant):
    st, state = tenant
    state["mints"] = lambda key, who, scope: True          # even gmail.insert mints
    out = readiness.check(st, "source", "migrate")
    ro = next(r for r in out["checks"] if r["name"] == "read-only")
    assert ro["ok"] is False and "gmail.insert" in ro["detail"]


def test_a_lapsed_service_fails_and_a_timeout_is_unknown(tenant):
    st, state = tenant

    def boom(name):
        raise RuntimeError('<HttpError 401 "Active session is invalid">')
    state["call"] = boom
    out = readiness.check(st, "target", "migrate")
    assert "service: Drive" in names(out, False)

    def slow(name):
        raise TimeoutError("read timed out")
    state["call"] = slow
    out = readiness.check(st, "target", "migrate")
    assert "service: Drive" in names(out, None) and out["ready"] is False


def test_the_gate_refuses_a_definite_failure_and_runs_past_an_unknown(tenant, monkeypatch, capsys):
    st, state = tenant
    monkeypatch.setenv("BITPORT_READINESS_GATE", "1")
    state["mints"] = lambda key, who, scope: scope != G + "gmail.insert"   # source is read-only

    def slow(name):
        raise TimeoutError("read timed out")
    state["call"] = slow
    readiness.gate(st)                                     # unknown: runs on
    assert "running anyway" in capsys.readouterr().out

    def boom(name):
        raise RuntimeError('<HttpError 401 "Active session is invalid">')
    state["call"] = boom
    with pytest.raises(SystemExit) as e:
        readiness.gate(st)
    assert e.value.code == 2 and "NOT READY" in capsys.readouterr().out
