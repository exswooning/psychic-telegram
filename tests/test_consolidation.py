"""
Several source users into ONE target account.

Consolidating leavers into an archive, or merging duplicate accounts, is an
ordinary migration shape and the schema always allowed it: source_email is
the primary key, target_email is not unique and carries its own index, and
id_mapping is keyed by (source_user, source_id, type) precisely so "two
users may both own 'root'".

What did not hold was the Drive engine. Every user mirrored into the same
My Drive root, so two people's trees interleaved -- two "Documents"
folders, two of every top-level file -- and Drive permits duplicate names,
so nothing errored and nothing on the target recorded which was whose.
"""

from __future__ import annotations

import pytest

from tests.conftest import SRC_USER, TGT_USER


class TestDetectingIt:
    def test_one_to_one_is_not_consolidation(self, db, identity):
        assert db.sources_for_target(TGT_USER) == 1

    def test_two_sources_on_one_target_is(self, db):
        from db import bulk_seed_identities
        bulk_seed_identities(db, [("a@tenanta.com", "archive@tenantb.com"),
                                  ("b@tenanta.com", "archive@tenantb.com")])
        assert db.sources_for_target("archive@tenantb.com") == 2

    def test_it_is_case_insensitive(self, db):
        """Google addresses are case-insensitive; a map that disagrees would
        silently drop back to interleaving."""
        from db import bulk_seed_identities
        bulk_seed_identities(db, [("a@tenanta.com", "Archive@TenantB.com"),
                                  ("b@tenanta.com", "archive@tenantb.com")])
        assert db.sources_for_target("ARCHIVE@tenantb.com") == 2

    def test_an_unknown_target_is_zero_not_an_error(self, db):
        assert db.sources_for_target("nobody@tenantb.com") == 0
        assert db.sources_for_target("") == 0


class TestNestingUnderAFolder:
    def test_the_tree_is_rooted_at_a_folder_named_for_the_source_user(
            self, auth, db, settings, identity, quota):
        import drive_engine
        eng = drive_engine.DriveMigrator(auth, db, settings, SRC_USER,
                                         TGT_USER, quota)
        eng.consolidate_under = SRC_USER
        eng._walk = lambda s, t, depth: eng.__dict__.setdefault("_roots", (s, t))
        eng._fixup_shortcuts = lambda: None

        eng.run()

        created = [c["body"] for c in eng.tgt.calls_to("files.create")]
        folder = next(b for b in created if b.get("name") == SRC_USER)
        assert folder["mimeType"] == drive_engine.FOLDER_MIME
        # and the walk starts there, not at My Drive root
        assert eng._roots[1] != "root-target"

    def test_without_it_the_walk_still_starts_at_my_drive_root(
            self, auth, db, settings, identity, quota):
        """The default one-to-one path must not grow a folder nobody asked
        for."""
        import drive_engine
        eng = drive_engine.DriveMigrator(auth, db, settings, SRC_USER,
                                         TGT_USER, quota)
        eng._walk = lambda s, t, depth: eng.__dict__.setdefault("_roots", (s, t))
        eng._fixup_shortcuts = lambda: None

        eng.run()

        names = [c["body"].get("name") for c in eng.tgt.calls_to("files.create")]
        assert SRC_USER not in names
        assert eng._roots[1] == "root-target"

    def test_a_second_run_reuses_the_folder_rather_than_making_another(
            self, auth, db, settings, identity, quota):
        """Looked up by name, so it survives a reset ledger -- which is
        exactly when a re-run would otherwise build a second copy beside
        the first."""
        import drive_engine
        for _ in range(2):
            eng = drive_engine.DriveMigrator(auth, db, settings, SRC_USER,
                                             TGT_USER, quota)
            eng.consolidate_under = SRC_USER
            eng._walk = lambda s, t, depth: None
            eng._fixup_shortcuts = lambda: None
            eng.run()

        made = [c["body"] for c in eng.tgt.calls_to("files.create")
                if c["body"].get("name") == SRC_USER]
        assert len(made) == 1, "created the consolidation folder twice"

    def test_dry_run_creates_nothing(self, auth, db, settings, identity, quota):
        import drive_engine
        settings.dry_run = True
        eng = drive_engine.DriveMigrator(auth, db, settings, SRC_USER,
                                         TGT_USER, quota)
        eng.consolidate_under = SRC_USER
        eng._walk = lambda s, t, depth: None
        eng._fixup_shortcuts = lambda: None

        eng.run()

        assert eng.tgt.call_count("files.create") == 0


class TestTheMigrationTurnsItOnByItself:
    """No flag to remember. The identity map already says whether this is a
    consolidation, and a run that needed nesting but did not get it is not
    recoverable by re-running -- the files are already interleaved."""

    def test_migrate_user_sets_it_when_the_target_is_shared(
            self, auth, db, settings, monkeypatch):
        import main
        from db import bulk_seed_identities
        bulk_seed_identities(db, [("a@tenanta.com", "archive@tenantb.com"),
                                  ("b@tenanta.com", "archive@tenantb.com")])
        seen = {}

        class _Eng:
            def __init__(self, *a, **k):
                self.consolidate_under = None

            def run(self, delta=False):
                seen["under"] = self.consolidate_under
                return {"files": 0, "folders": 0, "failed": 0}

        monkeypatch.setattr(main, "DriveMigrator", _Eng)
        main.migrate_user(auth, db, settings, "a@tenanta.com",
                          "archive@tenantb.com", {"drive"}, False, 0)

        assert seen["under"] == "a@tenanta.com"

    def test_it_stays_off_for_an_ordinary_one_to_one_move(
            self, auth, db, settings, identity, monkeypatch):
        import main
        seen = {}

        class _Eng:
            def __init__(self, *a, **k):
                self.consolidate_under = None

            def run(self, delta=False):
                seen["under"] = self.consolidate_under
                return {"files": 0, "folders": 0, "failed": 0}

        monkeypatch.setattr(main, "DriveMigrator", _Eng)
        main.migrate_user(auth, db, settings, SRC_USER, TGT_USER,
                          {"drive"}, False, 0)

        assert seen["under"] is None
