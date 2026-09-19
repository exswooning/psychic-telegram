"""
tests/test_auto_provision_target_users.py
==========================================
A fresh target tenant has nothing but the admin. Before this, `migrate`
assumed every identity_map row already had a real target account and found
out otherwise the hard way: 299 of 300 mapped users failed on every single
service with `invalid_grant: Invalid email or User ID` -- Google rejecting
an impersonation subject that was never created. That read as a broken
migration; it was a missing-provisioning step, and `provision-users`
already existed to do it but nothing ever called it automatically.
"""

from __future__ import annotations

import provision
import main


class _Settings:
    def __init__(self, auto_provision_users=True, dry_run=False,
                target_domain="target.example"):
        self.auto_provision_users = auto_provision_users
        self.dry_run = dry_run
        self.target_domain = target_domain


class _FakeAuth:
    def __init__(self):
        self.directory_calls: list[tuple[str, bool]] = []

    def directory(self, tenant, writable=False):
        self.directory_calls.append((tenant, writable))
        return object()


class TestAutoProvisioningTargetUsers:
    def test_creates_missing_target_accounts_before_migrating(self, monkeypatch):
        created: list[str] = []

        def fake_ensure_users(directory, emails, dry_run=False):
            created.extend(emails)
            return {"created": [(e, "pw") for e in emails],
                    "existing": [], "failed": []}

        monkeypatch.setattr(provision, "ensure_users", fake_ensure_users)
        auth = _FakeAuth()
        pairs = [("a@source.example", "a@target.example"),
                 ("b@source.example", "b@target.example")]

        main._ensure_target_accounts(auth, _Settings(), pairs)

        assert created == ["a@target.example", "b@target.example"]
        # Writable, and against the target tenant specifically -- this is
        # the one non-provision-users caller auth.directory now expects.
        assert auth.directory_calls == [("target", True)]

    def test_a_dry_run_creates_nothing(self, monkeypatch):
        monkeypatch.setattr(provision, "ensure_users",
                           lambda *a, **k: (_ for _ in ()).throw(
                               AssertionError("should never be called")))
        auth = _FakeAuth()

        main._ensure_target_accounts(
            auth, _Settings(dry_run=True), [("a@source.example", "a@target.example")])

        assert auth.directory_calls == []

    def test_the_escape_hatch_disables_it_entirely(self, monkeypatch):
        """A tenant provisioning through its own IdP wants a gap in
        identity_map to fail loudly, not get an account created for it."""
        monkeypatch.setattr(provision, "ensure_users",
                           lambda *a, **k: (_ for _ in ()).throw(
                               AssertionError("should never be called")))
        auth = _FakeAuth()

        main._ensure_target_accounts(
            auth, _Settings(auto_provision_users=False),
            [("a@source.example", "a@target.example")])

        assert auth.directory_calls == []

    def test_addresses_outside_the_target_domain_are_never_auto_created(self, monkeypatch):
        """A typo or a stray consolidation mapping must not create an
        account in a domain nobody asked this run to provision."""
        seen: list[str] = []
        monkeypatch.setattr(
            provision, "ensure_users",
            lambda directory, emails, dry_run=False: (seen.extend(emails)
                                                       or {"created": [], "existing": [], "failed": []}))
        auth = _FakeAuth()

        main._ensure_target_accounts(
            auth, _Settings(), [("a@source.example", "a@somewhere-else.example")])

        assert seen == []
        assert auth.directory_calls == []

    def test_a_provisioning_failure_does_not_cancel_the_run(self, monkeypatch):
        """run_batch must still migrate whatever DOES have a real account;
        this is exercised at the run_batch level, not just the helper."""
        class FakeDB:
            def all_identities(self):
                return [{"entity_type": "user", "source_email": "a@source.example",
                        "target_email": "a@target.example", "status": "PENDING"}]

            def services_done(self, u):
                return set()

        monkeypatch.setattr(main, "_coordination_enabled", lambda: False)
        monkeypatch.setattr(main, "_warn_if_ledger_is_stale", lambda *a, **k: None)

        def boom(auth, settings, pairs):
            raise RuntimeError("directory API is down")

        monkeypatch.setattr(main, "_ensure_target_accounts", boom)

        migrated: list[str] = []

        def fake_migrate(auth, db, settings, s, t, services, delta, days):
            migrated.append(s)
            return {"source": s, "status": "DONE"}

        monkeypatch.setattr(main, "migrate_user", fake_migrate)

        class S:
            user_workers = 4
            account_id = 7

        out = main.run_batch(None, FakeDB(), S(), {"gmail"},
                             delta=False, delta_days=0)

        assert migrated == ["a@source.example"]
        assert len(out) == 1
