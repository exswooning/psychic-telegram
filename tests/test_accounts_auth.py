"""
tests/test_accounts_auth.py
============================
Real SaaS accounts: password hashing, sessions, and the legacy-account
bootstrap that makes the in-flight single-tenant deployment become
"account #1" without moving a single file.

The property that matters most here isn't "signup works" -- it's that
Settings(account_id=1) is byte-identical to the plain Settings() every
existing call site already uses. If that ever drifted, the live migration
this whole feature was built next to would start reading a different
domain or key file out from under itself.
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
    """A throwaway shared control-plane db, migrated but with no accounts
    yet -- the state the very first api_server.py startup finds."""
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


class TestPasswordHashing:
    def test_a_correct_password_verifies(self):
        stored = aa.hash_password("correct horse battery staple")
        assert aa.verify_password("correct horse battery staple", stored)

    def test_a_wrong_password_does_not_verify(self):
        stored = aa.hash_password("correct horse battery staple")
        assert not aa.verify_password("wrong password entirely", stored)

    def test_the_same_password_hashes_differently_each_time(self):
        """A random salt per call -- otherwise two accounts sharing a
        password would have identical rows, which leaks that fact to
        anyone who can read the accounts table."""
        a = aa.hash_password("hunter22222")
        b = aa.hash_password("hunter22222")
        assert a != b
        assert aa.verify_password("hunter22222", a)
        assert aa.verify_password("hunter22222", b)

    def test_garbage_stored_values_fail_closed(self):
        """A corrupted or hand-edited password_hash column must reject
        every password, never raise past this function into a 500 that
        might read as "server error, try again"."""
        assert not aa.verify_password("anything", "not-a-real-hash")
        assert not aa.verify_password("anything", "")
        assert not aa.verify_password("anything", "bcrypt$stuff$here")


class TestCreateAccount:
    def test_creates_the_account_and_both_tenant_config_rows(self, db):
        account_id = aa.create_account("new@example.com", "hunter22222", "New User")
        account = aa.get_account(account_id)
        assert account["email"] == "new@example.com"
        assert account["name"] == "New User"
        assert account["plan"] == "trial"

        with cpdb.ro() as conn:
            rows = {r["side"]: dict(r) for r in conn.execute(
                "SELECT * FROM tenant_configs WHERE account_id=?", (account_id,))}
        assert set(rows) == {"source", "target"}
        for side, row in rows.items():
            assert row["db_path"] == os.path.join("data", "accounts", str(account_id), "migration.db")
            assert row["sa_key_path"] == os.path.join("keys", str(account_id), f"{side}-sa.json")
            # Not yet configured -- that only happens once this account
            # runs its own Quick Setup.
            assert row["domain"] is None

    def test_creates_the_account_directories_on_disk(self, db):
        account_id = aa.create_account("dirs@example.com", "hunter22222", "Dir User")
        assert os.path.isdir(os.path.join(aa.HERE, "data", "accounts", str(account_id)))
        assert os.path.isdir(os.path.join(aa.HERE, "keys", str(account_id)))
        import shutil
        shutil.rmtree(os.path.join(aa.HERE, "data", "accounts", str(account_id)))
        shutil.rmtree(os.path.join(aa.HERE, "keys", str(account_id)))

    def test_a_duplicate_email_is_rejected_with_a_useful_message(self, db):
        aa.create_account("dupe@example.com", "hunter22222", "First")
        with pytest.raises(aa.AccountError, match="already exists"):
            aa.create_account("dupe@example.com", "different-pw", "Second")

    def test_email_is_case_insensitive_for_uniqueness(self, db):
        aa.create_account("case@example.com", "hunter22222", "First")
        with pytest.raises(aa.AccountError, match="already exists"):
            aa.create_account("CASE@EXAMPLE.COM", "hunter22222", "Second")

    def test_rejects_an_invalid_email(self, db):
        with pytest.raises(aa.AccountError, match="valid email"):
            aa.create_account("not-an-email", "hunter22222", "Someone")

    def test_rejects_a_short_password(self, db):
        with pytest.raises(aa.AccountError, match="8 characters"):
            aa.create_account("short@example.com", "1234567", "Someone")

    def test_rejects_a_short_name(self, db):
        with pytest.raises(aa.AccountError, match="2 characters"):
            aa.create_account("shortname@example.com", "hunter22222", "X")


class TestAuthenticate:
    def test_correct_credentials_resolve_to_the_account_id(self, db):
        account_id = aa.create_account("auth@example.com", "hunter22222", "Auth User")
        assert aa.authenticate("auth@example.com", "hunter22222") == account_id

    def test_wrong_password_returns_none(self, db):
        aa.create_account("auth2@example.com", "hunter22222", "Auth User")
        assert aa.authenticate("auth2@example.com", "wrong-password") is None

    def test_unknown_email_returns_none_not_an_error(self, db):
        """Same return value as a wrong password (see module docstring) --
        a distinguishing error would let a login form enumerate emails."""
        assert aa.authenticate("nobody@example.com", "whatever123") is None


class TestSubscriptionAndSuperadmin:
    """The manual v1 billing gate (accounts.subscription_active) and the
    superadmin flag that drives the admin dashboard -- see
    require_active_subscription/require_superadmin in api_server.py for
    where these actually get enforced."""

    def test_a_new_account_starts_with_an_active_subscription(self, db):
        """Signup still grants immediate access -- see Pricing.tsx's "no
        card required to start"; the manual step is deciding who *stays*
        active, not gating the trial itself."""
        account_id = aa.create_account("new@example.com", "hunter22222", "New User")
        account = aa.get_account(account_id)
        assert account["subscription_active"] == 1
        assert account["is_superadmin"] == 0

    def test_set_subscription_active_round_trips(self, db):
        account_id = aa.create_account("toggle@example.com", "hunter22222", "Toggle")
        aa.set_subscription_active(account_id, False)
        assert aa.get_account(account_id)["subscription_active"] == 0
        aa.set_subscription_active(account_id, True)
        assert aa.get_account(account_id)["subscription_active"] == 1

    def test_promote_to_superadmin_round_trips(self, db):
        aa.create_account("owner@example.com", "hunter22222", "Owner")
        aa.promote_to_superadmin("owner@example.com")
        account = aa.get_account(aa.authenticate("owner@example.com", "hunter22222"))
        assert account["is_superadmin"] == 1

    def test_promote_to_superadmin_is_case_insensitive(self, db):
        aa.create_account("mixedcase@example.com", "hunter22222", "Mixed")
        aa.promote_to_superadmin("MixedCase@Example.com")
        account_id = aa.authenticate("mixedcase@example.com", "hunter22222")
        assert aa.get_account(account_id)["is_superadmin"] == 1

    def test_promote_to_superadmin_on_an_unknown_email_raises(self, db):
        with pytest.raises(aa.AccountError, match="sign up first"):
            aa.promote_to_superadmin("nobody@example.com")

    def test_list_accounts_returns_everything_newest_first(self, db):
        first = aa.create_account("first@example.com", "hunter22222", "First")
        second = aa.create_account("second@example.com", "hunter22222", "Second")
        rows = aa.list_accounts()
        ids = [r["id"] for r in rows]
        assert ids.index(second) < ids.index(first)
        assert {r["email"] for r in rows} == {"first@example.com", "second@example.com"}
        assert "subscription_active" in rows[0] and "is_superadmin" in rows[0]


class TestSessions:
    def test_a_new_session_resolves_to_its_account(self, db):
        account_id = aa.create_account("sess@example.com", "hunter22222", "Sess User")
        token = aa.create_session(account_id)
        assert aa.resolve_session(token) == account_id

    def test_an_unknown_token_resolves_to_none(self, db):
        assert aa.resolve_session("not-a-real-token") is None

    def test_an_empty_token_resolves_to_none(self, db):
        assert aa.resolve_session("") is None

    def test_a_deleted_session_no_longer_resolves(self, db):
        account_id = aa.create_account("sess2@example.com", "hunter22222", "Sess User")
        token = aa.create_session(account_id)
        aa.delete_session(token)
        assert aa.resolve_session(token) is None

    def test_an_expired_session_does_not_resolve(self, db):
        account_id = aa.create_account("sess3@example.com", "hunter22222", "Sess User")
        token = aa.create_session(account_id)
        with cpdb.rw() as conn:
            conn.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00Z' "
                        "WHERE token=?", (token,))
        assert aa.resolve_session(token) is None

    def test_two_accounts_get_different_tokens(self, db):
        a1 = aa.create_account("multi1@example.com", "hunter22222", "One")
        a2 = aa.create_account("multi2@example.com", "hunter22222", "Two")
        t1, t2 = aa.create_session(a1), aa.create_session(a2)
        assert t1 != t2
        assert aa.resolve_session(t1) == a1
        assert aa.resolve_session(t2) == a2


class TestBootstrapLegacyAccount:
    def test_creates_account_id_1_on_an_empty_accounts_table(self, db):
        aa.bootstrap_legacy_account()
        account = aa.get_account(1)
        assert account is not None
        assert account["plan"] == "legacy"

    def test_is_idempotent(self, db):
        aa.bootstrap_legacy_account()
        aa.bootstrap_legacy_account()
        with cpdb.ro() as conn:
            n = conn.execute("SELECT COUNT(*) n FROM accounts").fetchone()["n"]
        assert n == 1

    def test_does_nothing_if_accounts_already_exist(self, db):
        """A real signup happening to land before this ever runs (should
        not happen in practice -- lifespan calls it right after migrating
        -- but the guard is what makes 'idempotent' true) must not get a
        phantom second account #1 inserted around it."""
        real_id = aa.create_account("first@example.com", "hunter22222", "Real")
        aa.bootstrap_legacy_account()
        with cpdb.ro() as conn:
            n = conn.execute("SELECT COUNT(*) n FROM accounts").fetchone()["n"]
        assert n == 1
        assert real_id != 1 or aa.get_account(1)["email"] == "first@example.com"

    def test_the_legacy_account_cannot_log_in_with_a_guessable_password(self, db):
        aa.bootstrap_legacy_account()
        for guess in ("", "password", "admin", "legacy", "bitport"):
            assert aa.authenticate("legacy@bitport.local", guess) is None

    def test_settings_account_id_1_matches_plain_settings(self, db, monkeypatch):
        """The whole point: nothing about the live, in-flight migration
        this account represents can be allowed to read a different value
        through Settings(account_id=1) than plain Settings() already gave
        it every time before this feature existed."""
        monkeypatch.setenv("SOURCE_DOMAIN", "c.example.com")
        monkeypatch.setenv("TARGET_DOMAIN", "a.example.com")
        monkeypatch.setenv("SOURCE_ADMIN", "admin@c.example.com")
        aa.bootstrap_legacy_account()

        from config import Settings

        plain = Settings()
        scoped = Settings(account_id=1)
        assert scoped.source_domain == plain.source_domain == "c.example.com"
        assert scoped.target_domain == plain.target_domain == "a.example.com"
        assert scoped.source_admin == plain.source_admin == "admin@c.example.com"
        assert scoped.db_path == plain.db_path
        assert scoped.source_sa_key == plain.source_sa_key
        assert scoped.target_sa_key == plain.target_sa_key


