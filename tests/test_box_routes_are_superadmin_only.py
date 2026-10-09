"""
The stdlib server's routes that act on the box itself answer only a superadmin.

_authorised() asks only that the caller is signed in, and webui checked no role anywhere
but host restart: any client account could POST /api/deploy with include_credentials --
every tenant's delegated service-account keys, rsynced to a host the caller names -- or
read another account's job logs with GET /api/logs?account=N.
"""
from __future__ import annotations

import io

import pytest

import webui


def _handler(verb: str, path: str, superadmin: bool, sent: list):
    h = object.__new__(webui.Handler)
    h.path = path
    h.headers = {"Content-Length": "2"}
    h.rfile = io.BytesIO(b"{}")
    h._authorised = lambda: True
    h._caller = lambda: (1 if superadmin else 7, superadmin)
    h._json = lambda obj, code=200: sent.append(code)
    return h


@pytest.mark.parametrize("entry", sorted(webui.Handler._SUPERADMIN_ONLY))
def test_a_client_account_is_refused_before_the_route_runs(entry, monkeypatch):
    verb, path = entry.split(" ", 1)
    ran = []
    if verb == "POST":
        monkeypatch.setitem(webui.Handler._POST_ROUTES, path, lambda self, body: ran.append(path))
    sent = []
    h = _handler(verb, path, superadmin=False, sent=sent)
    getattr(webui.Handler, f"_do_{verb}")(h)
    assert sent == [403] and not ran


def test_a_superadmin_still_gets_through(monkeypatch):
    ran = []
    monkeypatch.setitem(webui.Handler._POST_ROUTES, "/api/deploy", lambda self, body: ran.append(1))
    webui.Handler._do_POST(_handler("POST", "/api/deploy", superadmin=True, sent=[]))
    assert ran == [1]


def test_a_client_still_reaches_its_own_migration(monkeypatch):
    ran = []
    monkeypatch.setitem(webui.Handler._POST_ROUTES, "/api/run", lambda self, body: ran.append(1))
    webui.Handler._do_POST(_handler("POST", "/api/run", superadmin=False, sent=[]))
    assert ran == [1]


def test_a_client_saving_its_config_leaves_the_box_tenant_alone(monkeypatch):
    import accounts_auth

    wrote, tenant = [], []
    monkeypatch.setattr(webui, "validate_config", lambda body: ({"SOURCE_DOMAIN": "a.example"}, ""))
    monkeypatch.setattr(webui, "write_config", wrote.append)
    monkeypatch.setattr(webui, "read_config", lambda acct=None: {})
    monkeypatch.setattr(accounts_auth, "update_tenant_config", lambda acct, side, **kw: tenant.append(acct))
    h = _handler("POST", "/api/config", superadmin=False, sent=[])
    h._on_screen = lambda: 7
    webui.Handler._post_config(h, {})
    assert wrote == [] and tenant == [7, 7]


# Box-wide and harmless: page shells, the OAuth redirect, and lists that name nobody's data.
HARMLESS = {"GET /", "GET /console", "GET /console/", "GET /app", "GET /app/", "GET /app/assets/",
            "GET /oauth/callback", "GET /api/version", "GET /api/actions", "GET /api/toggles",
            "GET /api/seed-scopes"}
SCOPED = ("_on_screen()", "resolve_target_account", "_account_id()", "_caller()[1]")


def _routes():
    """(verb, path, handler source) for every route webui answers."""
    import ast
    import inspect
    import re

    for path, fn in webui.Handler._POST_ROUTES.items():
        yield "POST", path, inspect.getsource(fn)
    src = inspect.getsource(webui.Handler._do_GET)
    tree = ast.parse("class _:\n" + src)
    chain = next(n for n in ast.walk(tree) if isinstance(n, ast.If) and ast.unparse(n.test) == "path == '/'")
    while isinstance(chain, ast.If):
        body = "\n".join(ast.unparse(s) for s in chain.body)
        for path in re.findall(r"'(/[a-z_/\-]*)'", ast.unparse(chain.test)):
            yield "GET", path, body
        chain = chain.orelse[0] if len(chain.orelse) == 1 and isinstance(chain.orelse[0], ast.If) else None


def test_every_route_is_scoped_gated_or_named_harmless():
    """A new route that reads or changes anything must say whose: an account resolver, the
    superadmin gate, or a deliberate place on HARMLESS."""
    routes = list(_routes())
    assert len(routes) > 60                       # a collection bug would make this vacuous
    loose = [f"{v} {p}" for v, p, code in routes
             if f"{v} {p}" not in webui.Handler._SUPERADMIN_ONLY and f"{v} {p}" not in HARMLESS
             and not any(m in code for m in SCOPED)]
    assert not loose, f"these routes name no account and are not superadmin-only: {loose}"
