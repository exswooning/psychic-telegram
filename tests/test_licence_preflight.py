"""
tests/test_licence_preflight.py
===============================
An unlicensed target account has no Drive and no Gmail, and Google says so
with 401 "Active session is invalid. Error code: 4" and 400 "Mail service
not enabled" -- neither of which contains the word licence. Live, a tenant
holding 201 accounts against 200 seats produced exactly that, twice, and it
was read as an outage both times.

So the question is asked BEFORE a run, where it is still answerable, and
per PAIR rather than per tenant: what matters is whether the specific
target address this source user is mapped to holds a licence.
"""

from __future__ import annotations

import pytest

import webui


class _S:
    def __init__(self, db_path):
        self.db_path = db_path
        self.source_domain, self.target_domain = "src.test", "tgt.test"


@pytest.fixture
def ledger(tmp_path):
    from db import MigrationDB

    d = MigrationDB(str(tmp_path / "m.db"))
    return d


def _pairs(d, pairs):
    import db as dbmod

    dbmod.bulk_seed_identities(d, pairs)
    return d


@pytest.fixture
def wired(monkeypatch, ledger):
    """Point the preflight at a temp ledger and a scripted Licensing API."""
    import tenant_inventory

    def install(source: dict, target: dict, src_err="", tgt_err=""):
        monkeypatch.setattr(
            tenant_inventory, "licenses",
            lambda st, side: ((source, src_err) if side == "source"
                              else (target, tgt_err)))
        import config
        monkeypatch.setattr(config, "Settings",
                            lambda **kw: _S(ledger.path))
        return ledger

    return install


class TestItCountsPairsNotTenants:
    def test_a_target_with_no_licence_is_the_shortfall(self, wired, ledger):
        wired(source={"a@src.test": "sku", "b@src.test": "sku"},
              target={"a@tgt.test": "sku", "b@tgt.test": ""})
        _pairs(ledger, [("a@src.test", "a@tgt.test"),
                        ("b@src.test", "b@tgt.test")])
        out = webui.licence_preflight()
        assert out["pairs"] == 2
        assert out["shortfall"] == 1
        assert out["unlicensedTargets"] == ["b@tgt.test"]

    def test_a_target_absent_from_the_licence_list_counts_too(self, wired, ledger):
        """Never assigned at all is the same problem as assigned nothing."""
        wired(source={"a@src.test": "sku"}, target={})
        _pairs(ledger, [("a@src.test", "a@tgt.test")])
        assert webui.licence_preflight()["shortfall"] == 1

    def test_case_does_not_invent_a_shortfall(self, wired, ledger):
        """The Licensing API answers in the case the account was created in;
        the ledger holds whatever was typed."""
        wired(source={"a@src.test": "sku"}, target={"A@Tgt.Test": "sku"})
        _pairs(ledger, [("a@src.test", "a@tgt.test")])
        assert webui.licence_preflight()["shortfall"] == 0

    def test_a_fully_licensed_map_warns_about_nothing(self, wired, ledger):
        wired(source={"a@src.test": "sku"}, target={"a@tgt.test": "sku"})
        _pairs(ledger, [("a@src.test", "a@tgt.test")])
        out = webui.licence_preflight()
        assert out["shortfall"] == 0 and out["unlicensedTargets"] == []


class TestTheMergeIsReported:
    def test_several_sources_on_one_target_are_named(self, wired, ledger):
        """Consolidation is a real remedy for too few target seats, and the
        ledger may already be doing it -- so it is reported, not assumed."""
        wired(source={}, target={"one@tgt.test": "sku"})
        _pairs(ledger, [("a@src.test", "one@tgt.test"),
                        ("b@src.test", "one@tgt.test"),
                        ("c@src.test", "solo@tgt.test")])
        out = webui.licence_preflight()
        assert out["mergedTargets"] == [{"target": "one@tgt.test", "sources": 2}]


class TestItNeverGuesses:
    def test_a_missing_scope_is_reported_not_rendered_as_zero(self, wired, ledger):
        """This needs a scope most tenants have never granted. A panel that
        cannot read licences must say so rather than show a confident 0."""
        wired(source={}, target={},
              tgt_err="licence data needs the apps.licensing scope")
        _pairs(ledger, [("a@src.test", "a@tgt.test")])
        out = webui.licence_preflight()
        assert "apps.licensing" in out["targetError"]

    def test_an_unreadable_ledger_says_so(self, monkeypatch, wired, ledger):
        wired(source={}, target={})
        import db as dbmod
        monkeypatch.setattr(dbmod, "MigrationDB",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("database is locked")))
        out = webui.licence_preflight()
        assert "database is locked" in out["error"]