class TestSettingsAccountScoping:
    def test_a_new_accounts_settings_are_isolated_from_the_legacy_ones(self, db, monkeypatch):
        monkeypatch.setenv("SOURCE_DOMAIN", "legacy-source.com")
        monkeypatch.setenv("TARGET_DOMAIN", "legacy-target.com")
        aa.bootstrap_legacy_account()
        account_id = aa.create_account("iso@example.com", "hunter22222", "Iso User")
        aa.update_tenant_config(account_id, "source", domain="iso-source.com",
                                admin_email="admin@iso-source.com")

        from config import Settings

        legacy = Settings(account_id=1)
        new = Settings(account_id=account_id)
        assert legacy.source_domain == "legacy-source.com"
        assert new.source_domain == "iso-source.com"
        assert new.db_path != legacy.db_path
        assert new.source_sa_key != legacy.source_sa_key

    def test_a_fresh_account_with_no_setup_yet_never_sees_the_legacy_domain(self, db, monkeypatch):
        """Regression: before this, a brand-new account's unconfigured
        source_domain fell through to the env-derived default -- which on
        a real deployment is the LIVE production tenant's domain, not
        'unset'. A new customer's error messages must say 'not configured
        yet', never name another account's tenant."""
        monkeypatch.setenv("SOURCE_DOMAIN", "production-legacy-tenant.com")
        monkeypatch.setenv("TARGET_DOMAIN", "production-legacy-target.com")
        aa.bootstrap_legacy_account()
        account_id = aa.create_account("fresh@example.com", "hunter22222", "Fresh User")

        from config import Settings

        fresh = Settings(account_id=account_id)
        assert fresh.source_domain == ""
        assert fresh.target_domain == ""
        assert fresh.source_admin == ""
        assert fresh.target_admin == ""
        assert "production-legacy-tenant.com" not in (fresh.source_domain, fresh.target_domain)

    def test_an_unknown_account_id_raises_rather_than_silently_falling_back(self, db):
        from config import Settings

        with pytest.raises(ValueError, match="no tenant_configs rows"):
            Settings(account_id=999999)

    def test_update_tenant_config_only_overwrites_given_columns(self, db):
        account_id = aa.create_account("merge@example.com", "hunter22222", "Merge User")
        aa.update_tenant_config(account_id, "source", domain="first.com")
        aa.update_tenant_config(account_id, "source", admin_email="admin@first.com")

        with cpdb.ro() as conn:
            row = conn.execute(
                "SELECT domain, admin_email FROM tenant_configs "
                "WHERE account_id=? AND side='source'", (account_id,)).fetchone()
        assert row["domain"] == "first.com"
        assert row["admin_email"] == "admin@first.com"

    def test_get_tenant_config_round_trips_what_update_wrote(self, db):
        account_id = aa.create_account("read@example.com", "hunter22222", "Read User")
        aa.update_tenant_config(account_id, "target", domain="t.example.com",
                                admin_email="admin@t.example.com")
        cfg = aa.get_tenant_config(account_id, "target")
        assert cfg["domain"] == "t.example.com"
        assert cfg["admin_email"] == "admin@t.example.com"
        # sa_key_path/db_path were set at account creation, not by the
        # update above -- still present, not clobbered to NULL.
        assert cfg["sa_key_path"] == os.path.join("keys", str(account_id), "target-sa.json")
        assert cfg["db_path"] == os.path.join("data", "accounts", str(account_id), "migration.db")

    def test_get_tenant_config_for_an_unknown_account_returns_none(self, db):
        assert aa.get_tenant_config(999999, "source") is None

    def test_get_tenant_config_rejects_a_bad_side(self, db):
        account_id = aa.create_account("badside@example.com", "hunter22222", "XX")
        with pytest.raises(ValueError, match="side must be"):
            aa.get_tenant_config(account_id, "sideways")
