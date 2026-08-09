"""
tests/test_acl_audit.py
=======================
Per-file share-access verification.

phases.py reconciles totals, which catches loss and is blind to substitution:
a target where every file arrived and half are shared with the wrong people
reconciles perfectly. These pin the checks that make the difference.
"""

from __future__ import annotations

import json

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

    def test_a_domain_grant_rewritten_to_the_target_tenant_is_preserved(self, wired):
        """
        The engine rewrites "everyone at the source company" to "everyone at
        the target company", which is right -- and an audit that does not
        mirror it reads every one of those as a loss AND a disclosure.

        That is not hypothetical: it reported 562 missing and 562 extra on a
        live run, dropping apparent fidelity from 100% to 79.4% and pointing
        the finger at the engine. Every one was this.
        """
        auth, db, settings = wired
        _pair(auth, db,
              [{"type": "domain", "role": "reader",
                "domain": settings.source_domain}],
              [{"type": "domain", "role": "reader",
                "domain": settings.target_domain}])

        r = _run(auth, db, settings)

        assert r["grants_matched"] == 1
        assert r["missing_grants"] == 0 and r["extra_grants"] == 0

    def test_a_domain_grant_to_an_unrelated_domain_is_not_rewritten(self, wired):
        """Only the source tenant's own domain follows the migration. A share
        with a partner company must still be compared as itself."""
        auth, db, settings = wired
        _pair(auth, db,
              [{"type": "domain", "role": "reader", "domain": "partner.com"}],
              [])

        r = _run(auth, db, settings)

        assert r["missing_grants"] == 1

    def test_a_domain_grant_genuinely_dropped_is_still_reported(self, wired):
        """The fix must not make domain grants unfailable."""
        auth, db, settings = wired
        _pair(auth, db,
              [{"type": "domain", "role": "writer",
                "domain": settings.source_domain}],
              [])

        r = _run(auth, db, settings)

        assert r["missing_grants"] == 1


class TestItDoesNotManufactureFindings:
    def test_owner_rows_are_ignored_on_both_sides(self, wired):
        """The target file is owned by the target user by construction.
        Reporting that would bury the real findings under one per file."""
        auth, db, settings = wired
        _pair(auth, db, [], [])
        r = _run(auth, db, settings)
        assert r["missing_grants"] == 0 and r["extra_grants"] == 0
        assert r["exact"] == 1

    def test_an_unprovisioned_source_domain_grantee_is_not_counted_as_loss(
            self, wired):
        """A source-domain identity with no identity_map row is one
        drive_engine._sync_acls itself drops (SKIPPED_UNMAPPED_IDENTITY) --
        the grant genuinely cannot exist on the target, so this is a
        provisioning gap, not data loss. Calling it a loss would send
        someone to re-run a migration that cannot fix it."""
        auth, db, settings = wired
        _pair(auth, db,
              [{"type": "user", "role": "reader",
                "emailAddress": f"contractor@{settings.source_domain}"}],
              [])

        r = _run(auth, db, settings)

        assert r["unmapped_grantees"] == 1
        assert r["missing_grants"] == 0

    def test_a_preserved_external_grantee_is_matched_not_flagged_extra(
            self, wired):
        """An address outside the source domain is never dropped by
        drive_engine._sync_acls -- it is preserved verbatim on the target
        regardless of identity_map, since sharing with an external address
        needs no target-tenant account at all. Confirmed live: acl_audit.py
        used to exclude these from `want` entirely (treating "no
        identity_map row" as equivalent to "dropped"), so every one of
        5,127 correctly-preserved external grants -- all one deliberately
        seeded address -- read as the target over-sharing when nothing was
        actually wrong."""
        auth, db, settings = wired
        grant = {"type": "user", "role": "reader",
                 "emailAddress": "aryan@nestnepal.com.np"}
        _pair(auth, db, [grant], [grant])

        r = _run(auth, db, settings)

        assert r["unmapped_grantees"] == 1
        assert r["missing_grants"] == 0
        assert r["extra_grants"] == 0
        assert r["exact"] == 1

    def test_a_dropped_external_grantee_is_reported_as_missing(self, wired):
        """The inverse of the case above: if an external grant genuinely did
        not make it to the target, that is a real loss and must still be
        caught, not swallowed by the same fix that stops false positives."""
        auth, db, settings = wired
        _pair(auth, db,
              [{"type": "user", "role": "reader",
                "emailAddress": "aryan@nestnepal.com.np"}],
              [])

        r = _run(auth, db, settings)

        assert r["missing_grants"] == 1

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


class TestMainIsolatesPerUserFailures:
    """
    Confirmed live: a dead/suspended source account (session invalid, a 401
    on every call) made audit_user() raise, and with no per-user try/except
    in main()'s loop that took down the *entire* audit -- every other
    already-migrated user's real ACL numbers were lost along with it, and
    acl_audit.json was never written at all. migrate_user() in main.py
    already isolates failures the same way, per user; this is that same
    fix for the audit script.
    """

    def test_one_dead_account_does_not_abort_the_whole_audit(
            self, wired, monkeypatch, capsys, tmp_path):
        auth, db, settings = wired
        real_audit_user = acl_audit.audit_user

        def flaky(auth_, db_, settings_, source_user, target_user):
            if source_user == SRC_USER:
                raise Exception("HTTP 401 (authError): Active session is invalid")
            return real_audit_user(auth_, db_, settings_, source_user, target_user)

        monkeypatch.setattr(acl_audit, "audit_user", flaky)
        monkeypatch.setattr(acl_audit, "Settings", lambda: settings)
        monkeypatch.setattr(acl_audit, "MigrationDB", lambda *_a, **_k: db)
        monkeypatch.setattr(acl_audit, "AuthManager", lambda *_a, **_k: auth)

        out_json = tmp_path / "acl_audit.json"
        rc = acl_audit.main(["--json", str(out_json)])

        printed = capsys.readouterr().out
        assert SRC_USER.split("@")[0] in printed
        assert "AUDIT FAILED" in printed
        # A user whose account is dead is reported, not silently dropped --
        # but it must not itself flip the run to a failing exit code, since
        # that would be indistinguishable from a real ACL loss.
        assert rc == 0

        data = json.loads(out_json.read_text())
        assert SRC_USER in data["failed_users"]
        # bob and carol (from the `wired` fixture) still got audited fine --
        # they have no files paired in this test, so their contribution to
        # totals is legitimately all zero, not absent because SRC_USER's
        # exception ate the whole run.
        assert all(v == 0 for v in data["totals"].values())
        assert len(data["users"]) == 2  # bob, carol -- not SRC_USER
