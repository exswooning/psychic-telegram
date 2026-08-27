""""Reset first" + "Start seeding" deleted everything and seeded nothing.

seed_sandbox.py's --reset branch returns 0 before the seed block -- it is
reset-ONLY. The checkbox said "Reset first" and the button said "Start
seeding", so the obvious reading was "reset, then seed". Pressed live
against 201 users, it wiped the tenant and stopped, silently.

The silence was the second half: the reset used pool.map, which yields in
SUBMISSION order, so one slow first user held back every completion line.
Fifteen minutes of no output while working normally is indistinguishable
from a wedged job.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _seeder():
    return open(os.path.join(ROOT, "data-generator", "seed_sandbox.py"),
                encoding="utf-8").read()


def _reset_branch():
    src = _seeder()
    return src.split("# --- Reset ---")[1].split("# --- Seed ---")[0]


class TestTheResetReportsProgressAsItHappens:
    def test_it_does_not_use_submission_ordered_map(self):
        assert "pool.map(lambda u: reset_one_user" not in _reset_branch()

    def test_it_prints_as_each_user_finishes(self):
        b = _reset_branch()
        assert "as_completed" in b

    def test_it_counts_toward_a_total(self):
        # "[7/201]" is what separates slow from stuck.
        assert "len(all_users)" in _reset_branch()

    def test_one_failure_does_not_abandon_the_rest(self):
        b = _reset_branch()
        assert "fut.result()" in b and "continue" in b


class TestItSaysItDoesNotSeed:
    def test_the_script_says_so_before_deleting(self):
        b = _reset_branch()
        assert "does NOT seed" in b

    def test_the_reset_branch_still_returns_without_seeding(self):
        # Documenting the behaviour, not changing it: reset-only is useful.
        # If this ever stops being true, the wording above must change too.
        assert "return 0" in _reset_branch()

    def test_the_button_stops_claiming_to_seed(self):
        page = open(os.path.join(ROOT, "migration-webui", "src", "pages",
                                 "SeedWizard.tsx"), encoding="utf-8").read()
        assert "Delete seeded data" in page
        assert "Reset only (deletes, does not seed)" in page
