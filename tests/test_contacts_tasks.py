"""
tests/test_contacts_tasks.py
============================
Contacts and Tasks: the two cheapest wins in the tool, and the two most
visible to users on day one. An empty contact list is noticed within the hour
by everyone; a flattened task list looks complete while having lost its
structure.

Both are opt-in, and the first test here is about that rather than about
data: enabling them widens the OAuth grant, and a scope the Admin Console has
not authorised makes every call in the run fail.
"""

from __future__ import annotations

import pytest

from tests.conftest import SRC_USER, TGT_USER


class TestOptIn:
    def test_off_by_default_so_upgrades_do_not_widen_the_grant(self, settings):
        from config import (CONTACTS_READONLY_SCOPE, TASKS_READONLY_SCOPE,
                            source_scopes)

        assert settings.migrate_contacts is False
        assert settings.migrate_tasks is False
        scopes = source_scopes(settings)
        assert CONTACTS_READONLY_SCOPE not in scopes
        assert TASKS_READONLY_SCOPE not in scopes

    def test_the_source_never_gets_a_write_scope(self, settings):
        """The source credential's read-only property is structural, not
        policy, and neither of these services may weaken it."""
        from config import (CONTACTS_WRITE_SCOPE, TASKS_WRITE_SCOPE,
                            source_scopes, target_scopes)

        settings.migrate_contacts = True
        settings.migrate_tasks = True
        assert CONTACTS_WRITE_SCOPE not in source_scopes(settings)
        assert TASKS_WRITE_SCOPE not in source_scopes(settings)
        assert CONTACTS_WRITE_SCOPE in target_scopes(settings)
        assert TASKS_WRITE_SCOPE in target_scopes(settings)


@pytest.fixture
def contacts(auth, db, settings, identity):
    import contacts_engine

    settings.migrate_contacts = True
    return contacts_engine.ContactsMigrator(auth, db, settings, SRC_USER, TGT_USER)


class TestContacts:
    def test_contacts_and_their_groups_arrive(self, contacts, auth):
        src = auth.source_people(SRC_USER)
        g = src.add_group("Suppliers")
        src.add_contact("Ada", "ada@example.com", groups=[g])
        src.add_contact("Grace", "grace@example.com")

        stats = contacts.run()

        tgt = auth.target_people(TGT_USER)
        assert stats["contacts"] == 2
        assert stats["groups"] == 1
        assert len(tgt.contacts) == 2

    def test_a_contact_lands_in_the_migrated_group(self, contacts, auth):
        """A contact that arrives ungrouped looks migrated and is unfindable
        in a list of nine thousand."""
        src = auth.source_people(SRC_USER)
        g = src.add_group("Suppliers")
        src.add_contact("Ada", "ada@example.com", groups=[g])

        contacts.run()

        tgt = auth.target_people(TGT_USER)
        new_group = next(iter(tgt.groups))
        assert len(tgt.group_members[new_group]) == 1

    def test_groups_are_created_before_contacts(self, contacts, auth):
        """A group cannot reference a person who does not exist yet, so the
        order is load-bearing."""
        src = auth.source_people(SRC_USER)
        g = src.add_group("Suppliers")
        src.add_contact("Ada", "ada@example.com", groups=[g])

        contacts.run()

        order = [n for n, _ in auth.target_people(TGT_USER).calls]
        assert order.index("contactGroups.create") < order.index("people.createContact")

    def test_a_contact_with_no_writable_fields_is_recorded_not_dropped(
            self, contacts, auth, db):
        """The API rejects an empty person. Recording it keeps the count
        reconcilable instead of quietly differing by a handful."""
        src = auth.source_people(SRC_USER)
        rid = "people/empty-1"
        src.contacts[rid] = {"resourceName": rid}

        stats = contacts.run()

        assert stats["failed"] == 0
        row = db.conn.execute(
            "SELECT status FROM audit_log WHERE item_type='contact'").fetchone()
        assert row["status"] == "SKIPPED_EMPTY"

    def test_a_rerun_does_not_duplicate(self, contacts, auth, db, settings):
        import contacts_engine

        src = auth.source_people(SRC_USER)
        src.add_contact("Ada", "ada@example.com")
        contacts.run()

        second = contacts_engine.ContactsMigrator(auth, db, settings,
                                                  SRC_USER, TGT_USER)
        again = second.run()

        assert again["contacts"] == 0
        assert len(auth.target_people(TGT_USER).contacts) == 1


@pytest.fixture
def tasks(auth, db, settings, identity):
    import tasks_engine

    settings.migrate_tasks = True
    return tasks_engine.TasksMigrator(auth, db, settings, SRC_USER, TGT_USER)


