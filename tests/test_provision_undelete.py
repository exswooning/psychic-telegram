"""A full domain, and the missing accounts are sitting in the recycle bin.

Deleting a Workspace user does not release its place in the domain user
limit -- Google holds it, restorable, for 20 days. So a tenant that has been
wiped and re-provisioned is refused new accounts while the very names it
wants are still there, occupying the slots being denied.

Live: three wipes of a 200-user tenant left 600 held deletions,
re-provisioning died at 172 of 201 with "Domain user limit reached", and
every one of the 29 missing accounts restored without consuming a new slot.
"""
from googleapiclient.errors import HttpError

import provision


class _Resp(dict):
    def __init__(self, status):
        super().__init__(status=status)
        self.status = status
        self.reason = "fake"


def _err(status, message):
    return HttpError(_Resp(status),
                     ('{"error":{"code":%d,"message":"%s"}}' % (status, message)).encode())


DOMAIN_FULL = "Domain user limit reached. Start paid subscription."


class FakeDirectory:
    def __init__(self, live=(), deleted=(), full=True):
        self.live = list(live)
        self.deleted = [{"primaryEmail": e, "id": f"id-{e}"} for e in deleted]
        self.full = full
        self.undeleted = []
        self.inserted = []

    def users(self):
        return self

    def get(self, userKey, **kw):
        outer = self

        class _R:
            def execute(self):
                if userKey in outer.live:
                    return {"primaryEmail": userKey}
                raise _err(404, "Resource Not Found")
        return _R()

    def list(self, showDeleted=False, **kw):
        outer = self

        class _R:
            def execute(self):
                return {"users": outer.deleted if showDeleted else
                        [{"primaryEmail": e} for e in outer.live]}
        return _R()

    def insert(self, body, **kw):
        outer = self

        class _R:
            def execute(self):
                if outer.full:
                    raise _err(412, DOMAIN_FULL)
                outer.inserted.append(body["primaryEmail"])
                return body
        return _R()

    def undelete(self, userKey, body=None, **kw):
        outer = self

        class _R:
            def execute(self):
                outer.undeleted.append(userKey)
                return {}
        return _R()


class TestRestoringInsteadOfFailing:
    def test_a_full_domain_restores_the_deleted_account(self):
        d = FakeDirectory(deleted=["alice@t.com"])
        res = provision.ensure_users(d, ["alice@t.com"])
        assert d.undeleted == ["id-alice@t.com"]
        assert res["failed"] == []
        assert res["created"][0][0] == "alice@t.com"

    def test_a_full_domain_with_nothing_to_restore_still_fails(self):
        """Restoring is a recovery, not a way to hide a real capacity wall."""
        d = FakeDirectory(deleted=[])
        res = provision.ensure_users(d, ["bob@t.com"])
        assert d.undeleted == []
        assert res["failed"] and "Domain user limit" in res["failed"][0][1]

    def test_only_the_matching_name_is_restored(self):
        """Restoring some other deleted account would create a user nobody
        asked for and consume the slot the real one needs."""
        d = FakeDirectory(deleted=["someone-else@t.com"])
        res = provision.ensure_users(d, ["alice@t.com"])
        assert d.undeleted == []
        assert res["failed"]

    def test_an_unrelated_failure_is_not_treated_as_a_full_domain(self):
        class Broken(FakeDirectory):
            def insert(self, body, **kw):
                class _R:
                    def execute(self):
                        raise _err(403, "insufficientPermissions")
                return _R()

        d = Broken(deleted=["alice@t.com"])
        res = provision.ensure_users(d, ["alice@t.com"])
        assert d.undeleted == []
        assert res["failed"]

    def test_a_failed_restore_reports_the_original_error(self):
        """The create error is the more accurate thing to show."""
        class BadUndelete(FakeDirectory):
            def undelete(self, userKey, body=None, **kw):
                class _R:
                    def execute(self):
                        raise _err(500, "backend error")
                return _R()

        d = BadUndelete(deleted=["alice@t.com"])
        res = provision.ensure_users(d, ["alice@t.com"])
        assert res["failed"] and "Domain user limit" in res["failed"][0][1]

    def test_an_existing_live_user_is_untouched(self):
        d = FakeDirectory(live=["alice@t.com"], deleted=["alice@t.com"])
        res = provision.ensure_users(d, ["alice@t.com"])
        assert res["existing"] == ["alice@t.com"]
        assert d.undeleted == [] and d.inserted == []


class TestTheDomainFullMatcher:
    def test_it_recognises_the_message(self):
        assert provision._is_domain_full(_err(412, DOMAIN_FULL)) is True

    def test_it_recognises_the_reason_code(self):
        assert provision._is_domain_full(_err(412, "limitExceeded")) is True

    def test_an_ordinary_error_is_not_matched(self):
        assert provision._is_domain_full(_err(403, "forbidden")) is False
