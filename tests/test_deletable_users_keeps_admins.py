"""
tests/test_deletable_users_keeps_admins.py
==========================================
"Delete all users except the admin" is one API call away from locking
everyone out of a tenant permanently. A deleted Workspace address stays
reserved for 20 days, so a super-admin removed by mistake cannot simply be
recreated -- there is no cheap undo, and no remaining credential to undo it
with.

The original guard skipped one address: whoever was driving the run. That
covers the operator deleting themselves mid-run, and nothing else. A
tenant's break-glass admin is routinely not the account automation signs in
as.
"""

from __future__ import annotations

import pytest

import wipe_target


class FakeDirectory:
    def __init__(self, users, pages=1):
        self._users = users
        self._pages = pages
        self.fields_asked = ""

    def users(self):
        return self

    def list(self, **kw):
        self.fields_asked = kw.get("fields", "")
        self._page_token = kw.get("pageToken")
        return self

    def execute(self):
        return {"users": self._users}


def u(email, admin=False, delegated=False):
    return {"primaryEmail": email, "isAdmin": admin,
            "isDelegatedAdmin": delegated}


DOMAIN = "acme.com"


class TestAdminsSurvive:
    def test_the_driving_admin_is_kept(self):
        d = FakeDirectory([u("admin@acme.com", admin=True), u("bob@acme.com")])
        assert wipe_target.deletable_users(d, "admin@acme.com", DOMAIN) \
            == ["bob@acme.com"]

    def test_another_super_admin_is_kept_too(self):
        """The break-glass account. It is not the one automation signs in
        as, and it is the one someone reaches for when that key fails."""
        d = FakeDirectory([u("driver@acme.com"), u("breakglass@acme.com", admin=True),
                           u("bob@acme.com")])
        out = wipe_target.deletable_users(d, "driver@acme.com", DOMAIN)
        assert "breakglass@acme.com" not in out
        assert out == ["bob@acme.com"]

    def test_a_delegated_admin_is_kept(self):
        d = FakeDirectory([u("helpdesk@acme.com", delegated=True), u("bob@acme.com")])
        out = wipe_target.deletable_users(d, "driver@acme.com", DOMAIN)
        assert out == ["bob@acme.com"]

    def test_it_asks_the_api_for_the_admin_flags(self):
        """Without them every user comes back looking like an ordinary one,
        and the guard silently protects nobody but the driver."""
        d = FakeDirectory([u("bob@acme.com")])
        wipe_target.deletable_users(d, "driver@acme.com", DOMAIN)
        assert "isAdmin" in d.fields_asked
        assert "isDelegatedAdmin" in d.fields_asked


class TestItStillDeletesWhatItShould:
    def test_ordinary_users_go(self):
        d = FakeDirectory([u("a@acme.com"), u("b@acme.com"), u("c@acme.com")])
        assert wipe_target.deletable_users(d, "admin@acme.com", DOMAIN) \
            == ["a@acme.com", "b@acme.com", "c@acme.com"]

    def test_other_domains_are_untouched(self):
        """Aiming a wipe at one domain must not reach a second one hosted in
        the same tenant."""
        d = FakeDirectory([u("a@acme.com"), u("x@other.com")])
        assert wipe_target.deletable_users(d, "admin@acme.com", DOMAIN) \
            == ["a@acme.com"]

    def test_case_does_not_smuggle_an_admin_through(self):
        d = FakeDirectory([u("Admin@Acme.com", admin=True), u("Bob@ACME.com")])
        out = wipe_target.deletable_users(d, "ADMIN@ACME.COM", DOMAIN)
        assert out == ["bob@acme.com"]


class TestItReportsWhoItSpared:
    def test_the_kept_list_is_filled(self):
        """A count that does not add up invites the reader to guess. Name
        them."""
        kept: list[str] = []
        d = FakeDirectory([u("admin@acme.com", admin=True),
                           u("second@acme.com", admin=True), u("bob@acme.com")])
        wipe_target.deletable_users(d, "admin@acme.com", DOMAIN, kept=kept)
        assert sorted(kept) == ["admin@acme.com", "second@acme.com"]

    def test_omitting_it_is_still_fine(self):
        d = FakeDirectory([u("admin@acme.com", admin=True)])
        assert wipe_target.deletable_users(d, "admin@acme.com", DOMAIN) == []
