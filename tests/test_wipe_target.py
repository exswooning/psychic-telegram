"""Deleting a Workspace user does not return its slot.

Google keeps a deleted user restorable for 20 days, and it goes on consuming
a place in the domain user limit for that entire time. So a wipe borrows
against the next 20 days of capacity rather than freeing any, and each cycle
borrows again.

Measured the hard way: three wipes of a 200-user tenant left 600 deleted
accounts plus 172 live ones against the limit. Re-provisioning died at 172
with "Domain user limit reached. Start paid subscription.", leaving 29 users
missing and no way to create them until the oldest deletions aged out.
"""
import wipe_target


class FakeDirectory:
    def __init__(self, live=(), deleted=()):
        self.live = list(live)
        self.deleted = list(deleted)

    def users(self):
        return self

    def list(self, showDeleted=False, **kw):
        rows = self.deleted if showDeleted else self.live
        outer = self

        class _R:
            def execute(self):
                return {"users": [{"primaryEmail": e} for e in rows]}
        return _R()


class TestCountingWhatIsStillHeld:
    def test_it_counts_deleted_accounts(self):
        d = FakeDirectory(deleted=[f"u{i}@t" for i in range(600)])
        assert wipe_target.deleted_user_count(d) == 600

    def test_a_clean_tenant_holds_none(self):
        assert wipe_target.deleted_user_count(FakeDirectory()) == 0


class TestTheAdminIsNeverDeleted:
    def test_the_operators_own_account_is_excluded(self):
        """A run that removes its own credentials cannot finish or be undone."""
        d = FakeDirectory(live=["admin@t.com", "alice@t.com", "bob@t.com"])
        got = wipe_target.deletable_users(d, "admin@t.com", "t.com")
        assert got == ["alice@t.com", "bob@t.com"]

    def test_it_matches_the_admin_case_insensitively(self):
        d = FakeDirectory(live=["Admin@T.com", "alice@t.com"])
        assert wipe_target.deletable_users(d, "admin@t.com", "t.com") == [
            "alice@t.com"]

    def test_other_domains_are_left_alone(self):
        """Only the target domain. A tenant can hold aliases and accounts on
        domains this migration has no business touching."""
        d = FakeDirectory(live=["alice@t.com", "carol@other.com"])
        assert wipe_target.deletable_users(d, "admin@t.com", "t.com") == [
            "alice@t.com"]


class TestOneFailureDoesNotStrandTheRest:
    def test_a_single_undeletable_account_is_reported_not_raised(self):
        """Half a wiped tenant is a worse state than either finishing or not
        starting."""
        class Flaky(FakeDirectory):
            def delete(self, userKey):
                class _R:
                    def execute(self):
                        if userKey == "bad@t.com":
                            raise RuntimeError("403 forbidden")
                        return {}
                return _R()

        d = Flaky()
        stats = wipe_target.delete_users(
            d, ["a@t.com", "bad@t.com", "c@t.com"], dry_run=False)
        assert stats["deleted"] == 2
        assert stats["failed"] == 1
        assert "bad@t.com" in stats["errors"][0]

    def test_a_dry_run_deletes_nothing(self):
        calls = []

        class Counting(FakeDirectory):
            def delete(self, userKey):
                calls.append(userKey)

                class _R:
                    def execute(self):
                        return {}
                return _R()

        stats = wipe_target.delete_users(Counting(), ["a@t.com"], dry_run=True)
        assert calls == []
        assert stats["deleted"] == 1
