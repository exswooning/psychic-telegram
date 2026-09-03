"""
tests/test_seed_user_scoping.py
===============================
The seed endpoint can seed a few named users, not only the whole tenant.

seed_sandbox.py has always taken --users. The HTTP endpoint did not pass it,
so the only seed reachable from the product was every user the tenant has --
200 here, days rather than hours. That made "seed, then check the change I
just made" impossible to do through the UI, which is the one workflow the UI
exists for. A bounded seed is the common case, not the exotic one.

The localpart guard matters more than it looks: --users takes localparts, and
an address passed instead matches no account, so the seeder does a live
Directory lookup, finds nothing, and seeds silently nothing. Refusing it here
turns a confusing empty run into a sentence.
"""

from __future__ import annotations

import pytest

import webui
from config import Settings


@pytest.fixture
def domain():
    return Settings().source_domain


class TestNamingUsers:
    def test_named_users_reach_the_seeder(self, domain):
        argv, _, err = webui.seed_argv(
            {"confirm_domain": domain, "users": "george,ivan"})
        assert not err
        assert "--users" in argv
        assert argv[argv.index("--users") + 1] == "george,ivan"

    def test_blank_still_means_the_whole_tenant(self, domain):
        """The previous behaviour has to survive -- a rehearsal wants it."""
        argv, _, err = webui.seed_argv({"confirm_domain": domain, "users": ""})
        assert not err and "--users" not in argv

    def test_absent_is_the_same_as_blank(self, domain):
        argv, _, err = webui.seed_argv({"confirm_domain": domain})
        assert not err and "--users" not in argv

    def test_whitespace_only_is_not_a_user_list(self, domain):
        argv, _, err = webui.seed_argv({"confirm_domain": domain, "users": "   "})
        assert not err and "--users" not in argv


class TestTheLocalpartGuard:
    def test_an_address_is_refused_with_a_reason(self, domain):
        """Not cosmetic: an address seeds nothing, silently, after a live
        Directory lookup that finds no such localpart."""
        argv, _, err = webui.seed_argv(
            {"confirm_domain": domain, "users": f"george@{domain}"})
        assert argv == [] and "localparts, not addresses" in err
        assert f"george@{domain}" in err

    def test_it_names_every_offender_not_just_the_first(self, domain):
        _, _, err = webui.seed_argv(
            {"confirm_domain": domain,
             "users": f"ok, bad@{domain}, worse@{domain}"})
        assert f"bad@{domain}" in err and f"worse@{domain}" in err

    def test_a_good_list_alongside_a_bad_one_still_refuses(self, domain):
        argv, _, err = webui.seed_argv(
            {"confirm_domain": domain, "users": f"george, ivan@{domain}"})
        assert argv == [] and err


class TestItComposesWithTheOtherOptions:
    def test_shared_drives_and_users_together(self, domain):
        argv, _, err = webui.seed_argv({
            "confirm_domain": domain, "users": "george", "shared_drives": "2"})
        assert not err
        assert "--users" in argv and "--shared-drives" in argv

    def test_the_domain_gate_still_applies(self):
        """The typed-domain confirmation is what makes this safe to expose at
        all; naming users must not become a way around it."""
        argv, _, err = webui.seed_argv(
            {"confirm_domain": "not-the-domain.example", "users": "george"})
        assert argv == [] and err
