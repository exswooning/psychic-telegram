"""
tests/test_provision.py
=======================
Provisioning is the one part of this tool that can create licensed accounts,
so these tests are about what it must *never* do as much as what it does.

The `changePasswordAtNextLogin` assertion is not cosmetic: a pending password
change silently breaks domain-wide delegation, and two accounts in live
testing failed impersonation with "Active session is invalid" for exactly that
reason. An account provisioned for a migration it then cannot participate in
is worse than no account.
"""

from __future__ import annotations

import pytest
from googleapiclient.errors import HttpError

import provision
from tests.fakes import FakeResp


class FakeDirectory:
    """Directory API double: users().get/insert, and a record of both."""

    def __init__(self, existing: list[str] | None = None,
                 fail_on: str | None = None):
        self.users_db = {e.lower(): {"primaryEmail": e} for e in (existing or [])}
        self.inserted: list[dict] = []
        self.fail_on = fail_on

    def users(self):
        return self

    def get(self, userKey: str = "", **kw):
        db, outer = self.users_db, self

        class _Req:
            def execute(self, num_retries: int = 0):
                if userKey.lower() not in db:
                    raise HttpError(FakeResp(404), b'{"error":{"code":404}}')
                return db[userKey.lower()]

        return _Req()

    def insert(self, body: dict | None = None, **kw):
        outer = self

        class _Req:
            def execute(self, num_retries: int = 0):
                email = body["primaryEmail"]
                if outer.fail_on and outer.fail_on in email:
                    raise HttpError(FakeResp(403), b'{"error":{"code":403}}')
                outer.users_db[email.lower()] = {"primaryEmail": email}
                outer.inserted.append(body)
                return body

        return _Req()


def test_creates_only_missing_accounts():
    d = FakeDirectory(existing=["alice@x.com"])
    res = provision.ensure_users(d, ["alice@x.com", "bob@x.com"])

    assert [e for e, _ in res["created"]] == ["bob@x.com"]
    assert res["existing"] == ["alice@x.com"]
    assert [b["primaryEmail"] for b in d.inserted] == ["bob@x.com"]


def test_never_modifies_an_existing_account():
    """An address that already exists must be left alone entirely --
    overwriting a real account's name or password is unrecoverable."""
    d = FakeDirectory(existing=["alice@x.com"])
    provision.ensure_users(d, ["alice@x.com"])
    assert d.inserted == [], "no write of any kind against an existing account"


def test_password_change_is_not_forced():
    """A pending password change blocks domain-wide delegation, so the
    migration could not impersonate the account it just created."""
    d = FakeDirectory()
    provision.ensure_users(d, ["bob@x.com"])
    assert d.inserted[0]["changePasswordAtNextLogin"] is False


def test_generated_passwords_are_unique_and_long():
    d = FakeDirectory()
    provision.ensure_users(d, ["a@x.com", "b@x.com", "c@x.com"])
    pws = [b["password"] for b in d.inserted]
    assert len(set(pws)) == 3
    assert all(len(p) >= 16 for p in pws)


def test_dry_run_creates_nothing():
    d = FakeDirectory()
    res = provision.ensure_users(d, ["new@x.com"], dry_run=True)
    assert d.inserted == []
    assert [e for e, _ in res["created"]] == ["new@x.com"]


def test_one_failure_does_not_stop_the_rest():
    d = FakeDirectory(fail_on="blocked")
    res = provision.ensure_users(d, ["ok1@x.com", "blocked@x.com", "ok2@x.com"])
    assert sorted(e for e, _ in res["created"]) == ["ok1@x.com", "ok2@x.com"]
    assert [e for e, _ in res["failed"]] == ["blocked@x.com"]


@pytest.mark.parametrize("email,expected", [
    ("alice.brown@x.com", ("Alice", "Brown")),
    ("bob@x.com", ("Bob", "User")),
    ("mary.jane.watson@x.com", ("Mary", "Jane Watson")),
])
def test_names_are_derived_from_the_localpart(email, expected):
    assert provision._split_name(email) == expected
