"""
tests/test_tenant_inventory.py
==============================
How many accounts a tenant has, and how much data each one holds.

This renders inside a setup panel, which constrains it in two ways the
deep scan in inventory.py is not constrained by:

* it must be cheap -- two single-shot summaries per account, not a walk of
  every file;
* it must never fail whole. A suspended or never-provisioned mailbox
  answers 400/401, which is entirely ordinary; one such account must not
  turn "200 of 201 accounts read" into a blank card. Observed live on a
  real 201-account tenant: exactly one.

The second constraint has a trap in it, which these tests pin: partial
totals that do not say what they cover read as the whole tenant, and the
reader has no way to tell. `covered` is that denominator.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import tenant_inventory  # noqa: E402


class FakeExec:
    def __init__(self, result=None, exc=None):
        self._result, self._exc = result, exc

    def execute(self):
        if self._exc:
            raise self._exc
        return self._result


class FakeGmail:
    def __init__(self, profiles):
        self.profiles = profiles

    def users(self):
        return self

    def getProfile(self, userId):        # noqa: N802 - Google's spelling
        val = self.profiles.get(userId)
        if isinstance(val, Exception):
            return FakeExec(exc=val)
        return FakeExec(result=val)


class FakeDrive:
    def __init__(self, quotas):
        self.quotas = quotas
        self._user = None

    def about(self):
        return self

    def get(self, fields):
        val = self.quotas.get(self._user)
        if isinstance(val, Exception):
            return FakeExec(exc=val)
        return FakeExec(result={"storageQuota": val})


class FakeAuth:
    def __init__(self, emails, profiles, quotas, list_exc=None):
        self.emails, self.list_exc = emails, list_exc
        self.profiles, self.quotas = profiles, quotas

    def directory(self, side, writable=False):
        if self.list_exc:
            raise self.list_exc
        outer = self

        class D:
            def users(self):
                return self

            def list(self, **kw):
                return FakeExec(result={
                    "users": [{"primaryEmail": e} for e in outer.emails]})

        return D()

    def _gmail(self, user):
        return FakeGmail(self.profiles)

    def _drive(self, user):
        d = FakeDrive(self.quotas)
        d._user = user
        return d

    source_gmail = target_gmail = _gmail
    source_drive = target_drive = _drive


@pytest.fixture
def wired(monkeypatch, settings):
    settings.source_domain = "src.example.com"

    def _install(auth):
        monkeypatch.setattr(tenant_inventory, "AuthManager", lambda s: auth)
        return auth

    return settings, _install


class TestHeadcountAndData:
    def test_it_reports_the_accounts_and_each_ones_data(self, wired):
        settings, install = wired
        install(FakeAuth(
            emails=["a@x.com", "b@x.com"],
            profiles={"a@x.com": {"messagesTotal": 10, "threadsTotal": 4},
                      "b@x.com": {"messagesTotal": 20, "threadsTotal": 9}},
            quotas={"a@x.com": {"usageInDrive": "100"},
                    "b@x.com": {"usageInDrive": "250"}}))
        snap = tenant_inventory.snapshot(settings, "source")
        assert snap["accounts"] == 2
        assert snap["totals"] == {"messages": 30, "threads": 13,
                                  "driveBytes": 350, "covered": 2}
        assert [u["email"] for u in snap["users"]] == ["a@x.com", "b@x.com"]

    def test_rows_come_back_in_a_stable_order(self, wired):
        """They are gathered from a thread pool, which completes in whatever
        order it likes; a table that reshuffles between refreshes is unusable
        for comparing two runs."""
        settings, install = wired
        install(FakeAuth(
            emails=["c@x.com", "a@x.com", "b@x.com"],
            profiles={e: {"messagesTotal": 1, "threadsTotal": 1}
                      for e in ("a@x.com", "b@x.com", "c@x.com")},
            quotas={e: {"usageInDrive": "1"}
                    for e in ("a@x.com", "b@x.com", "c@x.com")}))
        snap = tenant_inventory.snapshot(settings, "source")
        assert [u["email"] for u in snap["users"]] == [
            "a@x.com", "b@x.com", "c@x.com"]

    def test_drive_usage_excludes_gmail_and_photos(self, wired):
        """`usage` folds in Gmail and Photos; reporting it beside a message
        count double-counts the mailbox and makes the two columns disagree."""
        settings, install = wired
        install(FakeAuth(
            emails=["a@x.com"],
            profiles={"a@x.com": {"messagesTotal": 5, "threadsTotal": 5}},
            quotas={"a@x.com": {"usage": "999999", "usageInDrive": "42"}}))
        snap = tenant_inventory.snapshot(settings, "source")
        assert snap["users"][0]["driveBytes"] == 42


class TestPartialsAreHonest:
    def test_one_unreadable_account_does_not_fail_the_scan(self, wired):
        """The live case: a never-provisioned account answers 400 on Gmail
        and 401 on Drive. 200 of 201 accounts read is useful; a blank card
        is not."""
        settings, install = wired
        install(FakeAuth(
            emails=["ok@x.com", "dead@x.com"],
            profiles={"ok@x.com": {"messagesTotal": 7, "threadsTotal": 3},
                      "dead@x.com": Exception("HttpError 400")},
            quotas={"ok@x.com": {"usageInDrive": "70"},
                    "dead@x.com": Exception("HttpError 401")}))
        snap = tenant_inventory.snapshot(settings, "source")
        assert snap["accounts"] == 2
        assert snap["error"] == ""
        dead = [u for u in snap["users"] if u["email"] == "dead@x.com"][0]
        assert dead["messages"] is None and dead["driveBytes"] is None
        assert "400" in dead["error"] and "401" in dead["error"]

    def test_totals_name_the_accounts_they_actually_cover(self, wired):
        """The trap. A total summing 1 of 2 accounts reads as the whole
        tenant unless the denominator travels with it."""
        settings, install = wired
        install(FakeAuth(
            emails=["ok@x.com", "dead@x.com"],
            profiles={"ok@x.com": {"messagesTotal": 7, "threadsTotal": 3},
                      "dead@x.com": Exception("HttpError 400")},
            quotas={"ok@x.com": {"usageInDrive": "70"},
                    "dead@x.com": Exception("HttpError 401")}))
        snap = tenant_inventory.snapshot(settings, "source")
        assert snap["totals"]["messages"] == 7
        assert snap["totals"]["covered"] == 1
        assert snap["totals"]["covered"] < snap["accounts"]

    def test_a_half_readable_account_still_counts_what_it_gave(self, wired):
        """Gmail answered, Drive did not. Discarding the message count
        because one of two probes failed loses real data."""
        settings, install = wired
        install(FakeAuth(
            emails=["a@x.com"],
            profiles={"a@x.com": {"messagesTotal": 11, "threadsTotal": 2}},
            quotas={"a@x.com": Exception("HttpError 403")}))
        snap = tenant_inventory.snapshot(settings, "source")
        assert snap["totals"]["messages"] == 11
        assert snap["totals"]["covered"] == 1
        assert snap["users"][0]["driveBytes"] is None
        assert "403" in snap["users"][0]["error"]


class TestItNeverBlowsUpThePanel:
    def test_an_unlistable_tenant_reports_the_reason(self, wired):
        """This renders in a card; an exception is a blank screen."""
        settings, install = wired
        install(FakeAuth(emails=[], profiles={}, quotas={},
                         list_exc=RuntimeError("SOURCE_ADMIN is not set")))
        snap = tenant_inventory.snapshot(settings, "source")
        assert snap["users"] == []
        assert "SOURCE_ADMIN is not set" in snap["error"]

    def test_no_domain_configured_is_stated_not_guessed(self, settings):
        settings.source_domain = ""
        snap = tenant_inventory.snapshot(settings, "source")
        assert "no source domain configured" in snap["error"]

    def test_an_empty_tenant_is_a_valid_answer(self, wired):
        settings, install = wired
        install(FakeAuth(emails=[], profiles={}, quotas={}))
        snap = tenant_inventory.snapshot(settings, "source")
        assert snap["accounts"] == 0
        assert snap["error"] == ""
        assert snap["totals"]["covered"] == 0


class TestLimit:
    def test_the_headcount_stays_true_when_the_rows_are_capped(self, wired):
        """`limit` bounds the probing, not the count. Reporting 2 accounts
        for a 5-account tenant because the UI asked for 2 rows would be a
        lie about the tenant."""
        settings, install = wired
        emails = [f"u{i}@x.com" for i in range(5)]
        install(FakeAuth(
            emails=emails,
            profiles={e: {"messagesTotal": 1, "threadsTotal": 1} for e in emails},
            quotas={e: {"usageInDrive": "1"} for e in emails}))
        snap = tenant_inventory.snapshot(settings, "source", limit=2)
        assert snap["accounts"] == 5
        assert len(snap["users"]) == 2
        assert snap["truncated"] is True

    def test_no_truncation_flag_when_everything_fits(self, wired):
        settings, install = wired
        install(FakeAuth(
            emails=["a@x.com"],
            profiles={"a@x.com": {"messagesTotal": 1, "threadsTotal": 1}},
            quotas={"a@x.com": {"usageInDrive": "1"}}))
        snap = tenant_inventory.snapshot(settings, "source", limit=10)
        assert snap["truncated"] is False
