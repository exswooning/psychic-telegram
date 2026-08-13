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
    """Directory API double: users().get/insert, and a record of both.

    `transient_get_on`/`transient_insert_on` simulate a flaky backend on
    each call independently: {email: N} means the next N calls of that
    specific kind against that email raise `transient_status` (a 5xx by
    default) before behaving normally. Separate dicts because get() always
    runs before insert() for a given email in create_until_full -- a single
    shared counter would get consumed by the existence check before insert
    ever saw it, making the two paths impossible to test independently.
    """

    def __init__(self, existing: list[str] | None = None,
                 fail_on: str | None = None,
                 transient_get_on: dict[str, int] | None = None,
                 transient_insert_on: dict[str, int] | None = None,
                 transient_status: int = 503):
        self.users_db = {e.lower(): {"primaryEmail": e} for e in (existing or [])}
        self.inserted: list[dict] = []
        self.fail_on = fail_on
        self.transient_get_on = dict(transient_get_on or {})
        self.transient_insert_on = dict(transient_insert_on or {})
        self.transient_status = transient_status
        self.get_attempts: dict[str, int] = {}
        self.insert_attempts: dict[str, int] = {}

    def users(self):
        return self

    def _maybe_transient(self, store: dict, key: str) -> None:
        remaining = store.get(key, 0)
        if remaining > 0:
            store[key] = remaining - 1
            raise HttpError(FakeResp(self.transient_status),
                            b'{"error":{"code":%d}}' % self.transient_status)

    def get(self, userKey: str = "", **kw):
        db, outer = self.users_db, self

        class _Req:
            def execute(self, num_retries: int = 0):
                key = userKey.lower()
                outer.get_attempts[key] = outer.get_attempts.get(key, 0) + 1
                outer._maybe_transient(outer.transient_get_on, key)
                if key not in db:
                    raise HttpError(FakeResp(404), b'{"error":{"code":404}}')
                return db[key]

        return _Req()

    def insert(self, body: dict | None = None, **kw):
        outer = self

        class _Req:
            def execute(self, num_retries: int = 0):
                email = body["primaryEmail"]
                key = email.lower()
                outer.insert_attempts[key] = outer.insert_attempts.get(key, 0) + 1
                outer._maybe_transient(outer.transient_insert_on, key)
                if outer.fail_on and outer.fail_on in email:
                    raise HttpError(FakeResp(403), b'{"error":{"code":403}}')
                outer.users_db[key] = {"primaryEmail": email}
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


class TestCreateUntilFull:
    """The empirical alternative to --fit-to-licenses: no pre-flight
    Reports API call (which can lag days on a low-usage tenant), just
    keep creating accounts until the Directory API itself says no."""

    def test_stops_at_the_first_failure_and_never_pulls_past_it(self):
        d = FakeDirectory(fail_on="blocked")
        pulled = []

        def candidates():
            for email in ["ok1@x.com", "blocked@x.com", "ok2@x.com", "ok3@x.com"]:
                pulled.append(email)
                yield email

        res = provision.create_until_full(d, candidates())
        assert res["created"] == ["ok1@x.com"]
        assert [b["primaryEmail"] for b in d.inserted] == ["ok1@x.com"]
        # The candidates after the failure were never even generated --
        # proof this stops immediately rather than plowing through the
        # rest and reporting a wall of identical errors.
        assert pulled == ["ok1@x.com", "blocked@x.com"]
        assert res["stopped_reason"]

    def test_existing_accounts_are_recorded_not_recreated(self):
        d = FakeDirectory(existing=["alice@x.com"])
        res = provision.create_until_full(d, iter(["alice@x.com", "bob@x.com"]))
        assert res["existing"] == ["alice@x.com"]
        assert res["created"] == ["bob@x.com"]
        assert [b["primaryEmail"] for b in d.inserted] == ["bob@x.com"]

    def test_dry_run_creates_nothing(self):
        d = FakeDirectory()
        res = provision.create_until_full(d, iter(["a@x.com", "b@x.com"]), dry_run=True)
        assert d.inserted == []
        assert res["created"] == ["a@x.com", "b@x.com"]

    def test_exhausting_the_generator_without_a_failure_is_reported(self):
        """A finite candidate stream that never hits a real limit is a
        caller bug (too small a name pool), not a silent success --
        the result must say so rather than implying it found the ceiling."""
        d = FakeDirectory()
        res = provision.create_until_full(d, iter(["a@x.com"]))
        assert res["created"] == ["a@x.com"]
        assert "ran out" in res["stopped_reason"]


