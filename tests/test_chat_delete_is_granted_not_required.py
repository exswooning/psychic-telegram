"""A toggle whose scope never reached a grant could only ever fail.

CHAT_ALLOW_DELETE was added so reset_chat could delete the spaces it has
never managed to delete. But the flag was added in isolation:

  - source_scopes() had no branch for it, and the seeder and reset_target
    both act on the SOURCE tenant, so the scope could not reach a source
    grant at all;
  - every_toggle_scopes() did not vary it, so grant_scopes() -- the thing
    that writes the Admin Console line -- never contained it either.

So the only reachable outcome of turning the flag on was the
unauthorized_client that every_toggle_scopes exists specifically to
prevent. Its own docstring names this exact failure for migrate_chat:
a toggle off by default, its scope missing from the granted line, and the
whole token request failing the moment somebody switched it on.

The asymmetry that makes it safe is OPTIONAL_SCOPES' asymmetry:

    granting a scope nobody requests   costs nothing (grants are monotonic)
    requesting a scope nobody granted  fails the ENTIRE token exchange

so chat.delete belongs in grant_scopes and must stay out of
required_scopes.
"""
import os

import pytest

import verify_scopes
from config import CHAT_DELETE_SCOPE, Settings


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("SOURCE_DOMAIN", "src.example")
    monkeypatch.setenv("TARGET_DOMAIN", "tgt.example")
    st = Settings()
    st.migrate_chat = True
    return st


class TestItIsWrittenIntoTheGrant:
    @pytest.mark.parametrize("side", ["source", "target"])
    def test_grant_scopes_carries_it(self, settings, side):
        assert CHAT_DELETE_SCOPE in verify_scopes.grant_scopes(settings, side)

    @pytest.mark.parametrize("side", ["source", "target"])
    def test_even_when_the_flag_is_off(self, settings, side):
        """The point of a grant covering every toggle: turning the flag on
        later must not need a second visit to the Admin Console."""
        settings.chat_allow_delete = False
        assert CHAT_DELETE_SCOPE in verify_scopes.grant_scopes(settings, side)

    def test_every_toggle_scopes_varies_the_flag(self, settings):
        assert CHAT_DELETE_SCOPE in \
            verify_scopes.every_toggle_scopes(settings, "source")


class TestItIsNeverRequired:
    @pytest.mark.parametrize("side", ["source", "target"])
    def test_the_default_configuration_does_not_require_it(self, settings, side):
        """The property that protects existing deployments.

        required_scopes answers "what does THIS configuration request", so
        with the flag ON the scope genuinely is required -- and that is
        fine, because grant_scopes always writes it. What must never happen
        is the DEFAULT requiring it: that would break every migration on
        every tenant that had not re-pasted its grant, and scope_guard
        would then refuse to start them.
        """
        settings.chat_allow_delete = False
        assert CHAT_DELETE_SCOPE not in \
            verify_scopes.required_scopes(settings, side)

    @pytest.mark.parametrize("side", ["source", "target"])
    def test_turning_it_on_is_covered_by_the_grant(self, settings, side):
        # Requested only where it is also granted -- which is the whole
        # reason every_toggle_scopes has to know about the flag.
        settings.chat_allow_delete = True
        req = set(verify_scopes.required_scopes(settings, side))
        assert CHAT_DELETE_SCOPE in req
        settings.chat_allow_delete = False
        assert req <= set(verify_scopes.grant_scopes(settings, side)), (
            "the flag can request a scope the console line never carries")

    def test_the_grant_is_a_superset_of_the_requirement(self, settings):
        for side in ("source", "target"):
            req = set(verify_scopes.required_scopes(settings, side))
            grant = set(verify_scopes.grant_scopes(settings, side))
            assert req <= grant, sorted(req - grant)


class TestTheSourceSideCanAskForIt:
    """The seeder and reset_target both write to the SOURCE tenant."""

    def test_source_scopes_honours_the_flag(self, settings):
        from config import source_scopes
        settings.chat_allow_delete = True
        assert CHAT_DELETE_SCOPE in source_scopes(settings)

    def test_and_omits_it_when_off(self, settings):
        from config import source_scopes
        settings.chat_allow_delete = False
        assert CHAT_DELETE_SCOPE not in source_scopes(settings)

    def test_it_needs_chat_on_at_all(self, settings):
        from config import source_scopes
        settings.migrate_chat = False
        settings.chat_allow_delete = True
        assert CHAT_DELETE_SCOPE not in source_scopes(settings)
