"""
tests/test_no_route_is_ungated.py
=================================
Every /api/ route on **both** servers is behind a credential.

This exists because of a bug shape, not a bug. Bitport serves its API from
two processes -- api_server.py (FastAPI, 64 routes) and webui.py (stdlib,
47) -- and every cross-cutting concern has to be implemented twice with
nothing forcing the second one to happen. Authorisation was implemented
once. api_server grew require_reader, whose docstring names the exact
symptom it fixed:

    with no cookie and no header, /api/v2/failures returned user email
    addresses, Drive file ids and error text

webui.py, serving the same class of data, got nothing. Live, that meant
an unauthenticated GET of /api/spa/users returned another account's user
list, /api/config returned the source admin, and POST /api/run would start
an action -- including the destructive ones -- for anyone who asked.

It was found by someone reading curl output, which is not a mechanism. So:
a new route that forgets its credential now fails here instead of waiting
to be noticed. Both servers, one test, because covering only the server
that happened to break is how this happened in the first place.
"""

from __future__ import annotations

import inspect
import os
import re

import api_server
import webui

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The dependency names that constitute a credential check on the FastAPI side.
AUTH_MARKERS = (
    "require_reader", "require_login", "require_admin", "require_superadmin",
    "require_active_subscription", "node_auth", "operator",
)

# Routes that MUST stay reachable without one, with the reason. A route may
# only be added here deliberately -- that is the point of the list.
FASTAPI_PUBLIC = {
    "/api/v2/auth/signup",   # creating the account that will hold the session
    "/api/v2/auth/logout",   # clearing a cookie must work with a stale one
    "/api/v2/auth/login",    # the endpoint that issues the session
}


def _fastapi_routes() -> list[tuple[str, str, bool]]:
    out = []
    for r in api_server.app.routes:
        path = getattr(r, "path", None)
        ep = getattr(r, "endpoint", None)
        if not path or not path.startswith("/api/") or ep is None:
            continue
        try:
            src = inspect.getsource(ep)
        except (OSError, TypeError):
            src = ""
        sig = str(inspect.signature(ep)) if callable(ep) else ""
        guarded = any(m in src or m in sig for m in AUTH_MARKERS)
        methods = ",".join(sorted(set(getattr(r, "methods", set())) - {"HEAD", "OPTIONS"}))
        out.append((methods, path, guarded))
    return out


def _webui_api_paths() -> set[str]:
    """Every /api/ path literal webui.py routes on."""
    src = open(os.path.join(ROOT, "webui.py"), encoding="utf-8").read()
    return set(re.findall(r'path == "(/api/[^"]*)"', src)) | \
           set(re.findall(r'self\.path == "(/api/[^"]*)"', src))


class TestTheFastApiServer:
    def test_it_actually_has_routes_to_check(self):
        """A collection bug here would make every assertion below vacuous."""
        assert len(_fastapi_routes()) > 40

    def test_every_route_is_guarded_or_deliberately_public(self):
        stray = [f"{m} {p}" for m, p, guarded in _fastapi_routes()
                 if not guarded and p not in FASTAPI_PUBLIC]
        assert not stray, (
            "these routes reference no auth dependency and are not in "
            f"FASTAPI_PUBLIC: {stray}")

    def test_the_public_list_has_not_quietly_grown(self):
        """Every entry costs a deliberate decision. Three is signup, logout
        and login -- nothing else needs to work signed-out."""
        assert len(FASTAPI_PUBLIC) == 3


class TestTheStdlibServer:
    def test_it_actually_has_routes_to_check(self):
        assert len(_webui_api_paths()) > 30

    def test_no_api_route_is_in_the_public_set(self):
        """webui gates by exclusion: anything not in _PUBLIC_PATHS needs a
        credential. So the check is that no data route ever lands in there."""
        public = webui.Handler._PUBLIC_PATHS
        leaked = sorted(p for p in _webui_api_paths() if p in public)
        assert not leaked, f"these /api/ routes are public: {leaked}"

    def test_the_public_set_contains_only_shells_and_the_oauth_redirect(self):
        assert webui.Handler._PUBLIC_PATHS == frozenset({
            "/", "/app", "/app/", "/console", "/console/", "/oauth/callback"})

    def test_both_verbs_are_gated(self):
        """A gate on do_GET alone would leave /api/run -- which starts
        migrations and tenant wipes -- open to anyone."""
        src = open(os.path.join(ROOT, "webui.py"), encoding="utf-8").read()
        for verb in ("def do_GET", "def do_POST"):
            body = src.split(verb, 1)[1][:900]
            assert "_authorised()" in body, f"{verb} does not check a credential"

    def test_the_gate_runs_before_any_handler_work(self):
        """Ordering matters: a check placed after the body is read, or after
        a route dispatches, is not a gate."""
        src = open(os.path.join(ROOT, "webui.py"), encoding="utf-8").read()
        post = src.split("def do_POST", 1)[1][:900]
        assert post.index("_authorised()") < post.index("Content-Length"), \
            "do_POST reads the request body before checking the credential"


class TestBothServersAgree:
    def test_neither_server_trusts_a_bare_operator_header(self):
        """The X-Operator name is a claim, not a secret. Honouring it without
        the shared token was the hole api_server documented; webui must not
        reintroduce it."""
        wsrc = inspect.getsource(webui.Handler._authorised)
        assert "BITPORT_OPERATOR_TOKEN" in wsrc and "compare_digest" in wsrc
        asrc = open(os.path.join(ROOT, "api_server.py"), encoding="utf-8").read()
        assert "BITPORT_OPERATOR_TOKEN" in asrc and "compare_digest" in asrc
