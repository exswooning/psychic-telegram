"""Creating the run's target accounts, live on account 3 (2026-11-02): a 409 for an account another
run had just made was logged as a failure, and a profile written straight after a create answered
412 "User creation is not complete" and was given up."""
import httplib2
import pytest
from googleapiclient.errors import HttpError

import provision
import user_profiles as U
from tests.test_profile_read_back_waits import _Dir, _R, _run


def _err(status, msg):
    return HttpError(httplib2.Response({"status": status}), ('{"error":{"message":"%s"}}' % msg).encode())


class _Directory:
    """users().get answers 404 first (not there yet), then whatever `later` says; insert raises 409."""
    def __init__(self, later_exists):
        self.later_exists, self.gets = later_exists, 0

    def users(self):
        return self

    def get(self, userKey, **k):
        self.gets += 1
        exists = self.gets > 1 and self.later_exists

        class R:
            def execute(_):
                if not exists:
                    raise _err(404, "Resource Not Found: userKey")
                return {"primaryEmail": userKey}
        return R()

    def insert(self, body):
        class R:
            def execute(_):
                raise _err(409, "Entity Already Exists")
        return R()


def test_a_409_for_an_account_that_is_there_is_existing_not_failed():
    out = provision.ensure_users(_Directory(later_exists=True), ["ward9@t.example"])
    assert out["existing"] == ["ward9@t.example"] and out["failed"] == []


def test_a_409_for_a_conflicting_alias_is_still_a_failure():
    out = provision.ensure_users(_Directory(later_exists=False), ["ward9@t.example"])
    assert out["existing"] == [] and "409" in out["failed"][0][1]


def test_a_profile_write_waits_for_google_to_finish_creating_the_account(db, settings, monkeypatch):
    slept = []
    monkeypatch.setattr(U, "_sleep", slept.append)
    refusals = {"n": 2}
    real = _R.execute

    def execute(self, num_retries=0):
        if self.headers.get("If-Match") and refusals["n"]:
            refusals["n"] -= 1
            raise _err(412, "User creation is not complete.")
        return real(self, num_retries)
    monkeypatch.setattr(_R, "execute", execute)
    row = _run(db, settings, stale_reads=0)
    assert row["status"] == "UPDATED", row
    assert slept[:2] == list(U.CREATION_PAUSES[:2])


def test_a_412_that_is_not_about_creation_still_fails_at_once(db, settings, monkeypatch):
    slept = []
    monkeypatch.setattr(U, "_sleep", slept.append)

    def execute(self, num_retries=0):
        if self.headers.get("If-Match"):
            raise _err(412, "Precondition Failed")
        return self.value
    monkeypatch.setattr(_R, "execute", execute)
    row = _run(db, settings, stale_reads=0)
    assert row["status"] == "ERROR" and not any(p in slept for p in U.CREATION_PAUSES[1:])