class TestCreateUntilFullRetriesTransientErrors:
    """Live on source.rohitrokaya.com.np, a single 503 'backendError' on an
    existence check ended a create_until_full run at 122 accounts -- a
    backend blip, not Google saying anything about licence capacity. These
    cover the fix: retry a 5xx a bounded number of times, but let a real
    4xx (403/quotaExceeded on the insert, in particular) stop immediately,
    exactly as before."""

    @staticmethod
    def _quiet_sleep():
        """Records delays without blocking -- these tests must not spend
        real wall-clock time on the backoff they're verifying happens."""
        calls: list[float] = []
        return calls, calls.append

    def test_a_transient_503_on_the_existence_check_is_retried_and_recovers(self):
        d = FakeDirectory(transient_get_on={"new@x.com": 2})
        delays, sleep = self._quiet_sleep()
        res = provision.create_until_full(d, iter(["new@x.com"]), sleep=sleep)
        assert res["created"] == ["new@x.com"]
        assert d.get_attempts["new@x.com"] == 3, "2 failures then a success"
        assert len(delays) == 2, "one sleep per retry, none after the final success"

    def test_a_transient_503_on_insert_is_retried_and_recovers(self):
        d = FakeDirectory(transient_insert_on={"new@x.com": 1})
        delays, sleep = self._quiet_sleep()
        res = provision.create_until_full(d, iter(["new@x.com"]), sleep=sleep)
        assert res["created"] == ["new@x.com"]
        assert d.get_attempts["new@x.com"] == 1, "the existence check itself was clean"
        assert d.insert_attempts["new@x.com"] == 2, "1 failure then a success"
        assert len(delays) == 1

    def test_gives_up_after_max_retries_on_a_persistent_5xx(self):
        """A backend that never recovers must still stop eventually -- this
        is bounded retry, not infinite -- but the reason says it gave up
        after retrying, distinct from a clean 4xx stop."""
        d = FakeDirectory(transient_get_on={"stuck@x.com": 999})
        _, sleep = self._quiet_sleep()
        res = provision.create_until_full(d, iter(["stuck@x.com", "next@x.com"]),
                                          max_retries=2, sleep=sleep)
        assert res["created"] == []
        assert d.get_attempts["stuck@x.com"] == 3, "1 initial + 2 retries"
        assert "after retrying" in res["stopped_reason"]
        assert "next@x.com" not in d.get_attempts, "stopped, did not move on"

    def test_a_403_on_insert_is_not_retried_and_stops_immediately(self):
        """The real signal (licence exhaustion, or any other 4xx) must not
        be mistaken for backend flakiness and retried away."""
        d = FakeDirectory(fail_on="blocked")
        delays, sleep = self._quiet_sleep()
        res = provision.create_until_full(d, iter(["blocked@x.com"]), sleep=sleep)
        assert res["created"] == []
        assert d.insert_attempts["blocked@x.com"] == 1, "no retry on a 4xx"
        assert delays == []
        assert "403" in res["stopped_reason"]


@pytest.mark.parametrize("email,expected", [
    ("alice.brown@x.com", ("Alice", "Brown")),
    ("bob@x.com", ("Bob", "User")),
    ("mary.jane.watson@x.com", ("Mary", "Jane Watson")),
])
def test_names_are_derived_from_the_localpart(email, expected):
    assert provision._split_name(email) == expected
