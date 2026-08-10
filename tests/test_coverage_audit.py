"""
tests/test_coverage_audit.py
============================
The join between "what the engine supports" and "what this tenant has".

The interesting failure mode is not a wrong count, it is a category that
quietly stops being checked. A probe keyed by a scope row's wording becomes
dead the moment that wording is edited, and the report gets *shorter* rather
than wrong -- so the tenant looks better covered than it is. That is the
same defect as a benchmark judge passing a run which migrated nothing, and
it is what test_every_probe_key_exists_in_the_scope_matrix exists to stop.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import coverage_audit as cov  # noqa: E402
import scope                  # noqa: E402


def _totals(**over) -> dict:
    base = {
        "drive": {"kinds": {"folders": 10, "binaries": 20, "documents": 5,
                            "shortcuts": 1, "forms": 0, "apps_script": 0},
                  "shared_internal": 3, "shared_domain": 2,
                  "shared_externally": 0, "shared_with_anyone": 4},
        "gmail": {"messages": 100, "labels": 5, "drafts": 2},
        "calendar": {"calendars": 3, "events": 40},
        "shared_drives": 0,
        "contacts": None,
        "tasks": None,
        "chat": None,
        "perUser": {},
        "errors": {},
    }
    base.update(over)
    return base


class TestProbeKeysStayWiredUp:
    def test_every_probe_key_exists_in_the_scope_matrix(self):
        """A probe keyed to wording that no longer exists is a silent hole:
        its category degrades to UNPROBED and the report just looks thinner.
        Four keys were already wrong when this was first written."""
        known = {i.item for i in scope.filter_scope()}
        stale = sorted(k for k in cov.PROBES if k not in known)
        assert not stale, f"probe keys no longer in scope.py: {stale}"

    def test_no_probe_targets_a_none_row(self):
        """NONE means the engine does not migrate it, so counting it on the
        source would imply a coverage obligation that does not exist."""
        none_items = {i.item for i in scope.filter_scope()
                      if i.status == scope.NONE}
        assert not (set(cov.PROBES) & none_items)


class TestVerdicts:
    def test_a_supported_category_with_data_is_covered(self):
        rows = {r["item"]: r for r in cov.assess(_totals())}
        assert rows["Folder hierarchy (full depth)"]["verdict"] == cov.COVERED
        assert rows["Folder hierarchy (full depth)"]["count"] == 10

    def test_a_supported_category_with_no_data_is_absent(self):
        """The finding this module was built for: this tenant's seeder tried
        to create external grants, Google refused, and nothing noticed."""
        rows = {r["item"]: r for r in cov.assess(_totals())}
        ext = rows["External collaborator ACLs"]
        assert ext["verdict"] == cov.ABSENT
        assert ext["count"] == 0

    def test_unmigrated_categories_are_not_reported_as_gaps(self):
        rows = {r["item"]: r for r in cov.assess(_totals())}
        assert rows["Revision / version history"]["verdict"] == cov.NA

    def test_an_unmeasurable_category_is_unprobed_not_absent(self):
        """`contacts: None` means the People scope was not granted, so the
        question was never asked. Calling that ABSENT would send an operator
        to seed contacts when the real fix is a scope grant."""
        rows = {r["item"]: r for r in cov.assess(_totals())}
        assert rows["Google Contacts (personal)"]["verdict"] == cov.UNPROBED
        assert rows["Google Contacts (personal)"]["count"] is None

    def test_zero_is_absent_but_none_is_unprobed(self):
        """The distinction the whole module rests on."""
        rows = {r["item"]: r for r in cov.assess(_totals(tasks=0))}
        assert rows["Google Tasks"]["verdict"] == cov.ABSENT
        rows = {r["item"]: r for r in cov.assess(_totals(tasks=None))}
        assert rows["Google Tasks"]["verdict"] == cov.UNPROBED

    def test_secondary_calendars_exclude_the_primary(self):
        """calendarList always returns the primary, so a user with only a
        primary calendar has zero secondaries -- not one."""
        rows = {r["item"]: r for r in
                cov.assess(_totals(calendar={"calendars": 1, "events": 5}))}
        assert rows["Secondary calendars owned by the user"]["count"] == 0
        assert rows["Secondary calendars owned by the user"]["verdict"] == cov.ABSENT


class TestExitCode:
    def test_render_names_the_absent_categories(self):
        out = cov.render(cov.assess(_totals()), _totals())
        assert "ABSENT" in out
        assert "External collaborator ACLs" in out

    def test_unscannable_user_is_reported_not_silently_dropped(self):
        """A source account that cannot be read has unmeasured data, not
        zero data. This tenant has exactly one such account."""
        t = _totals(errors={"3@src": "invalid_grant: Invalid email or User ID"})
        out = cov.render(cov.assess(t), t)
        assert "COULD NOT SCAN" in out
        assert "3@src" in out
