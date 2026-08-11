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


class TestStagingDrivesAreNotCoverage:
    """
    server_side mode creates a MIGRATION-STAGING-<user> shared drive in the
    target and adds the *source* user as an organizer, so it appears in that
    user's drives().list(). Teardown deliberately refuses to delete one that
    still holds files, so an interrupted run leaves it there for good.

    Counted naively, the first live coverage run reported "Shared Drives:
    2 COVERED" for a tenant whose only shared drives were the migrator's own
    litter. Claiming a path is exercised when it is not is the single most
    damaging thing this report can do -- an absent category is a to-do, a
    falsely-covered one is a decision made on bad evidence.
    """

    class _Auth:
        def __init__(self, names):
            self.names = names

        def source_drive(self, user):
            outer = self

            class _D:
                def drives(self):
                    return self

                def list(self, **kw):
                    return self

                def execute(self):
                    return {"drives": [{"id": n, "name": n} for n in outer.names]}
            return _D()

    class _Settings:
        staging_drive_prefix = "MIGRATION-STAGING"

    def test_staging_drives_do_not_count(self):
        ids = cov._shared_drive_ids(
            self._Auth(["MIGRATION-STAGING-alice"]), self._Settings(), "alice@s")
        assert ids == set()

    def test_real_shared_drives_still_count(self):
        ids = cov._shared_drive_ids(
            self._Auth(["MIGRATION-STAGING-alice", "Engineering", "Legal"]),
            self._Settings(), "alice@s")
        assert ids == {"Engineering", "Legal"}

    def test_a_drive_seen_by_many_members_is_counted_once(self):
        """A shared drive is a TENANT-level object -- every member's
        drives().list() returns the same one. Summing per user reported the
        2 seeded drives as 10 (2 drives x 5 members), which is the same
        false-precision failure as counting staging drives as coverage:
        the verdict was right and the number was fiction.
        """
        auth = self._Auth(["Engineering", "Legal"])
        seen: set[str] = set()
        for user in ("alice@s", "bob@s", "carol@s", "dave@s", "erin@s"):
            seen |= cov._shared_drive_ids(auth, self._Settings(), user)
        assert len(seen) == 2


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


class TestExternalSharedWithMeIsARisk:
    """
    The one row on this report that is a live data-loss risk rather than a
    coverage gap.

    A file shared into a user by an owner OUTSIDE the org has no owner
    inside the tenant, so no user's migration carries it. With
    MIGRATE_EXTERNAL_SHARES off -- the default -- it is dropped, not
    deferred. A colleague-owned shared file is the opposite case and is
    correctly skipped: its owner migrates it and the ACL translation
    restores access.
    """

    def test_a_nonzero_count_with_the_flag_off_is_called_out(self):
        t = _totals(external_shared_with_me=7, migrate_external_shares=False)
        out = cov.render(cov.assess(t), t)
        assert "DATA LOSS RISK" in out
        assert "7 file(s)" in out
        assert "MIGRATE_EXTERNAL_SHARES" in out

    def test_the_flag_being_on_is_not_a_risk(self):
        t = _totals(external_shared_with_me=7, migrate_external_shares=True)
        assert "DATA LOSS RISK" not in cov.render(cov.assess(t), t)

    def test_zero_is_not_a_risk(self):
        t = _totals(external_shared_with_me=0, migrate_external_shares=False)
        assert "DATA LOSS RISK" not in cov.render(cov.assess(t), t)

    def test_colleague_owned_files_are_not_counted(self):
        """Same-domain owners must not inflate this. Counting them would
        turn a correct design decision into a false alarm on every tenant."""
        class _Auth:
            def source_drive(self, user):
                class _D:
                    def files(self): return self
                    def list(self, **kw): return self
                    def execute(self):
                        return {"files": [
                            {"owners": [{"emailAddress": "colleague@src.com"}]},
                            {"owners": [{"emailAddress": "partner@other.com"}]},
                        ]}
                return _D()

        class _S:
            source_domain = "src.com"

        assert cov._count_external_shared_with_me(_Auth(), _S(), "a@src.com") == 1
