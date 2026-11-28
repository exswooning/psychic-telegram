from __future__ import annotations

import pytest

from tests.conftest import SRC_USER, TGT_USER


@pytest.fixture
def contacts(auth, db, settings, identity):
    import contacts_engine

    return contacts_engine.ContactsMigrator(auth, db, settings, SRC_USER, TGT_USER)


@pytest.fixture
def tasks(auth, db, settings, identity):
    import tasks_engine

    return tasks_engine.TasksMigrator(auth, db, settings, SRC_USER, TGT_USER)


def _fail(*_args, **_kwargs):
    raise RuntimeError("synthetic inventory failure")


def test_missing_target_contact_inventory_does_not_skip_a_gone_copy(
        contacts, auth, db, monkeypatch):
    from tests import fakes

    source_id = auth.source_people(SRC_USER).add_contact("Ada", "ada@example.com")
    db.record_mapping(SRC_USER, source_id, "people/missing-target", "contact")
    original = fakes._PeoplePeople._list

    def target_inventory_failure(self, **kwargs):
        if self.s.owner == TGT_USER:
            return _fail()
        return original(self, **kwargs)

    monkeypatch.setattr(fakes._PeoplePeople, "_list", target_inventory_failure)

    with pytest.raises(RuntimeError, match="synthetic inventory failure"):
        contacts.run()


def test_contact_group_inventory_failure_does_not_migrate_contacts_ungrouped(
        contacts, auth, monkeypatch):
    source = auth.source_people(SRC_USER)
    group = source.add_group("Suppliers")
    source.add_contact("Ada", "ada@example.com", groups=[group])
    monkeypatch.setattr(contacts, "_iter_groups", _fail)

    with pytest.raises(RuntimeError, match="synthetic inventory failure"):
        contacts.run()
    assert auth.target_people(TGT_USER).contacts == {}


def test_task_list_inventory_failure_does_not_look_like_empty_success(tasks, monkeypatch):
    monkeypatch.setattr(tasks, "_iter_lists", _fail)

    with pytest.raises(RuntimeError, match="synthetic inventory failure"):
        tasks.run()


def test_unreadable_tasks_before_recopy_is_counted_as_failure(tasks, auth, monkeypatch):
    source = auth.source_tasks(SRC_USER)
    task_list = source.add_list("Move")
    source.add_task(task_list, "Recover me")
    tasks.run()
    auth.target_tasks(TGT_USER).lists.clear()
    import tasks_engine
    again = tasks_engine.TasksMigrator(auth, tasks.db, tasks.settings, SRC_USER, TGT_USER)
    monkeypatch.setattr(again, "_iter_tasks", _fail)

    stats = again.run()

    assert stats.get("failed") == 1


def test_unreadable_target_task_inventory_does_not_create_duplicate_tasks(
        tasks, auth, monkeypatch):
    from tests import fakes

    source = auth.source_tasks(SRC_USER)
    task_list = source.add_list("Move")
    source.add_task(task_list, "Recover me")
    original = fakes._Tasks._list

    def target_inventory_failure(self, **kwargs):
        if self.s.owner == TGT_USER:
            return _fail()
        return original(self, **kwargs)

    monkeypatch.setattr(fakes._Tasks, "_list", target_inventory_failure)

    with pytest.raises(RuntimeError, match="synthetic inventory failure"):
        tasks.run()
    target = auth.target_tasks(TGT_USER)
    assert all(not values for values in target.task_store.values())


def test_unreadable_drive_target_inventory_does_not_copy_an_unledgered_file(
        migrator, auth, monkeypatch):
    from tests import fakes

    target = auth.target_drive(TGT_USER)
    original = fakes._DriveFiles._list
    calls = 0

    def target_inventory_failure(self, **kwargs):
        nonlocal calls
        if self.s.owner == TGT_USER:
            calls += 1
            return _fail()
        return original(self, **kwargs)

    monkeypatch.setattr(fakes._DriveFiles, "_list", target_inventory_failure)
    item = {"id": "source-file", "name": "Report", "mimeType": "application/pdf",
            "createdTime": "2024-01-01T00:00:00Z"}

    for _ in range(2):
        with pytest.raises(RuntimeError, match="synthetic inventory failure"):
            migrator._take_placed(item, target.root_id)
    assert calls == 1


def test_unreadable_staging_inventory_does_not_ignore_a_stranded_copy(
        migrator, auth, monkeypatch):
    from tests import fakes

    target = auth.target_drive(TGT_USER)
    migrator._staging_drive_id = "staging-drive"
    original = fakes._DriveFiles._list

    def target_inventory_failure(self, **kwargs):
        if self.s.owner == TGT_USER:
            return _fail()
        return original(self, **kwargs)

    monkeypatch.setattr(fakes._DriveFiles, "_list", target_inventory_failure)

    with pytest.raises(RuntimeError, match="synthetic inventory failure"):
        migrator._index_staged()


def test_chat_space_inventory_failure_does_not_look_like_empty_success(auth, db, settings, monkeypatch):
    import chat_engine

    m = chat_engine.ChatMigrator(auth, db, settings, SRC_USER, TGT_USER)
    monkeypatch.setattr(m, "_iter_spaces", _fail)

    with pytest.raises(RuntimeError, match="synthetic inventory failure"):
        m.run()


def test_secondary_calendar_inventory_failure_is_not_swallowed(cal_migrator, monkeypatch):
    monkeypatch.setattr(cal_migrator, "_all_pages", _fail)

    with pytest.raises(RuntimeError, match="synthetic inventory failure"):
        cal_migrator._migrate_secondary_calendars(None)


def test_calendar_acl_inventory_failure_does_not_look_like_no_acl_rules(cal_migrator, monkeypatch):
    monkeypatch.setattr(cal_migrator, "_all_pages", _fail)

    with pytest.raises(RuntimeError, match="synthetic inventory failure"):
        cal_migrator._sync_calendar_acl("primary", "primary")
