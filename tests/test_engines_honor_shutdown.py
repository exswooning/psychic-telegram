"""
tests/test_engines_honor_shutdown.py
=====================================
migrate_user() only checks SHUTDOWN between whole services (main.py:421),
never inside one. Live, that meant a Stop pressed twice against a running
migration sat unresponsive for minutes: the currently in-flight service was
still walking hundreds of Chat spaces one at a time, none of which yielded
back to check anything. Each engine's own per-item loop now calls
resilience.shutdown_requested() and breaks -- these pin that it actually
does, using a fake that flips true after a chosen number of items rather
than only at the very start, so an untested "checks once and always says
yes" stub could not pass by accident.
"""

from __future__ import annotations

import calendar_engine
import chat_engine
import contacts_engine
import drive_engine
import gmail_engine
import resilience
import tasks_engine


def _after(n):
    """False for the first n calls, True from then on."""
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        return calls["n"] > n
    return check


class TestSharedFlag:
    def test_reads_mains_own_shutdown_event(self, monkeypatch):
        import main

        assert resilience.shutdown_requested() is False
        main.SHUTDOWN.set()
        try:
            assert resilience.shutdown_requested() is True
        finally:
            main.SHUTDOWN.clear()


class TestChatStopsMidSpaceList:
    def _mig(self):
        m = object.__new__(chat_engine.ChatMigrator)
        m.source_user = "u@src.test"
        m.stats = {"spaces": 0, "messages": 0, "members": 0, "skipped": 0,
                  "failed": 0, "unmapped_senders": 0}
        m.db = type("DB", (), {"get_target_id": lambda *a, **k: None})()
        return m

    def test_stops_before_the_next_space_once_flagged(self, monkeypatch):
        m = self._mig()
        m._iter_spaces = lambda: iter(
            [{"name": f"s{i}", "spaceType": "SPACE"} for i in range(3)])
        migrated: list[str] = []
        m._migrate_space = lambda space: migrated.append(space["name"])
        monkeypatch.setattr(chat_engine, "shutdown_requested", _after(1))

        m.run()

        assert migrated == ["s0"]


class TestCalendarStopsMidEventList:
    def _mig(self, settings):
        m = object.__new__(calendar_engine.CalendarMigrator)
        m.settings = settings
        m.source_user = "u@src.test"
        return m

    def test_stops_before_the_next_event_once_flagged(self, settings, monkeypatch):
        m = self._mig(settings)
        m._iter_events = lambda updated_min, calendar_id: iter(
            [{"id": f"e{i}"} for i in range(3)])
        migrated: list[str] = []
        m.migrate_event = lambda item, tgt, src: migrated.append(item["id"])
        monkeypatch.setattr(calendar_engine, "shutdown_requested", _after(1))

        m._migrate_calendar("primary", "primary", None)

        assert migrated == ["e0"]


class TestContactsStopsMidList:
    def _mig(self, settings):
        m = object.__new__(contacts_engine.ContactsMigrator)
        m.settings = settings
        m.source_user = "u@src.test"
        m.stats = {"contacts": 0, "groups": 0, "skipped": 0, "failed": 0}
        m._migrate_groups = lambda: {}
        return m

    def test_stops_before_the_next_contact_once_flagged(self, settings, monkeypatch):
        m = self._mig(settings)
        m._iter_contacts = lambda: iter(
            [{"resourceName": f"c{i}"} for i in range(3)])
        migrated: list[str] = []
        m._migrate_contact = lambda person, group_map: migrated.append(
            person["resourceName"])
        monkeypatch.setattr(contacts_engine, "shutdown_requested", _after(1))

        m.run()

        assert migrated == ["c0"]


class TestTasksStopsMidListAndMidTree:
    def _mig(self, settings):
        m = object.__new__(tasks_engine.TasksMigrator)
        m.settings = settings
        m.source_user = "u@src.test"
        m.stats = {"lists": 0, "tasks": 0, "skipped": 0, "failed": 0}
        return m

    def test_stops_before_the_next_list_once_flagged(self, settings, monkeypatch):
        m = self._mig(settings)
        m._iter_lists = lambda: iter([{"id": f"l{i}"} for i in range(3)])
        m._target_has_tasks = lambda: True
        migrated: list[str] = []
        m._migrate_list = lambda tl: migrated.append(tl["id"])
        monkeypatch.setattr(tasks_engine, "shutdown_requested", _after(1))

        m.run()

        assert migrated == ["l0"]

    def test_stops_walking_the_task_tree_once_flagged(self, settings, monkeypatch):
        """A parent's own children are a separate recursive call
        (emit(t["id"], new_id)) from the sibling loop that finds them --
        both have to see the flag, not just the top-level list."""
        m = self._mig(settings)
        m._iter_tasks = lambda src_list: iter(
            [{"id": f"t{i}"} for i in range(3)])
        created: list[str] = []

        def fake_create(t, tgt_list, parent_tgt):
            created.append(t["id"])
            return f"tgt-{t['id']}"
        m._create_task = fake_create
        monkeypatch.setattr(tasks_engine, "shutdown_requested", _after(1))

        m._migrate_tasks("src-list", "tgt-list")

        assert created == ["t0"]


class TestGmailStopsMidMailbox:
    def _mig(self, settings):
        m = object.__new__(gmail_engine.GmailMigrator)
        m.settings = settings
        m.settings.mail_workers = 1
        m.source_user = "u@src.test"
        m.stats = {}
        m.db = type("DB", (), {
            "has_drive_mappings": lambda *a, **k: True,
            "preload_mappings": lambda *a, **k: 0,
        })()
        m.sync_labels = lambda: None
        m._migrate_drafts = lambda: None
        m.settings.rewrite_drive_links = False
        m.settings.redo_unrewritten_links = False
        m.settings.migrate_gmail_settings = False
        return m

    def test_stops_before_the_next_message_once_flagged(self, settings, monkeypatch):
        m = self._mig(settings)
        m._iter_messages = lambda query: iter(
            [{"id": f"m{i}"} for i in range(3)])
        migrated: list[str] = []
        m._migrate_one_message = lambda ref: migrated.append(ref["id"])
        monkeypatch.setattr(gmail_engine, "shutdown_requested", _after(1))

        m.run()

        assert migrated == ["m0"]


class TestDriveStopsMidTreeWalk:
    def _mig(self, settings):
        m = object.__new__(drive_engine.DriveMigrator)
        m.settings = settings
        m.source_user = "u@src.test"
        m._sync_folder = lambda item, tgt_parent: None
        m._defer_shortcut = lambda item, tgt_parent: None
        return m

    def test_stops_before_the_next_sibling_once_flagged(self, settings, monkeypatch):
        m = self._mig(settings)
        m._list_children = lambda parent: iter(
            [{"id": f"f{i}", "mimeType": "text/plain"} for i in range(3)])
        synced: list[str] = []
        m._sync_files = lambda files, tgt_parent: synced.extend(
            f["id"] for f in files)
        monkeypatch.setattr(drive_engine, "shutdown_requested", _after(1))

        m._walk("src-root", "tgt-root", depth=1)

        assert synced == ["f0"]
