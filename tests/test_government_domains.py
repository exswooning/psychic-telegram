"""A government domain is only ever read: always protected, never a sandbox."""
import pytest

import domain_guard


@pytest.mark.parametrize("d,gov", [("dharan.gov.np", True), ("it.dharan.gov.np", True),
                                   ("agency.gov", True), ("gov.example.com", False),
                                   ("government.com", False), ("gorkhatweed.com", False)])
def test_government_domains_are_recognised(d, gov):
    assert domain_guard.is_government(d) is gov


def test_a_government_domain_is_protected_and_cannot_be_unprotected(monkeypatch):
    monkeypatch.setattr(domain_guard, "protected_domains", lambda: set())
    assert domain_guard.is_protected("dharan.gov.np")
    assert "government" in domain_guard.refuse_reason("dharan.gov.np", "Seeding")
    with pytest.raises(ValueError):
        domain_guard.revoke("dharan.gov.np", by="op", reason="testing")


def test_a_government_source_is_a_customer_source(monkeypatch):
    from types import SimpleNamespace
    import verify_scopes
    monkeypatch.setattr(domain_guard, "protected_domains", lambda: set())
    assert verify_scopes.customer_source(SimpleNamespace(source_domain="dharan.gov.np"))
