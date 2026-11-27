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
# The positive half exercises the real request dispatcher without a socket.
# Loopback binds are disabled in the test sandbox; constructing the handler
# directly still covers routing, cookie parsing, and the auth gate.
# ----------------------------------------------------------------------
import pytest

import accounts_auth


@pytest.fixture
def auth_db(tmp_path, monkeypatch):
    # Isolate the session database so no test writes an operator's real rows.
    import control_plane_db as cpdb

    monkeypatch.setenv("MIGRATION_DB", str(tmp_path / "cp.db"))
    cpdb.apply_migrations(str(tmp_path / "cp.db"))
    return tmp_path


def _get(path, cookie=None):
    h = object.__new__(webui.Handler)
    h.path = path
    h.headers = {"Cookie": cookie} if cookie else {}
    response = {}
    h._json = lambda _obj, code=200: response.update(status=code)
    h._send = lambda code, _body, _ctype, **_kwargs: response.update(status=code)
    h.send_response = lambda code: response.update(status=code)
    h.send_header = lambda *_args: None
    h.end_headers = lambda: None
    webui.Handler._do_GET(h)
    return response["status"]


class TestTheGateOnTheRealServer:
    def test_anonymous_is_refused(self, auth_db):
        assert _get("/api/actions") == 401

    def test_a_page_shell_still_serves_anonymously(self, auth_db, tmp_path, monkeypatch):
        """If this 401s, the login page itself is unreachable and nobody can
        ever sign in to satisfy the gate."""
        monkeypatch.setattr(webui, "HERE", str(tmp_path))
        assert _get("/console") in (200, 404)

    def test_a_real_session_cookie_gets_through(self, auth_db):
        """A valid cookie must pass the actual router and reach its handler."""
        aid = accounts_auth.create_account("gate@test.local", "pw-CorrectHorse-9", "Gate")
        token = accounts_auth.create_session(aid)
        assert _get("/api/actions", f"bp_session={token}") == 200

    def test_a_garbage_cookie_is_refused(self, auth_db):
        assert _get("/api/actions", "bp_session=not-a-real-token") == 401


def test_hashed_assets_are_cached_and_nothing_else_is(tmp_path, monkeypatch):
    """A content-hashed file can never go stale, so the 1.6 MB bundle need not be fetched
    on every page load; a missing one (a 404) and every other response stay no-store."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc123.js").write_text("x")
    monkeypatch.setattr(webui, "SPA_DIST_DIR", str(tmp_path))
    sent = []
    h = type("H", (), {"_ASSET_CTYPES": webui.Handler._ASSET_CTYPES,
                       "_send": lambda self, *a, **k: sent.append(k.get("cache", "no-store"))})()
    webui.Handler._serve_spa_asset(h, "/app/assets/index-abc123.js")
    webui.Handler._serve_spa_asset(h, "/app/assets/gone-123.js")
    assert sent == ["public, max-age=31536000, immutable", "no-store"]
