"""A deleted Workspace address is not free for 20 days.

Google holds a deleted user restorable for 20 days and its email stays
taken that whole time. GENERATED_LOCALPARTS is a fixed list, so a
wipe-and-recreate cycle asks for exactly the names it just deleted and
every create fails with "Entity already exists" until they age out.

A prefix also makes a run identifiable afterwards: r2-aiden.kumar28 came
from the second reseed, which the ledger alone cannot tell you.
"""
import sys
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "data-generator"))


@pytest.fixture(autouse=True)
def _clean():
    import seed_sandbox
    before = seed_sandbox.GENERATED_PREFIX
    yield
    seed_sandbox.GENERATED_PREFIX = before


class TestTheSeeder:
    def test_no_prefix_keeps_the_historical_names(self):
        import seed_sandbox
        seed_sandbox.GENERATED_PREFIX = ""
        assert seed_sandbox._generated_localpart(0, set()) == \
            seed_sandbox.GENERATED_LOCALPARTS[0]

    def test_a_prefix_changes_every_name(self):
        import seed_sandbox
        seed_sandbox.GENERATED_PREFIX = "r2-"
        got = seed_sandbox._generated_localpart(0, set())
        assert got.startswith("r2-")
        assert got != seed_sandbox.GENERATED_LOCALPARTS[0]

    def test_collisions_are_still_avoided(self):
        import seed_sandbox
        seed_sandbox.GENERATED_PREFIX = "r2-"
        taken = {"r2-" + seed_sandbox.GENERATED_LOCALPARTS[0]}
        assert seed_sandbox._generated_localpart(0, taken) not in taken

    def test_it_applies_past_the_fixed_list(self):
        import seed_sandbox
        seed_sandbox.GENERATED_PREFIX = "r2-"
        n = len(seed_sandbox.GENERATED_LOCALPARTS) + 5
        assert seed_sandbox._generated_localpart(n, set()).startswith("r2-")


class TestTheEndpointValidatesIt:
    @pytest.fixture(autouse=True)
    def _cfg(self, monkeypatch):
        import config
        real = config.Settings

        def fake(account_id=None, **kw):
            st = real.__new__(real)
            st.source_domain, st.target_domain = "src.example", "tgt.example"
            st.source_admin, st.source_sa_key = "a@src.example", "/k/s.json"
            st.target_admin, st.target_sa_key = "a@tgt.example", "/k/t.json"
            st.db_path, st.account_id = "/tmp/m.db", account_id
            return st

        monkeypatch.setattr(config, "Settings", fake)
        monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)

    def _argv(self, prefix):
        import webui
        return webui.seed_argv({"confirm_domain": "src.example", "scale": "huge",
                                "localpart_prefix": prefix}, 7)

    def test_a_sane_prefix_is_passed(self):
        argv, _, err = self._argv("r2-")
        assert not err
        assert argv[argv.index("--localpart-prefix") + 1] == "r2-"

    def test_blank_is_omitted_entirely(self):
        argv, _, err = self._argv("")
        assert not err and "--localpart-prefix" not in argv

    def test_an_address_breaking_prefix_is_refused(self):
        # It becomes part of an email address; a space or @ makes every
        # create fail in a way that reads like a Google problem.
        for bad in ("has space", "with@at", "sla/sh", "-leading"):
            assert self._argv(bad)[2], f"{bad!r} was accepted"

    def test_it_is_length_capped(self):
        assert self._argv("x" * 40)[2]

    def test_dots_and_dashes_are_fine(self):
        # Real localparts use them.
        assert not self._argv("run2.b-c_d")[2]


class TestTheFormOffersIt:
    def test_the_field_exists(self):
        src = open(os.path.join(ROOT, "migration-webui/src/pages/SeedWizard.tsx"),
                   encoding="utf-8").read()
        assert "seed-form-prefix" in src

    def test_it_says_why_it_is_there(self):
        import re
        src = re.sub(r"\s+", " ", open(
            os.path.join(ROOT, "migration-webui/src/pages/SeedWizard.tsx"),
            encoding="utf-8").read())
        assert "20 days" in src and "Entity already exists" in src


class TestAnEmptyTenantCanBeFilledFromTheForm:
    """After a wipe there is one account left -- the admin.

    "Create users" creates the accounts the tenant already implies, so it
    found one and the run ended "FAILED: 0 of 1 users seeded". The flag
    that generates its own candidates existed in client.ts and the form
    never offered it, so filling an empty tenant was impossible from the
    UI.

    It is also the honest way to meet a domain user limit: deleted accounts
    hold their slots for 20 days, so no count known in advance is right --
    creating until the Directory API refuses is the only thing that is.
    """

    def _src(self):
        return open(os.path.join(ROOT, "migration-webui/src/pages/SeedWizard.tsx"),
                    encoding="utf-8").read()

    def test_the_form_offers_it(self):
        assert "seed-form-until-full" in self._src()

    def test_it_is_wired_to_the_call(self):
        src = self._src()
        assert "allUsers, createUntilFull" in src, (
            "the checkbox exists but the request still sends undefined")

    def test_it_is_off_by_default(self):
        # It creates real accounts; that must be asked for.
        assert "useState(false)" in self._src().split("createUntilFull")[1][:80]

    def test_the_form_says_what_it_is_for(self):
        import re
        src = re.sub(r"\s+", " ", self._src())
        assert "fills an empty tenant" in src

    def test_the_seeder_still_refuses_the_bad_combinations(self):
        """--create-until-full replaces --users/--all-users/--fit-to-licenses
        rather than combining with them."""
        seeder = open(os.path.join(ROOT, "data-generator/seed_sandbox.py"),
                      encoding="utf-8").read()
        assert "--create-until-full replaces --users/--all-users/" in seeder
        assert "--create-until-full requires --create-users." in seeder


class TestThePrefixIsSetBeforeAnyNameIsGenerated:
    """Ordering, not logic, is what broke this.

    _generated_localpart honoured GENERATED_PREFIX correctly and the unit
    tests above passed -- but main() assigned it ~140 lines AFTER
    --create-until-full had already built its candidate list. A run asked
    for "r2-" and created fiona@, george@, hannah@: exactly the names it had
    just deleted, which is the collision the prefix exists to prevent.

    A test that only checks _generated_localpart cannot see this. It has to
    look at the order things happen in main().
    """

    def _main_src(self):
        import inspect

        import seed_sandbox
        return inspect.getsource(seed_sandbox.main)

    def test_the_prefix_is_assigned_before_create_until_full(self):
        src = self._main_src()
        assert src.index("GENERATED_PREFIX = args.localpart_prefix") < \
            src.index("if args.create_until_full:")

    def test_the_prefix_is_assigned_before_candidates_are_built(self):
        src = self._main_src()
        assert src.index("GENERATED_PREFIX = args.localpart_prefix") < \
            src.index("_candidates()")

    def test_it_is_assigned_only_once(self):
        # Two assignments would mean one of them is the stale path again.
        assert self._main_src().count(
            "GENERATED_PREFIX = args.localpart_prefix") == 1

    def test_the_comment_records_why(self):
        src = self._main_src()
        head = src[:src.index("GENERATED_PREFIX = args.localpart_prefix")]
        assert "before any username is generated" in head.lower()
