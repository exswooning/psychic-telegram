"""Per-licence storage for the Top Up panel.

Workspace storage is pooled: Drive reports the TENANT's pool as every user's
limit (300 Business Starter accounts report 300 x 30 GiB, not 30). The panel
must show one ACCOUNT's share, from its licence -- reading the pool as a
per-account limit made a "100%" fill try to load the whole pool into each
account. An unlimited plan is None (not 0); one SKU's failure does not blank
the rest."""
import auth
import tenant_inventory
import webui

GIB = 1024 ** 3


class _About:
    def __init__(self, limit): self.limit = limit
    def about(self): return self
    def get(self, **_): return self
    def execute(self):
        if self.limit == "boom":
            raise RuntimeError("403")
        return {"storageQuota": {"limit": self.limit} if self.limit else {}}


def _run(monkeypatch, licences, limits):
    monkeypatch.setattr(tenant_inventory, "licenses", lambda st, side: (licences, ""))

    class FakeAuth:
        def __init__(self, st): pass
        def source_drive(self, u): return _About(limits[u])

    monkeypatch.setattr(auth, "AuthManager", FakeAuth)
    import config
    monkeypatch.setattr(config, "Settings", lambda **k: object())
    return {s["skuId"]: s for s in webui.storage_summary_payload()["skus"]}


def test_a_pooled_tenant_shows_the_licences_share_not_the_pool(monkeypatch):
    starter = "1010020027"
    licences = {f"u{i}@x": starter for i in range(300)}
    pool = str(300 * 30 * GIB)            # what Drive reports for EVERY user
    by = _run(monkeypatch, licences, {"u0@x": pool})[starter]
    assert by["name"] == "Business Starter" and by["accounts"] == 300
    assert by["limitBytes"] == 30 * GIB           # one account's share
    assert by["poolBytes"] == 300 * 30 * GIB      # the whole tenant's pool


def test_each_known_licence_has_its_own_share(monkeypatch):
    by = _run(monkeypatch,
              {"a@x": "1010020028", "b@x": "1010020025"},
              {"a@x": str(10 * GIB), "b@x": str(10 * GIB)})
    assert by["1010020028"]["limitBytes"] == 2048 * GIB
    assert by["1010020025"]["limitBytes"] == 5120 * GIB


def test_an_unlisted_licence_is_not_guessed_when_it_is_not_the_only_one(monkeypatch):
    by = _run(monkeypatch, {"a@x": "1010020027", "b@x": "MYSTERY"},
              {"a@x": str(60 * GIB), "b@x": str(60 * GIB)})
    assert by["MYSTERY"]["limitBytes"] is None
    assert by["1010020027"]["limitBytes"] == 30 * GIB


def test_the_only_unlisted_licence_is_exact_pool_over_accounts(monkeypatch):
    by = _run(monkeypatch, {f"u{i}@x": "MYSTERY" for i in range(4)},
              {"u0@x": str(4 * 15 * GIB)})
    assert by["MYSTERY"]["limitBytes"] == 15 * GIB


def test_an_unlimited_plan_is_none_and_one_failure_does_not_blank_the_rest(monkeypatch):
    by = _run(monkeypatch, {"c@x": "U", "d@x": "E", "a@x": "1010020027"},
              {"c@x": None, "d@x": "boom", "a@x": str(30 * GIB)})
    assert by["U"]["limitBytes"] is None and by["U"]["poolBytes"] is None
    assert not by["U"]["error"]
    assert "403" in by["E"]["error"]
    assert by["1010020027"]["limitBytes"] == 30 * GIB
