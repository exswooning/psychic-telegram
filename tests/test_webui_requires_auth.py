"""
tests/test_webui_requires_auth.py
=================================
Every data route on the stdlib server needs a credential.

api_server.require_reader closed this hole on port 8090 -- with no cookie
and no header it had been returning user email addresses and Drive file ids
to anyone -- and the same fix never reached webui.py on 8080, which serves
the same class of data through /api/spa/*, /api/config and /api/status.

Confirmed against the deployed host before this was written:

    GET /api/spa/users   200  {"users":[{"email":"1@c.anupam-poudel.com.np"...
    GET /api/config      200  {"config":{"source_admin": ...
    GET /api/actions     200  the full action catalogue

and do_POST checked nothing at all, so /api/run would start an action --
including the destructive ones -- for an unauthenticated caller.

The root cause was reading _account_id() == None as "the legacy
single-operator path" and handing back the box's own tenant. Safe when one
operator ran one box; on a public host with client accounts it makes every
visitor that operator.
"""

from __future__ import annotations

import webui


class FakeHandler:
    """webui's handler without the socket. Only the three things
    _authorised touches."""

    _PUBLIC_PATHS = webui.Handler._PUBLIC_PATHS
    _is_public = webui.Handler._is_public
    _authorised = webui.Handler._authorised

    def __init__(self, cookie: str = "", operator: str = "", token: str = ""):
        h = {}
        if cookie:
            h["Cookie"] = cookie
        if operator:
            h["X-Operator"] = operator
        if token:
            h["X-Operator-Token"] = token
        self.headers = h
        self._aid = None

    def _account_id(self):
        return self._aid


class TestAnUnauthenticatedCallerIsRefused:
    def test_no_credential_at_all_is_not_authorised(self):
        assert FakeHandler()._authorised() is False

    def test_a_resolved_session_is_authorised(self):
        h = FakeHandler(cookie="bp_session=x")
        h._aid = 66
        assert h._authorised() is True

    def test_an_operator_name_alone_grants_nothing(self, monkeypatch):
        """The header is a claim, not a credential -- a name in CP_OPERATORS
        is not a secret. This is the hole api_server documented and closed;
        it must not reopen here."""
        monkeypatch.delenv("BITPORT_OPERATOR_TOKEN", raising=False)
        assert FakeHandler(operator="aryan")._authorised() is False

    def test_an_operator_with_the_right_token_is_authorised(self, monkeypatch):
        monkeypatch.setenv("BITPORT_OPERATOR_TOKEN", "s3cret")
        assert FakeHandler(operator="aryan", token="s3cret")._authorised() is True

    def test_a_wrong_token_is_refused(self, monkeypatch):
        monkeypatch.setenv("BITPORT_OPERATOR_TOKEN", "s3cret")
        assert FakeHandler(operator="aryan", token="nope")._authorised() is False

    def test_an_unset_server_token_fails_closed(self, monkeypatch):
        """A host that never configures the token must not be talked into
        trusting the header by a caller who sends an empty one."""
        monkeypatch.setenv("BITPORT_OPERATOR_TOKEN", "")
        assert FakeHandler(operator="aryan", token="")._authorised() is False


class TestOnlyThePageShellsArePublic:
    def test_the_shells_and_the_oauth_redirect_stay_public(self):
        h = FakeHandler()
        for p in ("/", "/app", "/app/", "/app/assets/index.js",
                  "/console", "/oauth/callback"):
            assert h._is_public(p), f"{p} must stay reachable signed-out"

    def test_every_data_route_is_gated(self):
        """The routes that actually leaked. Named individually so adding a
        new /api route without auth shows up as a failure here."""
        h = FakeHandler()
        for p in ("/api/spa/users", "/api/spa/metrics", "/api/spa/report",
                  "/api/spa/shared_drives", "/api/spa/activity",
                  "/api/config", "/api/status", "/api/actions", "/api/logs",
                  "/api/identities", "/api/dwd", "/api/scope",
                  "/api/snapshot", "/api/job", "/api/deploy_history"):
            assert not h._is_public(p), f"{p} must require a credential"


# ----------------------------------------------------------------------
# The positive half, against the real server.
#
# Refusing anonymous callers is only half the change; the other half is that
# a signed-in one still gets through. Testing _authorised() alone would not
# catch a mistake in routing, cookie parsing, or the order the gate runs in
# -- and that mistake locks every customer out of the product. So this
# starts the actual HTTPServer and speaks HTTP to it.
# ----------------------------------------------------------------------
import http.client
import threading
from http.server import HTTPServer

import pytest

import accounts_auth


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    # control_plane_db._db_path() is Settings().db_path, so the only way to
    # isolate this is the environment -- an attribute patch on accounts_auth
    # silently does nothing (there is no such attribute) and the test then
    # writes a real account into the developer's own control-plane database.
    # It did exactly that before this line existed.
    import control_plane_db as cpdb

    monkeypatch.setenv("MIGRATION_DB", str(tmp_path / "cp.db"))
    cpdb.apply_migrations(str(tmp_path / "cp.db"))
    srv = HTTPServer(("127.0.0.1", 0), webui.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


def _get(srv, path, cookie=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    c.request("GET", path, headers={"Cookie": cookie} if cookie else {})
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


class TestTheGateOnTheRealServer:
    def test_anonymous_is_refused(self, live_server):
        assert _get(live_server, "/api/actions") == 401

    def test_a_page_shell_still_serves_anonymously(self, live_server):
        """If this 401s, the login page itself is unreachable and nobody can
        ever sign in to satisfy the gate."""
        assert _get(live_server, "/console") in (200, 404)

    def test_a_real_session_cookie_gets_through(self, live_server):
        """The half that matters most: a mistake here locks out every
        customer, and no amount of testing the predicate alone would show
        it -- this goes through routing and cookie parsing as deployed."""
        aid = accounts_auth.create_account("gate@test.local", "pw-CorrectHorse-9", "Gate")
        token = accounts_auth.create_session(aid)
        assert _get(live_server, "/api/actions", f"bp_session={token}") == 200

    def test_a_garbage_cookie_is_refused(self, live_server):
        assert _get(live_server, "/api/actions", "bp_session=not-a-real-token") == 401
