"""
tests/test_ab_scoping.py
========================
The A/B harness must only destroy what the experiment names.

ab_transfer.py resets the target and clears the ledger before each arm. On
the corpus it was written against that was fine. Against a real tenant it
was not: unscoped, it empties every mailbox on the target and executes

    DELETE FROM audit_log

which on the live account is 1,270,532 rows -- the record of every migration
ever run there. wipe_target goes out of its way to preserve exactly those
rows ("a tool that erases its own history cannot explain afterwards what it
did"), so an experiment on two mailboxes destroying them is not a defensible
trade. The full corpus is also 489k files, roughly six days per arm, which
makes the unscoped run useless as well as destructive.

These tests pin the blast radius, because that is the property whose failure
is unrecoverable.
"""

from __future__ import annotations

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AB = open(os.path.join(ROOT, "ab_transfer.py"), encoding="utf-8").read()
RESET = open(os.path.join(ROOT, "reset_target.py"), encoding="utf-8").read()


class TestTheLedgerDeleteIsScoped:
    @pytest.mark.parametrize("table", ["audit_log", "id_mapping", "label_map"])
    def test_no_unscoped_delete_survives(self, table):
        """An unscoped DELETE here is unrecoverable and silent."""
        bare = re.search(rf'DELETE FROM \{{?t?\}}?{table}\b(?!\s*WHERE)', AB)
        assert bare is None, f"unscoped DELETE FROM {table} in ab_transfer.py"

    def test_the_delete_loop_carries_a_where_clause(self):
        assert "DELETE FROM {t} WHERE {col} IN" in AB

    def test_identity_map_is_only_reopened_for_the_users_under_test(self):
        """Reopening every user marks a finished 200-user migration PENDING."""
        assert "UPDATE identity_map SET status='PENDING'" in AB
        stmt = AB.split("UPDATE identity_map SET status='PENDING'", 1)[1][:120]
        assert "WHERE source_email IN" in stmt

    def test_the_success_counts_are_scoped_too(self):
        """Counting the whole ledger would attribute 1.27M prior successes to
        whichever arm happened to run, which is worse than no number."""
        seg = AB.split("SELECT item_type, COUNT(*) FROM audit_log", 1)[1][:200]
        assert "source_user IN" in seg


class TestTheTargetResetIsScoped:
    def test_reset_target_accepts_a_user_filter(self):
        assert '"--user"' in RESET

    def test_it_refuses_a_user_it_cannot_find(self):
        """Silently resetting nothing -- or everything -- because a name was
        mistyped is the failure that matters here."""
        assert "REFUSING: not in identity_map" in RESET

    def test_ab_passes_its_users_through_to_the_reset(self):
        """If the harness scopes its own SQL but resets the whole tenant, the
        experiment still empties 200 mailboxes."""
        assert 'scoped = [a for u in users_src for a in ("--user", u)]' in AB
        reset_call = AB.split('"reset_target.py"', 1)[1][:200]
        assert "*scoped" in reset_call

    def test_the_migration_arm_is_scoped_as_well(self):
        mig = AB.split('"migrate", "--services", "drive"', 1)[1][:80]
        assert "*scoped" in mig


class TestTheHarnessWarnsAboutTheUnscopedRun:
    def test_the_help_text_states_the_real_cost(self):
        """--user is advice, not a default, so the help has to carry the
        number that makes the advice obvious."""
        assert "489k files" in AB and "per arm" in AB.lower()
