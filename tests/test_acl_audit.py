"""
tests/test_acl_audit.py
=======================
Per-file share-access verification.

phases.py reconciles totals, which catches loss and is blind to substitution:
a target where every file arrived and half are shared with the wrong people
reconciles perfectly. These pin the checks that make the difference.
"""

from __future__ import annotations

import pytest

import acl_audit
from db import MigrationDB, bulk_seed_identities
from tests.conftest import SRC_USER, TGT_USER


@pytest.fixture
def wired(auth, db, settings):
    bulk_seed_identities(db, [(SRC_USER, TGT_USER),
                              ("bob@tenanta.com", "bob@tenantb.com"),
                              ("carol@tenanta.com", "carol@tenantb.com")])
    return auth, db, settings


def _pair(auth, db, perms_src, perms_tgt, name="doc.pdf"):
    """One file on each side with the given grants, paired through the ledger."""
    src = auth.source_drive(SRC_USER)
    tgt = auth.target_drive(TGT_USER)
    sid = src.add_binary(name)
    tid = tgt.add_binary(name)
    src.perms[sid] = [{"id": "o", "type": "user", "role": "owner",
                       "emailAddress": SRC_USER}] + list(perms_src)
    tgt.perms[tid] = [{"id": "o", "type": "user", "role": "owner",
                       "emailAddress": TGT_USER}] + list(perms_tgt)
    db.record_mapping(SRC_USER, sid, tid, "file")
    return sid, tid


def _run(auth, db, settings):
    return acl_audit.audit_user(auth, db, settings, SRC_USER, TGT_USER)


class TestItComparesEffectiveAccess:
    def test_a_translated_grant_counts_as_preserved(self, wired):
        auth, db, settings = wired
        _pair(auth, db,
              [{"type": "user", "role": "writer", "emailAddress": "bob@tenanta.com"}],
              [{"type": "user", "role": "writer", "emailAddress": "bob@tenantb.com"}])

        r = _run(auth, db, settings)

        assert r["grants_matched"] == 1
        assert r["missing_grants"] == 0
        assert r["exact"] == 1

    def test_a_lost_grant_is_reported(self, wired):
        """The case counting cannot see: the file arrived, the sharing did not."""
        auth, db, settings = wired
        _pair(auth, db,
              [{"type": "user", "role": "writer", "emailAddress": "bob@tenanta.com"}],
              [])

        r = _run(auth, db, settings)

        assert r["missing_grants"] == 1
        assert r["exact"] == 0
        assert "bob@tenantb.com" in str(r["detail"])

    def test_a_grant_the_source_never_had_is_reported(self, wired):
        """Sharing more widely than the source is a disclosure, not a rounding
        error, so it fails the audit the same way a loss does."""
        auth, db, settings = wired
        _pair(auth, db, [],
              [{"type": "user", "role": "writer", "emailAddress": "carol@tenantb.com"}])

        r = _run(auth, db, settings)

        assert r["extra_grants"] == 1
        assert r["exact"] == 0

    def test_a_downgraded_role_is_not_a_match(self, wired):
        """writer becoming reader is the kind of change nobody notices until
        somebody cannot edit a document."""
        auth, db, settings = wired
        _pair(auth, db,
              [{"type": "user", "role": "writer", "emailAddress": "bob@tenanta.com"}],
              [{"type": "user", "role": "reader", "emailAddress": "bob@tenantb.com"}])

        r = _run(auth, db, settings)

        assert r["missing_grants"] == 1 and r["extra_grants"] == 1

    def test_anyone_with_the_link_is_compared(self, wired):
        auth, db, settings = wired
        _pair(auth, db, [{"type": "anyone", "role": "reader"}], [])
        r = _run(auth, db, settings)
        assert r["missing_grants"] == 1

    def test_a_domain_grant_is_compared(self, wired):
        auth, db, settings = wired
        _pair(auth, db,
              [{"type": "domain", "role": "reader", "domain": "tenanta.com"}],
              [{"type": "domain", "role": "reader", "domain": "tenanta.com"}])
        r = _run(auth, db, settings)
        assert r["grants_matched"] == 1


class TestItDoesNotManufactureFindings:
    def test_owner_rows_are_ignored_on_both_sides(self, wired):
        """The target file is owned by the target user by construction.
        Reporting that would bury the real findings under one per file."""
        auth, db, settings = wired
        _pair(auth, db, [], [])
        r = _run(auth, db, settings)
        assert r["missing_grants"] == 0 and r["extra_grants"] == 0
        assert r["exact"] == 1

    def test_an_unmapped_grantee_is_not_counted_as_loss(self, wired):
        """No target account exists for them, so this is a provisioning gap.
        Calling it data loss would send someone to re-run a migration that
        cannot fix it."""
        auth, db, settings = wired
        _pair(auth, db,
              [{"type": "user", "role": "reader",
                "emailAddress": "contractor@outside.com"}],
              [])

        r = _run(auth, db, settings)

        assert r["unmapped_grantees"] == 1
        assert r["missing_grants"] == 0

    def test_files_are_paired_through_the_ledger_not_by_name(self, wired):
        """Two files can share a name in different folders; matching on it
        would compare the wrong pair and report confident nonsense."""
        auth, db, settings = wired
        src, tgt = auth.source_drive(SRC_USER), auth.target_drive(TGT_USER)
        s1 = src.add_binary("report.pdf")
        s2 = src.add_binary("report.pdf")
        t1 = tgt.add_binary("report.pdf")
        t2 = tgt.add_binary("report.pdf")
        src.perms[s1] = [{"type": "user", "role": "writer",
                          "emailAddress": "bob@tenanta.com"}]
        tgt.perms[t2] = [{"type": "user", "role": "writer",
                          "emailAddress": "bob@tenantb.com"}]
        db.record_mapping(SRC_USER, s1, t2, "file")     # crossed on purpose
        db.record_mapping(SRC_USER, s2, t1, "file")

        r = _run(auth, db, settings)

        assert r["missing_grants"] == 0, "pairing followed names, not the ledger"

    def test_an_unmigrated_file_is_a_migration_gap_not_an_acl_one(self, wired):
        auth, db, settings = wired
        auth.source_drive(SRC_USER).add_binary("never-copied.pdf")

        r = _run(auth, db, settings)

        assert r["unmapped_files"] == 1
        assert r["missing_grants"] == 0

    def test_a_mapped_file_absent_from_the_target_is_reported(self, wired):
        auth, db, settings = wired
        src = auth.source_drive(SRC_USER)
        sid = src.add_binary("vanished.pdf")
        db.record_mapping(SRC_USER, sid, "tgt-does-not-exist", "file")

        r = _run(auth, db, settings)

        assert r["missing_files"] == 1
