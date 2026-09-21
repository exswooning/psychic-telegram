"""
tests/test_reopen_service.py
=============================
Undo a completion marker reconcile_service_markers deliberately won't touch.

reconcile_service_markers only reopens a service with zero items AND at
least one FAILURE -- on purpose, because zero items alone is the ordinary
shape of a user with nothing to migrate. But zero items and zero failures is
ALSO exactly what a delta's --days filter produces against a mailbox that
has plenty of mail, just none inside the window: 300 users, Gmail
"succeeded" on every one, real message count 304 total. Indistinguishable
from empty by the ledger alone -- reconcile_service_markers will never fix
it, and it should not, because it can't tell the two cases apart.

`reopen-service` is the operator saying "no, that one really has more."
"""

from __future__ import annotations

import argparse

import pytest

import db as db_mod
import main


@pytest.fixture
def db(tmp_path):
    d = db_mod.MigrationDB(str(tmp_path / "t.db"))
    for email, services in (
        ("a@s.com", "calendar,chat,contacts,drive,gmail,tasks"),
        ("b@s.com", "calendar,chat,contacts,drive,gmail,tasks"),
        ("c@s.com", "drive"),  # never had gmail marked -- must stay untouched
    ):
        d.conn.execute(
            "INSERT INTO identity_map (source_email, target_email, "
            "entity_type, status, services_done) VALUES (?,?,?,?,?)",
            (email, email.replace("@s.com", "@t.com"), "user", "DONE", services))
    d.conn.commit()
    return d


def args(**kw):
    base = dict(services=["gmail"], user=None, all_users=False)
    base.update(kw)
    return argparse.Namespace(**base)


class TestItRemovesExactlyTheNamedService:
    def test_gmail_is_cleared_but_siblings_survive(self, db, capsys):
        main.cmd_reopen_service(args(all_users=True), None, db, None)
        assert db.services_done("a@s.com") == {
            "calendar", "chat", "contacts", "drive", "tasks"}

    def test_a_user_without_the_service_is_not_counted_as_changed(self, db, capsys):
        main.cmd_reopen_service(args(all_users=True), None, db, None)
        out = capsys.readouterr().out
        assert "reopened gmail for 2 of 3" in out
        assert db.services_done("c@s.com") == {"drive"}  # untouched


class TestItRequiresAnExplicitScope:
    def test_neither_user_nor_all_users_refuses(self, db):
        with pytest.raises(SystemExit, match="--user.*--all-users"):
            main.cmd_reopen_service(args(), None, db, None)

    def test_user_alone_is_enough(self, db):
        main.cmd_reopen_service(args(user=["a@s.com"]), None, db, None)
        assert "gmail" not in db.services_done("a@s.com")
        assert "gmail" in db.services_done("b@s.com")  # not named, not touched


class TestItMakesTheUserDispatchableAgain:
    def test_after_reopening_already_done_no_longer_skips_it(self, db):
        """The property this whole command exists for: run_batch's own
        _already_done must flip from True to False."""
        row = next(r for r in db.all_identities() if r["source_email"] == "a@s.com")
        requested = {"calendar", "chat", "contacts", "drive", "gmail", "tasks"}
        before = set(requested) <= db.services_done("a@s.com")
        assert before is True
        main.cmd_reopen_service(args(user=["a@s.com"]), None, db, None)
        after = set(requested) <= db.services_done("a@s.com")
        assert after is False


class TestSetServicesDoneReplacesRatherThanUnions:
    def test_it_can_shrink_the_set(self, db):
        """mark_services_done can only grow it -- this is deliberately the
        other direction, which is why it is a separate method rather than
        an argument to the existing one."""
        db.set_services_done("a@s.com", {"drive"})
        assert db.services_done("a@s.com") == {"drive"}

    def test_mark_services_done_is_unaffected_still_unions(self, db):
        db.mark_services_done("c@s.com", ["gmail"])
        assert db.services_done("c@s.com") == {"drive", "gmail"}
