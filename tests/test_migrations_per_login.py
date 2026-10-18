"""A login runs as many migrations as it is provisioned for, each its own account.

Live: setting up dharan.gov.np -> mdharan.com on the account that held gorkhatweed.com
-> s.sarafgloabalexim.com moved gorkhatweed's keys aside, with nest@ still waiting to
migrate. A pair is an account (its own keys/, ledger and jobs); a login now OWNS
several. The rules that must hold: a login reaches its own migrations and nobody
else's, however the browser asks, and stops at its pair_limit.
"""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import accounts_auth
import control_plane_db as cpdb
from db import MigrationDB


@pytest.fixture
def cp(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    import api_server
    with TestClient(api_server.app) as client:
        yield client
    try:
        os.unlink(path)
    except OSError:
        pass


def _login(email: str) -> tuple[int, dict]:
    aid = accounts_auth.create_account(email, "a-long-password", "Someone")
    return aid, {"bp_session": accounts_auth.create_session(aid)}


def _set_up(aid: int, src: str, tgt: str) -> None:
    with cpdb.rw() as conn:
        for side, d in (("source", src), ("target", tgt)):
            conn.execute("UPDATE tenant_configs SET domain=? WHERE account_id=? AND side=?",
                         (d, aid, side))


def test_a_login_holds_only_what_it_was_provisioned_for(cp):
    a, _ = _login("a@one.example")
    _set_up(a, "gorkhatweed.com", "s.sarafgloabalexim.com")
    with pytest.raises(accounts_auth.AccountError, match="may hold 1"):
        accounts_auth.create_migration(a, "dharan.gov.np -> mdharan.com")
    accounts_auth.set_pair_limit(a, 2)
    m = accounts_auth.create_migration(a, "dharan.gov.np -> mdharan.com")
    assert accounts_auth.owned_migrations(a) == [m]
    with pytest.raises(accounts_auth.AccountError):
        accounts_auth.create_migration(a, "a third")
    # Its own keys and ledger, never the login's.
    cfg = accounts_auth.get_tenant_config(m, "source")
    assert f"keys/{m}/" in cfg["sa_key_path"] and f"accounts/{m}/" in cfg["db_path"]


def test_a_migration_cannot_be_signed_into(cp):
    a, _ = _login("b@one.example")
    m = accounts_auth.create_migration(a, "x", is_superadmin=True)
    email = accounts_auth.get_account(m)["email"]
    for guess in ("!", "", "a-long-password"):
        assert accounts_auth.authenticate(email, guess) is None


def test_the_browser_chooses_among_its_own_and_nothing_else(cp):
    a, a_cookie = _login("c@one.example")
    b, b_cookie = _login("c@two.example")
    accounts_auth.set_pair_limit(a, 5)
    accounts_auth.set_pair_limit(b, 5)
    mine = accounts_auth.create_migration(a, "dharan.gov.np -> mdharan.com")
    theirs = accounts_auth.create_migration(b, "someone else's")

    def me(cookie, chosen):
        cp.cookies.clear()
        for k, v in cookie.items():
            cp.cookies.set(k, v)
        r = cp.get("/api/v2/auth/me", headers={"X-Bitport-Migration": str(chosen)})
        assert r.status_code == 200, r.text
        return r.json()

    got = me(a_cookie, mine)
    assert got["id"] == a and got["migration_id"] == mine      # still the login
    assert me(a_cookie, theirs)["migration_id"] == a           # foreign: ignored
    assert me(a_cookie, b)["migration_id"] == a                # another login: ignored
    assert me(a_cookie, "nonsense")["migration_id"] == a

    # The websocket cannot send a header; the query parameter obeys the same rule.
    cp.cookies.clear()
    cp.cookies.set("bp_session", a_cookie["bp_session"])
    assert cp.get(f"/api/v2/auth/me?bp_migration={theirs}").json()["migration_id"] == a
    assert cp.get(f"/api/v2/auth/me?bp_migration={mine}").json()["migration_id"] == mine

    # A path that names an account: its own migration yes, anyone else's no.
    assert cp.get(f"/api/v2/full-setup/status?side=source&account={mine}").status_code == 200
    assert cp.get(f"/api/v2/full-setup/status?side=source&account={theirs}").status_code == 403

    listed = {m["accountId"]: m for m in cp.get("/api/v2/migrations").json()["migrations"]}
    assert mine in listed and theirs not in listed
    assert listed[mine]["ownerId"] == a


def test_opening_a_migration_through_the_api(cp):
    a, cookie = _login("d@one.example")
    _set_up(a, "gorkhatweed.com", "s.sarafgloabalexim.com")
    cp.cookies.set("bp_session", cookie["bp_session"])
    refused = cp.post("/api/v2/migrations", json={"reason": "second pair", "name": "x"}).json()
    assert refused["ok"] is False and refused["accountId"] is None
    accounts_auth.set_pair_limit(a, 2)
    made = cp.post("/api/v2/migrations",
                   json={"reason": "second pair", "name": "dharan.gov.np -> mdharan.com"}).json()
    assert made["ok"] is True and made["accountId"] in accounts_auth.owned_migrations(a)


def test_webui_decides_as_the_login_behind_a_chosen_migration(cp):
    import webui
    a, _ = _login("e@one.example")
    b, _ = _login("e@two.example")
    accounts_auth.set_pair_limit(a, 5)
    mine1 = accounts_auth.create_migration(a, "one")
    mine2 = accounts_auth.create_migration(a, "two")
    theirs = accounts_auth.create_migration(b, "theirs", is_superadmin=True)
    assert webui.resolve_target_account(mine1, mine2) == (mine2, "")
    assert webui.resolve_target_account(mine1, a) == (a, "")
    assert webui.resolve_target_account(mine1, theirs)[0] is None
    assert webui.resolve_target_account(a, theirs)[0] is None
