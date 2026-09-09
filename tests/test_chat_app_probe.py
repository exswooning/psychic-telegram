"""
tests/test_chat_app_probe.py
============================
Chat needs an app configured in the Cloud console -- a form with no API
behind it. Until now the tool could only say "this API has a console step",
which is true of every project forever: identical before and after somebody
does the step, unable to confirm one was done, and therefore a warning
people learn to skim. It sat over a real 200-user seed that produced 193
chat 404s and zero chat data.

One list call answers it properly. What matters is that all three outcomes
stay distinct -- works, definitely does not, and could not tell -- because
reporting the third as the second sends someone to fix a console page that
is already correct.
"""

from __future__ import annotations

import pytest

import ensure_apis


class Boom:
    """A client whose call raises whatever Google would have raised."""

    def __init__(self, message):
        self.message = message

    def spaces(self):
        return self

    def create(self, **kw):
        return self

    def delete(self, **kw):
        return self

    def list(self, **kw):
        # An unconfigured project answers list with 200. That is exactly why
        # listing was the wrong thing to probe.
        return _Ok({"spaces": []})

    def execute(self):
        raise RuntimeError(self.message)


class _Ok:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class Fine:
    """A project whose Chat app exists: create succeeds, delete succeeds."""

    def __init__(self):
        self.created = 0
        self.deleted = 0

    def spaces(self):
        return self

    def create(self, **kw):
        self.created += 1
        self._resp = {"name": "spaces/probe1"}
        return self

    def delete(self, **kw):
        self.deleted += 1
        self._resp = {}
        return self

    def list(self, **kw):
        # Present precisely so a probe that calls THIS instead of create
        # would pass -- which is the bug these tests exist to prevent.
        self._resp = {"spaces": []}
        return self

    def execute(self):
        return self._resp


@pytest.fixture
def settings(tmp_path):
    key = tmp_path / "sa.json"
    key.write_text('{"project_id": "p", "client_email": "sa@p.iam"}')
    return type("S", (), {"source_sa_key": str(key), "target_sa_key": str(key),
                          "source_admin": "admin@acme.com",
                          "target_admin": "admin@acme.com"})()


class TestTheThreeAnswersStayDistinct:
    def test_a_working_tenant_says_so(self, settings, monkeypatch):
        monkeypatch.setattr(ensure_apis, "_chat_client", lambda *a: Fine(),
                            raising=False)
        ok, why = ensure_apis.chat_app_configured(settings, "source")
        assert ok is True and "configured" in why

    def test_an_unconfigured_project_is_identified_by_googles_own_words(
            self, settings, monkeypatch):
        monkeypatch.setattr(
            ensure_apis, "_chat_client",
            lambda *a: Boom('404 "Google Chat app not found. To create a Chat '
                            'app, you must turn on the Chat API..."'),
            raising=False)
        ok, why = ensure_apis.chat_app_configured(settings, "source")
        assert ok is False
        assert "404" in why or "chat call" in why

    def test_anything_else_is_could_not_tell_not_broken(self, settings, monkeypatch):
        """A missing scope or an unreachable network says nothing about the
        console page. Calling that "broken" sends someone to fix something
        that is already correct."""
        monkeypatch.setattr(ensure_apis, "_chat_client",
                            lambda *a: Boom("403 insufficient authentication scopes"),
                            raising=False)
        ok, why = ensure_apis.chat_app_configured(settings, "source")
        assert ok is None and "could not tell" in why

    def test_no_admin_on_file_is_also_could_not_tell(self, monkeypatch):
        s = type("S", (), {"source_sa_key": "k", "source_admin": ""})()
        ok, why = ensure_apis.chat_app_configured(s, "source")
        assert ok is None and "admin" in why


class TestItUsesAScopeThatIsActuallyGranted:
    def test_the_probe_scope_is_in_the_delegation(self):
        """chat.spaces.readonly would be the narrower, more obviously right
        choice -- and it is not granted, so the probe would answer 403 on
        every correctly configured tenant and never say anything useful."""
        import config

        assert ensure_apis.CHAT_PROBE_SCOPE in config.CHAT_SCOPES


class TestTheWarningStopsWhenItIsFixed:
    def test_the_caveat_is_probed_not_asserted(self):
        """The old note fired for every tenant forever. A warning that
        cannot go away is one nobody reads."""
        import inspect

        import api_server

        src = inspect.getsource(api_server)
        assert "chat_app_configured(s, tenant)" in src
        assert "if usable is True:" in src

    def test_setup_asks_chat_rather_than_the_browser_step(self):
        import inspect

        import full_setup

        src = inspect.getsource(full_setup)
        assert "chat_app_configured" in src


class TestItProbesTheOperationThatActuallyFails:
    """The first version listed spaces. An unconfigured project answers
    list with 200, so it reported "configured" -- and a 200-user chat
    backfill launched on the strength of that produced 200 more
    "Google Chat app not found" errors.

    spaces.list returns the spaces a USER already belongs to and needs no
    Chat app. spaces.create acts AS one, and is what the seeder and the
    migrator actually call. Only the operation that fails in production is
    evidence about production.
    """

    def test_it_creates_a_space(self, settings, monkeypatch):
        client = Fine()
        monkeypatch.setattr(ensure_apis, "_chat_client", lambda *a: client,
                            raising=False)
        ensure_apis.chat_app_configured(settings, "source")
        assert client.created == 1, "probed without ever creating a space"

    def test_it_cleans_up_after_itself(self, settings, monkeypatch):
        """A probe that leaves objects in a customer tenant is one nobody
        leaves switched on."""
        client = Fine()
        monkeypatch.setattr(ensure_apis, "_chat_client", lambda *a: client,
                            raising=False)
        ensure_apis.chat_app_configured(settings, "source")
        assert client.deleted == 1

    def test_listing_alone_would_not_satisfy_it(self):
        import inspect

        src = inspect.getsource(ensure_apis.chat_app_configured)
        assert "spaces().create(" in src
        assert "spaces().list(" not in src

    def test_it_asks_for_the_delete_scope_too(self):
        """chat.spaces covers create but NOT delete -- config.py grants
        chat.delete separately. Without it the probe works and litters."""
        assert any("chat.delete" in s for s in ensure_apis.CHAT_PROBE_SCOPES)

    def test_a_space_it_cannot_remove_is_reported_not_hidden(self, settings,
                                                             monkeypatch):
        class Litter(Fine):
            def delete(self, **kw):
                raise RuntimeError("403 insufficient scope")

        monkeypatch.setattr(ensure_apis, "_chat_client", lambda *a: Litter(),
                            raising=False)
        ok, why = ensure_apis.chat_app_configured(settings, "source")
        assert ok is True
        assert "could not be removed" in why and "by hand" in why
