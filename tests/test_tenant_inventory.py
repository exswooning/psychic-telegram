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


@pytest.fixture(autouse=True)
def _no_live_licence_call(monkeypatch):
    """The licence read is a real Google call on its own
    credential; these tests are about the counting, not it."""
    monkeypatch.setattr(tenant_inventory, "licenses",
                        lambda s, side: ({}, ""))


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
        assert snap["totals"] == {"emails": 30, "threads": 13,
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
        assert dead["emails"] is None and dead["driveBytes"] is None
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
        assert snap["totals"]["emails"] == 7
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
        assert snap["totals"]["emails"] == 11
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


class TestLicences:
    """The one metric behind a scope most tenants have never granted.

    It must never be folded into the migration's own scope list: a scope the
    Admin Console has not authorised fails the ENTIRE token request, so
    adding it there would break every migration on every tenant that had not
    re-pasted its scope line. It gets its own single-scope credential and
    degrades.
    """

    def test_licences_are_attached_per_account_and_counted(self, wired, monkeypatch):
        settings, install = wired
        install(FakeAuth(
            emails=["a@x.com", "b@x.com"],
            profiles={e: {"messagesTotal": 1, "threadsTotal": 1}
                      for e in ("a@x.com", "b@x.com")},
            quotas={e: {"usageInDrive": "1"} for e in ("a@x.com", "b@x.com")}))
        monkeypatch.setattr(tenant_inventory, "licenses", lambda s, side: (
            {"a@x.com": "Business Standard", "b@x.com": "Business Starter"}, ""))
        snap = tenant_inventory.snapshot(settings, "source")
        by = {u["email"]: u["license"] for u in snap["users"]}
        assert by == {"a@x.com": "Business Standard",
                      "b@x.com": "Business Starter"}
        assert snap["licenseCounts"] == {"Business Standard": 1,
                                         "Business Starter": 1}

    def test_an_ungranted_scope_reports_itself_and_does_not_break_the_panel(
            self, wired, monkeypatch):
        """"We could not read licences" must not render as "this tenant has
        none" -- those are opposite facts."""
        settings, install = wired
        install(FakeAuth(
            emails=["a@x.com"],
            profiles={"a@x.com": {"messagesTotal": 5, "threadsTotal": 1}},
            quotas={"a@x.com": {"usageInDrive": "9"}}))
        monkeypatch.setattr(tenant_inventory, "licenses", lambda s, side: (
            {}, "licence data needs the .../apps.licensing scope"))
        snap = tenant_inventory.snapshot(settings, "source")
        assert "apps.licensing" in snap["licenseError"]
        assert snap["licenseCounts"] == {}
        assert snap["users"][0]["license"] == ""
        # The rest of the panel is unaffected.
        assert snap["totals"]["emails"] == 5
        assert snap["error"] == ""

    def test_an_unknown_sku_id_falls_back_to_the_raw_id(self):
        """A new SKU is far likelier than a bug, and the raw id is still
        actionable -- 'unknown' is not."""
        assert tenant_inventory.SKU_NAMES.get("1010020027") == "Business Starter"
        assert tenant_inventory.SKU_NAMES.get("9999999999") is None


class TestDeepScan:
    """Share access and the rest of what inventory.py measures.

    Off by default and explicitly triggered: it walks every file every user
    owns to read ACLs, which is minutes per tenant rather than seconds.
    """

    def _wire_deep(self, monkeypatch, per_user):
        monkeypatch.setattr(tenant_inventory, "deep_probe",
                            lambda auth, s, side, email: per_user[email])

    def test_the_default_fetch_does_no_deep_probing(self, wired, monkeypatch):
        """The panel's own load must stay in seconds."""
        settings, install = wired
        install(FakeAuth(emails=["a@x.com"],
                         profiles={"a@x.com": {"messagesTotal": 1, "threadsTotal": 1}},
                         quotas={"a@x.com": {"usageInDrive": "1"}}))

        def boom(*a, **k):
            raise AssertionError("deep probe ran on the default fetch")

        monkeypatch.setattr(tenant_inventory, "deep_probe", boom)
        snap = tenant_inventory.snapshot(settings, "source")
        assert snap["deep"] is False

    def test_a_deep_scan_totals_the_sharing_facts(self, wired, monkeypatch):
        """The facts that change what a migration MEANS: what is shared
        outside the company, and what is link-shared to anyone."""
        settings, install = wired
        emails = ["a@x.com", "b@x.com"]
        install(FakeAuth(
            emails=emails,
            profiles={e: {"messagesTotal": 1, "threadsTotal": 1} for e in emails},
            quotas={e: {"usageInDrive": "1"} for e in emails}))
        self._wire_deep(monkeypatch, {
            "a@x.com": {"driveKinds": {"document": 3, "folder": 1}, "shared": 4,
                        "external": 2, "anyone": 1, "calendarEvents": 10,
                        "calendars": 1, "chatSpaces": None,
                        "chatMessages": None, "error": ""},
            "b@x.com": {"driveKinds": {"document": 2}, "shared": 1,
                        "external": 0, "anyone": 0, "calendarEvents": 5,
                        "calendars": 1, "chatSpaces": None,
                        "chatMessages": None, "error": ""},
        })
        snap = tenant_inventory.snapshot(settings, "source", deep=True)
        assert snap["deep"] is True
        assert snap["totals"]["shared"] == 5
        assert snap["totals"]["external"] == 2
        assert snap["totals"]["anyone"] == 1
        assert snap["totals"]["calendarEvents"] == 15
        assert snap["totals"]["driveKinds"]["document"] == 5

    def test_one_users_deep_failure_does_not_lose_the_others(self, wired, monkeypatch):
        settings, install = wired
        emails = ["ok@x.com", "bad@x.com"]
        install(FakeAuth(
            emails=emails,
            profiles={e: {"messagesTotal": 1, "threadsTotal": 1} for e in emails},
            quotas={e: {"usageInDrive": "1"} for e in emails}))

        def probe(auth, s, side, email):
            if email == "bad@x.com":
                raise RuntimeError("drive listing blew up")
            return {"driveKinds": {}, "shared": 3, "external": 1, "anyone": 0,
                    "calendarEvents": 2, "calendars": 1, "chatSpaces": None,
                    "chatMessages": None, "error": ""}

        monkeypatch.setattr(tenant_inventory, "deep_probe", probe)
        snap = tenant_inventory.snapshot(settings, "source", deep=True)
        assert snap["totals"]["shared"] == 3
        bad = [u for u in snap["users"] if u["email"] == "bad@x.com"][0]
        assert "deep" in bad["error"]


class TestTheLicenceScopeIsGrantedButNeverRequired:
    """The asymmetry that keeps this feature from breaking migrations.

    A console grant is monotonic -- authorising a scope nobody requests costs
    nothing. A scope in the code's own request list that the console has not
    authorised fails the entire token exchange. So apps.licensing belongs on
    the paste line and must never reach required_scopes().
    """

    def test_it_is_on_the_grant_line(self):
        import webui
        payload = webui.dwd_payload()
        for key in ("migrate_source_full", "seed"):
            entry = payload.get(key)
            if not entry:
                continue
            scopes = entry.get("scope_list") if isinstance(entry, dict) else entry
            assert tenant_inventory.LICENSING_SCOPE in (scopes or []), key

    def test_it_is_NOT_in_required_scopes(self):
        """If this ever flips, every tenant that has not re-pasted its scope
        line stops migrating -- and scope_guard would correctly refuse to
        start them, which makes the breakage total."""
        import verify_scopes
        from config import Settings
        st = Settings()
        for side in ("source", "target"):
            assert tenant_inventory.LICENSING_SCOPE not in \
                verify_scopes.required_scopes(st, side)


class TestTheDeepScanIsAnHonestSample:
    """Measured on a real account in this tenant: 180 seconds and 29,056
    files for ONE mailbox's Drive. Across 201 accounts that is ~75 minutes
    even at eight workers -- past any HTTP request. So the deep tier samples,
    and the numbers it produces are the sample's, not the tenant's.
    """

    def _tenant(self, install, n):
        emails = [f"u{i}@x.com" for i in range(n)]
        install(FakeAuth(
            emails=emails,
            profiles={e: {"messagesTotal": 1, "threadsTotal": 1} for e in emails},
            quotas={e: {"usageInDrive": "1"} for e in emails}))
        return emails

    def test_only_the_sample_is_walked(self, wired, monkeypatch):
        settings, install = wired
        self._tenant(install, 20)
        probed: list[str] = []

        def probe(auth, s, side, email):
            probed.append(email)
            return {"driveKinds": {}, "shared": 1, "external": 0, "anyone": 0,
                    "calendarEvents": 1, "calendars": 1, "chatSpaces": None,
                    "chatMessages": None, "error": ""}

        monkeypatch.setattr(tenant_inventory, "deep_probe", probe)
        snap = tenant_inventory.snapshot(settings, "source", deep=True,
                                         deep_sample=3)
        assert len(probed) == 3
        assert snap["deepSampled"] == 3
        assert snap["accounts"] == 20

    def test_sharing_totals_are_the_samples_not_the_tenants(self, wired, monkeypatch):
        """The trap: summing 3 of 20 accounts and rendering it beside a
        headcount of 20 invites reading it as the whole tenant."""
        settings, install = wired
        self._tenant(install, 20)
        monkeypatch.setattr(tenant_inventory, "deep_probe",
                            lambda a, s, side, e: {
                                "driveKinds": {}, "shared": 2, "external": 1,
                                "anyone": 0, "calendarEvents": 4, "calendars": 1,
                                "chatSpaces": None, "chatMessages": None,
                                "error": ""})
        snap = tenant_inventory.snapshot(settings, "source", deep=True,
                                         deep_sample=3)
        assert snap["totals"]["shared"] == 6        # 3 sampled x 2, not 20 x 2
        assert snap["deepSampled"] == 3

    def test_a_quick_scan_reports_no_sample_at_all(self, wired, monkeypatch):
        settings, install = wired
        self._tenant(install, 5)
        snap = tenant_inventory.snapshot(settings, "source")
        assert snap["deepSampled"] == 0


class TestChatIsReportedRegardlessOfMigrationFlags:
    """A tenant panel describes a TENANT, not a planned migration.

    inventory.py gates its Chat scan on settings.migrate_chat because it is
    describing a migration about to run. Reusing that gate here meant the
    Chat columns were always empty -- migrate_chat defaults False -- on
    tenants that demonstrably have Chat. That is the panel telling the
    operator something false about their own data.
    """

    def test_chat_is_probed_even_with_migrate_chat_off(self, monkeypatch):
        import inventory

        scanned: list[str] = []

        class FakeAuthChat:
            def source_drive(self, u):
                raise RuntimeError("not under test")
            def source_calendar(self, u):
                raise RuntimeError("not under test")
            def source_chat(self, u):
                scanned.append(u)
                return object()

        monkeypatch.setattr(inventory, "scan_chat",
                            lambda c: {"spaces": 4, "messages": 40})

        class S:
            migrate_chat = False       # the default, and the whole point

        out = tenant_inventory.deep_probe(FakeAuthChat(), S(), "source",
                                          "a@x.com")
        assert scanned == ["a@x.com"]
        assert out["chatSpaces"] == 4
        assert out["chatMessages"] == 40

    def test_a_tenant_without_chat_scopes_records_the_reason(self, monkeypatch):
        """"Not granted" and "none present" must stay distinguishable."""
        import inventory

        class FakeAuthChat:
            def source_drive(self, u):
                raise RuntimeError("drive off")
            def source_calendar(self, u):
                raise RuntimeError("cal off")
            def source_chat(self, u):
                raise RuntimeError("unauthorized_client")

        class S:
            migrate_chat = False

        out = tenant_inventory.deep_probe(FakeAuthChat(), S(), "source",
                                          "a@x.com")
        assert out["chatSpaces"] is None
        assert "chat: unauthorized_client" in out["error"]
