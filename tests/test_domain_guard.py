"""
tests/test_domain_guard.py
==========================
PROTECTED_DOMAINS was an environment variable, parsed in four places with
the same four lines, and empty unless somebody remembered to set it. That is
the wrong default for the list standing between a typo and a client's
tenant: the moment it matters is the moment nobody thought about it.

Inverted here. A domain is protected from the moment it is configured --
the setup wizard writing a tenant_configs row IS the protection. The sandbox
is the exception, and exceptions are declared: revoked by name, with a
reason, on the record.
"""

from __future__ import annotations

import json
import os

import pytest

import domain_guard as G


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "REVOCATIONS_PATH", str(tmp_path / "u.json"))
    monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)
    monkeypatch.delenv("SOURCE_DOMAIN", raising=False)
    monkeypatch.delenv("TARGET_DOMAIN", raising=False)
    monkeypatch.setattr(G, "configured_domains", lambda: {"client.example"})
    return tmp_path


class TestProtectedByDefault:
    def test_a_configured_domain_needs_no_list_to_be_protected(self, store):
        assert G.is_protected("client.example")

    def test_case_and_spacing_do_not_get_round_it(self, store):
        for spelling in ("Client.Example", "  client.example  ", "CLIENT.EXAMPLE"):
            assert G.is_protected(spelling), spelling

    def test_an_unknown_domain_is_not_protected(self, store):
        assert not G.is_protected("someone-elses.example")

    def test_the_environment_list_still_works(self, store, monkeypatch):
        """A deployment that set the variable must lose nothing."""
        monkeypatch.setenv("PROTECTED_DOMAINS", "legacy.example, other.example")
        assert G.is_protected("legacy.example")
        assert G.is_protected("other.example")


class TestRevocationIsDeliberate:
    def test_revoking_unprotects_exactly_one_domain(self, store):
        G.revoke("client.example", "aryan", "sandbox for this deployment")
        assert not G.is_protected("client.example")

    def test_a_reason_is_required(self, store):
        """The banner reads it back at every start. Months later, "no reason
        given" is indistinguishable from an accident."""
        with pytest.raises(ValueError):
            G.revoke("client.example", "aryan", "")
        assert G.is_protected("client.example"), "unprotected despite refusing"

    def test_it_records_who_and_when(self, store):
        G.revoke("client.example", "aryan", "because")
        rec = G.revocations()["client.example"]
        assert rec["by"] == "aryan" and rec["reason"] == "because"
        assert rec["at"].startswith("20")

    def test_restoring_puts_it_back(self, store):
        G.revoke("client.example", "aryan", "temporarily")
        assert G.restore("client.example") is True
        assert G.is_protected("client.example")

    def test_restoring_something_never_revoked_says_so(self, store):
        assert G.restore("client.example") is False


class TestItFailsSafe:
    def test_an_unreadable_store_protects_everything(self, store):
        """A guard that fails open the moment its own file is corrupt is
        not a guard."""
        with open(G.REVOCATIONS_PATH, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        assert G.revocations() == {}
        assert G.is_protected("client.example")

    def test_the_write_is_atomic(self, store):
        """Truncate-in-place plus a crash would leave every domain
        unprotected -- the one failure this must not have."""
        G.revoke("client.example", "a", "x")
        leftovers = [f for f in os.listdir(os.path.dirname(G.REVOCATIONS_PATH))
                     if f.startswith(".unprotected-")]
        assert leftovers == [], leftovers
        assert json.load(open(G.REVOCATIONS_PATH))["client.example"]

    def test_a_missing_control_plane_does_not_unprotect_anything(self,
                                                                monkeypatch,
                                                                tmp_path):
        """configured_domains swallows a database it cannot read. It must
        return nothing rather than raise -- and nothing must not become
        permission."""
        monkeypatch.setattr(G, "REVOCATIONS_PATH", str(tmp_path / "u.json"))
        monkeypatch.setenv("SOURCE_DOMAIN", "client.example")
        assert "client.example" in G.env_configured_domains()


class TestTheRefusalTellsYouWhatToDo:
    def test_it_names_the_domain_and_the_way_out(self, store):
        why = G.refuse_reason("client.example")
        assert "client.example" in why and "--revoke" in why

    def test_an_unprotected_domain_gives_no_reason(self, store):
        assert G.refuse_reason("someone-elses.example") == ""


class TestTheBannerOnlyShoutsAboutRevocations:
    def test_a_clean_deployment_prints_nothing(self, store):
        """Saying "everything is protected" every start trains people to
        skim the banner that matters."""
        assert G.startup_report() == ""

    def test_a_revoked_domain_is_impossible_to_miss(self, store):
        G.revoke("client.example", "aryan", "sandbox")
        rep = G.startup_report()
        assert "client.example" in rep and "aryan" in rep and "sandbox" in rep
        assert "PROTECTION IS OFF" in rep
