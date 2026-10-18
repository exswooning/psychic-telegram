"""Live, mdharan.com pilot: three accounts created for the run had no licence, and Gmail,
Calendar, Chat and Tasks all refused them. The run licenses what it writes into."""
import main
import tenant_inventory


def test_unlicensed_run_accounts_get_the_tenants_sku(monkeypatch, settings):
    assigned = []
    monkeypatch.setattr(tenant_inventory, "licenses", lambda s, side: (
        {"nestnepal@t.example": "1010020027", "webmaster@t.example": "1010020027"}, ""))
    monkeypatch.setattr(tenant_inventory, "assign_license",
                        lambda s, side, e, sku: assigned.append((e, sku)) or (True, "assigned"))
    got = main._license_run_accounts(settings, ["shiv@t.example", "nestnepal@t.example"])
    assert assigned == [("shiv@t.example", "1010020027")] and got == ["shiv@t.example"]


def test_an_unreadable_licence_list_changes_nothing(monkeypatch, settings):
    monkeypatch.setattr(tenant_inventory, "licenses", lambda s, side: ({}, "403 not delegated"))
    monkeypatch.setattr(tenant_inventory, "assign_license",
                        lambda *a: (_ for _ in ()).throw(AssertionError("must not assign")))
    assert main._license_run_accounts(settings, ["shiv@t.example"]) == []


def test_it_waits_for_the_mailbox_then_goes_on(monkeypatch):
    answers = {"a@t": iter([False, True])}

    class Gmail:
        def __init__(self, who):
            self.who = who

        def users(self):
            return self

        def getProfile(self, userId):
            return self

        def execute(self):
            if not next(answers[self.who]):
                raise Exception("Mail service not enabled")
            return {}

    class Auth:
        def target_gmail(self, who):
            return Gmail(who)
    t = [0]
    left = main._wait_until_mail(Auth(), ["a@t"], sleep=lambda s: t.__setitem__(0, t[0] + s),
                                 now=lambda: t[0])
    assert left == []
