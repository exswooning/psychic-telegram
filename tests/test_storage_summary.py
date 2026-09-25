"""One sample account per licence SKU, its real storageQuota.limit; an
unlimited plan is None (not 0), one SKU's failure does not blank the rest."""
import auth
import tenant_inventory
import webui


class _About:
    def __init__(self, limit): self.limit = limit
    def about(self): return self
    def get(self, **_): return self
    def execute(self):
        if self.limit == "boom":
            raise RuntimeError("403")
        return {"storageQuota": {"limit": self.limit} if self.limit else {}}


def test_one_limit_per_sku(monkeypatch):
    monkeypatch.setattr(tenant_inventory, "licenses", lambda st, side: (
        {"a@x": "1010020028", "b@x": "1010020028", "c@x": "U", "d@x": "E"}, ""))
    limits = {"a@x": "2000000000000", "c@x": None, "d@x": "boom"}

    class FakeAuth:
        def __init__(self, st): pass
        def source_drive(self, u): return _About(limits[u])

    monkeypatch.setattr(auth, "AuthManager", FakeAuth)
    import config
    monkeypatch.setattr(config, "Settings", lambda **k: object())
    out = webui.storage_summary_payload()
    by = {s["skuId"]: s for s in out["skus"]}
    assert by["1010020028"]["accounts"] == 2
    assert by["1010020028"]["name"] == "Business Standard"
    assert by["1010020028"]["limitBytes"] == 2_000_000_000_000
    assert by["U"]["limitBytes"] is None and not by["U"]["error"]
    assert "403" in by["E"]["error"]
