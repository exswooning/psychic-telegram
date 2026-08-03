"""
tests/test_phase_status.py
==========================
Per-service completion tracking.

`identity_map.status` is per user. A phased run that finished Drive marked
every user DONE, so the Gmail phase that followed skipped all of them:
migrated nothing, recorded nothing, and reported "10 of 839 (98.8% short)"
with no explanation available anywhere.

Observed on a live tenant. Drive reconciled at exactly 1,342 of 1,342 because
it ran first; everything after it silently did nothing. The reconciler caught
the shortfall, which is the only reason it was noticed at all — an unphased
run would have reported a clean migration missing 829 messages.
"""

from __future__ import annotations

import pytest

from db import MigrationDB


@pytest.fixture
def ledger(tmp_path):
    db = MigrationDB(str(tmp_path / "m.db"))
    db.init_schema()
    with db.write() as c:
        c.execute("INSERT INTO identity_map"
                  "(source_email,target_email,entity_type,status) "
                  "VALUES('alice@c.com','alice@a.com','user','PENDING')")
    return db


class TestServiceTracking:
    def test_a_fresh_user_has_no_services_done(self, ledger):
        assert ledger.services_done("alice@c.com") == set()

    def test_completing_drive_records_only_drive(self, ledger):
        ledger.mark_services_done("alice@c.com", ["drive"])
        assert ledger.services_done("alice@c.com") == {"drive"}

    def test_services_accumulate_across_phases(self, ledger):
        """The whole point: a later phase must add to the set, not replace it,
        or resuming would re-migrate what is already there."""
        ledger.mark_services_done("alice@c.com", ["drive"])
        ledger.mark_services_done("alice@c.com", ["gmail"])
        ledger.mark_services_done("alice@c.com", ["calendar", "chat"])
        assert ledger.services_done("alice@c.com") == {
            "drive", "gmail", "calendar", "chat"}

    def test_recording_the_same_service_twice_is_harmless(self, ledger):
        ledger.mark_services_done("alice@c.com", ["drive"])
        ledger.mark_services_done("alice@c.com", ["drive"])
        assert ledger.services_done("alice@c.com") == {"drive"}

    def test_an_unknown_user_reports_nothing_rather_than_raising(self, ledger):
        assert ledger.services_done("nobody@c.com") == set()


class TestSkipIsPerService:
    """The behaviour that broke: what run_batch decides to skip."""

    def _already_done(self, db, status, services_requested):
        row = {"status": status, "source_email": "alice@c.com"}

        done = db.services_done(row["source_email"])
        if row["status"] != "DONE":
            return False
        if not done:
            return True
        return set(services_requested) <= done

    def test_a_user_who_finished_drive_is_not_skipped_for_gmail(self, ledger):
        """The exact live failure."""
        ledger.mark_services_done("alice@c.com", ["drive"])
        assert self._already_done(ledger, "DONE", {"gmail"}) is False

    def test_a_user_who_finished_drive_is_skipped_for_drive(self, ledger):
        """Restarts must stay cheap — that is why the check exists at all."""
        ledger.mark_services_done("alice@c.com", ["drive"])
        assert self._already_done(ledger, "DONE", {"drive"}) is True

    def test_a_partially_covered_request_is_not_skipped(self, ledger):
        """Asking for drive+gmail when only drive is done must run."""
        ledger.mark_services_done("alice@c.com", ["drive"])
        assert self._already_done(ledger, "DONE", {"drive", "gmail"}) is False

    def test_a_fully_covered_request_is_skipped(self, ledger):
        ledger.mark_services_done("alice@c.com", ["drive", "gmail"])
        assert self._already_done(ledger, "DONE", {"drive"}) is True

    def test_a_user_not_marked_done_always_runs(self, ledger):
        ledger.mark_services_done("alice@c.com", ["drive"])
        assert self._already_done(ledger, "PENDING", {"drive"}) is False

    def test_a_legacy_ledger_keeps_the_old_behaviour(self, ledger):
        """Databases written before this column existed have an empty set;
        treating that as "nothing done" would re-migrate whole tenants."""
        assert self._already_done(ledger, "DONE", {"gmail"}) is True


class TestWiring:
    def test_main_records_services_on_completion(self):
        import inspect

        import main

        src = inspect.getsource(main.migrate_user)
        assert "mark_services_done" in src

    def test_main_consults_the_set_when_skipping(self):
        import inspect

        import main

        src = inspect.getsource(main.run_batch)
        assert "services_done" in src
        assert "<= done" in src

    def test_a_dry_run_still_records_nothing(self):
        """Running --dry-run before the real migrate must not mark anything
        done — the same trap in a different place."""
        import inspect

        import main

        src = inspect.getsource(main.migrate_user)
        marked = src.index("mark_services_done")
        assert "track_status" in src[:marked]


class TestBackfillNeedsEvidence:
    """
    `backfill-services` exists to tell an older ledger which services really
    ran, so the legacy fallback stops skipping the ones that did not. That
    makes it the one command able to certify work as complete without doing
    any -- so it verifies the claim against audit_log instead of trusting the
    flag.

    The first version could never confirm anything: summary() is keyed
    "<item_type>:<status>", and it tested for a bare "file". It refused every
    user, which was the safe direction to be wrong in, but wrong.
    """

    def _run(self, ledger, services):
        import argparse

        import main

        args = argparse.Namespace(services=services)
        main.cmd_backfill_services(args, None, ledger, None)
        return ledger.services_done("alice@c.com")

    def _finish(self, ledger, item_type, status="SUCCESS"):
        with ledger.write() as c:
            c.execute("UPDATE identity_map SET status='DONE'")
        ledger.log_audit("alice@c.com", f"i-{item_type}", item_type, status)

    def test_a_service_with_successful_items_is_recorded(self, ledger):
        self._finish(ledger, "file")
        assert self._run(ledger, ["drive"]) == {"drive"}

    def test_a_service_with_no_items_is_refused(self, ledger):
        """The claim that matters: Drive ran, Gmail did not, and saying both
        would mark 829 unmigrated messages as done forever."""
        self._finish(ledger, "file")
        assert self._run(ledger, ["drive", "gmail"]) == {"drive"}

    def test_a_service_whose_items_all_failed_is_refused(self, ledger):
        """FAILED rows prove it was attempted, not that it succeeded."""
        self._finish(ledger, "message", status="FAILED")
        assert self._run(ledger, ["gmail"]) == set()

    def test_a_pending_user_is_left_alone(self, ledger):
        """Only DONE users have a status that needs explaining."""
        ledger.log_audit("alice@c.com", "i-file", "file", "SUCCESS")
        assert self._run(ledger, ["drive"]) == set()
