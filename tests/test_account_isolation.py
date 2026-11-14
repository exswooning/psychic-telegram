"""A job whose settings come from two accounts never starts (8 Nov 2026: account 2's DMS ran with account 3's logins)."""
import account_isolation as A
import accounts_auth


def _tenants(monkeypatch):
    monkeypatch.setattr(accounts_auth, "get_tenant_config", lambda aid, side: {
        ("3", "source"): {"domain": "s.example.com"}, ("3", "target"): {"domain": "t.example.com"},
    }.get((str(aid), side)))


def test_a_consistent_env_passes_and_the_box_itself_is_unchecked(monkeypatch):
    _tenants(monkeypatch)
    ok = {"MIGRATION_DB": "/o/data/accounts/3/migration.db", "SOURCE_DOMAIN": "s.example.com",
          "TARGET_DOMAIN": "t.example.com", "DWD_EMAIL": "nest@t.example.com", "DWD_EMAIL_SOURCE": "info@s.example.com"}
    assert A.violations(3, ok) == [] and A.refusal(3, ok) == ""
    assert A.violations(None, {"MIGRATION_DB": "/o/data/accounts/9/x"}) == [] and A.violations(3, None) == []


def test_each_mix_up_is_named(monkeypatch):
    _tenants(monkeypatch)
    assert "ledger belongs to account 2" in A.refusal(3, {"MIGRATION_DB": "/o/data/accounts/2/migration.db"})
    assert "TARGET_DOMAIN" in A.refusal(3, {"TARGET_DOMAIN": "new.other.com"})
    why = A.refusal(3, {"DWD_EMAIL": "admin@new.other.com", "DWD_EMAIL_SOURCE": "x@other.com"})
    assert "DWD_EMAIL is an admin of new.other.com" in why and "DWD_EMAIL_SOURCE" in why


def test_both_start_points_refuse_before_spawning(monkeypatch):
    import api_server
    import webui
    _tenants(monkeypatch)
    bad = {"MIGRATION_DB": "/o/data/accounts/2/migration.db"}

    def boom(*a, **k):
        raise AssertionError("a child was spawned")
    monkeypatch.setattr(api_server.subprocess, "Popen", boom)
    ok, why = api_server._start_admitted(["python", "x.py"], 3, "migrate", bad)
    assert not ok and "account 2" in why
    ok, why = webui.Job(3).start("x", ["python", "x.py"], env=bad)
    assert not ok and "account 2" in why
