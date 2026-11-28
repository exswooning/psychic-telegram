from types import SimpleNamespace

import pytest

import discovery
from tests.conftest import SRC_USER, TGT_USER


@pytest.fixture
def contacts_engine_migrator(auth, db, settings, identity):
    import contacts_engine

    return contacts_engine.ContactsMigrator(auth, db, settings, SRC_USER, TGT_USER)


@pytest.fixture
def tasks_engine_migrator(auth, db, settings, identity):
    import tasks_engine

    return tasks_engine.TasksMigrator(auth, db, settings, SRC_USER, TGT_USER)


@pytest.fixture
def chat_engine_migrator(auth, db, settings, identity):
    import chat_engine

    return chat_engine.ChatMigrator(auth, db, settings, SRC_USER, TGT_USER)


class _RepeatingPages:
    def __init__(self, collection):
        self.collection = collection
        self.calls = 0

    def list(self, **kwargs):
        self.calls += 1
        if self.calls > 3:
            raise AssertionError("pagination did not stop")
        return SimpleNamespace(execute=lambda: {
            self.collection: [], "nextPageToken": "repeat"
        })


class _RepeatingItemPages:
    def __init__(self, collection, item_key):
        self.collection, self.item_key = collection, item_key
        self.calls = 0

    def list(self, **kwargs):
        self.calls += 1
        if self.calls > 3:
            raise AssertionError("pagination did not stop")
        return SimpleNamespace(execute=lambda: {
            self.collection: [{self.item_key: f"item-{self.calls}"}],
            "nextPageToken": "repeat",
        })


def test_gmail_migration_stops_on_a_repeated_message_token(gmail_migrator):
    pages = _RepeatingItemPages("messages", "id")
    gmail_migrator.src = SimpleNamespace(users=lambda: SimpleNamespace(
        messages=lambda: pages
    ))

    seen = []
    with pytest.raises(RuntimeError, match="repeated.*page token"):
        seen.extend(gmail_migrator._iter_messages(""))
    assert pages.calls == 2
    assert seen == [{"id": "item-1"}]


def test_drive_migration_stops_on_a_repeated_child_token(migrator):
    pages = _RepeatingItemPages("files", "id")
    migrator.src = SimpleNamespace(files=lambda: pages)

    seen = []
    with pytest.raises(RuntimeError, match="repeated.*page token"):
        seen.extend(migrator._list_children("parent"))
    assert pages.calls == 2
    assert seen == [{"id": "item-1"}]


def test_drive_discovery_stops_on_a_repeated_page_token(settings):
    pages = _RepeatingPages("files")
    drive = SimpleNamespace(files=lambda: pages)

    with pytest.raises(RuntimeError, match="repeated.*page token"):
        list(discovery.iter_all_drive_items(drive, settings))
    assert pages.calls == 2


def test_calendar_events_stop_before_yielding_a_repeated_page(cal_migrator):
    pages = _RepeatingItemPages("items", "id")
    cal_migrator.src = SimpleNamespace(events=lambda: pages)

    seen = []
    with pytest.raises(RuntimeError, match="repeated.*page token"):
        seen.extend(cal_migrator._iter_events(None))
    assert pages.calls == 2
    assert seen == [{"id": "item-1"}]


def test_contacts_stop_before_yielding_a_repeated_page(contacts_engine_migrator):
    pages = _RepeatingItemPages("connections", "resourceName")
    contacts_engine_migrator.src = SimpleNamespace(people=lambda: SimpleNamespace(
        connections=lambda: pages
    ))

    seen = []
    with pytest.raises(RuntimeError, match="repeated.*page token"):
        seen.extend(contacts_engine_migrator._iter_contacts())
    assert pages.calls == 2
    assert seen == [{"resourceName": "item-1"}]


def test_tasks_stop_before_yielding_a_repeated_page(tasks_engine_migrator):
    pages = _RepeatingItemPages("items", "id")
    tasks_engine_migrator.src = SimpleNamespace(tasklists=lambda: pages)

    seen = []
    with pytest.raises(RuntimeError, match="repeated.*page token"):
        seen.extend(tasks_engine_migrator._iter_lists())
    assert pages.calls == 2
    assert seen == [{"id": "item-1"}]


def test_chat_stops_before_yielding_a_repeated_page(chat_engine_migrator, monkeypatch):
    pages = _RepeatingItemPages("spaces", "name")
    monkeypatch.setattr(chat_engine_migrator.auth, "source_chat", lambda _user: SimpleNamespace(
        spaces=lambda: pages
    ))

    seen = []
    with pytest.raises(RuntimeError, match="repeated.*page token"):
        seen.extend(chat_engine_migrator._iter_spaces())
    assert pages.calls == 2
    assert seen == [{"name": "item-1"}]


def test_calendar_subscription_inventory_stops_on_a_repeated_token(cal_migrator):
    pages = _RepeatingPages("items")
    cal_migrator.src = SimpleNamespace(calendarList=lambda: pages)

    with pytest.raises(RuntimeError, match="repeated.*page token"):
        cal_migrator.sync_subscriptions()
    assert pages.calls == 2


def test_directory_group_inventory_stops_on_a_repeated_token():
    import groups_engine

    pages = _RepeatingPages("groups")
    migrator = object.__new__(groups_engine.GroupMigrator)
    migrator._dir = lambda _tenant: SimpleNamespace(groups=lambda: pages)

    with pytest.raises(RuntimeError, match="repeated.*page token"):
        migrator.read_groups("source")
    assert pages.calls == 2
