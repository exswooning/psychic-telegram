"""
tests/test_tenant_config_cannot_collide.py
==========================================
Live, an account's SOURCE row was overwritten with its TARGET tenant's
domain and admin. The Setup Wizard treats the first domain you give it as
the source, and somebody used it to set up their destination.

The row being wrong is not the danger. seed_argv's typed-domain gate --
the thing standing between "seed the sandbox" and "write fabricated data
into production" -- compares what an operator types against
source_domain. With the target's name sitting in that column, typing the
target's name PASSES the check that exists to stop precisely that.
"""

from __future__ import annotations

import pytest

import accounts_auth
import control_plane_db as cpdb


@pytest.fixture
def account(tmp_path, monkeypatch):
    path = str(tmp_path / "cp.db")
    cpdb.apply_migrations(path)
    monkeypatch.setattr(cpdb, "_db_path", lambda: path)
    return accounts_auth.create_account("a@x.test", "hunter22222", "Tester")


class TestOneDomainCannotBeBothSides:
    def test_the_target_cannot_be_written_into_source(self, account):
        accounts_auth.update_tenant_config(account, "target", domain="acme.com")
        with pytest.raises(ValueError, match="cannot be both"):
            accounts_auth.update_tenant_config(account, "source", domain="acme.com")

    def test_and_the_source_cannot_be_written_into_target(self, account):
        accounts_auth.update_tenant_config(account, "source", domain="acme.com")
        with pytest.raises(ValueError, match="cannot be both"):
            accounts_auth.update_tenant_config(account, "target", domain="acme.com")

    def test_case_and_padding_do_not_get_around_it(self, account):
        accounts_auth.update_tenant_config(account, "target", domain="acme.com")
        with pytest.raises(ValueError):
            accounts_auth.update_tenant_config(account, "source", domain="  ACME.com ")

    def test_the_refusal_names_the_domain_and_the_side(self, account):
        """So the operator knows what to clear, rather than being told no."""
        accounts_auth.update_tenant_config(account, "target", domain="acme.com")
        with pytest.raises(ValueError) as e:
            accounts_auth.update_tenant_config(account, "source", domain="acme.com")
        assert "acme.com" in str(e.value) and "target" in str(e.value)

    def test_the_bad_write_does_not_land(self, account):
        accounts_auth.update_tenant_config(account, "source", domain="src.example")
        accounts_auth.update_tenant_config(account, "target", domain="tgt.example")
        with pytest.raises(ValueError):
            accounts_auth.update_tenant_config(account, "source", domain="tgt.example")
        assert accounts_auth.get_tenant_config(account, "source")["domain"] \
            == "src.example"


class TestOrdinaryWritesStillWork:
    def test_two_different_domains_are_fine(self, account):
        accounts_auth.update_tenant_config(account, "source", domain="src.example")
        accounts_auth.update_tenant_config(account, "target", domain="tgt.example")
        assert accounts_auth.get_tenant_config(account, "target")["domain"] \
            == "tgt.example"

    def test_rewriting_a_side_with_its_own_domain_is_fine(self, account):
        """Re-running setup for the same tenant must not trip the guard."""
        accounts_auth.update_tenant_config(account, "source", domain="src.example")
        accounts_auth.update_tenant_config(account, "source", domain="src.example",
                                           admin_email="a@src.example")
        assert accounts_auth.get_tenant_config(account, "source")["admin_email"] \
            == "a@src.example"

    def test_updating_only_the_admin_is_unaffected(self, account):
        accounts_auth.update_tenant_config(account, "target", domain="acme.com")
        accounts_auth.update_tenant_config(account, "source",
                                           admin_email="a@other.example")
        assert accounts_auth.get_tenant_config(account, "source")["admin_email"] \
            == "a@other.example"

    def test_the_other_side_being_unset_is_fine(self, account):
        accounts_auth.update_tenant_config(account, "source", domain="src.example")
        assert accounts_auth.get_tenant_config(account, "source")["domain"] \
            == "src.example"
