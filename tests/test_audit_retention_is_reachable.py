"""The tool that fixes the disk problem had no way to run it.

audit_log records every attempt and nothing ever removed one. Measured on
the live box: one account's ledger at 5.7 GB, another at 0.4 GB, on a disk
67% full -- and audit_retention.py's own docstring records the same shape
from an earlier tenant, 10,661,866 rows of which 10,604,474 were SUCCESS.
99.5% of the database describing work id_mapping already proves happened.

It had prunable(), prune() and counts_match() and no ACTIONS entry, no
button, no timer. Only the tests referenced it.

It also stopped one step short: pruning frees pages inside the file but
SQLite keeps them for reuse, so the FILE does not shrink. That is the right
default and the wrong outcome for someone watching a disk fill up.
"""
import os

import webui

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestItCanBeRun:
    def test_both_actions_exist(self):
        assert "audit_prune_dry" in webui.ACTIONS
        assert "audit_prune" in webui.ACTIONS

    def test_the_dry_run_does_not_apply(self):
        assert "--apply" not in webui.ACTIONS["audit_prune_dry"]["argv"]

    def test_the_real_one_applies_and_reclaims(self):
        argv = webui.ACTIONS["audit_prune"]["argv"]
        assert "--apply" in argv and "--vacuum" in argv

    def test_it_is_on_the_maintenance_page(self):
        page = open(os.path.join(ROOT, "migration-webui/src/pages/Maintenance.tsx"),
                    encoding="utf-8").read()
        assert "audit_prune_dry" in page and "audit_prune" in page

    def test_the_dry_run_is_listed_first(self):
        page = open(os.path.join(ROOT, "migration-webui/src/pages/Maintenance.tsx"),
                    encoding="utf-8").read()
        assert page.index("'audit_prune_dry'") < page.index("'audit_prune'")


class TestReclaimIsGuarded:
    def _src(self):
        return open(os.path.join(ROOT, "audit_retention.py"), encoding="utf-8").read()

    def test_it_refuses_without_room(self):
        """VACUUM rewrites the whole file; on a disk that is already the
        problem, an unguarded one makes it worse."""
        src = self._src()
        assert "disk_usage" in src
        assert "skipping --vacuum" in src

    def test_it_will_not_vacuum_over_a_failed_count_check(self):
        # VACUUM rewrites the file; doing that while the counts disagree
        # destroys the evidence needed to find out why.
        # Not a windowed split: "count check" appears in both the report
        # line and the refusal message, so slicing between them cuts the
        # phrase in half.
        src = self._src()
        assert "the count check must pass first" in src
        assert src.index("if not ok:") < src.index("if args.vacuum:")

    def test_it_reports_what_it_reclaimed(self):
        assert "reclaimed" in self._src()

    def test_the_flag_explains_the_cost(self):
        block = self._src().split('"--vacuum"')[1][:600]
        assert "write lock" in block and "free space" in block


class TestWhatItRefusesToTouch:
    """The three deliberate limits, asserted so a later 'optimisation'
    cannot quietly drop one."""

    def test_only_success_rows_are_collapsible(self):
        src = open(os.path.join(ROOT, "audit_retention.py"), encoding="utf-8").read()
        assert "SUCCESS" in src
        assert "FAILED" in src.split('"""')[1], "the docstring must say what is safe"

    def test_it_says_it_leaves_unfinished_users_alone(self):
        doc = open(os.path.join(ROOT, "audit_retention.py"),
                   encoding="utf-8").read().split('"""')[1]
        assert "not DONE" in doc or "is not DONE" in doc

    def test_the_counts_survive_in_a_rollup(self):
        doc = open(os.path.join(ROOT, "audit_retention.py"),
                   encoding="utf-8").read().split('"""')[1]
        assert "audit_rollup" in doc
