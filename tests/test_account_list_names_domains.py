"""The tenant chooser listed logins, not domains.

Reported live, on /app/services: "it gives me option to select user ids
not the verified domains". Every button on that page acts on a Google
Workspace domain -- shared drives, groups, SSO profiles all belong to a
tenant, none of them to a Bitport account -- but the chooser naming which
tenant to act on rendered `a.email`, the address somebody signed up with.
An operator running three migrations cannot tell those apart.

The frontend could not do better, because /api/v2/admin/accounts returned
no domain at all: id, email, name, plan, created_at, subscription_active,
is_superadmin, seed_enabled. So the fix is here, in the one query the
admin list is built from.

The second half is the endpoint the chips read. verified-domains answered
about op.account_id unconditionally, so picking somebody else's tenant
showed YOUR domains back with nothing on screen saying so -- which is
worse than showing logins, because it looks like an answer.
"""
from __future__ import annotations

import os
import tempfile

import pytest

import accounts_auth as aa
import control_plane_db as cpdb
from db import MigrationDB


@pytest.fixture
def db(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _account(email: str, source: str = "", target: str = "") -> int:
    account_id = aa.create_account(email, "hunter2hunter2",
                                   f"Acct {email.split(chr(64))[0]}")
    if source:
        aa.update_tenant_config(account_id, "source", domain=source,
                                admin_email=f"admin@{source}")
    if target:
        aa.update_tenant_config(account_id, "target", domain=target,
                                admin_email=f"admin@{target}")
    return account_id


class TestTheListCarriesDomains:
    def test_each_account_names_its_tenants(self, db):
        _account("ops@x.test", "old.acme.test", "new.acme.test")
        row = aa.list_accounts()[0]
        assert row["source_domain"] == "old.acme.test"
        assert row["target_domain"] == "new.acme.test"

    def test_an_account_before_the_wizard_has_run_reports_none(self, db):
        """Not "" and not a missing key: the chooser falls back to the
        login for exactly this row, and it has to be able to tell."""
        _account("fresh@z.test")
        row = aa.list_accounts()[0]
        assert row["source_domain"] is None
        assert row["target_domain"] is None

    def test_one_side_configured_is_not_mistaken_for_the_other(self, db):
        _account("half@y.test", source="only-source.test")
        row = aa.list_accounts()[0]
        assert row["source_domain"] == "only-source.test"
        assert row["target_domain"] is None

    def test_domains_are_not_smeared_across_accounts(self, db):
        """A LEFT JOIN missing its side predicate would give every account
        every row -- and the chooser would name three tenants identically,
        which is the bug this fixes, restated."""
        _account("alpha@x.test", "a-src.test", "a-tgt.test")
        _account("bravo@x.test", "b-src.test", "b-tgt.test")
        by_email = {r["email"]: r for r in aa.list_accounts()}
        assert len(by_email) == 2
        assert by_email["alpha@x.test"]["source_domain"] == "a-src.test"
        assert by_email["bravo@x.test"]["target_domain"] == "b-tgt.test"

    def test_the_existing_columns_are_still_there(self, db):
        """The admin dashboard reads all of these; adding a join must not
        drop one, and `SELECT a.*` was not used precisely so this is
        checkable."""
        _account("ops@x.test", "old.acme.test")
        row = aa.list_accounts()[0]
        for key in ("id", "email", "name", "plan", "created_at",
                    "subscription_active", "is_superadmin", "seed_enabled"):
            assert key in row, key

    def test_it_is_one_query_not_one_per_account(self, db):
        """Ten accounts must not be twenty-one round trips. Asserted on the
        source rather than by counting: the point is the JOIN, and a future
        rewrite that keeps the JOIN should keep passing."""
        import inspect
        src = inspect.getsource(aa.list_accounts)
        # Counted in the SQL only -- the docstring says the words too.
        sql = src.split('"""')[-1]
        assert sql.count("LEFT JOIN") == 2
        assert "get_tenant_config" not in src


class TestVerifiedDomainsAnswersAboutTheAccountAsked:
    def test_it_takes_an_account_id(self):
        import inspect

        import api_server
        sig = inspect.signature(api_server.verified_domains)
        assert "account_id" in sig.parameters
        assert sig.parameters["account_id"].default is None

    def test_an_absent_one_still_means_the_caller(self):
        """Every existing caller sends nothing and must keep getting its
        own domains back."""
        import inspect

        import api_server
        src = inspect.getsource(api_server.verified_domains)
        assert "account_id if account_id is not None else op.account_id" in src

    def test_it_cannot_be_used_to_read_another_tenant(self):
        """The parameter is caller-supplied, so it is a privilege boundary:
        without the check, any logged-in account could enumerate every
        other tenant's domains and admin addresses by counting up from 1."""
        import inspect

        import api_server
        src = inspect.getsource(api_server.verified_domains)
        assert "_require_account_access(account_id, op)" in src
        assert (src.index("_require_account_access")
                < src.index("def _check_side"))

    def test_settings_are_built_for_that_account_too(self, db):
        """Resolving the domain from the chosen account and then reading
        keys and scopes from the caller's Settings would report one
        tenant's domain with another's delegation status."""
        import inspect

        import api_server
        src = inspect.getsource(api_server.verified_domains)
        assert "Settings(account_id=account_id)" in src
        assert "Settings(account_id=op.account_id)" not in src
