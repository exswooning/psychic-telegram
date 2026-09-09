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

    def list(self, **kw):
        return self

    def execute(self):
        raise RuntimeError(self.message)


class Fine:
    def spaces(self):
        return self

    def list(self, **kw):
        return self

    def execute(self):
        return {"spaces": []}


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
