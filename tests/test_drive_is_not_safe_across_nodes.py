"""
tests/test_drive_is_not_safe_across_nodes.py
============================================
Why a 17 GB machine sat idle while a 3.7 GB one ran at 95% CPU.

MULTINODE.md states it in a table cell -- Drive: "id_mapping in the LOCAL
ledger", safe across nodes: no -- and a doc line is not a test. This is the
executable version of that claim, so the day someone makes Drive
multi-node-safe, the test that says it is not fails and has to be read.

The mechanism: a claim is keyed on (account_id, source_user) and nothing
else, so it is all-or-nothing per user. One node takes the whole user or
none of it. That is correct ONLY because Drive's id_mapping is local: a
second node picking up a half-migrated user has no record of which files
already landed and re-delivers every one of them.

The consequence worth naming: Gmail, Calendar, Contacts and Tasks are all
safe to split (Gmail dedupes on Message-ID at the target), but they cannot
be, because the lock has no service dimension. That is the change this test
is the acceptance criterion for -- not a bug to fix quietly.
"""

from __future__ import annotations

import inspect

import user_claims


class TestTheLockHasNoServiceDimension:
    def test_a_claim_is_one_row_per_user(self):
        """(account_id, source_user) with no service column in the key is
        exactly what makes splitting a user impossible."""
        src = inspect.getsource(user_claims._local_acquire)
        assert "WHERE account_id IS ? AND source_user=?" in src, (
            "the claim key changed -- if a service dimension was added, "
            "this whole file is the thing to rewrite")

    def test_services_are_recorded_but_do_not_gate_the_claim(self):
        """They are read for the reassignment POLICY (a Drive redo costs
        more than a Gmail redo), never to let two nodes hold one user."""
        src = inspect.getsource(user_claims._local_acquire)
        before_key = src.split("WHERE account_id IS ?")[0]
        assert "services" in before_key, "services no longer selected"
        assert "AND services" not in src, (
            "services now gates the claim -- Drive may have become "
            "splittable; confirm id_mapping is no longer node-local")


class TestTheDocumentedReasonStillHolds:
    def test_drive_ids_are_written_to_the_local_ledger(self):
        """The root cause, checked where it lives rather than quoted."""
        import db

        src = inspect.getsource(db.MigrationDB)
        assert "id_mapping" in src, (
            "id_mapping left the local ledger; if it moved to the "
            "coordinator, Drive may now be safe across nodes")

    def test_multinode_doc_still_says_drive_is_unsafe(self):
        """Doc and test must not drift apart in either direction."""
        import pathlib

        doc = (pathlib.Path(__file__).resolve().parent.parent
               / "MULTINODE.md").read_text()
        row = [ln for ln in doc.splitlines()
               if ln.strip().startswith("| Drive")]
        assert row and "no" in row[0].lower(), (
            "MULTINODE.md no longer marks Drive unsafe across nodes")


class TestWhatIsActuallySafeToSplit:
    SAFE = ("Gmail", "Calendar", "Contacts", "Tasks")

    def test_the_doc_names_gmail_as_safe(self):
        """Gmail dedupes on Message-ID at the target, so a re-delivery is a
        no-op rather than a duplicate -- the one service that is safe today
        and still cannot be split."""
        import pathlib

        doc = (pathlib.Path(__file__).resolve().parent.parent
               / "MULTINODE.md").read_text()
        assert "Message-ID" in doc
