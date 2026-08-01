"""
tests/test_oauth.py
===================
The OAuth path, which exists so a tenant admin can grant access by clicking
"Allow" instead of building a GCP project.

Two things here are worth more than the rest of the file:

* the refusal to act as anyone but the consenting admin. An OAuth grant is
  *not* delegation -- it acts as the person who consented. If that check ever
  regresses, the migration does not fail, it quietly reads the admin's own
  mailbox while the audit log claims it migrated someone else's.
* the refresh token. It only comes back with `access_type=offline` and
  `prompt=consent` together. Missing it is invisible for an hour and then
  breaks the run mid-flight, so the failure is forced to the front.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

import oauth_store
from auth import AuthManager
from config import Settings


@pytest.fixture
def store(tmp_path):
    return oauth_store.TokenStore(str(tmp_path / "oauth"))


def _token(account="admin@src.com", domain="src.com", refresh="rt-1"):
    return {
        "token": "at-1",
        "refresh_token": refresh,
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid",
        "client_secret": "csec",
        "scopes": ["https://www.googleapis.com/auth/drive"],
        "account": account,
        "domain": domain,
        "obtained": "2026-07-30T00:00:00+00:00",
    }


class TestTokenStore:
    def test_absent_tenant_reads_as_unconnected(self, store):
        assert store.exists("source") is False
        assert store.load("source") is None
        assert store.describe("source") == {"connected": False}

    def test_roundtrip(self, store):
        store.save("source", _token())
        assert store.exists("source")
        assert store.load("source")["refresh_token"] == "rt-1"

    def test_tenants_are_independent(self, store):
        """Reconnecting one side must not disturb the other -- a shared file
        would silently point both tenants at the last domain consented."""
        store.save("source", _token("a@src.com", "src.com"))
        store.save("target", _token("b@tgt.com", "tgt.com"))

        store.clear("source")

        assert store.exists("source") is False
        assert store.load("target")["account"] == "b@tgt.com"

    def test_token_file_is_not_group_or_world_readable(self, store):
        store.save("source", _token())
        mode = os.stat(store._path("source")).st_mode
        assert not mode & (stat.S_IRWXG | stat.S_IRWXO)

    def test_describe_omits_the_secret_material(self, store):
        """describe() feeds the web UI's status panel, which is rendered into a
        page -- it must not carry the refresh token along with it."""
        store.save("source", _token())
        blob = json.dumps(store.describe("source"))
        assert "admin@src.com" in blob
        assert "rt-1" not in blob and "csec" not in blob

    def test_clear_is_idempotent(self, store):
        store.clear("source")  # never connected; must not raise


class TestAuthorizationURL:
    def test_requests_offline_access_and_forces_the_consent_screen(self):
        seen = {}

        class FakeFlow:
            def authorization_url(self, **kw):
                seen.update(kw)
                return "https://accounts.google.com/o/oauth2/auth?x=1", "state"

        oauth_store.authorization_url(FakeFlow())

        # Both, or Google returns an access token with no refresh token and the
        # run dies an hour in.
        assert seen["access_type"] == "offline"
        assert seen["prompt"] == "consent"

    def test_login_hint_is_passed_through_when_given(self):
        seen = {}

        class FakeFlow:
            def authorization_url(self, **kw):
                seen.update(kw)
                return "u", "s"

        oauth_store.authorization_url(FakeFlow(), login_hint="info@src.com")
        assert seen["login_hint"] == "info@src.com"

    def test_no_login_hint_key_when_empty(self):
        seen = {}

        class FakeFlow:
            def authorization_url(self, **kw):
                seen.update(kw)
                return "u", "s"

        oauth_store.authorization_url(FakeFlow(), login_hint="")
        assert "login_hint" not in seen


class TestCredentialsSerialisation:
    def test_roundtrip_preserves_what_refresh_needs(self):
        creds = oauth_store.credentials_from_dict(_token())
        again = oauth_store.credentials_to_dict(creds, "admin@src.com", "src.com")

        # Losing any one of these turns a long-lived grant into a 60-minute one.
        for field in ("refresh_token", "token_uri", "client_id", "client_secret"):
            assert again[field] == _token()[field]
        assert again["account"] == "admin@src.com"

    def test_obtained_is_stamped(self):
        creds = oauth_store.credentials_from_dict(_token())
        assert oauth_store.credentials_to_dict(creds)["obtained"]


class TestAuthManagerOAuthMode:
    def _settings(self, tmp_path) -> Settings:
        s = Settings()
        s.auth_mode = "oauth"
        s.oauth_token_dir = str(tmp_path / "oauth")
        return s

    def test_unconnected_tenant_names_the_fix(self, tmp_path):
        auth = AuthManager(self._settings(tmp_path))
        with pytest.raises(RuntimeError, match="not connected"):
            auth._oauth_credentials("source", "alice@src.com")

    def test_acting_as_the_consenting_admin_is_allowed(self, tmp_path):
        settings = self._settings(tmp_path)
        auth = AuthManager(settings)
        auth._token_store.save("source", _token("info@src.com", "src.com"))

        creds = auth._oauth_credentials("source", "info@src.com")
        assert creds.refresh_token == "rt-1"

    def test_case_differences_in_the_address_are_not_treated_as_a_mismatch(self, tmp_path):
        auth = AuthManager(self._settings(tmp_path))
        auth._token_store.save("source", _token("Info@Src.com", "src.com"))

        assert auth._oauth_credentials("source", "info@src.com") is not None

    def test_acting_as_another_user_is_refused(self, tmp_path):
        """The whole point. OAuth acts as the consenting admin; if this ever
        stops raising, every user's migration reads the admin's own data."""
        auth = AuthManager(self._settings(tmp_path))
        auth._token_store.save("source", _token("info@src.com", "src.com"))

        with pytest.raises(RuntimeError) as exc:
            auth._oauth_credentials("source", "alice@src.com")

        msg = str(exc.value)
        assert "alice@src.com" in msg and "info@src.com" in msg
        # and it should say what would actually make it work
        assert "delegation" in msg.lower() or "marketplace" in msg.lower()

    def test_each_tenant_uses_its_own_grant(self, tmp_path):
        auth = AuthManager(self._settings(tmp_path))
        auth._token_store.save("source", _token("info@src.com", "src.com", "rt-src"))
        auth._token_store.save("target", _token("info@tgt.com", "tgt.com", "rt-tgt"))

        assert auth._oauth_credentials("source", "info@src.com").refresh_token == "rt-src"
        assert auth._oauth_credentials("target", "info@tgt.com").refresh_token == "rt-tgt"

    def test_connecting_target_does_not_satisfy_source(self, tmp_path):
        auth = AuthManager(self._settings(tmp_path))
        auth._token_store.save("target", _token("info@tgt.com", "tgt.com"))

        with pytest.raises(RuntimeError, match="not connected"):
            auth._oauth_credentials("source", "info@src.com")

    def test_key_mode_is_unaffected_by_the_oauth_plumbing(self, tmp_path):
        """AuthManager builds a TokenStore unconditionally; that must not make
        the default service-account path depend on OAuth being set up."""
        s = Settings()
        s.auth_mode = "key"
        s.oauth_token_dir = str(tmp_path / "nonexistent")
        auth = AuthManager(s)
        assert auth.settings.auth_mode == "key"


class TestWizardPayload:
    """
    The wizard UI is driven entirely by these dicts, and the failure mode is
    silent: STEP_ACTIONS names actions by string, and an unknown name is
    filtered out rather than raising -- so a typo removes a button from the
    page and nothing anywhere says so.
    """

    def test_every_step_action_exists(self):
        import webui

        unknown = {n: [a for a in keys if a not in webui.ACTIONS]
                   for n, keys in webui.STEP_ACTIONS.items()}
        assert not any(unknown.values()), f"unknown action keys: {unknown}"

    def test_seeding_is_never_a_one_click_action(self):
        """The seeder is reachable from the UI, but never through ACTIONS.

        ACTIONS entries are fixed argv with no per-request input, which is what
        makes them safe to fire from a button. The seeder writes fabricated
        data into a live tenant and has to be aimed by hand, so it goes through
        /api/seed, which refuses to build a command until the source domain is
        typed back. If it ever appears here, that confirmation is gone.
        """
        import webui

        assert 7 not in webui.STEP_ACTIONS
        for name, spec in webui.ACTIONS.items():
            assert "seed_sandbox" not in " ".join(spec["argv"]), name

    def test_destructive_actions_all_carry_a_confirmation_phrase(self):
        import webui

        for name, spec in webui.ACTIONS.items():
            if spec.get("destructive"):
                assert spec.get("confirm"), f"{name} is destructive with no phrase"

    def test_actions_never_build_a_shell_string(self):
        import webui

        for name, spec in webui.ACTIONS.items():
            assert isinstance(spec["argv"], list), name
            assert all(isinstance(a, str) for a in spec["argv"]), name
