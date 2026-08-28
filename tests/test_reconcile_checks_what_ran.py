"""A fidelity check must cover the migration it is checking.

phases.py reads MIGRATE_CHAT / MIGRATE_CONTACTS / MIGRATE_TASKS from the
environment, and webui built those from _RUN_STATE's checkboxes, which
default all three off. Live, a reconcile over a run that had already
migrated 4,975 contacts and 3,980 tasks printed:

    note: contacts requested but MIGRATE_CONTACTS is off -- skipping.
    note: tasks requested but MIGRATE_TASKS is off -- skipping.

and compared three services out of six -- while presenting itself as the
fidelity check. The notes are in the log; the summary is not qualified by
them.

The verifier now unions in whatever the ledger proves was migrated. The
migration itself deliberately does NOT: it must do exactly what its
checkboxes say, or a toggle means nothing.
"""
import sqlite3

import pytest

import webui
from db import MigrationDB


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = str(tmp_path / "m.db")
    d = MigrationDB(path)
    d.log_audit("u@src", "c1", "contact", "SUCCESS", "")
    d.log_audit("u@src", "t1", "task", "SUCCESS", "")
    d.log_audit("u@src", "f1", "file", "SUCCESS", "")
    d.conn.commit()
    d.close()
    monkeypatch.setattr(webui, "_db_conn",
                        lambda account_id=None: sqlite3.connect(path))
    return path


class TestWhatTheLedgerProves:
    def test_it_finds_the_services_that_ran(self, ledger):
        assert webui._services_in_ledger(66) == {"contacts", "tasks"}

    def test_a_service_that_never_ran_is_not_claimed(self, ledger):
        assert "chat" not in webui._services_in_ledger(66)

    def test_no_ledger_is_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(webui, "_db_conn", lambda account_id=None: None)
        assert webui._services_in_ledger(66) == set()

    def test_an_unreadable_ledger_is_not_a_crash(self, monkeypatch):
        class _Bad:
            def execute(self, *a):
                raise sqlite3.DatabaseError("malformed")

            def close(self):
                pass

        monkeypatch.setattr(webui, "_db_conn", lambda account_id=None: _Bad())
        assert webui._services_in_ledger(66) == set()


class TestTheVerifierWidensItself:
    def test_it_turns_on_what_was_migrated(self, ledger, monkeypatch):
        monkeypatch.setitem(webui._RUN_STATE["services"], "contacts", False)
        monkeypatch.setitem(webui._RUN_STATE["services"], "tasks", False)
        env = webui._service_env(66, from_ledger=True)
        assert env["MIGRATE_CONTACTS"] == "true"
        assert env["MIGRATE_TASKS"] == "true"

    def test_it_does_not_invent_a_service(self, ledger):
        env = webui._service_env(66, from_ledger=True)
        assert env["MIGRATE_CHAT"] == "false", (
            "claiming to verify Chat that never ran is its own lie")

    def test_a_checkbox_still_counts(self, ledger, monkeypatch):
        monkeypatch.setitem(webui._RUN_STATE["services"], "chat", True)
        assert webui._service_env(66, from_ledger=True)["MIGRATE_CHAT"] == "true"


class TestTheMigrationIsNotWidened:
    def test_performing_still_obeys_the_checkboxes(self, ledger, monkeypatch):
        """phased_migrate MOVES data. Widening it from the ledger would
        migrate services the operator did not ask for."""
        monkeypatch.setitem(webui._RUN_STATE["services"], "contacts", False)
        env = webui._service_env(66, from_ledger=False)
        assert env["MIGRATE_CONTACTS"] == "false"

    def test_only_the_counter_reads_the_ledger(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "webui.py"), encoding="utf-8").read()
        block = src.split("if name in _PHASE_GATED_ACTIONS:")[1][:300]
        assert 'name == "phased_count_only"' in block
        assert "phased_migrate" not in block.split("else")[0]