class TestTasks:
    def test_lists_and_tasks_arrive(self, tasks, auth):
        src = auth.source_tasks(SRC_USER)
        tl = src.add_list("Move")
        src.add_task(tl, "Book the cutover window")
        src.add_task(tl, "Tell the vendors", status="completed")

        stats = tasks.run()

        assert stats["lists"] == 1
        assert stats["tasks"] == 2

    def test_subtasks_keep_their_parent(self, tasks, auth):
        """The fake rejects an unknown parent, exactly as the API does, so a
        flattened hierarchy fails here rather than shipping."""
        src = auth.source_tasks(SRC_USER)
        tl = src.add_list("Move")
        parent = src.add_task(tl, "Cutover")
        src.add_task(tl, "Freeze mail", parent=parent)

        stats = tasks.run()

        assert stats["failed"] == 0
        assert stats["tasks"] == 2
        tgt = auth.target_tasks(TGT_USER)
        new_list = next(iter(tgt.lists))
        children = [t for t in tgt.task_store[new_list] if t.get("parent")]
        assert len(children) == 1

    def test_completion_and_due_dates_survive(self, tasks, auth):
        src = auth.source_tasks(SRC_USER)
        tl = src.add_list("Move")
        src.add_task(tl, "Tell the vendors", status="completed",
                     due="2026-09-01T00:00:00.000Z")

        tasks.run()

        tgt = auth.target_tasks(TGT_USER)
        got = tgt.task_store[next(iter(tgt.lists))][0]
        assert got["status"] == "completed"
        assert got["due"] == "2026-09-01T00:00:00.000Z"

    def test_an_untitled_task_is_given_a_title_rather_than_dropped(
            self, tasks, auth):
        """Tasks itself allows an untitled task; the API refuses to create
        one. Dropping the row would lose a real item."""
        src = auth.source_tasks(SRC_USER)
        tl = src.add_list("Move")
        src.task_store[tl].append({"id": "bare-1", "status": "needsAction"})

        stats = tasks.run()

        assert stats["tasks"] == 1
        assert stats["failed"] == 0

    def test_a_rerun_does_not_duplicate(self, tasks, auth, db, settings):
        import tasks_engine

        src = auth.source_tasks(SRC_USER)
        tl = src.add_list("Move")
        src.add_task(tl, "Book the cutover window")
        tasks.run()

        second = tasks_engine.TasksMigrator(auth, db, settings, SRC_USER, TGT_USER)
        again = second.run()

        assert again["tasks"] == 0
        tgt = auth.target_tasks(TGT_USER)
        assert len(tgt.lists) == 1


class TestAnExistingContactGroupIsAdopted:
    """People rejects a duplicate group name with 409 ALREADY_EXISTS rather
    than returning the existing group. Without handling that, a re-run fails
    the same group forever and its contacts lose their membership -- while
    the group sits on the target, correct.

    gmail_engine has always reused an existing target label; contacts never
    did, and it surfaced as live failures on an otherwise clean 122,849-item
    run rather than as a red test, because the fake used to allow duplicates.
    """

    def test_a_pre_existing_group_is_mapped_not_failed(self, auth, db,
                                                        settings, identity):
        import contacts_engine
        src = auth.source_people(SRC_USER)
        src.add_group("Clients")
        tgt = auth.target_people(TGT_USER)
        existing = tgt.add_group("Clients")

        m = contacts_engine.ContactsMigrator(auth, db, settings,
                                             SRC_USER, TGT_USER)
        mapping = m._migrate_groups()

        assert existing in mapping.values(), "must adopt the target's group"
        assert m.stats["failed"] == 0

    def test_the_mapping_is_recorded_so_the_next_run_skips_it(self, auth, db,
                                                              settings,
                                                              identity):
        import contacts_engine
        src = auth.source_people(SRC_USER)
        gid = src.add_group("Projects")
        auth.target_people(TGT_USER).add_group("Projects")

        m = contacts_engine.ContactsMigrator(auth, db, settings,
                                             SRC_USER, TGT_USER)
        m._migrate_groups()
        assert db.get_target_id(SRC_USER, gid, "contact_group")

    def test_a_genuinely_new_group_is_still_created(self, auth, db, settings,
                                                    identity):
        import contacts_engine
        auth.source_people(SRC_USER).add_group("Brand New")
        m = contacts_engine.ContactsMigrator(auth, db, settings,
                                             SRC_USER, TGT_USER)
        mapping = m._migrate_groups()
        names = {g["name"] for g in auth.target_people(TGT_USER).groups.values()}
        assert "Brand New" in names
        assert len(mapping) == 1

    def test_an_unrelated_failure_is_still_a_failure(self, auth, db, settings,
                                                     identity, monkeypatch):
        """Only ALREADY_EXISTS is adopted. Swallowing anything else would
        report a group as migrated that is not there."""
        import contacts_engine
        import fakes
        auth.source_people(SRC_USER).add_group("Boom")
        tgt = auth.target_people(TGT_USER)
        monkeypatch.setattr(
            type(tgt.contactGroups()), "_create",
            lambda self, body, **k: (_ for _ in ()).throw(
                fakes.http_error(500, reason="internalError")))
        m = contacts_engine.ContactsMigrator(auth, db, settings,
                                             SRC_USER, TGT_USER)
        m._migrate_groups()
        assert m.stats["failed"] == 1
