"""Redo setup in one click uses the login an earlier setup kept -- only when that login is
on the domain set up in this slot now."""
import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
import admin_secrets  # noqa: E402
import api_server  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def _run(monkeypatch, login, domain="gorkhatweed.com"):
    started = []
    monkeypatch.setattr(api_server, "_resolve_account", lambda body, op: 3)
    monkeypatch.setattr(api_server.accounts_auth, "get_tenant_config",
                        lambda aid, side: {"domain": domain})
    monkeypatch.setattr(admin_secrets, "teardown_login", lambda aid, side: login)

    async def fake_start(body, op):
        started.append(body)
        return {"ok": True}
    monkeypatch.setattr(api_server, "full_setup_start", fake_start)
    body = api_server.RedoFullSetup(reason="redo", side="source")
    return asyncio.run(api_server.full_setup_redo(body, SimpleNamespace())), started


def test_it_launches_a_real_setup_with_the_kept_login(monkeypatch):
    out, started = _run(monkeypatch, ("info@gorkhatweed.com", "pw"))
    b = started[0]
    assert (b.domain, b.admin_email, b.admin_password, b.dry_run, b.account_id) == \
        ("gorkhatweed.com", "info@gorkhatweed.com", "pw", False, 3)


@pytest.mark.parametrize("login", [("", ""), ("info@source.old.com", "pw")])
def test_no_login_or_another_domains_login_is_refused(monkeypatch, login):
    with pytest.raises(HTTPException) as e:
        _run(monkeypatch, login)
    assert e.value.status_code == 409
