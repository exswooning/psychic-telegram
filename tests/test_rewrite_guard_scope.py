"""The ordering guard must not block a migration that has no Drive in it.

Rewriting defaults on. The guard refuses to start when rewriting is on and
no Drive has migrated, because mail copied ahead of Drive keeps source links
forever. That is right when Drive is coming and wrong when it is not: a
mail-only migration has nothing to repoint links to and never will, so
refusing would block it over a rewrite that could not have happened.
"""
import pytest

import gmail_engine


class _DB:
    def __init__(self, has): self._has = has
    def has_drive_mappings(self): return self._has
    def preload_mappings(self, *_a): raise _Stop()


class _Stop(Exception):
    """Reached the work; the guard let us through."""


def _migrator(settings, has_drive):
    m = object.__new__(gmail_engine.GmailMigrator)
    m.settings = settings
    m.db = _DB(has_drive)
    m.source_user = "u@src.test"
    return m


class TestTheGuardAsksWhetherDriveIsComing:
    def test_it_still_refuses_mail_before_drive(self, settings):
        settings.rewrite_drive_links = True
        m = _migrator(settings, has_drive=False)
        with pytest.raises(RuntimeError, match="no Drive files have migrated"):
            m.run(drive_in_scope=True)

    def test_it_lets_a_mail_only_migration_run(self, settings):
        settings.rewrite_drive_links = True
        m = _migrator(settings, has_drive=False)
        with pytest.raises(_Stop):          # got past the guard, into the work
            m.run(drive_in_scope=False)

    def test_and_turns_rewriting_off_rather_than_pretending(self, settings):
        """A flag that silently does nothing is worse than one that is off."""
        settings.rewrite_drive_links = True
        m = _migrator(settings, has_drive=False)
        with pytest.raises(_Stop):
            m.run(drive_in_scope=False)
        assert settings.rewrite_drive_links is False

    def test_drive_already_migrated_is_fine_either_way(self, settings):
        settings.rewrite_drive_links = True
        for scope in (True, False):
            m = _migrator(settings, has_drive=True)
            with pytest.raises(_Stop):
                m.run(drive_in_scope=scope)


class TestTheCallSitePassesIt:
    def test_main_tells_gmail_whether_drive_is_in_this_run(self):
        import inspect, main
        src = inspect.getsource(main)
        assert 'drive_in_scope="drive" in services' in src, (
            "gmail would fall back to the default and refuse mail-only runs")
