"""
tests/test_sso.py
=================
SSO is the one part of this tool that can lock an admin out of the tenant
they are migrating into: an assignment sends users to an IdP, and if that IdP
has not been told about the new tenant yet, nobody can sign in -- including
whoever is running the migration.

So these tests are mostly about what the module *refuses* to do.
"""

from __future__ import annotations

import pytest

from db import MigrationDB


@pytest.fixture
def ledger(tmp_path):
    db = MigrationDB(str(tmp_path / "m.db"))
    db.init_schema()
    return db


@pytest.fixture
def mig(ledger, settings):
    import sso

    settings.migrate_sso = True
    return sso.SSOMigrator(auth=None, db=ledger, settings=settings)


class TestScopes:
    def test_source_is_read_only_and_target_can_write(self, settings):
        """The source tenant's login configuration is the last thing a
        migration should be able to edit."""
        from config import SSO_READONLY_SCOPE, SSO_WRITE_SCOPE
        from config import source_scopes, target_scopes

        settings.migrate_sso = True
        assert SSO_READONLY_SCOPE in source_scopes(settings)
        assert SSO_WRITE_SCOPE not in source_scopes(settings)
        assert SSO_WRITE_SCOPE in target_scopes(settings)

    def test_nothing_is_requested_when_sso_is_off(self, settings):
        from config import SSO_READONLY_SCOPE, SSO_WRITE_SCOPE
        from config import source_scopes, target_scopes

        settings.migrate_sso = False
        assert SSO_READONLY_SCOPE not in source_scopes(settings)
        assert SSO_WRITE_SCOPE not in target_scopes(settings)


class TestAssignmentSafety:
    """The lockout cases."""

    def test_a_tenant_wide_assignment_is_refused_by_default(self, mig, ledger):
        mig.read_assignments = lambda tenant: [{
            "name": "assignments/1", "rank": 0,
            "samlSsoInfo": {"inboundSamlSsoProfile": "profiles/src"},
        }]
        mig.migrate_assignments({"profiles/src": "profiles/tgt"},
                                force_tenant_wide=False)

        rows = ledger.conn.execute(
            "SELECT status, error_message FROM audit_log "
            "WHERE item_type='sso_assignment'").fetchall()
        assert rows[0]["status"] == "SKIPPED_TENANT_WIDE"
        assert mig.stats["assignments"] == 0

    def test_an_assignment_for_an_unmigrated_profile_is_skipped(self, mig, ledger):
        """It would otherwise point at a profile that does not exist, which
        fails open -- users sent to a login that was never configured."""
        mig.read_assignments = lambda tenant: [{
            "name": "assignments/1",
            "samlSsoInfo": {"inboundSamlSsoProfile": "profiles/never-copied"},
        }]
        mig.migrate_assignments({}, force_tenant_wide=True)

        row = ledger.conn.execute(
            "SELECT status FROM audit_log "
            "WHERE item_type='sso_assignment'").fetchone()
        assert row["status"] == "SKIPPED_NO_PROFILE"

    def test_an_org_unit_missing_on_the_target_is_recorded_not_guessed(
            self, mig, ledger):
        """Ids are tenant-local. Copying one across produces an assignment
        pointing at nothing."""
        mig.read_assignments = lambda tenant: [{
            "name": "assignments/1", "targetOrgUnit": "orgUnits/03ph8a2z",
            "samlSsoInfo": {"inboundSamlSsoProfile": "profiles/src"},
        }]
        mig._org_unit_path = lambda tenant, resource: "/Engineering"
        mig._org_unit_by_path = lambda tenant, path: None   # absent on target
        mig.migrate_assignments({"profiles/src": "profiles/tgt"},
                                force_tenant_wide=True)

        row = ledger.conn.execute(
            "SELECT status, error_message FROM audit_log "
            "WHERE item_type='sso_assignment'").fetchone()
        assert row["status"] == "SKIPPED_UNMAPPED"
        assert "/Engineering" in row["error_message"]

    def test_a_group_is_remapped_by_email_not_by_id(self, mig, settings):
        """The localpart carries across; the id does not."""
        settings.target_domain = "b.example.com"
        mig._group_email = lambda tenant, resource: "eng@a.example.com"
        mig._group_exists = lambda tenant, email: True

        scope, resolved = mig._remap_target({"targetGroup": "groups/xyz"})

        assert scope == "group"
        assert resolved == "eng@b.example.com"


class TestProfiles:
    def test_an_existing_profile_is_not_duplicated(self, mig, ledger):
        """A resumed run must not leave two profiles with one name and no way
        to say which is authoritative."""
        mig.read_profiles = lambda tenant: (
            [{"name": "profiles/src", "displayName": "Okta"}] if tenant == "source"
            else [{"name": "profiles/tgt", "displayName": "Okta"}])

        mapping = mig.migrate_profiles()

        assert mapping == {"profiles/src": "profiles/tgt"}
        assert mig.stats["profiles"] == 0
        assert mig.stats["skipped"] == 1
